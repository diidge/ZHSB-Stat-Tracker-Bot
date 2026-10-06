"""
Zero Hour Discord bot. Slash commands: /stats, /mapstats, /leaderboard, /rank, /top10, /training, /playercount, /showservers

Needs these files in the same folder: zerohour_bot.py, zerohour_db.py, steam_stats.py,
steam_leaderboard.py, zhsb_feed.py. (/rank reads the Steam leaderboard and does not need the client.)

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
import steam_stats
from steam_leaderboard import BOARD_ID, get_current_players, get_leaderboard, get_top, lookup_rank, search as search_steam
from steam_stats import PlayerNotFound, SteamStatsError, find_steam_id, get_summary, get_user_stats, is_direct, leaderboard_entry
from zerohour_db import find_players, get_link, leaderboard, player_stats, remove_link, set_link
from zhsb_ingest import start_ingest_server
from zhsb_feed import dig, get_map_images, get_zhsb

COLOR = 0x2B8CFF

# Which commands reply privately. True = only the person who used the command sees the reply.
# False = everyone in the channel sees it. Change any of these and restart the bot.
EPHEMERAL = {
    "stats": False,
    "mapstats": True,
    "rank": False,
    "leaderboard": False,
    "top10": False,
    "training": True,
    "playercount": False,
    "showservers": True,
    "link": True,
    "unlink": True,
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
        await start_ingest_server()  # small upload endpoint for the readers/recorder (off unless ZH_INGEST_KEY is set)


client = StatsBot()


def link(name: str, url) -> str:
    """Bold player name that links to their Steam profile (plain bold name if there is no link)."""
    text = str(name)
    if url:
        # Discord won't make a link out of text that contains square brackets or looks like a web
        # address (e.g. "discord.gg/zhsb"), so inside a link those characters are swapped for
        # look-alikes: full-width brackets and a "one dot leader".
        text = text.replace("[", "\uff3b").replace("]", "\uff3d").replace(".", "\u2024")
    safe = discord.utils.escape_markdown(text)
    return f"**[{safe}]({url})**" if url else f"**{safe}**"


def steam_url(player_key):
    """The tracker stores the game's player ID. If it is a Steam ID number, return the profile link."""
    key = str(player_key or "")
    if re.fullmatch(r"7656119\d{10}", key):
        return f"https://steamcommunity.com/profiles/{key}"
    return None


async def resolve_target(interaction: discord.Interaction, player: Optional[str], user: Optional[discord.User]):
    """Works out who to look up. Returns (what_to_search, None) or (None, message_to_show)."""
    if player:
        return player, None
    target = user or interaction.user
    steam_id = await asyncio.to_thread(get_link, target.id)
    if steam_id:
        return steam_id, None
    if user:
        return None, f"**{user.display_name}** hasn't linked a Steam account yet. They can do it with /link."
    return None, "Tell me who to look up, or link your own Steam account first with /link."


def not_found(name: str) -> str:
    return f"No stats found for **{name}**. They may not have been tracked yet."


def steam_stats_embed(st: dict, summary: dict, fallback_name: str) -> discord.Embed:
    def num(v, suffix=""):
        """A number for display, or 'not available' when Steam didn't give us a trustworthy value."""
        return "not available" if v is None else (f"{v:,}{suffix}" if isinstance(v, int) else f"{v}{suffix}")

    name = summary.get("name") or fallback_name
    url = summary.get("url") or f"https://steamcommunity.com/profiles/{st['steam_id']}"
    e = discord.Embed(title=f"{name} - Zero Hour", url=url, color=COLOR)
    e.add_field(name="K/D", value=num(st["kd"]))
    e.add_field(name="Kills / Deaths", value=f"{num(st['kills'])} / {num(st['deaths'])}")
    e.add_field(name="Win rate", value=num(st["win_rate"], "%"))
    e.add_field(name="Wins / Losses", value=f"{num(st['wins'])} / {num(st['losses'])}")
    e.add_field(name="Matches", value=num(st["matches"]))
    e.add_field(name="Damage done", value=num(st["damage"]))
    if st.get("matchpoints") is not None:
        e.add_field(name="Total matchpoints", value=num(st["matchpoints"]))
    top = leaderboard_entry(st["steam_id"])  # only if the Steam leaderboard is already loaded
    if top:
        e.add_field(name="Steam rank", value=f"#{top['rank']:,} of {top['total']:,}" if top.get("total") else f"#{top['rank']:,}")
    if summary.get("avatar"):
        e.set_thumbnail(url=summary["avatar"])
    footer = "Lifetime stats from Steam | updated every 10 minutes"
    if st.get("incomplete"):
        footer = "Steam's data for this player looks incomplete, so kills, losses, K/D and win rate are hidden. | " + footer
    e.set_footer(text=footer)
    return e


def local_stats_embed(s: dict) -> discord.Embed:
    e = discord.Embed(title=f"{s['name']} - Zero Hour", url=steam_url(s.get("player_key")), color=COLOR)
    e.add_field(name="K/D", value=f"{s['kd']}")
    e.add_field(name="Wins / Losses", value=f"{s['wins']} / {s['losses']}")
    e.add_field(name="Win rate", value=f"{s['win_rate']}%")
    e.add_field(name="Matches", value=f"{s['matches']}")
    e.add_field(name="Total matchpoints", value=f"{s['total_matchpoints']:,}")
    e.add_field(name="Avg matchpoints", value=f"{s['avg_matchpoints']}")
    e.set_footer(text="From matches tracked by this bot")
    return e


@client.tree.command(name="stats", description="Show a player's overall Zero Hour stats")
@app_commands.describe(
    player="Player name (or part of it), Steam ID, or Steam profile link. Leave empty to use your linked account",
    user="Or pick a Discord user who has linked their Steam account",
)
async def stats(interaction: discord.Interaction, player: Optional[str] = None, user: Optional[discord.User] = None):
    await interaction.response.defer(ephemeral=private(interaction))
    player, problem = await resolve_target(interaction, player, user)
    if problem:
        await respond(interaction, problem)
        return

    # Names the bot has tracked (full or partial). A Steam ID or profile link skips this search.
    matches = [] if steam_stats.is_direct(player) else await asyncio.to_thread(find_players, player)
    if len(matches) > 1:
        shown = matches[:10]
        lines = [f"{link(m['name'], steam_url(m['player_key']))} - `{m['player_key']}`" for m in shown]
        title = f"{len(shown)}{'+' if len(matches) > 10 else ''} players match \"{player}\""
        e = discord.Embed(
            title=title,
            description="\n".join(lines) + "\n\nRun /stats again with the full name or the Steam ID.",
            color=COLOR,
        )
        await respond(interaction, embed=e)
        return

    local = await asyncio.to_thread(player_stats, matches[0]["name"]) if matches else None
    try:
        steam_id = await asyncio.to_thread(
            find_steam_id, local["name"] if local else player, local.get("player_key") if local else None
        )
        st = await asyncio.to_thread(get_user_stats, steam_id)
        summary = await asyncio.to_thread(get_summary, steam_id)
    except SteamStatsError as err:
        print(f"[stats] {type(err).__name__}: {err}")  # shows in the host's console
        if local:  # Steam has nothing, but the bot has tracked this player's matches
            await respond(interaction, embed=local_stats_embed(local))
        elif isinstance(err, steam_stats.PrivateProfile):
            await respond(interaction, f"I found **{player}**, but their Steam game details are private, so their stats can't be read.")
        elif isinstance(err, (steam_stats.NotConfigured, steam_stats.BadKey)):
            await respond(interaction, "Steam stats aren't available right now. Please tell the bot's owner.")
        else:
            await respond(interaction, str(err) or not_found(player))
        return
    await respond(interaction, embed=steam_stats_embed(st, summary, local["name"] if local else player))


@client.tree.command(name="link", description="Link your Discord account to your Steam account")
@app_commands.describe(player="Your Steam ID, Steam profile link, or Steam name (names work for the top 200 only)")
async def link_command(interaction: discord.Interaction, player: str):
    await interaction.response.defer(ephemeral=private(interaction))
    try:
        steam_id = await asyncio.to_thread(find_steam_id, player)
    except SteamStatsError as err:
        await respond(interaction, str(err) or "I couldn't find that Steam account. Try your Steam ID or profile link.")
        return
    summary = await asyncio.to_thread(get_summary, steam_id)
    await asyncio.to_thread(set_link, interaction.user.id, steam_id)
    name = summary.get("name") or steam_id
    url = summary.get("url") or f"https://steamcommunity.com/profiles/{steam_id}"
    e = discord.Embed(
        title=f"Linked to {name}",
        url=url,
        description="Now /stats and /rank with no name will show this account, and other people can "
                    "pick you with the user option. Use /unlink to remove the link.",
        color=COLOR,
    )
    if summary.get("avatar"):
        e.set_thumbnail(url=summary["avatar"])
    e.set_footer(text=f"Steam ID {steam_id}")
    await respond(interaction, embed=e)


@client.tree.command(name="unlink", description="Remove the link between your Discord and Steam accounts")
async def unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=private(interaction))
    removed = await asyncio.to_thread(remove_link, interaction.user.id)
    await respond(interaction, "Your Steam account has been unlinked." if removed else "You didn't have a Steam account linked.")


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


@client.tree.command(name="rank", description="Check a player's Steam rank and total matchpoints")
@app_commands.describe(
    player="Steam ID or profile link (any player), or a Steam name (top 200 only). Leave empty for your linked account",
    user="Or pick a Discord user who has linked their Steam account",
)
async def rank(interaction: discord.Interaction, player: Optional[str] = None, user: Optional[discord.User] = None):
    await interaction.response.defer(ephemeral=private(interaction))
    player, problem = await resolve_target(interaction, player, user)
    if problem:
        await respond(interaction, problem)
        return
    q = player.strip()
    steam_id = None
    top_entry = None  # the top-200 page entry, which carries a name and picture

    if is_direct(q):
        try:
            steam_id = await asyncio.to_thread(find_steam_id, q)
        except SteamStatsError as ex:
            await respond(interaction, str(ex))
            return
    else:
        try:  # names can only be searched on the top 200, because the full list has no names
            entries, _ = await asyncio.to_thread(get_leaderboard)
        except Exception:
            entries = []
        matches = search_steam(entries, q)
        if len(matches) > 1:
            lines = [f"`#{m['rank']}` {link(m['name'], m['url'])} - {m['score']:,} pts" for m in matches[:5]]
            e = discord.Embed(
                title=f"{len(matches)} players match \"{q}\"",
                description="\n".join(lines) + "\n\nTry the full name, a Steam ID, or a profile link to narrow it down.",
                color=COLOR,
            )
            await respond(interaction, embed=e)
            return
        if matches:
            top_entry = matches[0]
            ident = top_entry["profile"]
            try:
                steam_id = ident if ident.isdigit() else await asyncio.to_thread(find_steam_id, f"https://steamcommunity.com/id/{ident}")
            except SteamStatsError:
                steam_id = None
        else:
            try:  # last try: a custom profile name
                steam_id = await asyncio.to_thread(find_steam_id, q)
            except SteamStatsError:
                await respond(interaction,
                    f"I couldn't find **{q}**. Names can only be searched on the Steam top 200. "
                    "For anyone else, use their Steam ID or profile link.")
                return

    try:
        hit = await asyncio.to_thread(lookup_rank, steam_id)
    except Exception:
        await respond(interaction, "I couldn't reach the Steam leaderboard right now. Please try again in a few minutes.")
        return

    summary = {}
    if top_entry:
        summary = {"name": top_entry["name"], "url": top_entry["url"], "avatar": top_entry.get("avatar")}
    else:
        summary = await asyncio.to_thread(get_summary, steam_id)
    name = summary.get("name") or steam_id
    url = summary.get("url") or f"https://steamcommunity.com/profiles/{steam_id}"

    if not hit:
        await respond(interaction, f"**{name}** isn't on the Steam MP Points leaderboard (no matchpoints recorded yet).")
        return

    e = discord.Embed(title=name, url=url, color=COLOR)
    e.add_field(name="Steam rank", value=f"#{hit['rank']:,} of {hit['total']:,}")
    e.add_field(name="Total matchpoints", value=f"{hit['score']:,}")
    if hit["total"]:
        e.add_field(name="Top", value=f"{max(0.01, hit['rank'] / hit['total'] * 100):.2f}%")
    if summary.get("avatar"):
        e.set_thumbnail(url=summary["avatar"])
    e.set_footer(text="Steam MP Points leaderboard | refreshed hourly")
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
