# Envizn careers-page crawler

Finds companies hiring for web, design, Webflow, Framer, Shopify, WordPress and UI/UX roles on their **own** careers pages, across the whole web, for free. New leads land in the existing Lead Watch Google Sheet.

## How it works

| Workflow | When | What it does |
|---|---|---|
| **Mine Common Crawl** | 5th of each month | Searches Common Crawl's free index of billions of pages for `/careers`, `/jobs`, `/join-us`... pages and builds a pool of company domains. Skips hiring platforms the sheet already covers, job boards, universities, government and companies with very large numbers of job pages. Flags companies whose job pages changed since last month. |
| **Check careers pages** | Daily, 6 AM IST | Checks 60,000 sites from the pool (changed and never-checked first). Respects robots.txt. Hands embedded hiring platforms (Greenhouse, BambooHR...) to the sheet's Boards tab, reads job-posting tags for dated postings, and compares job links with the last visit to find new roles. |

Leads go to the sheet's **Leads** tab with source `careers-page` and pass through the same filters as everything else (size, Upwork, blogs, duplicates, scoring).

## Privacy in a public repo

The repo is public so GitHub Actions minutes are unlimited and free. Only the code is visible.
- The company pool and the crawler's memory are kept in the repo's **workflow cache**, which only this repo's workflows can read. They are never committed or published.
- Leads are sent straight to the Google Sheet; they are never written to the repo.
- The sheet's web app URL and password are stored as **GitHub secrets**, which are encrypted and hidden from logs.
- Run logs only show counts (sites checked, leads sent), not company names or links.
- Recommended: Settings > Actions > General > *Fork pull request workflows from outside collaborators* > **Require approval for all outside collaborators**.

## Setup

1. **Sheet script:** paste the latest `LeadWatch_GoogleSheet.gs` into Apps Script, save, run `setup`. Type any long random text into the `WEBHOOK_TOKEN` row of the Settings tab.
2. **Web app:** Apps Script > Deploy > New deployment > Web app. Execute as **Me**, access **Anyone**. Copy the `/exec` URL.
3. **Repo:** create a **public** repo `careers-crawler`, upload `cc_miner.py`, `rotate.py`, `README.md`, and create `.github/workflows/mine.yml` and `.github/workflows/daily.yml`.
4. **Secrets:** Settings > Secrets and variables > Actions: `SHEET_WEBHOOK_URL` (the `/exec` URL) and `SHEET_TOKEN` (same text as `WEBHOOK_TOKEN`).

## First run

1. Actions > *Mine Common Crawl* > Run workflow with *files_limit* `1` (quick sample, about 20 minutes).
2. Actions > *Check careers pages* > Run workflow with *daily_sites* `500`. The sheet's Log tab shows "Careers-page crawler: +X leads".
3. Run *Mine Common Crawl* again with *files_limit* `0` for the full pool (20 parallel jobs, a few hours). After that everything runs on its own.

Plain careers pages produce leads from their second check (the first visit records what is already there). Pages with job-posting tags produce leads on the first check if the posting is recent.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `DAILY_SITES` | 60000 | Sites checked per day |
| `MAX_OPEN_JOBS` | 15 | Skip companies with more job links than this (big-company proxy) |
| `MAX_JOB_PAGES` | 60 | When building the pool, skip domains with more job pages than this |
| `CC_ENGLISH_ONLY` | 1 | Only English-language pages |
| `MAX_AGE_DAYS` | 14 | How recent a tagged posting must be |

## Limits

- Pages that only show jobs after JavaScript runs are missed.
- Common Crawl does not visit every page of every site each month, so some small companies are not in the pool.
- Workflow caches unused for 7 days are deleted by GitHub. The daily run keeps them alive; if the crawler is paused for over a week, rerun *Mine Common Crawl*.
