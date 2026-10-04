"""
Zero Hour Discord bot. Slash commands: /stats, /mapstats, /leaderboard, /rank, /top10, /training, /playercount, /showservers

Needs these files in the same folder: zerohour_bot.py, zerohour_db.py,
steam_leaderboard.py, zhsb_feed.py. (/rank reads the Steam top 200 and does not need the client.)

Setup:
    pip install discord.py fastapi uvicorn
    set DISCORD_TOKEN=your-bot-token        (Windows)
    export DISCORD_TOKEN=your-bot-token     (Mac/Linux)
    python zerohour_bot.py

Keep this file in the same folder as zerohour_db.py. The bot reads the same
database file (zerohour_stats.db) that the tracker fills with match data.
"""
import asyncio
import os
import time
from typing import Optional

import discord
from discord import app_commands

import steam_leaderboard
from steam_leaderboard import BOARD_ID, get_current_players, get_leaderboard, get_top, search as search_steam
from zerohour_db import leaderboard, player_stats
from zhsb_feed import dig, get_map_images, get_zhsb

COLOR = 0x2B8CFF


async def respond(interaction: discord.Interaction, content: Optional[str] = None, **kwargs):
    """Send a reply that @mentions the person who used the command.
    Only that one person can be pinged, so names typed into a command can't ping anyone else."""
    mention = interaction.user.mention
    await interaction.followup.send(
        f"{mention} {content}" if content else mention,
        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[interaction.user]),
        **kwargs,
    )


class StatsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()  # registers the slash commands with Discord


client = StatsBot()


def not_found(name: str) -> str:
    return f"No stats found for **{name}**. They may not have been tracked yet."


@client.tree.command(name="stats", description="Show a player's overall Zero Hour stats")
@app_commands.describe(player="The player's name")
async def stats(interaction: discord.Interaction, player: str):
    await interaction.response.defer()
    s = await asyncio.to_thread(player_stats, player)
    if not s:
        await respond(interaction, not_found(player))
        return
    e = discord.Embed(title=f"{s['name']} - Zero Hour", color=COLOR)
    e.add_field(name="K/D", value=f"{s['kd']}")
    e.add_field(name="Wins / Losses", value=f"{s['wins']} / {s['losses']}")
    e.add_field(name="Win rate", value=f"{s['win_rate']}%")
    e.add_field(name="Matches", value=f"{s['matches']}")
    e.add_field(name="Total matchpoints", value=f"{s['total_matchpoints']:,}")
    e.add_field(name="Avg matchpoints", value=f"{s['avg_matchpoints']}")
    await respond(interaction, embed=e)


@client.tree.command(name="mapstats", description="Show a player's stats by map")
@app_commands.describe(player="The player's name", map="Optional: a specific map")
async def mapstats(interaction: discord.Interaction, player: str, map: Optional[str] = None):
    await interaction.response.defer()
    s = await asyncio.to_thread(player_stats, player)
    if not s:
        await respond(interaction, not_found(player))
        return
    maps = s["maps"]
    if map:
        maps = [m for m in maps if m["map"].lower() == map.lower()]
        if not maps:
            await respond(interaction, f"**{s['name']}** has no recorded matches on **{map}**.")
            return
    lines = [
        f"**{m['map']}**: {m['matches']} matches | K/D {m['kd']} | "
        f"{m['wins']}-{m['losses']} ({m['win_rate']}%) | {m['total_matchpoints']:,} pts"
        for m in maps[:15]
    ]
    e = discord.Embed(title=f"{s['name']} - by map", description="\n".join(lines), color=COLOR)
    await respond(interaction, embed=e)


SORTS = [
    app_commands.Choice(name="K/D", value="kd"),
    app_commands.Choice(name="Wins", value="wins"),
    app_commands.Choice(name="Win rate", value="win_rate"),
    app_commands.Choice(name="Total matchpoints", value="total_matchpoints"),
]


@client.tree.command(name="leaderboard", description="Show the top players")
@app_commands.describe(sort="What to rank by", min_matches="Minimum matches played (default 5)")
@app_commands.choices(sort=SORTS)
async def leaderboard_cmd(
    interaction: discord.Interaction,
    sort: Optional[app_commands.Choice[str]] = None,
    min_matches: int = 5,
):
    await interaction.response.defer()
    key = sort.value if sort else "kd"
    rows = await asyncio.to_thread(leaderboard, key, min_matches, 10)
    if not rows:
        await respond(interaction, "No players match that filter yet.")
        return
    lines = [
        f"`{i}.` **{r['name']}** - K/D {r['kd']} | {r['wins']}-{r['losses']} | {r['total_matchpoints']:,} pts"
        for i, r in enumerate(rows, 1)
    ]
    title = f"Top 10 by {sort.name if sort else 'K/D'}"
    await respond(interaction, embed=discord.Embed(title=title, description="\n".join(lines), color=COLOR))


@client.tree.command(name="rank", description="Check a player's rank and total matchpoints on the Steam top 200")
@app_commands.describe(player="Steam name, Steam ID, or Steam profile link")
async def rank(interaction: discord.Interaction, player: str):
    await interaction.response.defer()
    try:
        entries, fetched_at = await asyncio.to_thread(get_leaderboard)
    except Exception:
        await respond(interaction, "I couldn't reach the Steam leaderboard right now. Please try again in a few minutes.")
        return

    matches = search_steam(entries, player)
    if not matches:
        await respond(interaction, 
            f"I couldn't find **{player}** in the Steam top 200. Check the spelling, or they may be "
            "ranked below 200, since Steam only publishes the top 200."
        )
        return

    age = max(1, int((time.time() - fetched_at) / 60))
    footer = f"Steam MP Points leaderboard, top 200 | updated {age} min ago"

    if len(matches) > 1:
        lines = [f"`#{m['rank']}` **{m['name']}** - {m['score']:,} pts" for m in matches[:5]]
        e = discord.Embed(
            title=f"{len(matches)} players match \"{player}\"",
            description="\n".join(lines) + "\n\nTry the full name, a Steam ID, or a profile link to narrow it down.",
            color=COLOR,
        )
    else:
        m = matches[0]
        e = discord.Embed(title=m["name"], url=m["url"], color=COLOR)
        e.add_field(name="Steam rank", value=f"#{m['rank']} of 200")
        e.add_field(name="Total matchpoints", value=f"{m['score']:,}")
        if m.get("avatar"):
            e.set_thumbnail(url=m["avatar"])
    e.set_footer(text=footer)
    await respond(interaction, embed=e)


def top_embed(title: str, entries: list, fetched_at: float, unit: str) -> discord.Embed:
    lines = [
        f"`#{e['rank']}` **{discord.utils.escape_markdown(e['name'])}** - {e['score']:,}{unit}"
        for e in entries
    ]
    e = discord.Embed(title=title, description="\n".join(lines), color=COLOR)
    if entries and entries[0].get("avatar"):
        e.set_thumbnail(url=entries[0]["avatar"])  # picture of the player in first place
    age = max(1, int((time.time() - fetched_at) / 60))
    e.set_footer(text=f"Source: Steam leaderboard | updated {age} min ago")
    return e


@client.tree.command(name="top10", description="Show the top 10 players by total matchpoints on Steam")
async def top10(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        entries, fetched_at = await asyncio.to_thread(get_top, BOARD_ID, 10)
    except Exception:
        await respond(interaction, "I couldn't reach the Steam leaderboard right now. Please try again in a few minutes.")
        return
    await respond(interaction, embed=top_embed("Top 10 - Total matchpoints", entries, fetched_at, " pts"))


@client.tree.command(name="training", description="Show the top 10 on the Steam Training best scores leaderboard")
async def training(interaction: discord.Interaction):
    await interaction.response.defer()
    board = steam_leaderboard.TRAINING_BOARD_ID
    if board is None:
        await respond(interaction, "The Training leaderboard hasn't been set up yet.")
        return
    try:
        entries, fetched_at = await asyncio.to_thread(get_top, board, 10)
    except Exception:
        await respond(interaction, "I couldn't reach the Steam leaderboard right now. Please try again in a few minutes.")
        return
    await respond(interaction, embed=top_embed("Top 10 - Training best scores (lower is better)", entries, fetched_at, ""))


@client.tree.command(name="playercount", description="Show how many people are playing Zero Hour right now")
async def playercount(interaction: discord.Interaction):
    await interaction.response.defer()
    steam = site = None
    try:
        steam, _ = await asyncio.to_thread(get_current_players)
    except Exception:
        pass
    try:
        site, _ = await get_zhsb()
    except Exception:
        pass
    if steam is None and site is None:
        await respond(interaction, "I couldn't get the player count right now. Please try again shortly.")
        return

    e = discord.Embed(title="Zero Hour - players right now", color=COLOR)
    sources = []
    if steam is not None:
        e.add_field(name="Playing on Steam", value=f"{steam:,}")
        sources.append("Steam")
    if site is not None:
        e.add_field(name="In public servers", value=f"{dig(site, 'totals', 'public', 'players', default=0)}")
        e.add_field(name="In all servers", value=f"{dig(site, 'totals', 'all', 'players', default=0)}")
        sources.append("zhsb.info")
    e.set_footer(text="Sources: " + ", ".join(sources) + " | refreshed every 30-60 seconds")
    await respond(interaction, embed=e)


@client.tree.command(name="showservers", description="Show the public Zero Hour servers listed on zhsb.info")
async def showservers(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data, fetched_at = await get_zhsb()
    except Exception:
        await respond(interaction, "I couldn't reach the zhsb.info server list right now. Please try again shortly.")
        return

    rooms = data.get("rooms") or []
    if not rooms:
        await respond(interaction, "No public servers are active right now.")
        return

    players = dig(data, "totals", "public", "players", default="?")
    count = dig(data, "totals", "public", "rooms", default=len(rooms))
    images = await get_map_images()

    # One small embed per server, each with its map picture at the bottom (Discord allows 10 per message).
    embeds = []
    for r in rooms[:10]:
        e = discord.Embed(
            title=f"{dig(r, 'region', 'name')} | {dig(r, 'match', 'id')} ({dig(r, 'players', 'summary')})",
            description=f"{dig(r, 'map', 'name')} - {dig(r, 'game', 'summary')}\n{dig(r, 'match', 'summary')}",
            color=COLOR,
        )
        picture = images.get(str(dig(r, "map", "id"))) or images.get(str(dig(r, "map", "name")).lower())
        if picture:
            e.set_image(url=picture)
        embeds.append(e)

    age = int(time.time() - fetched_at)
    more = f" | ...and {len(rooms) - 10} more" if len(rooms) > 10 else ""
    embeds[-1].set_footer(text=f"Source: zhsb.info | public servers only | updated {age}s ago{more}")
    await respond(interaction, f"**{players} players in {count} public servers**", embeds=embeds)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    client.run(token)
