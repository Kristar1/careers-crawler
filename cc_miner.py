"""
MONTHLY: build a web-scale pool of company careers pages from Common Crawl.

Common Crawl publishes a free index of every page address it crawls each month
(billions of pages). This script searches that index for addresses like
/careers, /jobs, /join-us ... and turns them into one row per company domain.

Two modes (run by .github/workflows/mine.yml):
  python cc_miner.py shard --index 3 --total 20   # scan 1/20th of the index
  python cc_miner.py merge                          # combine shards into pool.csv.gz
"""
import argparse, gzip, io, os, sys, time, datetime
import duckdb, requests

BASE = "https://data.commoncrawl.org/"
JOB_RE = r"/(careers?|jobs?|join-us|join-our-team|open-roles|open-positions|openings|vacancies|work-with-us|we-are-hiring|hiring|positions)(/|$|-)"
ENGLISH_ONLY = os.environ.get("CC_ENGLISH_ONLY", "1") == "1"
MAX_JOB_PAGES = int(os.environ.get("MAX_JOB_PAGES", "60"))   # more than this = probably a big company
FILES_LIMIT = int(os.environ.get("CC_FILES_LIMIT", "0"))      # 0 = all files in this shard
# Private repos have 2,000 free Actions minutes a month, so by default each month scans a
# different slice of the index and the pool keeps growing month after month.
ROTATE = os.environ.get("CC_ROTATE", "1") == "1"

# Hiring platforms (already covered by the sheet), job boards, marketplaces and giant sites
EXCLUDE = """linkedin indeed glassdoor ziprecruiter monster wellfound angel upwork fiverr freelancer toptal contra
greenhouse lever ashbyhq smartrecruiters workable bamboohr recruitee breezy teamtailor personio pinpointhq
applytojob jobvite icims taleo successfactors myworkdayjobs workday ultipro adp paycomonline paylocity
careers-page hireology zohorecruit freshteam rippling gem keka factorialhr talentlyft applicantpro hiringthing
bebee jooble talent adzuna careerjet simplyhired snagajob dice builtin remoteok weworkremotely remotive himalayas
jobicy arbeitnow workingnomads naukri seek reed totaljobs stepstone xing craigslist
wikipedia google youtube facebook instagram twitter reddit github medium amazon microsoft apple oracle ibm
""".split()


def latest_crawl():
    return requests.get("https://index.commoncrawl.org/collinfo.json", timeout=60).json()[0]["id"]


def index_files(crawl):
    r = requests.get(f"{BASE}crawl-data/{crawl}/cc-index-table.paths.gz", timeout=120)
    r.raise_for_status()
    lines = gzip.decompress(r.content).decode().split()
    return [l for l in lines if "subset=warc" in l and l.endswith(".parquet")]


def connect():
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET http_retries = 8; SET http_retry_wait_ms = 2000; SET http_timeout = 600000;")
    return con


def shard(index, total):
    crawl = os.environ.get("CRAWL") or latest_crawl()
    files = index_files(crawl)[index::total]
    if FILES_LIMIT:
        if ROTATE and len(files) > FILES_LIMIT:
            today = datetime.date.today()
            start = ((today.year * 12 + today.month) * FILES_LIMIT) % len(files)
            files = (files + files)[start:start + FILES_LIMIT]
        else:
            files = files[:FILES_LIMIT]
    print(f"{crawl}: shard {index}/{total} scanning {len(files)} index files")
    con = connect()
    con.execute("CREATE TABLE pages (domain VARCHAR, host VARCHAR, path VARCHAR)")
    lang = "AND content_languages LIKE '%eng%'" if ENGLISH_ONLY else ""
    for n, f in enumerate(files, 1):
        sql = f"""
            INSERT INTO pages
            SELECT url_host_registered_domain, url_host_name, url_path
            FROM read_parquet('{BASE}{f}')
            WHERE fetch_status = 200 AND content_mime_detected = 'text/html' {lang}
              AND regexp_matches(url_path, '{JOB_RE}', 'i')"""
        for attempt in range(4):
            try:
                t = time.time()
                con.execute(sql)
                print(f"  [{n}/{len(files)}] ok in {time.time() - t:.0f}s, rows so far: "
                      f"{con.execute('SELECT count(*) FROM pages').fetchone()[0]}")
                break
            except Exception as e:
                print(f"  [{n}/{len(files)}] attempt {attempt + 1} failed: {e}")
                time.sleep(30 * (attempt + 1))
    os.makedirs("out", exist_ok=True)
    con.execute("COPY (SELECT DISTINCT * FROM pages) TO 'out/shard.parquet' (FORMAT PARQUET)")
    with open("out/crawl.txt", "w") as fh:
        fh.write(crawl)


def merge():
    con = connect()
    con.execute("CREATE TABLE pages AS SELECT DISTINCT domain, host, path FROM read_parquet('shards/**/shard.parquet')")
    excl = " OR ".join(f"domain LIKE '{w}.%' OR domain LIKE '%.{w}.%'" for w in EXCLUDE)
    con.execute(f"""DELETE FROM pages WHERE domain IS NULL OR host IS NULL
                    OR regexp_matches(domain, '\\.(edu|gov|mil|ac\\.[a-z]+|gov\\.[a-z]+)$') OR {excl}""")
    root = r"(?i)^(.*?/(?:careers?|jobs?|join-us|join-our-team|open-roles|open-positions|openings|vacancies|work-with-us|we-are-hiring|hiring|positions))(?:/|$)"
    today = datetime.date.today().isoformat()
    con.execute(f"""
        CREATE TABLE pool AS
        WITH c AS (SELECT domain, count(DISTINCT path) AS job_pages FROM pages GROUP BY domain)
        SELECT p.domain,
               arg_min('https://' || p.host || coalesce(nullif(regexp_extract(p.path, '{root}', 1), ''), p.path),
                       length(p.path)) AS careers_url,
               c.job_pages,
               md5(array_to_string(list_sort(list(DISTINCT p.path)), '|')) AS pages_hash
        FROM pages p JOIN c USING (domain)
        WHERE c.job_pages <= {MAX_JOB_PAGES}
        GROUP BY p.domain, c.job_pages""")
    full_scan = os.environ.get("FULL_SCAN", "0") == "1"
    if os.path.exists("prev_pool.csv.gz"):
        con.execute("""CREATE TABLE prev AS SELECT domain, careers_url, job_pages, pages_hash, mined
                       FROM read_csv('prev_pool.csv.gz', header = true, all_varchar = true, quote = '"', escape = '"')""")
        # "changed" is only reliable when the whole index was scanned (a partial slice sees only some pages)
        changed = "CASE WHEN p.pages_hash IS NOT NULL AND p.pages_hash <> n.pages_hash THEN 1 ELSE 0 END" if full_scan else "0"
        con.execute(f"""CREATE TABLE out AS
            SELECT n.domain, n.careers_url, n.job_pages, n.pages_hash, {changed} AS changed, '{today}' AS mined
            FROM pool n LEFT JOIN prev p USING (domain)
            UNION ALL   -- keep companies found in earlier months that this month's slice did not cover
            SELECT p.domain, p.careers_url, CAST(p.job_pages AS INTEGER), p.pages_hash, 0, p.mined
            FROM prev p WHERE p.domain NOT IN (SELECT domain FROM pool)""")
    else:
        con.execute(f"CREATE TABLE out AS SELECT *, 0 AS changed, '{today}' AS mined FROM pool")
    con.execute("COPY out TO 'pool.csv.gz' (FORMAT CSV, HEADER, COMPRESSION GZIP)")
    n, ch = con.execute("SELECT count(*), sum(changed) FROM out").fetchone()
    print(f"Pool: {n} company domains with careers pages ({ch or 0} changed since last month)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["shard", "merge"])
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--total", type=int, default=1)
    a = ap.parse_args()
    shard(a.index, a.total) if a.mode == "shard" else merge()
