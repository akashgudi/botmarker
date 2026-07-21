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

   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=job_scraper
   MONGO_COLLECTION=jobs
   MONGO_POSTED_COLLECTION=posted_jobs
   ```
   `MONGO_URI` can point at a local MongoDB instance or a hosted one (e.g. MongoDB Atlas).

3. Create a `feeds.json` file in this directory - one entry per (search URL, channel) pair, add as many as you like, each with its own filters baked into the URL's query string:
   ```json
   [
     {"name": "Internships", "url": "https://hitmarker.net/jobs?...", "channel_id": "111..."},
     {"name": "Full-time", "url": "https://hitmarker.net/jobs?...", "channel_id": "222..."}
   ]
   ```
   It's read once at process start (both `test_scraper.py` and `discord_bot.py`); restart the process after editing it. If a listing matches more than one feed's filters, it's posted to every matching feed's channel independently. Point `FEEDS_FILE` at a different path if you don't want to use `feeds.json`.

4. Create a bot application in the [Discord Developer Portal](https://discord.com/developers/applications), invite it to your server with the `Send Messages` and `Embed Links` permissions, and use its channel IDs in `feeds.json`.

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

Feeds are set in `feeds.json` (see above). Page count and staleness cutoff are shared across all feeds and set at the top of `test_scraper.py`:

| Constant | Purpose |
|---|---|
| `NUM_PAGES` | How many pages of results to click through, per feed |
| `MAX_AGE` | Only keep listings posted within this window |

## Deployment

A `Dockerfile` is included for deploying the bot as a persistent, always-on service (based on Playwright's official image, which ships Chromium preinstalled). Point any container host (Railway, Fly.io, a VPS, etc.) at it and set the same environment variables as your `.env` file in that platform's dashboard — never commit real secrets into the image. `feeds.json` is copied in as part of the image (`COPY . .`), so update it and rebuild/redeploy to change feeds.

## Features

- **Multi-feed scraping** — configure any number of (search URL, channel) pairs via `feeds.json`; each feed is scraped and posted independently, and listings matching multiple feeds' filters are posted to every matching channel.
- **Automatic polling** — scrapes every 15 minutes on a background loop, posting only newly-found listings.
- **Deduplication** — listings are upserted into MongoDB keyed on their link, with a separate per-(feed, link) tracking collection so a listing is never reposted to a channel it's already been sent to.
- **Rich embeds** — each posting includes title, company, location, position type, compensation, a dynamic viewer-local posted time, and the company logo as a thumbnail.
- **Forum channel support** — if a feed's target channel is a Discord forum, each listing is posted as its own thread instead of a plain message.
- **Save-for-later via reaction** — reacting 🔖 on a listing DMs that listing's embed to the reacting user, and works even on listings posted before the bot's current process started.
- **`/search` slash command** — search stored job listings by keyword against title/company/location/type and get the top matches back as embeds.
