"""
Template scraper: finds all <a> tags whose href matches
    url.com/jobs/<additional text>

Requires: pip install playwright beautifulsoup4 pymongo python-dotenv
    playwright install chromium
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError

load_dotenv()

# ---- Configuration ----------------------------------------------------
# Each feed is an independent hitmarker.net search-results URL (its filters baked
# into the query string) that gets posted to its own Discord channel - see
# discord_bot.py's poll_jobs. Configured as a JSON array in FEEDS_FILE, e.g.:
#   [{"name": "US Internships", "url": "https://hitmarker.net/jobs?...", "channel_id": "123..."}]
# test_scraper.py itself only reads name/url; channel_id is carried through for
# discord_bot.py to use.
FEEDS_FILE = os.environ.get("FEEDS_FILE", "feeds.json")
if os.path.exists(FEEDS_FILE):
    with open(FEEDS_FILE, encoding="utf-8") as f:
        FEEDS: list[dict] = json.load(f)
else:
    FEEDS = []
NUM_PAGES = 3                         # click through pages 1..NUM_PAGES of results, per feed
JOBS_PATH_PREFIX = "/jobs/"          # matches url.com/jobs/<anything>
OUTPUT_FILE = "jobs.json"
REQUEST_TIMEOUT = 30_000              # milliseconds
USER_AGENT = "Mozilla/5.0 (compatible; JobLinkScraper/1.0)"

# Mongo connection - falls back to a local instance if env vars aren't set,
# so the script still runs (against an empty local DB) without a .env file.
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "job_scraper")
MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "jobs")
# Tracks which (feed, link) pairs have already been reported as new, separately
# from `jobs` - so a listing matching more than one feed's filters still gets
# surfaced to each matching feed's channel, even though its content (in `jobs`)
# is only stored once.
POSTED_COLLECTION = os.environ.get("MONGO_POSTED_COLLECTION", "posted_jobs")

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


def fetch_pages(num_pages: int, target_url: str) -> list[str]:
    """Load target_url, then click through the pager, capturing each page's HTML.

    The pager's active-page indicator updates optimistically on click, before the
    async data fetch behind it resolves - so it can't be used to tell whether the
    job cards have actually swapped in yet. Instead, wait for the first job link's
    href to change from what it was before the click.
    """
    htmls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(target_url, timeout=REQUEST_TIMEOUT, wait_until="domcontentloaded")
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
        "company_logo": None,
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
            if img.get("src"):
                job["company_logo"] = urljoin(base_url, img["src"])
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


def get_posted_collection():
    """Connect and make sure (feed, link) is unique, so a feed can't double-post a link."""
    client = MongoClient(MONGO_URI)
    collection = client[MONGO_DB][POSTED_COLLECTION]
    collection.create_index([("feed", 1), ("link", 1)], unique=True)
    return collection


def save_new_jobs(
    jobs: list[dict],
    feed_name: str,
    jobs_collection=None,
    posted_collection=None,
) -> list[dict]:
    """Store job content once, but track "new" independently per feed.

    Two collections because dedup happens at two different scopes: `jobs` stores
    each listing once no matter how many feeds' filters it matches, while
    `posted_collection` upserts a (feed, link) row per feed - so a listing that
    matches two feeds is still reported as new to both, even though the second
    feed's upsert into `jobs` is a no-op.

    Uses one bulk_write per collection instead of N round trips, and reads
    upserted_ids back to tell "new to this feed" from "already posted to this
    feed" without a separate existence check.
    """
    if not jobs:
        return []

    owns_jobs = jobs_collection is None
    owns_posted = posted_collection is None
    if owns_jobs:
        jobs_collection = get_jobs_collection()
    if owns_posted:
        posted_collection = get_posted_collection()

    try:
        content_ops = [
            UpdateOne({"link": job["link"]}, {"$setOnInsert": job}, upsert=True)
            for job in jobs
        ]
        try:
            jobs_collection.bulk_write(content_ops, ordered=False)
        except PyMongoError as e:
            print(f"Mongo bulk_write (jobs) failed: {e}")
            return []

        posted_ops = [
            UpdateOne(
                {"feed": feed_name, "link": job["link"]},
                {"$setOnInsert": {"feed": feed_name, "link": job["link"]}},
                upsert=True,
            )
            for job in jobs
        ]
        try:
            result = posted_collection.bulk_write(posted_ops, ordered=False)
        except PyMongoError as e:
            print(f"Mongo bulk_write (posted) failed: {e}")
            return []
    finally:
        if owns_jobs:
            jobs_collection.database.client.close()
        if owns_posted:
            posted_collection.database.client.close()

    new_indices = set(result.upserted_ids.keys())
    return [job for i, job in enumerate(jobs) if i in new_indices]


def search_jobs(keyword: str, limit: int = 5, collection=None) -> list[dict]:
    """Case-insensitive substring search over stored jobs, most recently posted first.

    Matches against title/company/location/position_type - posted_at is stored as
    an ISO 8601 string (see parse_job_card), which sorts lexicographically the
    same as chronologically, so no datetime parsing is needed here.
    """
    owns_client = collection is None
    if owns_client:
        collection = get_jobs_collection()

    try:
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        query = {
            "$or": [
                {"title": pattern},
                {"company": pattern},
                {"location": pattern},
                {"position_type": pattern},
            ]
        }
        cursor = (
            collection.find(query, {"_id": 0})
            .sort("posted_at", -1)
            .limit(limit)
        )
        return list(cursor)
    finally:
        if owns_client:
            collection.database.client.close()


def scrape_and_store(feed: dict) -> list[dict]:
    """Scrape up to NUM_PAGES of results for one feed, persist, and return newly-posted jobs.

    "New" is tracked per feed (see save_new_jobs), so the same listing can be
    returned for more than one feed if it matches more than one feed's filters.
    """
    htmls = fetch_pages(NUM_PAGES, feed["url"])

    seen_links = set()
    jobs = []
    for html in htmls:
        jobs.extend(extract_jobs(html, feed["url"], seen_links))

    new_jobs = save_new_jobs(jobs, feed["name"])

    max_age_hours = int(MAX_AGE.total_seconds() // 3600)
    print(
        f"[{feed['name']}] Found {len(jobs)} job listing(s) across {len(htmls)} page(s) posted in the last "
        f"{max_age_hours}h ({len(new_jobs)} new for this feed, {len(jobs) - len(new_jobs)} already posted to it)"
    )
    return new_jobs


def main():
    if not FEEDS:
        print("No feeds configured - add entries to feeds.json")
        return

    results = {feed["name"]: scrape_and_store(feed) for feed in FEEDS}

    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    total_new = sum(len(jobs) for jobs in results.values())
    print(f"Wrote {total_new} new job listing(s) across {len(FEEDS)} feed(s) to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()


