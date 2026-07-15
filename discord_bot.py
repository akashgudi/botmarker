"""
Discord bot that periodically scrapes job listings and posts only the ones
not already seen (dedup is handled in test_scraper.save_new_jobs via a
unique Mongo index on `link`).

Requires: pip install discord.py python-dotenv
Env vars: DISCORD_TOKEN, DISCORD_CHANNEL_ID, MONGO_URI (optional, see test_scraper.py)
Loaded from a .env file in this directory.
"""

import asyncio
import os

# Must run before importing test_scraper - its Mongo config constants are read
# from os.environ at import time, so .env has to be loaded first or they'd
# pick up the hardcoded fallback defaults instead of your real values.
from dotenv import load_dotenv

load_dotenv()

import discord
from discord.ext import tasks

from test_scraper import scrape_and_store

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
POLL_MINUTES = 15

intents = discord.Intents.default()
client = discord.Client(intents=intents)


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
    if job.get("date_posted"):
        embed.set_footer(text=job["date_posted"])
    return embed


@tasks.loop(minutes=POLL_MINUTES)
async def poll_jobs():
    channel = client.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        # get_channel reads from the gateway cache only - this fires if the bot
        # was never invited to the server that channel belongs to (or the ID is
        # wrong), not just if the channel doesn't exist.
        print(f"Channel {DISCORD_CHANNEL_ID} not found")
        return

    # scrape_and_store is synchronous (Playwright's sync API + pymongo), so it
    # would block the whole event loop - including Discord's heartbeat - for
    # the duration of the scrape. to_thread runs it off the event loop instead.
    new_jobs = await asyncio.to_thread(scrape_and_store)
    for job in new_jobs:
        await channel.send(embed=job_embed(job))


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    if not poll_jobs.is_running():
        poll_jobs.start()


client.run(DISCORD_TOKEN)
