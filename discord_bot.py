"""
Discord bot that periodically scrapes each configured feed and posts only the
listings not already posted to that feed's channel (dedup is handled in
test_scraper.save_new_jobs via a unique Mongo index on `link` plus a
per-(feed, link) posted-tracking collection).

Requires: pip install discord.py python-dotenv
Env vars: DISCORD_TOKEN, MONGO_URI (optional, see test_scraper.py)
Loaded from a .env file in this directory. Feeds are configured in feeds.json.
"""

import asyncio
import os
from datetime import datetime

# Must run before importing test_scraper - its Mongo config constants are read
# from os.environ at import time, so .env has to be loaded first or they'd
# pick up the hardcoded fallback defaults instead of your real values.
from dotenv import load_dotenv

load_dotenv()

import discord
from discord import app_commands
from discord.ext import tasks

from test_scraper import FEEDS, scrape_and_store, search_jobs

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
# Optional: set this to a guild ID for instant command sync while testing -
# guild-scoped syncs apply immediately, global ones take up to an hour to
# propagate to Discord clients.
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID")
POLL_MINUTES = 60
# Reacting with this emoji on a job listing DMs the reacting user that listing,
# as a bookmark/save-for-later.
SAVE_EMOJI = "🔖"

if not FEEDS:
    raise RuntimeError("No feeds configured - add entries to feeds.json (see test_scraper.py)")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def job_embed(job: dict) -> discord.Embed:
    embed = discord.Embed(title=job.get("title") or "New job listing", url=job["link"])
    if job.get("company"):
        embed.add_field(name="Company", value=job["company"], inline=True)
    if job.get("location"):
        embed.add_field(name="Location", value=job["location"], inline=True)
    if job.get("position_type"):
        embed.add_field(name="Type", value=job["position_type"], inline=True)
    if job.get("compensation"):
        embed.add_field(name="Compensation", value=job["compensation"], inline=True)
    if job.get("posted_at"):
        # embed.timestamp renders as a dynamic, viewer-local time next to the
        # footer (e.g. "Today at 3:45 PM") instead of a static string.
        try:
            embed.timestamp = datetime.fromisoformat(job["posted_at"].replace("Z", "+00:00"))
        except ValueError:
            pass
    if job.get("company_logo"):
        embed.set_thumbnail(url=job["company_logo"])
    return embed


@tasks.loop(minutes=POLL_MINUTES)
async def poll_jobs():
    for feed in FEEDS:
        channel = client.get_channel(int(feed["channel_id"]))
        if channel is None:
            # get_channel reads from the gateway cache only - this fires if the bot
            # was never invited to the server that channel belongs to (or the ID is
            # wrong), not just if the channel doesn't exist.
            print(f"[{feed['name']}] channel {feed['channel_id']} not found")
            continue

        # scrape_and_store is synchronous (Playwright's sync API + pymongo), so it
        # would block the whole event loop - including Discord's heartbeat - for
        # the duration of the scrape. to_thread runs it off the event loop instead.
        # Feeds are scraped one at a time (not concurrently) to keep at most one
        # Playwright browser open per poll cycle.
        new_jobs = await asyncio.to_thread(scrape_and_store, feed)
        for job in new_jobs:
            embed = job_embed(job)
            if isinstance(channel, discord.ForumChannel):
                # Forum channels have no .send() - each listing has to become its
                # own post (thread), which requires a name and a starter message.
                thread_with_message = await channel.create_thread(
                    name=(job.get("title") or "New job listing")[:100],
                    embed=embed,
                )
                message = thread_with_message.message
            else:
                message = await channel.send(embed=embed)
            await message.add_reaction(SAVE_EMOJI)


@tree.command(name="search", description="Search stored job listings by keyword")
@app_commands.describe(keyword="Word or phrase to match against title/company/location/type")
async def search(interaction: discord.Interaction, keyword: str):
    await interaction.response.defer()

    # search_jobs hits Mongo synchronously - to_thread keeps it off the event loop.
    jobs = await asyncio.to_thread(search_jobs, keyword, 5)

    if not jobs:
        await interaction.followup.send(f"No jobs found matching '{keyword}'.")
        return

    await interaction.followup.send(embeds=[job_embed(job) for job in jobs])


@tree.command(name="clear_feeds", description="Delete all messages in every feed channel (server owner only)")
async def clear_feeds(interaction: discord.Interaction):
    if interaction.guild is None or interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message(
            "Only the server owner can use this command.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    cleared = []
    for feed in FEEDS:
        channel = client.get_channel(int(feed["channel_id"]))
        if channel is None:
            print(f"[{feed['name']}] channel {feed['channel_id']} not found")
            continue

        if isinstance(channel, discord.ForumChannel):
            # Forum channel content lives in threads (posts), not top-level
            # messages, so clearing it means deleting the threads themselves -
            # both active and archived, since purge() only affects normal channels.
            for thread in channel.threads:
                await thread.delete()
            async for thread in channel.archived_threads(limit=None):
                await thread.delete()
        else:
            await channel.purge(limit=None)
        cleared.append(feed["name"])

    await interaction.followup.send(
        f"Cleared {len(cleared)} feed channel(s): {', '.join(cleared) if cleared else 'none'}.",
        ephemeral=True,
    )


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Raw (not cached-message) event, so this fires even for listings sent before
    # the bot's current process started - a plain on_reaction_add only fires for
    # messages discord.py already has in its message cache.
    if payload.user_id == client.user.id or str(payload.emoji) != SAVE_EMOJI:
        return

    try:
        channel = client.get_channel(payload.channel_id) or await client.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
    except discord.HTTPException as e:
        print(f"Could not fetch reacted-to message {payload.message_id}: {e}")
        return
    if not message.embeds:
        return

    try:
        user = client.get_user(payload.user_id) or await client.fetch_user(payload.user_id)
        await user.send(embed=message.embeds[0])
    except discord.Forbidden:
        print(f"Could not DM user {payload.user_id} (DMs closed)")


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    if DISCORD_GUILD_ID:
        guild = discord.Object(id=int(DISCORD_GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()

    if not poll_jobs.is_running():
        poll_jobs.start()


client.run(DISCORD_TOKEN)
