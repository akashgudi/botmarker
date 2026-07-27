"""
One-off migration: moves the feeds in feeds.json (and their existing
posted_jobs dedup history) from the old single-guild, file-based config into
the new per-guild Mongo schema introduced alongside self-serve /add_feed.

Run this ONCE, against the real database, before deploying the new
discord_bot.py/test_scraper.py - then delete this file and feeds.json. Safe
to re-run if interrupted partway through (each step is idempotent).

Usage:
    python migrate_feeds.py
"""

import json
import os

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import OperationFailure

load_dotenv()

from test_scraper import MONGO_DB, MONGO_URI, POSTED_COLLECTION, add_feed, get_feed, get_feeds_collection

FEEDS_FILE = os.environ.get("FEEDS_FILE", "feeds.json")


def migrate():
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not guild_id:
        raise SystemExit(
            "DISCORD_GUILD_ID must be set in .env - this migration needs to know "
            "which server the existing feeds.json belongs to."
        )

    if not os.path.exists(FEEDS_FILE):
        raise SystemExit(f"{FEEDS_FILE} not found - nothing to migrate.")

    with open(FEEDS_FILE, encoding="utf-8") as f:
        old_feeds = json.load(f)

    if not old_feeds:
        print(f"{FEEDS_FILE} has no feeds - nothing to migrate.")
        return

    mongo_client = MongoClient(MONGO_URI)
    # Raw collection handle, deliberately bypassing get_posted_collection() -
    # that helper builds the new (guild_id, feed_id, link) unique index, which
    # must not run until every old (feed, link) doc below has been converted.
    # Two *different*-named old feeds can share an identical link (a listing
    # matching more than one feed's filters), and until they're migrated both
    # show up as guild_id=null/feed_id=null - building the new index before
    # that would fail on the collision.
    posted = mongo_client[MONGO_DB][POSTED_COLLECTION]
    feeds_collection = get_feeds_collection()

    try:
        # The OLD (feed, link) unique index is still live on this collection
        # (it predates this migration) and MUST be dropped before any
        # conversions below - otherwise it actively enforces uniqueness on
        # `feed` while we're $unsetting it, and the same shared-link collision
        # described above hits every write, not just a hypothetical rebuild.
        old_index_name = next(
            (
                name
                for name, info in posted.index_information().items()
                if info.get("key") == [("feed", 1), ("link", 1)]
            ),
            None,
        )
        if old_index_name:
            posted.drop_index(old_index_name)
            print(f"Dropped stale index '{old_index_name}' on posted_jobs.\n")

        for old_feed in old_feeds:
            name, url, channel_id = old_feed["name"], old_feed["url"], old_feed["channel_id"]

            try:
                feed = add_feed(
                    guild_id, name, url, channel_id, created_by="migration", collection=feeds_collection
                )
                print(f"[{name}] created feed {feed['_id']}")
            except ValueError as e:
                if "already exists" not in str(e):
                    raise
                feed = get_feed(guild_id, name, collection=feeds_collection)
                print(f"[{name}] feed already migrated ({feed['_id']}), reusing it")

            result = posted.update_many(
                {"feed": name},
                {"$set": {"guild_id": str(guild_id), "feed_id": feed["_id"]}, "$unset": {"feed": ""}},
            )
            print(f"[{name}] migrated {result.modified_count} posted-job record(s)")

        leftover = posted.count_documents({"feed": {"$exists": True}})
        if leftover:
            print(
                f"\n{leftover} posted_jobs record(s) still have an old-style 'feed' field "
                "(name not found in feeds.json) - the new unique index can't be built until "
                "these are resolved manually. Inspect them with "
                "db.posted_jobs.find({'feed': {'$exists': True}})."
            )
            return

        try:
            posted.create_index([("guild_id", 1), ("feed_id", 1), ("link", 1)], unique=True)
            print("\nBuilt the new (guild_id, feed_id, link) unique index on posted_jobs.")
        except OperationFailure as e:
            print(f"\nCould not build the new unique index - investigate duplicates: {e}")
            return

        print(f"\nDone. Migrated {len(old_feeds)} feed(s) for guild {guild_id}.")
        print(f"Once you've confirmed the bot works, delete {FEEDS_FILE} and this script.")
    finally:
        mongo_client.close()
        feeds_collection.database.client.close()


if __name__ == "__main__":
    migrate()
