"""
Discord bot that can be invited to any number of servers. Each server manages
its own feeds via /add_feed, /remove_feed, and /list_feeds; periodically it
scrapes every server's configured feeds and posts only the listings not
already posted to that feed's channel (dedup is handled in
test_scraper.save_new_jobs via a unique Mongo index on `link` plus a
per-(guild, feed, link) posted-tracking collection).

Requires: pip install discord.py python-dotenv
Env vars: DISCORD_TOKEN, MONGO_URI (optional, see test_scraper.py)
Loaded from a .env file in this directory.
"""

import asyncio
import os
from datetime import datetime
from typing import Optional, Union

# Must run before importing test_scraper - its Mongo config constants are read
# from os.environ at import time, so .env has to be loaded first or they'd
# pick up the hardcoded fallback defaults instead of your real values.
from dotenv import load_dotenv

load_dotenv()

import discord
from discord import app_commands
from discord.ext import tasks

from test_scraper import add_feed, list_feeds, remove_feed, scrape_and_store, search_jobs

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
# Optional dev convenience: a guild ID to additionally sync commands to
# instantly (guild-scoped syncs apply immediately, unlike the global sync in
# on_ready, which can take up to an hour to propagate to Discord clients).
# Not required - new servers get instant sync automatically via on_guild_join.
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID")
POLL_MINUTES = 60
# Reacting with this emoji on a job listing DMs the reacting user that listing,
# as a bookmark/save-for-later.
SAVE_EMOJI = "🔖"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True) -> bool:
    """Acknowledge an interaction, returning False (and doing nothing else) if
    it's already been acknowledged.

    Discord's gateway can rarely redeliver the same interaction (e.g. after a
    reconnect/session resume), spawning a second concurrent task for it - the
    first task to call defer()/send_message() wins, and the second's call
    raises HTTPException 40060 rather than silently no-opping. Callers should
    treat a False return as "another task already handled this" and return
    immediately instead of continuing.
    """
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.HTTPException as e:
        if e.code != 40060:
            raise
        print(f"Interaction {interaction.id} was already acknowledged elsewhere - skipping duplicate dispatch.")
        return False


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


async def scrape_and_post(feed: dict) -> int:
    """Scrape one feed and post any new listings to its channel. Returns the
    number of listings posted (0 if the channel couldn't be found)."""
    channel = client.get_channel(int(feed["channel_id"]))
    if channel is None:
        # get_channel reads from the gateway cache only - this fires if the bot
        # was never invited to the server that channel belongs to (or the ID is
        # wrong), not just if the channel doesn't exist.
        print(f"[{feed['name']}] channel {feed['channel_id']} not found")
        return 0

    # scrape_and_store is synchronous (Playwright's sync API + pymongo), so it
    # would block the whole event loop - including Discord's heartbeat - for
    # the duration of the scrape. to_thread runs it off the event loop instead.
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
    return len(new_jobs)


@tasks.loop(minutes=POLL_MINUTES)
async def poll_jobs():
    # tasks.loop runs its body immediately on start() before waiting out the
    # first interval - skip that iteration so launching the bot doesn't fire
    # off an unrequested scrape.
    if poll_jobs.current_loop == 0:
        return

    # Feeds are scraped one at a time (not concurrently, across every guild the
    # bot is in) to keep at most one Playwright browser open per poll cycle.
    for guild in client.guilds:
        feeds = await asyncio.to_thread(list_feeds, guild.id)
        for feed in feeds:
            await scrape_and_post(feed)


@tree.command(name="search", description="Search this server's job listings by keyword")
@app_commands.describe(keyword="Word or phrase to match against title/company/location/type")
@app_commands.guild_only()
async def search(interaction: discord.Interaction, keyword: str):
    if not await safe_defer(interaction):
        return

    # search_jobs hits Mongo synchronously - to_thread keeps it off the event loop.
    # Scoped to this guild's own feeds, not every server's scraped content.
    jobs = await asyncio.to_thread(search_jobs, keyword, interaction.guild_id, 5)

    if not jobs:
        await interaction.followup.send(f"No jobs found matching '{keyword}'.", ephemeral=True)
        return

    await interaction.followup.send(embeds=[job_embed(job) for job in jobs], ephemeral=True)


@tree.command(name="add_feed", description="Add a job feed for this server (Manage Server permission required)")
@app_commands.describe(
    name="Short name for this feed (must be unique in this server)",
    url="A hitmarker.net search-results URL with your filters already applied",
    channel="Channel (or forum) new listings for this feed should be posted to",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def add_feed_cmd(
    interaction: discord.Interaction,
    name: str,
    url: str,
    channel: Union[discord.TextChannel, discord.ForumChannel],
):
    if not await safe_defer(interaction):
        return

    try:
        await asyncio.to_thread(
            add_feed, interaction.guild_id, name, url, channel.id, str(interaction.user.id)
        )
    except ValueError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return

    await interaction.followup.send(f"Added feed '{name}' -> {channel.mention}.", ephemeral=True)


@tree.command(name="remove_feed", description="Remove one of this server's feeds (Manage Server permission required)")
@app_commands.describe(name="Name of the feed to remove")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def remove_feed_cmd(interaction: discord.Interaction, name: str):
    if not await safe_defer(interaction):
        return

    removed = await asyncio.to_thread(remove_feed, interaction.guild_id, name)
    if removed:
        await interaction.followup.send(f"Removed feed '{name}'.", ephemeral=True)
    else:
        await interaction.followup.send(f"No feed named '{name}' found.", ephemeral=True)


@remove_feed_cmd.autocomplete("name")
async def remove_feed_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []
    feeds = await asyncio.to_thread(list_feeds, interaction.guild_id)
    return [
        app_commands.Choice(name=f["name"], value=f["name"])
        for f in feeds
        if current.lower() in f["name"].lower()
    ][:25]


@tree.command(name="list_feeds", description="List this server's configured feeds (Manage Server permission required)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def list_feeds_cmd(interaction: discord.Interaction):
    if not await safe_defer(interaction):
        return

    feeds = await asyncio.to_thread(list_feeds, interaction.guild_id)
    if not feeds:
        await interaction.followup.send(
            "No feeds configured yet - use /add_feed to add one.", ephemeral=True
        )
        return

    lines = [f"**{f['name']}** -> <#{f['channel_id']}>\n{f['url']}" for f in feeds]
    await interaction.followup.send("\n\n".join(lines), ephemeral=True)


@tree.command(name="scrape", description="Run a feed scrape right now instead of waiting for the next poll")
@app_commands.describe(feed="Name of a specific feed to scrape (leave empty to scrape all feeds)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def scrape(interaction: discord.Interaction, feed: Optional[str] = None):
    guild_feeds = await asyncio.to_thread(list_feeds, interaction.guild_id)

    if feed is None:
        targets = guild_feeds
    else:
        targets = [f for f in guild_feeds if f["name"].lower() == feed.lower()]
        if not targets:
            names = ", ".join(f["name"] for f in guild_feeds) or "none configured"
            try:
                await interaction.response.send_message(
                    f"No feed named '{feed}'. Available feeds: {names}", ephemeral=True
                )
            except discord.HTTPException as e:
                if e.code != 40060:
                    raise
                print(f"Interaction {interaction.id} was already acknowledged elsewhere - skipping duplicate dispatch.")
            return

    if not await safe_defer(interaction):
        return

    # Scraped one at a time (not concurrently) to keep at most one Playwright
    # browser open at a time, same as the periodic poll.
    results = []
    for f in targets:
        count = await scrape_and_post(f)
        results.append(f"{f['name']}: {count} new")

    await interaction.followup.send(
        "Scrape complete.\n" + "\n".join(results) if results else "No feeds configured for this server.",
        ephemeral=True,
    )


@scrape.autocomplete("feed")
async def scrape_feed_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []
    feeds = await asyncio.to_thread(list_feeds, interaction.guild_id)
    return [
        app_commands.Choice(name=f["name"], value=f["name"])
        for f in feeds
        if current.lower() in f["name"].lower()
    ][:25]


@tree.command(name="clear_feeds", description="Delete all messages in every feed channel (Manage Server permission required)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.guild_only()
async def clear_feeds(interaction: discord.Interaction):
    if not await safe_defer(interaction):
        return

    feeds = await asyncio.to_thread(list_feeds, interaction.guild_id)
    cleared = []
    for feed in feeds:
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


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the Manage Server permission to use this command."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "This command can only be used in a server."
    else:
        print(f"Unhandled app command error: {error}")
        message = "Something went wrong running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException as e:
        if e.code != 40060:
            raise
        print(f"Interaction {interaction.id} was already acknowledged elsewhere - skipping error reply.")


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
async def on_guild_join(guild: discord.Guild):
    # Guild-scoped sync applies instantly, unlike the global sync in on_ready
    # (which can take up to an hour to reach Discord clients) - without this, a
    # newly-invited server would see no slash commands for a while.
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)

    channel = guild.system_channel
    if channel is None or not channel.permissions_for(guild.me).send_messages:
        channel = next(
            (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
            None,
        )
    if channel is None:
        return

    try:
        await channel.send(
            "Thanks for adding me! Use `/add_feed` (requires the Manage Server "
            f"permission) to configure a job feed - I'll scrape it every {POLL_MINUTES} "
            "minutes and post new listings to the channel you choose. `/list_feeds` "
            "shows what's configured, `/remove_feed` deletes one, and `/scrape` runs "
            "a feed immediately instead of waiting for the next poll."
        )
    except discord.Forbidden:
        pass


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    await tree.sync()

    if DISCORD_GUILD_ID:
        # Optional dev convenience for instant local sync - see DISCORD_GUILD_ID above.
        guild = discord.Object(id=int(DISCORD_GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)

    if not poll_jobs.is_running():
        poll_jobs.start()


client.run(DISCORD_TOKEN)
