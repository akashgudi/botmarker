"""
Template scraper: finds all <a> tags whose href matches
    url.com/jobs/<additional text>

Requires: pip install playwright beautifulsoup4 pymongo python-dotenv
    playwright install chromium
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError

load_dotenv()

# ---- Configuration ----------------------------------------------------
TARGET_URL = "https://hitmarker.net/jobs?location=233&contract=internship+fullTime&level=intermediate+junior+entry"  # <-- set the page to scrape
NUM_PAGES = 3                         # click through pages 1..NUM_PAGES of results
JOBS_PATH_PREFIX = "/jobs/"          # matches url.com/jobs/<anything>
OUTPUT_FILE = "jobs.json"
REQUEST_TIMEOUT = 30_000              # milliseconds
USER_AGENT = "Mozilla/5.0 (compatible; JobLinkScraper/1.0)"

# Mongo connection - falls back to a local instance if env vars aren't set,
# so the script still runs (against an empty local DB) without a .env file.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "job_scraper")
MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "jobs")

# CSS path to the job list container, copied from the rendered DOM (hitmarker.net
# is a client-rendered SPA, so this is Tailwind's generated classes, not
# hand-written markup - backslashes escape the ':' and '[...]' inside class names).
JOB_LIST_SELECTOR = (
    "#app > div.px-4.md\\:px-8.mt-8 > div > "
    "div.grid.grid-cols-1.lg\\:grid-cols-\\[minmax\\(0\\,1fr\\)_300px\\]."
    "xl\\:grid-cols-\\[300px_minmax\\(0\\,1fr\\)_300px\\].gap-x-6 > "
    "div:nth-child(2) > div > div:nth-child(3) > div.space-y-3"
)
# Wait for an actual job link, not just the container - the container div
# exists (as a skeleton/loading state) before the async data fetch resolves,
# so waiting on it alone races with real content and can grab placeholders.
WAIT_FOR_SELECTOR = f"{JOB_LIST_SELECTOR} a"
# Pagination is a client-side <nav> with plain <button>N</button> controls -
# there's no page= query param or href, so pages must be clicked through
# within a single session rather than requested as separate URLs.
PAGINATION_SELECTOR = f"{JOB_LIST_SELECTOR} > nav"
MAX_AGE = timedelta(hours=48)
# ------------------------------------------------------------------------


FIRST_JOB_LINK_SELECTOR = f"{JOB_LIST_SELECTOR} a[href*='/jobs/']"


def fetch_pages(num_pages: int) -> list[str]:
    """Load the target URL, then click through the pager, capturing each page's HTML.

    The pager's active-page indicator updates optimistically on click, before the
    async data fetch behind it resolves - so it can't be used to tell whether the
    job cards have actually swapped in yet. Instead, wait for the first job link's
    href to change from what it was before the click.
    """
    htmls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(TARGET_URL, timeout=REQUEST_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_selector(WAIT_FOR_SELECTOR, timeout=REQUEST_TIMEOUT)
        htmls.append(page.content())

        pagination = page.locator(PAGINATION_SELECTOR)
        for page_num in range(2, num_pages + 1):
            button = pagination.get_by_role("button", name=str(page_num), exact=True)
            if button.count() == 0:
                break  # fewer pages of results than requested

            prev_href = page.get_attribute(FIRST_JOB_LINK_SELECTOR, "href")
            button.click()
            page.wait_for_function(
                "({sel, prevHref}) => document.querySelector(sel)?.getAttribute('href') !== prevHref",
                arg={"sel": FIRST_JOB_LINK_SELECTOR, "prevHref": prev_href},
                timeout=REQUEST_TIMEOUT,
            )
            htmls.append(page.content())

        browser.close()
    return htmls


def parse_job_card(anchor, base_url: str) -> dict:
    link = urljoin(base_url, anchor["href"])

    title_el = anchor.select_one("span.font-bold")
    title = title_el.get_text(strip=True) if title_el else None

    job = {
        "title": title,
        "company": None,
        "location": None,
        "position_type": None,
        "compensation": None,
        "date_posted": None,
        "posted_at": None,
        "link": link,
    }

    # Each job card row is an icon + label pair (location emoji, company logo,
    # contract type, salary, post date) - there's no data attribute naming the
    # field, so the icon's alt text / class is the only way to tell rows apart.
    for row in anchor.find_all("div", class_=lambda c: c == "gap-x-1.5"):
        img = row.find("img")
        truncate_el = row.select_one("span.truncate")
        text = truncate_el.get_text(strip=True) if truncate_el else None
        if img is None or not text:
            continue

        alt = img.get("alt", "")
        classes = img.get("class") or []

        if "emoji" in classes:
            job["location"] = text
        elif alt.endswith(" logo"):
            job["company"] = text
        elif alt == "Contract":
            job["position_type"] = text
        elif alt == "Salary":
            job["compensation"] = text
        elif alt == "Post Date":
            job["date_posted"] = text
            job["posted_at"] = truncate_el.get("data-datetime")

    return job


def posted_within(job: dict, max_age: timedelta) -> bool:
    posted_at = job.get("posted_at")
    if not posted_at:
        return False

    try:
        posted_dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return False

    return datetime.now(timezone.utc) - posted_dt <= max_age


def extract_jobs(html: str, base_url: str, seen_links: set | None = None) -> list[dict]:
    # seen_links is shared across pages by the caller (scrape_and_store) so a
    # listing that happens to appear on more than one page - promoted/pinned
    # jobs do this - only gets parsed and counted once.
    if seen_links is None:
        seen_links = set()

    soup = BeautifulSoup(html, "html.parser")
    jobs = []

    job_list = soup.select_one(JOB_LIST_SELECTOR)
    if job_list is None:
        print("Warning: job list container not found, falling back to whole-page scan")
        job_list = soup

    for anchor in job_list.find_all("a", href=True):
        absolute_url = urljoin(base_url, anchor["href"])
        path = urlparse(absolute_url).path

        # Only real job postings, e.g. reject "/jobs" itself or "/jobs/" with nothing after it.
        if not (path.startswith(JOBS_PATH_PREFIX) and len(path) > len(JOBS_PATH_PREFIX)):
            continue
        if absolute_url in seen_links:
            continue

        seen_links.add(absolute_url)
        job = parse_job_card(anchor, base_url)
        if posted_within(job, MAX_AGE):
            jobs.append(job)

    return jobs


def get_jobs_collection():
    """Connect and make sure `link` is unique so duplicate listings can't be inserted twice."""
    client = MongoClient(MONGO_URI)
    collection = client[MONGO_DB][MONGO_COLLECTION]
    collection.create_index("link", unique=True)
    return collection


def save_new_jobs(jobs: list[dict], collection=None) -> list[dict]:
    """Upsert jobs keyed on `link`, returning only the ones that didn't already exist.

    Uses one bulk_write of per-job upserts instead of N round trips, and reads
    upserted_id back per-operation to tell "new" from "already seen" without a
    separate existence check.
    """
    if not jobs:
        return []

    owns_client = collection is None
    if owns_client:
        collection = get_jobs_collection()

    operations = [
        UpdateOne({"link": job["link"]}, {"$setOnInsert": job}, upsert=True)
        for job in jobs
    ]

    try:
        result = collection.bulk_write(operations, ordered=False)
    except PyMongoError as e:
        print(f"Mongo bulk_write failed: {e}")
        return []
    finally:
        if owns_client:
            collection.database.client.close()

    new_indices = set(result.upserted_ids.keys())
    return [job for i, job in enumerate(jobs) if i in new_indices]


def scrape_and_store() -> list[dict]:
    """Scrape up to NUM_PAGES of results and persist only newly-seen jobs. Returns the new ones."""
    htmls = fetch_pages(NUM_PAGES)

    seen_links = set()
    jobs = []
    for html in htmls:
        jobs.extend(extract_jobs(html, TARGET_URL, seen_links))

    new_jobs = save_new_jobs(jobs)

    print(
        f"Found {len(jobs)} job listing(s) across {len(htmls)} page(s) posted in the last 24 hours "
        f"({len(new_jobs)} new, {len(jobs) - len(new_jobs)} already in the database)"
    )
    return new_jobs


def main():
    new_jobs = scrape_and_store()

    with open(OUTPUT_FILE, "w") as f:
        json.dump(new_jobs, f, indent=2)

    print(f"Wrote {len(new_jobs)} new job listing(s) to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()


