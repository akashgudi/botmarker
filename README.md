# discord_work

Scrapes job listings from [hitmarker.net](https://hitmarker.net) and posts new ones to a Discord channel, with deduplication backed by MongoDB.

## How it works

- **`test_scraper.py`** — loads a target search results page with Playwright, clicks through pagination (hitmarker's pager is JS-driven, not URL-based), and parses each job card with BeautifulSoup. Listing content is upserted into MongoDB keyed on its link (stored once no matter how many feeds match it); a separate collection tracks which (feed, link) pairs have already been reported, so the same listing can still be posted to every feed's channel whose filters it matches.
- **`discord_bot.py`** — a Discord bot that, every 15 minutes, runs the scraper once per configured feed and posts an embed for each newly-found listing to that feed's channel.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Create a `.env` file in this directory:
   ```
   DISCORD_TOKEN=your-bot-token

   # One entry per (search URL, channel) pair - add as many as you like, each
   # with its own filters baked into the URL's query string.
   FEEDS_JSON=[{"name": "Internships", "url": "https://hitmarker.net/jobs?...", "channel_id": "111..."}, {"name": "Full-time", "url": "https://hitmarker.net/jobs?...", "channel_id": "222..."}]

   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=job_scraper
   MONGO_COLLECTION=jobs
   MONGO_POSTED_COLLECTION=posted_jobs
   ```
   `MONGO_URI` can point at a local MongoDB instance or a hosted one (e.g. MongoDB Atlas). If a listing matches more than one feed's filters, it's posted to every matching feed's channel independently.

3. Create a bot application in the [Discord Developer Portal](https://discord.com/developers/applications), invite it to your server with the `Send Messages` and `Embed Links` permissions, and use its channel IDs in `FEEDS_JSON`.

## Running

Scrape once and write results to `jobs.json` (only newly-found listings are written):
```
python test_scraper.py
```

Run the bot continuously, polling every 15 minutes:
```
python discord_bot.py
```

## Configuration

Feeds are set via `FEEDS_JSON` in `.env` (see above). Page count and staleness cutoff are shared across all feeds and set at the top of `test_scraper.py`:

| Constant | Purpose |
|---|---|
| `NUM_PAGES` | How many pages of results to click through, per feed |
| `MAX_AGE` | Only keep listings posted within this window |

## Deployment

A `Dockerfile` is included for deploying the bot as a persistent, always-on service (based on Playwright's official image, which ships Chromium preinstalled). Point any container host (Railway, Fly.io, a VPS, etc.) at it and set the same environment variables as your `.env` file in that platform's dashboard — never commit real secrets into the image.
