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
import re
import time
from typing import Optional

import discord
from discord import app_commands

import steam_leaderboard
from steam_leaderboard import BOARD_ID, get_current_players, get_leaderboard, get_top, search as search_steam
from zerohour_db import leaderboard, player_stats
from zhsb_feed import dig, get_map_images, get_zhsb

COLOR = 0x2B8CFF

# Which commands reply privately. True = only the person who used the command sees the reply.
# False = everyone in the channel sees it. Change any of these and restart the bot.
EPHEMERAL = {
    "stats": True,
    "mapstats": True,
    "rank": False,
    "leaderboard": False,
    "top10": False,
    "training": True,
    "playercount": False,
    "showservers": True,
}


def private(interaction: discord.Interaction) -> bool:
    return EPHEMERAL.get(interaction.command.name, False)

# Your own map pictures: put them in a folder named "maps" next to this file. Name each one by the
# map's number or its name, e.g. 5.png, bank heist.png, Bank_Heist.jpg (capitals, spaces and
# symbols don't matter; .png .jpg .jpeg .webp all work).
MAP_PICTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps")


def _simplify(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def local_map_picture(room: dict):
    """Finds your own picture for a server's map in the maps folder. Returns a file path or None."""
    if not os.path.isdir(MAP_PICTURE_DIR):
        return None
    files = {}
    for name in os.listdir(MAP_PICTURE_DIR):
        stem, ext = os.path.splitext(name)
        if ext.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            files.setdefault(_simplify(stem), os.path.join(MAP_PICTURE_DIR, name))
    for label in (dig(room, "map", "int"), dig(room, "map", "name")):
        key = _simplify(label)
        if key and key in files:
            return files[key]
    return None


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


def link(name: str, url) -> str:
    """Bold player name that links to their Steam profile (plain bold name if there is no link)."""
    safe = discord.utils.escape_markdown(str(name)).replace("[", "\\[").replace("]", "\\]")
    return f"**[{safe}]({url})**" if url else f"**{safe}**"


def steam_url(player_key):
    """The tracker stores the game's player ID. If it is a Steam ID number, return the profile link."""
    key = str(player_key or "")
    if re.fullmatch(r"7656119\d{10}", key):
        return f"https://steamcommunity.com/profiles/{key}"
    return None


def not_found(name: str) -> str:
    return f"No stats found for **{name}**. They may not have been tracked yet."


@client.tree.command(name="stats", description="Show a player's overall Zero Hour stats")
@app_commands.describe(player="The player's name")
async def stats(interaction: discord.Interaction, player: str):
    await interaction.response.defer(ephemeral=private(interaction))
    s = await asyncio.to_thread(player_stats, player)
    if not s:
        await respond(interaction, not_found(player))
        return
    e = discord.Embed(title=f"{s['name']} - Zero Hour", url=steam_url(s.get("player_key")), color=COLOR)
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
    await interaction.response.defer(ephemeral=private(interaction))
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
    await interaction.response.defer(ephemeral=private(interaction))
    key = sort.value if sort else "kd"
    rows = await asyncio.to_thread(leaderboard, key, min_matches, 10)
    if not rows:
        await respond(interaction, "No players match that filter yet.")
        return
    lines = [
        f"`{i}.` {link(r['name'], steam_url(r.get('player_key')))} - K/D {r['kd']} | {r['wins']}-{r['losses']} | {r['total_matchpoints']:,} pts"
        for i, r in enumerate(rows, 1)
    ]
    title = f"Top 10 by {sort.name if sort else 'K/D'}"
    await respond(interaction, embed=discord.Embed(title=title, description="\n".join(lines), color=COLOR))


@client.tree.command(name="rank", description="Check a player's rank and total matchpoints on the Steam top 200")
@app_commands.describe(player="Steam name, Steam ID, or Steam profile link")
async def rank(interaction: discord.Interaction, player: str):
    await interaction.response.defer(ephemeral=private(interaction))
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
        lines = [f"`#{m['rank']}` {link(m['name'], m['url'])} - {m['score']:,} pts" for m in matches[:5]]
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
        f"`#{e['rank']}` {link(e['name'], e.get('url'))} - {e['score']:,}{unit}"
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
    await interaction.response.defer(ephemeral=private(interaction))
    try:
        entries, fetched_at = await asyncio.to_thread(get_top, BOARD_ID, 10)
    except Exception:
        await respond(interaction, "I couldn't reach the Steam leaderboard right now. Please try again in a few minutes.")
        return
    await respond(interaction, embed=top_embed("Top 10 - Total matchpoints", entries, fetched_at, " pts"))


@client.tree.command(name="training", description="Show the top 10 on the Steam Training best scores leaderboard")
async def training(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=private(interaction))
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
    await interaction.response.defer(ephemeral=private(interaction))
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
        for label, group in (("Public servers", "public"), ("All servers", "all")):
            n_players = dig(site, "totals", group, "players", default=0)
            n_rooms = dig(site, "totals", group, "rooms", default=0)
            e.add_field(name=label, value=f"{n_players} players\n{n_rooms} servers")
        sources.append("zhsb.info")
    e.set_footer(text="Sources: " + ", ".join(sources) + " | refreshed every 30-60 seconds")
    await respond(interaction, embed=e)


@client.tree.command(name="showservers", description="Show the public Zero Hour servers listed on zhsb.info")
async def showservers(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=private(interaction))
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

    # One embed per server, each with its map picture on the right (Discord allows 10 per message).
    # Your own pictures from the "maps" folder are used first; otherwise the ones from zhsb.info.
    embeds, files = [], []
    for n, r in enumerate(rooms[:10]):
        e = discord.Embed(
            title=f"{dig(r, 'region', 'name')} | {dig(r, 'match', 'id')} ({dig(r, 'players', 'summary')})",
            description=f"{dig(r, 'map', 'name')} - {dig(r, 'game', 'summary')}\n{dig(r, 'match', 'summary')}",
            color=COLOR,
        )
        path = local_map_picture(r)
        web = images.get(str(dig(r, "map", "int"))) or images.get(str(dig(r, "map", "name")).lower())
        if path:
            filename = f"map{n}{os.path.splitext(path)[1].lower()}"
            files.append(discord.File(path, filename=filename))
            e.set_thumbnail(url=f"attachment://{filename}")
        elif web:
            e.set_thumbnail(url=web)
        else:
            print(f"[showservers] no picture found for map: {r.get('map')}")  # shows in the host's console
        embeds.append(e)

    age = int(time.time() - fetched_at)
    more = f" | ...and {len(rooms) - 10} more" if len(rooms) > 10 else ""
    embeds[-1].set_footer(text=f"Source: zhsb.info | public servers only | updated {age}s ago{more}")
    extra = {"files": files} if files else {}
    await respond(interaction, f"**{players} players in {count} public servers**", embeds=embeds, **extra)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    client.run(token)
