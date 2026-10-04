"""
Reads a player's lifetime Zero Hour stats (kills, deaths, matches won/lost, damage) from Steam.

Needs a Steam Web API key in a setting named STEAM_API_KEY (an environment variable on the host,
or a line STEAM_API_KEY=your-key in the .env file next to start.py). Never put the key in this file.

Test it on its own (prints one player's stats):
    python steam_stats.py 76561199388321185

Notes:
  * Steam only returns stats for players whose "Game details" privacy setting is Public.
  * Results are reused for 10 minutes.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import steam_leaderboard

APP_ID = steam_leaderboard.APP_ID
CACHE_SECONDS = 600

# The Steam stat that holds a player's total matchpoints. Checked against the in-game profile:
# a player showing 15284 matchpoints in game has zh_MP7 = 15284.
MATCHPOINT_STAT = "zh_MP7"


class SteamStatsError(Exception):
    """Base class. str(error) is a message that is fine to show to a Discord user."""


class NotConfigured(SteamStatsError):
    pass


class BadKey(SteamStatsError):
    pass


class PlayerNotFound(SteamStatsError):
    pass


class PrivateProfile(SteamStatsError):
    pass


def _read_env_file(path: str) -> dict:
    """Reads KEY=VALUE lines from a .env file. Returns {} if there isn't one."""
    values = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.replace("export ", "").strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _get_key() -> str:
    """The Steam API key: from the environment, or else straight from the .env file next to this file."""
    key = os.environ.get("STEAM_API_KEY", "").strip()
    if key:
        return key
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(here, ".env")
    found = _read_env_file(env_path)
    key = found.get("STEAM_API_KEY", "").strip()
    if not key:
        # Print only names, never values, to help find the problem.
        print(f"[steam_stats] No STEAM_API_KEY found. Looked for {env_path} "
              f"(exists: {os.path.exists(env_path)}). Setting names in it: {sorted(found)}")
    return key


def _api(path: str, **params) -> dict:
    key = _get_key()
    if not key:
        raise NotConfigured("Steam stats aren't set up yet (no Steam API key).")
    url = f"https://api.steampowered.com/{path}?" + urllib.parse.urlencode({**params, "key": key})
    req = urllib.request.Request(url, headers={"User-Agent": "ZeroHourStatTracker/1.0 (community stats bot)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BadKey("Steam rejected the bot's API key.")
        if e.code in (400, 500):
            raise PrivateProfile("No stats are available for that player.")
        raise SteamStatsError("I couldn't reach Steam right now. Please try again in a few minutes.")
    except Exception:
        raise SteamStatsError("I couldn't reach Steam right now. Please try again in a few minutes.")
    return json.loads(body) if body else {}


def _resolve_vanity(name: str) -> str:
    """Turns a custom profile name (steamcommunity.com/id/NAME) into the 17-digit Steam ID."""
    resp = _api("ISteamUser/ResolveVanityURL/v1/", vanityurl=name).get("response", {})
    if resp.get("success") == 1 and resp.get("steamid"):
        return str(resp["steamid"])
    raise PlayerNotFound("I couldn't find that player on Steam.")


def is_direct(query: str) -> bool:
    """True if the text is a Steam ID or a Steam profile link (so no name search is needed)."""
    q = query.strip()
    return bool(re.fullmatch(r"\d{17}", q) or re.search(r"steamcommunity\.com/(?:profiles|id)/", q))


def find_steam_id(query: str, local_key=None) -> str:
    """Works out a 17-digit Steam ID from what the user typed: an ID, a profile link, or a name."""
    q = query.strip()
    m = re.search(r"steamcommunity\.com/profiles/(\d{17})", q) or re.fullmatch(r"(\d{17})", q)
    if m:
        return m.group(1)
    m = re.search(r"steamcommunity\.com/id/([^/?\s]+)", q)
    if m:
        return _resolve_vanity(m.group(1))

    if local_key and re.fullmatch(r"7656119\d{10}", str(local_key)):  # the tracker already knows this player
        return str(local_key)

    try:  # look the name up on the Steam top 200
        entries, _ = steam_leaderboard.get_leaderboard()
    except Exception:
        entries = []
    hits = steam_leaderboard.search(entries, q)
    if len(hits) == 1:
        ident = hits[0]["profile"]
        return ident if re.fullmatch(r"\d{17}", ident) else _resolve_vanity(ident)
    if len(hits) > 1:
        raise PlayerNotFound(
            f"More than one top-200 player matches \"{q}\". Use their Steam ID or profile link instead."
        )

    try:  # last try: treat it as a custom profile name
        return _resolve_vanity(q)
    except PlayerNotFound:
        raise PlayerNotFound(
            f"I couldn't find \"{q}\". Steam can't search by name outside the top 200, "
            "so use the player's Steam ID or profile link."
        )


_lock = threading.Lock()
_cache = {}  # steam_id -> {"at": time, "data": {...}}


def get_user_stats(steam_id: str) -> dict:
    """Lifetime stats for one player. Raises PrivateProfile if Steam has nothing to show."""
    with _lock:
        cached = _cache.get(steam_id)
        if cached and time.time() - cached["at"] < CACHE_SECONDS:
            return cached["data"]

    data = _api("ISteamUserStats/GetUserStatsForGame/v2/", steamid=steam_id, appid=APP_ID)
    raw_stats = (data.get("playerstats") or {}).get("stats")
    if not raw_stats:
        raise PrivateProfile("No stats are available for that player.")
    values = {st["name"]: st.get("value") for st in raw_stats}

    def stat(name):
        """A stat's number, or None if Steam didn't send it (never guessed as 0)."""
        v = values.get(name)
        return v if isinstance(v, (int, float)) else None

    kills, deaths = stat("zh_Kills"), stat("zh_Deaths")
    wins, losses = stat("zh_MatchesWon"), stat("zh_MatchesLost")

    # Safeguard: Steam sometimes reports 0 kills for a player who has deaths. The game's own profile
    # screen showed real kills in that case, so Steam's copy was not up to date. Treat the kill count as
    # unknown, and don't trust the loss count, match count or win rate either.
    incomplete = kills == 0 and bool(deaths)
    if incomplete:
        kills = None
        losses = None  # the loss count was also behind in the one case seen, so it is not trusted either

    if kills is not None and deaths:
        kd = round(kills / deaths, 2)
    elif kills is not None and deaths == 0:
        kd = float(kills)
    else:
        kd = None

    matches = wins + losses if wins is not None and losses is not None else None
    win_rate = round(100 * wins / matches, 1) if matches and not incomplete else None

    result = {
        "steam_id": steam_id,
        "kills": kills,
        "deaths": deaths,
        "kd": kd,
        "wins": wins,
        "losses": losses,
        "matches": matches,
        "win_rate": win_rate,
        "damage": stat("zh_DamagesDone"),
        "matchpoints": stat(MATCHPOINT_STAT) if MATCHPOINT_STAT else None,
        "incomplete": incomplete,
        "raw": values,
    }
    with _lock:
        _cache[steam_id] = {"at": time.time(), "data": result}
    return result


def get_summary(steam_id: str) -> dict:
    """Steam name, profile link and avatar. Returns {} if unavailable (not treated as an error)."""
    try:
        players = _api("ISteamUser/GetPlayerSummaries/v2/", steamids=steam_id).get("response", {}).get("players", [])
    except SteamStatsError:
        return {}
    if not players:
        return {}
    p = players[0]
    return {"name": p.get("personaname"), "url": p.get("profileurl"), "avatar": p.get("avatarfull")}


def leaderboard_entry(steam_id: str):
    """The player's Steam top-200 entry, if the leaderboard is already loaded (never fetches it)."""
    for e in steam_leaderboard._cache.get("entries", []):
        if e["profile"] == steam_id:
            return e
    return None


if __name__ == "__main__":
    sid = find_steam_id(sys.argv[1] if len(sys.argv) > 1 else input("Steam ID, link or name: "))
    s = get_user_stats(sid)
    s.pop("raw")
    print(sid, json.dumps(s, indent=1))
    print(get_summary(sid))
