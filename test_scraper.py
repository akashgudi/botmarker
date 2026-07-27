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
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from pymongo import MongoClient, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# ---- Configuration ----------------------------------------------------
# Each feed is an independent hitmarker.net search-results URL (its filters baked
# into the query string) that gets posted to its own Discord channel - see
# discord_bot.py's poll_jobs. Feeds are per-guild and stored in Mongo (see the
# feeds collection helpers below) rather than a shared config file, so each
# invited server manages its own feeds via discord_bot.py's slash commands.
MAX_FEEDS_PER_GUILD = 15               # poll_jobs scrapes every guild's feeds
                                        # sequentially, one browser at a time -
                                        # this caps how much one server can add
                                        # to everyone else's poll cycle time.
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
# Tracks which (guild, feed, link) tuples have already been reported as new,
# separately from `jobs` - so a listing matching more than one feed's filters
# still gets surfaced to each matching feed's channel, even though its content
# (in `jobs`) is only stored once. Scoped by guild (not just feed) so two
# different servers naming a feed the same thing don't share dedup state.
POSTED_COLLECTION = os.environ.get("MONGO_POSTED_COLLECTION", "posted_jobs")
# Per-guild feed configuration (name/url/channel), replacing the old
# feeds.json - each invited Discord server manages its own feeds through
# discord_bot.py's slash commands rather than editing a shared file.
FEEDS_COLLECTION = os.environ.get("MONGO_FEEDS_COLLECTION", "feeds")

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
    """Connect and make sure (guild, feed, link) is unique, so a feed can't double-post a link."""
    client = MongoClient(MONGO_URI)
    collection = client[MONGO_DB][POSTED_COLLECTION]
    collection.create_index([("guild_id", 1), ("feed_id", 1), ("link", 1)], unique=True)
    return collection


def get_feeds_collection():
    """Connect and make sure feed names are unique per guild (not globally),
    so two different servers can each have their own feed named e.g. 'Marketing'."""
    client = MongoClient(MONGO_URI)
    collection = client[MONGO_DB][FEEDS_COLLECTION]
    collection.create_index([("guild_id", 1), ("name_key", 1)], unique=True)
    return collection


def add_feed(
    guild_id: str,
    name: str,
    url: str,
    channel_id: str,
    created_by: str | None = None,
    collection=None,
) -> dict:
    """Create a feed for one guild.

    Raises ValueError if the guild already has MAX_FEEDS_PER_GUILD feeds, or if
    a feed with that name (case-insensitive) already exists in this guild.
    """
    owns_client = collection is None
    if owns_client:
        collection = get_feeds_collection()

    try:
        guild_id = str(guild_id)
        if collection.count_documents({"guild_id": guild_id}) >= MAX_FEEDS_PER_GUILD:
            raise ValueError(f"This server already has the maximum of {MAX_FEEDS_PER_GUILD} feeds")

        feed = {
            "guild_id": guild_id,
            "name": name,
            "name_key": name.strip().lower(),
            "url": url,
            "channel_id": str(channel_id),
            "created_at": datetime.now(timezone.utc),
            "created_by": created_by,
        }
        try:
            result = collection.insert_one(feed)
        except DuplicateKeyError:
            raise ValueError(f"A feed named '{name}' already exists in this server")
        feed["_id"] = result.inserted_id
        return feed
    finally:
        if owns_client:
            collection.database.client.close()


def remove_feed(guild_id: str, name: str, collection=None) -> bool:
    """Delete one guild's feed by name (case-insensitive). Returns whether one was deleted."""
    owns_client = collection is None
    if owns_client:
        collection = get_feeds_collection()

    try:
        result = collection.delete_one(
            {"guild_id": str(guild_id), "name_key": name.strip().lower()}
        )
        return result.deleted_count > 0
    finally:
        if owns_client:
            collection.database.client.close()


def list_feeds(guild_id: str, collection=None) -> list[dict]:
    """All feeds configured for one guild."""
    owns_client = collection is None
    if owns_client:
        collection = get_feeds_collection()

    try:
        return list(collection.find({"guild_id": str(guild_id)}))
    finally:
        if owns_client:
            collection.database.client.close()


def get_feed(guild_id: str, name: str, collection=None) -> dict | None:
    """One guild's feed by name (case-insensitive), or None if it doesn't exist."""
    owns_client = collection is None
    if owns_client:
        collection = get_feeds_collection()

    try:
        return collection.find_one(
            {"guild_id": str(guild_id), "name_key": name.strip().lower()}
        )
    finally:
        if owns_client:
            collection.database.client.close()


def edit_feed(
    guild_id: str,
    name: str,
    *,
    new_name: str | None = None,
    url: str | None = None,
    channel_id: str | None = None,
    collection=None,
) -> dict:
    """Update an existing feed's name/url/channel in place - same `_id`, so its
    posted_jobs dedup history (keyed by feed_id) survives the edit, unlike
    remove_feed + add_feed which would start that history over from scratch.

    Raises ValueError if no feed named `name` exists in this guild, if no
    fields were given to update, or if `new_name` collides with a different
    feed already in this guild.
    """
    owns_client = collection is None
    if owns_client:
        collection = get_feeds_collection()

    try:
        guild_id = str(guild_id)
        updates = {}
        if new_name is not None:
            updates["name"] = new_name
            updates["name_key"] = new_name.strip().lower()
        if url is not None:
            updates["url"] = url
        if channel_id is not None:
            updates["channel_id"] = str(channel_id)

        if not updates:
            raise ValueError("Nothing to update - provide at least one of new_name, url, or channel")

        try:
            feed = collection.find_one_and_update(
                {"guild_id": guild_id, "name_key": name.strip().lower()},
                {"$set": updates},
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            raise ValueError(f"A feed named '{new_name}' already exists in this server")

        if feed is None:
            raise ValueError(f"No feed named '{name}' found")
        return feed
    finally:
        if owns_client:
            collection.database.client.close()


def reset_feed(guild_id: str, name: str, feeds_collection=None, posted_collection=None) -> int:
    """Clear one feed's dedup history (not the feed itself), so its next
    scrape reports every currently-matching listing as new again. Returns how
    many posted-job records were cleared.

    Raises ValueError if no feed named `name` exists in this guild.
    """
    owns_feeds = feeds_collection is None
    owns_posted = posted_collection is None
    if owns_feeds:
        feeds_collection = get_feeds_collection()
    if owns_posted:
        posted_collection = get_posted_collection()

    try:
        feed = get_feed(guild_id, name, collection=feeds_collection)
        if feed is None:
            raise ValueError(f"No feed named '{name}' found")

        result = posted_collection.delete_many({"guild_id": str(guild_id), "feed_id": feed["_id"]})
        return result.deleted_count
    finally:
        if owns_feeds:
            feeds_collection.database.client.close()
        if owns_posted:
            posted_collection.database.client.close()


def save_new_jobs(
    jobs: list[dict],
    feed: dict,
    jobs_collection=None,
    posted_collection=None,
) -> list[dict]:
    """Store job content once, but track "new" independently per (guild, feed).

    Two collections because dedup happens at two different scopes: `jobs` stores
    each listing once no matter how many feeds' filters it matches, while
    `posted_collection` upserts a (guild, feed, link) row per feed - so a listing
    that matches two feeds (in the same or different guilds) is still reported
    as new to each, even though the second feed's upsert into `jobs` is a no-op.
    Guild is part of the key (not just feed name) so two servers naming a feed
    the same thing don't share dedup state.

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

    guild_id = str(feed["guild_id"])
    feed_id = feed["_id"]

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
                {"guild_id": guild_id, "feed_id": feed_id, "link": job["link"]},
                {
                    "$setOnInsert": {
                        "guild_id": guild_id,
                        "feed_id": feed_id,
                        "link": job["link"],
                    }
                },
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


def search_jobs(
    keyword: str,
    guild_id: str,
    limit: int = 5,
    jobs_collection=None,
    posted_collection=None,
) -> list[dict]:
    """Case-insensitive substring search over jobs actually posted to this guild's
    feeds, most recently posted first.

    Matches against title/company/location/position_type - posted_at is stored as
    an ISO 8601 string (see parse_job_card), which sorts lexicographically the
    same as chronologically, so no datetime parsing is needed here.

    Mongo has no cheap join, so this is a two-step query rather than an
    aggregation `$lookup` or a denormalized guild list on every job document:
    first collect the links this guild's feeds have posted (indexed by
    guild_id), then filter job content down to just those links plus the
    keyword match. Keeps `jobs` shared/unscoped and per-guild state confined to
    `posted_collection`, with no extra write path.
    """
    owns_jobs = jobs_collection is None
    owns_posted = posted_collection is None
    if owns_jobs:
        jobs_collection = get_jobs_collection()
    if owns_posted:
        posted_collection = get_posted_collection()

    try:
        links = posted_collection.distinct("link", {"guild_id": str(guild_id)})
        if not links:
            return []

        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        query = {
            "link": {"$in": links},
            "$or": [
                {"title": pattern},
                {"company": pattern},
                {"location": pattern},
                {"position_type": pattern},
            ],
        }
        cursor = (
            jobs_collection.find(query, {"_id": 0})
            .sort("posted_at", -1)
            .limit(limit)
        )
        return list(cursor)
    finally:
        if owns_jobs:
            jobs_collection.database.client.close()
        if owns_posted:
            posted_collection.database.client.close()


def scrape_and_store(feed: dict) -> list[dict]:
    """Scrape up to NUM_PAGES of results for one feed, persist, and return newly-posted jobs.

    "New" is tracked per (guild, feed) - see save_new_jobs - so the same
    listing can be returned for more than one feed if it matches more than one
    feed's filters, including feeds belonging to different guilds.
    """
    htmls = fetch_pages(NUM_PAGES, feed["url"])

    seen_links = set()
    jobs = []
    for html in htmls:
        jobs.extend(extract_jobs(html, feed["url"], seen_links))

    new_jobs = save_new_jobs(jobs, feed)

    max_age_hours = int(MAX_AGE.total_seconds() // 3600)
    print(
        f"[{feed['name']}] Found {len(jobs)} job listing(s) across {len(htmls)} page(s) posted in the last "
        f"{max_age_hours}h ({len(new_jobs)} new for this feed, {len(jobs) - len(new_jobs)} already posted to it)"
    )
    return new_jobs


def main():
    """Manual one-off run for a single guild's feeds - the bot itself calls
    scrape_and_store directly per guild (see discord_bot.py's poll_jobs)."""
    if len(sys.argv) < 2:
        print("Usage: python test_scraper.py <guild_id>")
        return

    guild_id = sys.argv[1]
    feeds = list_feeds(guild_id)
    if not feeds:
        print(f"No feeds configured for guild {guild_id}")
        return

    results = {feed["name"]: scrape_and_store(feed) for feed in feeds}

    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    total_new = sum(len(jobs) for jobs in results.values())
    print(f"Wrote {total_new} new job listing(s) across {len(feeds)} feed(s) to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()


