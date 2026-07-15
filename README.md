# discord_work

Scrapes job listings from [hitmarker.net](https://hitmarker.net) and posts new ones to a Discord channel, with deduplication backed by MongoDB.

## How it works

- **`test_scraper.py`** — loads the target search results page with Playwright, clicks through pagination (hitmarker's pager is JS-driven, not URL-based), and parses each job card with BeautifulSoup. Listings are upserted into MongoDB keyed on their link, so a listing already in the database is never reprocessed or reposted.
- **`discord_bot.py`** — a Discord bot that runs the scraper every 15 minutes and posts an embed for each newly-found listing to a configured channel.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Create a `.env` file in this directory:
   ```
   DISCORD_TOKEN=your-bot-token
   DISCORD_CHANNEL_ID=the-channel-id-to-post-to

   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=job_scraper
   MONGO_COLLECTION=jobs
   ```
   `MONGO_URI` can point at a local MongoDB instance or a hosted one (e.g. MongoDB Atlas).

3. Create a bot application in the [Discord Developer Portal](https://discord.com/developers/applications), invite it to your server with the `Send Messages` and `Embed Links` permissions, and use its channel ID for `DISCORD_CHANNEL_ID`.

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

Search parameters, page count, and staleness cutoff are set at the top of `test_scraper.py`:

| Constant | Purpose |
|---|---|
| `TARGET_URL` | The hitmarker.net search results URL to scrape |
| `NUM_PAGES` | How many pages of results to click through |
| `MAX_AGE` | Only keep listings posted within this window |

## Deployment

A `Dockerfile` is included for deploying the bot as a persistent, always-on service (based on Playwright's official image, which ships Chromium preinstalled). Point any container host (Railway, Fly.io, a VPS, etc.) at it and set the same environment variables as your `.env` file in that platform's dashboard — never commit real secrets into the image.
