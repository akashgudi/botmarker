# discord_work

Scrapes job listings from [hitmarker.net](https://hitmarker.net) and posts new ones to Discord, with deduplication backed by MongoDB. The bot can be invited to any number of servers — each server configures its own feeds through slash commands, with no shared config file or restart required.

## How it works

- **`test_scraper.py`** — loads a target search results page with Playwright, clicks through pagination (hitmarker's pager is JS-driven, not URL-based), and parses each job card with BeautifulSoup. Listing content is upserted into MongoDB keyed on its link (stored once no matter how many feeds match it, across any server); a separate collection tracks which (server, feed, link) tuples have already been reported, so the same listing can still be posted to every feed's channel whose filters it matches — including feeds in different servers.
- **`discord_bot.py`** — a Discord bot that, every `POLL_MINUTES`, runs the scraper once per configured feed (across every server it's in) and posts an embed for each newly-found listing to that feed's channel.

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
   MONGO_FEEDS_COLLECTION=feeds
   ```
   `MONGO_URI` can point at a local MongoDB instance or a hosted one (e.g. MongoDB Atlas). `DISCORD_GUILD_ID` is optional — see the comment above it in `.env` — and only useful for local dev.

3. Create a bot application in the [Discord Developer Portal](https://discord.com/developers/applications). Under OAuth2 > URL Generator, select the `bot` and `applications.commands` scopes (both required — the second registers slash commands) and these bot permissions: `View Channels`, `Send Messages`, `Embed Links`, `Add Reactions`, `Read Message History`, `Manage Messages`, `Manage Threads`, `Create Public Threads` (permission integer `51539700800`). No privileged gateway intents (Message Content / Server Members / Presence) are needed. Use the generated URL to invite it — this is also the link to share for self-serve invites to other servers.

4. Run the bot (see below), then configure feeds from within Discord — no files to edit:
   ```
   /add_feed name:Internships url:https://hitmarker.net/jobs?... channel:#internships
   ```
   Requires the **Manage Server** permission. Each server manages its own feeds; the same feed name can be reused across different servers.

   | Command | Purpose |
   |---|---|
   | `/list_feeds` | Show this server's configured feeds |
   | `/edit_feed` | Update a feed's name/URL/channel in place, without losing its dedup history |
   | `/remove_feed` | Delete a feed entirely (asks for confirmation) |
   | `/reset_feed` | Clear one feed's dedup history so its next scrape reposts everything currently matching (asks for confirmation) |
   | `/clear_feed` | Delete all messages/threads in one feed's channel (asks for confirmation) |
   | `/clear_feeds` | Delete all messages/threads across every feed's channel (asks for confirmation) |
   | `/scrape` | Run a scrape immediately instead of waiting for the next poll |
   | `/search` | Search this server's own posted job listings by keyword |

## Running

Run the bot continuously, polling every `POLL_MINUTES` (see `discord_bot.py`):
```
python discord_bot.py
```

Scrape one server's feeds manually and write results to `jobs.json` (only newly-found listings are written):
```
python test_scraper.py <guild_id>
```

## Configuration

Feeds are managed per-server via `/add_feed`, `/remove_feed`, and `/list_feeds` (see Setup above) — there's no config file. Page count, staleness cutoff, and the per-server feed cap are shared across all servers and set at the top of `test_scraper.py`:

| Constant | Purpose |
|---|---|
| `NUM_PAGES` | How many pages of results to click through, per feed |
| `MAX_AGE` | Only keep listings posted within this window |
| `MAX_FEEDS_PER_GUILD` | Cap on feeds per server — scraping is sequential across every server's feeds, so this bounds how much one server can add to the poll cycle |

## Deployment

A `Dockerfile` is included for deploying the bot as a persistent, always-on service (based on Playwright's official image, which ships Chromium preinstalled). Point any container host (Railway, Fly.io, a VPS, etc.) at it and set the same environment variables as your `.env` file in that platform's dashboard — never commit real secrets into the image. Feed configuration lives in MongoDB, not the image, so adding servers or feeds never requires a rebuild.

### Migrating an existing single-server deployment

Older versions of this bot configured feeds via a `feeds.json` file for one hardcoded server. If you're upgrading from that setup, run `python migrate_feeds.py` once (with `DISCORD_GUILD_ID` and `feeds.json` still present) before deploying the new code — it moves those feeds and their dedup history into MongoDB. See the comment at the top of `migrate_feeds.py` for details, then delete both files.

## Features

- **Multi-server, self-serve** — invite the bot to any number of servers; each configures its own feeds via slash commands (Manage Server permission required), with feed names, dedup state, and search results all scoped per server.
- **Full feed management** — `/add_feed`, `/edit_feed` (update name/URL/channel without losing dedup history), `/remove_feed`, and `/list_feeds` cover the feed lifecycle; destructive commands (`/remove_feed`, `/reset_feed`, `/clear_feed`, `/clear_feeds`) ask for confirmation via buttons before doing anything.
- **Multi-feed scraping** — configure any number of (search URL, channel) pairs per server; each feed is scraped and posted independently, and listings matching multiple feeds' filters are posted to every matching channel.
- **Automatic polling** — scrapes every `POLL_MINUTES` on a background loop, posting only newly-found listings; `/scrape` runs a server's feeds immediately instead of waiting.
- **Deduplication** — listings are upserted into MongoDB keyed on their link, with a separate per-(server, feed, link) tracking collection so a listing is never reposted to a channel it's already been sent to; `/reset_feed` clears one feed's dedup history on demand.
- **Rich embeds** — each posting includes title, company, location, position type, compensation, a dynamic viewer-local posted time, and the company logo as a thumbnail.
- **Forum channel support** — if a feed's target channel is a Discord forum, each listing is posted as its own thread instead of a plain message; `/clear_feed`/`/clear_feeds` clear a server's feed channel(s), deleting threads for forum channels.
- **Save-for-later via reaction** — reacting 🔖 on a listing DMs that listing's embed to the reacting user, and works even on listings posted before the bot's current process started.
- **`/search` slash command** — search a server's own posted job listings by keyword against title/company/location/type and get the top matches back as embeds.
- **Instant onboarding** — joining a new server immediately syncs slash commands there (rather than waiting on global propagation) and posts a welcome message explaining `/add_feed`.
