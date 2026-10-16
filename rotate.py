"""
DAILY: check a rotating slice of the careers-page pool and send new relevant roles
to the Envizn Lead Watch Google Sheet.

For each company careers page it:
  1. respects robots.txt (checked once a month per site)
  2. spots embedded hiring platforms (Greenhouse, Lever, BambooHR ...) and hands those
     boards to the sheet, which then reads their clean daily feeds
  3. reads job-posting tags (JSON-LD) for structured, dated postings
  4. otherwise compares job links with the previous visit to find new roles
Leads go to the sheet, which applies the same filters as every other source
(size, Upwork, blogs, duplicates, scoring).
"""
import asyncio, csv, datetime, gzip, html, json, os, re, sqlite3, sys, time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import aiohttp
import requests

POOL_FILE, STATE_FILE = "pool.csv.gz", "state.db"
DAILY_SITES = int(os.environ.get("DAILY_SITES", "60000"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "150"))
MAX_OPEN_JOBS = int(os.environ.get("MAX_OPEN_JOBS", "15"))
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "14"))
MAX_NEW_PER_SITE = 5
TIME_BUDGET = int(os.environ.get("TIME_BUDGET_MIN", "240")) * 60
SHEET_WEBHOOK, SHEET_TOKEN = os.environ.get("SHEET_WEBHOOK_URL"), os.environ.get("SHEET_TOKEN", "")
UA = "Mozilla/5.0 (compatible; EnviznLeadWatch/3.0; careers page check)"
TODAY = datetime.date.today().isoformat()
NOW = datetime.datetime.now(datetime.timezone.utc)

STRONG = re.compile(r"\b(webflow|framer|wordpress|shopify|squarespace|wix studio|wix|web ?designer|web ?developer|"
                    r"website|ui ?/ ?ux|ux|ui designer|product designer|graphic designer|visual designer|"
                    r"brand designer|seo|aeo|front[- ]?end)\b", re.I)
WEAK = re.compile(r"\b(marketing|growth|content|creative|design|designer|digital)\b", re.I)

# Hiring platforms embedded on company careers pages -> (platform name as used by the sheet, pattern)
ATS = [
    ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("rippling", r"ats\.rippling\.com/([A-Za-z0-9_-]+)"),
    ("gem", r"jobs\.gem\.com/([A-Za-z0-9_-]+)"),
    ("jobvite", r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)"),
    ("personio", r"([a-z0-9-]+\.jobs\.personio\.(?:de|com))"),
] + [(name, r"([a-z0-9-]+)\." + re.escape(dom)) for name, dom in [
    ("recruitee", "recruitee.com"), ("breezy", "breezy.hr"), ("bamboohr", "bamboohr.com"),
    ("teamtailor", "teamtailor.com"), ("pinpoint", "pinpointhq.com"), ("jazzhr", "applytojob.com"),
    ("freshteam", "freshteam.com"), ("zohorecruit", "zohorecruit.com"), ("hiringthing", "hiringthing.com"),
    ("factorial", "factorialhr.com"), ("talentlyft", "talentlyft.com"), ("applicantpro", "applicantpro.com")]]
ATS = [(n, re.compile(p, re.I)) for n, p in ATS]
SKIP_SLUGS = {"www", "app", "api", "cdn", "static", "assets", "embed", "jobs", "job", "careers", "help",
              "support", "js", "css", "images", "img", "widget", "widgets", "boards", "apply"}

LD_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
A_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
JOB_PATH = re.compile(r"/(jobs?|careers?|positions?|openings?|vacanc\w*|apply|join-us|roles?|opportunities|job-openings)/[^/?#]+", re.I)
TAG_RE = re.compile(r"<[^>]+>")


# ---------------- helpers ----------------
def strip_tags(s, limit=12000):
    s = re.sub(r"<(script|style|noscript|svg|nav|footer)[\s\S]*?</\1>", " ", s or "", flags=re.I)
    return " ".join(html.unescape(TAG_RE.sub(" ", s)).split())[:limit]


def reg_domain(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def company_from(domain):
    return domain.split(".")[0].replace("-", " ").title()


def detect_ats(page):
    found = set()
    for name, rx in ATS:
        for m in rx.finditer(page):
            slug = m.group(1)
            if slug.lower() in SKIP_SLUGS:
                continue
            found.add((name, slug if name in ("ashby", "smartrecruiters") else slug.lower()))
    return sorted(found)


def job_postings(page):
    out = []
    for blob in LD_RE.findall(page):
        try:
            data = json.loads(blob.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                t = x.get("@type")
                if "JobPosting" in (t if isinstance(t, list) else [t]):
                    out.append(x)
                if "@graph" in x:
                    stack.append(x["@graph"])
    return out


def job_links(page_url, page):
    base = reg_domain(page_url)
    links = {}
    for href, inner in A_RE.findall(page):
        href = html.unescape(href).strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        try:
            absu = urljoin(page_url, href).split("#")[0]
            if reg_domain(absu) != base or not JOB_PATH.search(urlparse(absu).path):
                continue
        except ValueError:   # malformed link on the page
            continue
        if absu.rstrip("/") == page_url.rstrip("/"):
            continue
        text = strip_tags(inner, 140)
        if len(text) >= 5:
            links[absu] = text
    return links


def fresh(date_str):
    if not date_str:
        return False
    try:
        d = datetime.datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return (NOW - d).days <= MAX_AGE_DAYS
    except Exception:
        return False


# ---------------- state ----------------
def open_db():
    db = sqlite3.connect(STATE_FILE)
    db.execute("""CREATE TABLE IF NOT EXISTS sites (domain TEXT PRIMARY KEY, last_checked TEXT, links TEXT,
                  seen TEXT, ats TEXT, status TEXT, robots_ok INTEGER, robots_checked TEXT)""")
    db.execute("DROP TABLE IF EXISTS pool")
    db.execute("CREATE TABLE pool (domain TEXT PRIMARY KEY, careers_url TEXT, changed INTEGER, mined TEXT)")
    with gzip.open(POOL_FILE, "rt", newline="") as fh:
        rows = ((r["domain"], r["careers_url"], int(r.get("changed") or 0), r.get("mined") or "")
                for r in csv.DictReader(fh))
        db.executemany("INSERT OR IGNORE INTO pool VALUES (?,?,?,?)", rows)
    db.commit()
    return db


def pick_sites(db, n):
    q = """SELECT p.domain, p.careers_url, s.links, s.seen, s.robots_ok, s.robots_checked
           FROM pool p LEFT JOIN sites s ON s.domain = p.domain
           WHERE s.status IS NULL OR s.status NOT IN ('big', 'ats', 'robots')
                 OR s.last_checked < date('now', '-90 day')
           ORDER BY CASE WHEN p.changed = 1 AND (s.last_checked IS NULL OR s.last_checked < p.mined) THEN 0
                         WHEN s.last_checked IS NULL THEN 1 ELSE 2 END,
                    s.last_checked ASC
           LIMIT ?"""
    return db.execute(q, (n,)).fetchall()


# ---------------- network ----------------
async def fetch(session, url, max_bytes=2_000_000):
    try:
        async with session.get(url, allow_redirects=True, max_redirects=5) as r:
            if r.status != 200:
                return None, None
            if "html" not in r.headers.get("content-type", "") and not url.endswith("robots.txt"):
                return None, None
            body = await r.content.read(max_bytes)
            return body.decode(r.charset or "utf-8", errors="replace"), str(r.url)
    except Exception:
        return None, None


async def robots_allowed(session, url, cached_ok, checked):
    if checked and checked >= (datetime.date.today() - datetime.timedelta(days=30)).isoformat():
        return bool(cached_ok), False
    p = urlparse(url)
    txt, _ = await fetch(session, f"{p.scheme}://{p.netloc}/robots.txt", 300_000)
    rp = RobotFileParser()
    rp.parse((txt or "").splitlines())
    return rp.can_fetch(UA, url), True


async def check_site(session, sem, row):
    try:
        return await _check_site(session, sem, row)
    except Exception as e:   # one broken page must never stop the whole run
        return {"domain": row[0], "url": row[1], "status": "fail", "robots_ok": int(bool(row[4])),
                "robots_checked": row[5], "prev_links": json.loads(row[2]) if row[2] else None,
                "seen": set(json.loads(row[3])) if row[3] else set()}


async def _check_site(session, sem, row):
    domain, url, links_json, seen_json, robots_ok, robots_checked = row
    async with sem:
        ok, refreshed = await robots_allowed(session, url, robots_ok, robots_checked)
        res = {"domain": domain, "url": url, "robots_ok": int(ok),
               "robots_checked": TODAY if refreshed else robots_checked,
               "prev_links": json.loads(links_json) if links_json else None,
               "seen": set(json.loads(seen_json)) if seen_json else set()}
        if not ok:
            res["status"] = "robots"
            return res
        page, final = await fetch(session, url)
    if not page:
        res["status"] = "fail"
        return res
    res.update(status="ok", final=final, boards=detect_ats(page),
               postings=job_postings(page), links=job_links(final, page))
    return res


async def fetch_detail(session, sem, cand):
    try:
        async with sem:
            page, final = await fetch(session, cand["url"])
    except Exception:
        return cand
    if page:
        posts = job_postings(page)
        if posts:
            cand["jobSchema"] = True
            cand["posted"] = cand.get("posted") or posts[0].get("datePosted", "")
            org = posts[0].get("hiringOrganization")
            if isinstance(org, dict) and org.get("name"):
                cand["company"] = org["name"]
        cand["text"] = strip_tags(page)
    return cand


# ---------------- sheet ----------------
def send(payload):
    if not SHEET_WEBHOOK:
        print("  (no SHEET_WEBHOOK_URL set, not sending)", {k: len(v) for k, v in payload.items() if isinstance(v, list)})
        return
    payload["token"] = SHEET_TOKEN
    for attempt in range(3):
        try:
            r = requests.post(SHEET_WEBHOOK, json=payload, timeout=300)
            print("  sheet:", r.text[:200])
            return
        except Exception as e:
            print("  sheet error:", e)
            time.sleep(10)


def send_in_batches(cands, boards):
    for i in range(0, len(boards), 500):
        send({"boards": boards[i:i + 500]})
    for i in range(0, len(cands), 40):
        send({"candidates": cands[i:i + 40]})


# ---------------- main ----------------
async def main():
    if not os.path.exists(POOL_FILE):
        sys.exit("No pool yet. Run the 'Mine Common Crawl' workflow first.")
    t0 = time.time()
    db = open_db()
    rows = pick_sites(db, DAILY_SITES)
    print(f"Pool loaded. Checking {len(rows)} sites today.")
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    if not db.execute("SELECT 1 FROM meta WHERE k = 'ats_resent_v1'").fetchone():
        old = set()
        for (ats_json,) in db.execute("SELECT ats FROM sites WHERE ats IS NOT NULL AND ats <> '[]'"):
            old.update(tuple(b) for b in json.loads(ats_json))
        if old:
            print(f"Re-sending {len(old)} job boards found in earlier runs.")
            send_in_batches([], [list(b) for b in sorted(old)])
        db.execute("INSERT INTO meta VALUES ('ats_resent_v1', ?)", (TODAY,))
        db.commit()

    timeout = aiohttp.ClientTimeout(total=20, connect=8)
    conn = aiohttp.TCPConnector(limit=CONCURRENCY, limit_per_host=2, ttl_dns_cache=600, ssl=False)
    sem = asyncio.Semaphore(CONCURRENCY)
    boards, cands = set(), []
    stats = {"ok": 0, "fail": 0, "robots": 0, "big": 0, "ats": 0, "baseline": 0}
    sent_leads = sent_boards = 0

    async with aiohttp.ClientSession(timeout=timeout, connector=conn, headers={"User-Agent": UA}) as session:
        for start in range(0, len(rows), 2000):
            if time.time() - t0 > TIME_BUDGET:
                print("Time budget reached, stopping early; the rest are checked tomorrow.")
                break
            results = await asyncio.gather(*(check_site(session, sem, r) for r in rows[start:start + 2000]))
            detail_needed = []
            for res in results:
                d, status = res["domain"], res["status"]
                stats[status] = stats.get(status, 0) + 1
                links, seen = res.get("links") or {}, res["seen"]
                if status == "ok":
                    if res["boards"]:
                        boards.update(res["boards"])
                        status = "ats"
                    elif len(links) > MAX_OPEN_JOBS + 5 or len(res["postings"]) > MAX_OPEN_JOBS:
                        status = "big"
                    else:
                        # structured postings: send if recent and not sent before
                        for p in res["postings"]:
                            key = p.get("url") or (p.get("title", "") + "|" + d)
                            if key in seen or not fresh(p.get("datePosted")):
                                continue
                            title = strip_tags(str(p.get("title", "")), 140)
                            if not (STRONG.search(title) or WEAK.search(title)):
                                continue
                            seen.add(key)
                            org = p.get("hiringOrganization")
                            cands.append({"key": "cp:" + key, "url": p.get("url") or res["final"], "source": "careers-page",
                                          "company": (org.get("name") if isinstance(org, dict) else None) or company_from(d),
                                          "title": title, "text": strip_tags(str(p.get("description", ""))),
                                          "posted": p.get("datePosted", ""), "jobSchema": True})
                        # plain pages: new job links since the last visit
                        if res["prev_links"] is None:
                            stats["baseline"] += 1
                        else:
                            new = [u for u in links if u not in res["prev_links"] and u not in seen]
                            for u in new[:MAX_NEW_PER_SITE]:
                                if STRONG.search(links[u]) or WEAK.search(links[u]):
                                    seen.add(u)
                                    detail_needed.append({"key": "cp:" + u, "url": u, "source": "careers-page",
                                                          "company": company_from(d), "title": links[u],
                                                          "text": "", "posted": "", "jobSchema": False})
                    stats[status] = stats.get(status, 0) + (1 if status in ("ats", "big") else 0)
                keep_links = list(links)[:300] if status == "ok" else (res["prev_links"] or [])
                db.execute("""INSERT INTO sites (domain, last_checked, links, seen, ats, status, robots_ok, robots_checked)
                              VALUES (?,?,?,?,?,?,?,?)
                              ON CONFLICT(domain) DO UPDATE SET last_checked=excluded.last_checked, links=excluded.links,
                              seen=excluded.seen, ats=excluded.ats, status=excluded.status,
                              robots_ok=excluded.robots_ok, robots_checked=excluded.robots_checked""",
                           (d, TODAY, json.dumps(keep_links), json.dumps(sorted(seen)[-500:]),
                            json.dumps(res.get("boards") or []), status, res["robots_ok"], res["robots_checked"]))
            if detail_needed:
                cands += await asyncio.gather(*(fetch_detail(session, sem, c) for c in detail_needed))
            db.commit()
            print(f"  {min(start + 2000, len(rows))}/{len(rows)} checked | {stats} | {len(cands)} leads, {len(boards)} boards")
            # send this batch's results now, so a later failure never loses them
            send_in_batches([c for c in cands if len(c.get("text") or "") >= 150], [list(b) for b in sorted(boards)])
            sent_leads += len(cands); sent_boards += len(boards)
            boards, cands = set(), []

    db.execute("DROP TABLE pool")
    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"Done in {(time.time() - t0) / 60:.0f} min. Sent {sent_leads} leads and {sent_boards} job boards to the sheet.")


if __name__ == "__main__":
    asyncio.run(main())
