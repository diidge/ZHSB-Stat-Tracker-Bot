"""
Reads the public Steam "MP Points" leaderboard for Zero Hour.

Two sources are used:
  * The XML version of the leaderboard lists EVERY player (rank, Steam ID, matchpoints),
    about 5,000 per page. It has no names, so it is used for rank lookups by Steam ID.
  * The normal web page shows names and pictures, but only for the top 200.

Test it on its own (prints the top 5, then the size of the full leaderboard):
    python steam_leaderboard.py
    python steam_leaderboard.py 76561198046397667     (also looks up that player's rank)

Design notes:
  * Leaderboards are only fetched when someone asks for them, then cached for an hour.
    The full XML leaderboard is about 20 pages (roughly 15 MB), so it is fetched at most
    once an hour, not per command.
  * If Steam is unreachable, the last good copy is used instead.
"""
import html
import json
import re
import threading
import time
import urllib.request

APP_ID = 1359090
BOARD_ID = 5239837
BASE = f"https://steamcommunity.com/stats/{APP_ID}/leaderboards/{BOARD_ID}"
CACHE_SECONDS = 3600      # how long to reuse the leaderboard before fetching again
PAUSE_BETWEEN_PAGES = 0.5  # seconds, to stay polite to Steam

# Matches one leaderboard row: "#<rank>", the player's profile link and name, then the score.
ENTRY_RE = re.compile(
    r"#\s*(\d+)\s*<"
    r".*?href=\"https?://steamcommunity\.com/(profiles|id)/([^/\"?]+)[^\"]*\"[^>]*>"
    r"\s*([^<]+?)\s*</a>"
    r".*?>\s*([\d,]+)\s*<",
    re.S,
)

# Matches the small avatar picture inside each player's profile link.
AVATAR_RE = re.compile(
    r"<a[^>]+href=\"https?://steamcommunity\.com/(?:profiles|id)/([^/\"?]+)/?\"[^>]*>\s*"
    r"<img[^>]*?src=[\"']([^\"']+)[\"']",
    re.S,
)


def _bigger_avatar(url: str) -> str:
    """Steam serves avatars in three sizes; the page uses the smallest, so ask for the large one."""
    return re.sub(r"(_medium|_full)?\.jpg$", "_full.jpg", url)


_lock = threading.Lock()
_cache = {"at": 0.0, "entries": []}


def _get(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ZeroHourStatTracker/1.0 (community stats bot)", "Accept-Language": "en"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def _parse_page(page: str) -> dict:
    """Read one leaderboard page (any of Steam's Zero Hour leaderboards). Returns {rank: entry}."""
    avatars = {m.group(1): _bigger_avatar(m.group(2)) for m in AVATAR_RE.finditer(page)}
    found = {}
    for m in ENTRY_RE.finditer(page):
        rank = int(m.group(1))
        kind, ident = m.group(2), m.group(3)
        found[rank] = {
            "rank": rank,
            "name": html.unescape(m.group(4)).strip(),
            "score": int(m.group(5).replace(",", "")),
            "profile": ident,  # Steam ID number, or the custom profile name
            "url": f"https://steamcommunity.com/{kind}/{ident}",
            "avatar": avatars.get(ident),  # may be None if the page layout differs
        }
    return found


def fetch_all() -> list:
    """Download every page of the leaderboard and return a list sorted by rank."""
    found = {}
    for start in range(1, 201, 15):  # 15 entries per page: 1, 16, 31, ... 196
        page = _get(BASE if start == 1 else f"{BASE}?sr={start}")
        found.update(_parse_page(page))
        time.sleep(PAUSE_BETWEEN_PAGES)
    entries = [found[r] for r in sorted(found)]
    if len(entries) < 10:
        raise RuntimeError("Could not read the Steam leaderboard page (layout may have changed).")
    return entries


def get_leaderboard(force: bool = False):
    """Returns (entries, fetched_at). Uses the cache unless it is old or force=True."""
    with _lock:
        fresh = time.time() - _cache["at"] < CACHE_SECONDS
        if _cache["entries"] and fresh and not force:
            return _cache["entries"], _cache["at"]
        try:
            entries = fetch_all()
        except Exception:
            if _cache["entries"]:  # Steam is down: fall back to the last good copy
                return _cache["entries"], _cache["at"]
            raise
        _cache.update(at=time.time(), entries=entries)
        return entries, _cache["at"]


# ---------------------------------------------------------------------------
# Full leaderboard from Steam's XML pages (every player, but Steam IDs only - no names).
# ---------------------------------------------------------------------------
XML_PAGE_SIZE = 5000          # Steam returns at most this many entries per XML page
XML_MAX_PAGES = 60            # safety stop (about 300,000 players)
XML_CACHE_SECONDS = 3600
XML_ENTRY_RE = re.compile(r"<entry><steamid>(\d+)</steamid><score>(-?\d+)</score><rank>(\d+)</rank>")
XML_TOTAL_RE = re.compile(r"<totalLeaderboardEntries>(\d+)</totalLeaderboardEntries>")

_index_lock = threading.Lock()
_index = {"at": 0.0, "ranks": {}, "total": 0}   # ranks: {steam_id (int): (rank, score)}


def _parse_xml_page(page: str):
    """Reads one XML page. Returns (total_players, [(steam_id, score, rank), ...])."""
    m = XML_TOTAL_RE.search(page)
    total = int(m.group(1)) if m else 0
    rows = [(int(a), int(b), int(c)) for a, b, c in XML_ENTRY_RE.findall(page)]
    return total, rows


def fetch_index():
    """Downloads every XML page. Returns ({steam_id: (rank, score)}, total_players)."""
    ranks, total, start = {}, 0, 1
    for _ in range(XML_MAX_PAGES):
        page = _get(f"{BASE}/?xml=1&start={start}")
        page_total, rows = _parse_xml_page(page)
        total = page_total or total
        if not rows:
            break
        for steam_id, score, rank in rows:
            ranks[steam_id] = (rank, score)
        start += len(rows)
        if total and start > total:
            break
        time.sleep(PAUSE_BETWEEN_PAGES)
    if len(ranks) < 100:
        raise RuntimeError("Could not read the Steam leaderboard XML (format may have changed).")
    return ranks, max(total, len(ranks))


def get_index(force: bool = False):
    """Returns (ranks_by_steam_id, total_players, fetched_at). Cached; stale copy used if Steam is down."""
    with _index_lock:
        fresh = time.time() - _index["at"] < XML_CACHE_SECONDS
        if _index["ranks"] and fresh and not force:
            return _index["ranks"], _index["total"], _index["at"]
        try:
            ranks, total = fetch_index()
        except Exception:
            if _index["ranks"]:
                return _index["ranks"], _index["total"], _index["at"]
            raise
        _index.update(at=time.time(), ranks=ranks, total=total)
        return ranks, total, _index["at"]


def lookup_rank(steam_id):
    """Rank and matchpoints for a 17-digit Steam ID: {'rank', 'score', 'total'} or None if unranked.
    Uses the cached leaderboard; fetches it if it has not been loaded yet."""
    ranks, total, _ = get_index()
    hit = ranks.get(int(steam_id))
    return {"rank": hit[0], "score": hit[1], "total": total} if hit else None


def cached_rank(steam_id):
    """Same as lookup_rank but never downloads anything: None if the leaderboard isn't loaded yet."""
    try:
        hit = _index["ranks"].get(int(steam_id))
    except (TypeError, ValueError):
        return None
    return {"rank": hit[0], "score": hit[1], "total": _index["total"]} if hit else None


# The "Training Best Scores" leaderboard (the number at the end of its Steam link).
# Lower scores rank higher on this one: #1 has the lowest number.
TRAINING_BOARD_ID = 4951891

TOP_CACHE_SECONDS = 600  # top-10 lists are reused for 10 minutes
_top_lock = threading.Lock()
_top_cache = {}


def get_top(board_id: int, count: int = 10):
    """Top entries of any Steam leaderboard (reads only its first page). Returns (entries, fetched_at)."""
    with _top_lock:
        cached = _top_cache.get(board_id)
        if cached and time.time() - cached["at"] < TOP_CACHE_SECONDS:
            return cached["entries"][:count], cached["at"]
        try:
            page = _get(f"https://steamcommunity.com/stats/{APP_ID}/leaderboards/{board_id}")
            found = _parse_page(page)
            entries = [found[r] for r in sorted(found)]
            if not entries:
                raise RuntimeError("Could not read that Steam leaderboard page.")
        except Exception:
            if cached:  # Steam is down: fall back to the last good copy
                return cached["entries"][:count], cached["at"]
            raise
        _top_cache[board_id] = {"at": time.time(), "entries": entries}
        return entries[:count], _top_cache[board_id]["at"]


# Steam's own public "players right now" number (no key needed). SteamDB shows the same figure.
STEAM_PLAYERS_URL = f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={APP_ID}"
PLAYERS_CACHE_SECONDS = 60
_players_lock = threading.Lock()
_players_cache = {"at": 0.0, "count": None}


def get_current_players():
    """Returns (players_on_steam_now, fetched_at). Cached for a minute."""
    with _players_lock:
        if _players_cache["count"] is not None and time.time() - _players_cache["at"] < PLAYERS_CACHE_SECONDS:
            return _players_cache["count"], _players_cache["at"]
        data = json.loads(_get(STEAM_PLAYERS_URL))
        count = int(data["response"]["player_count"])
        _players_cache.update(at=time.time(), count=count)
        return count, _players_cache["at"]


def search(entries: list, query: str) -> list:
    """Find players by Steam name, Steam ID, or profile link. Exact matches win over partial ones."""
    q = query.strip().lower()
    link = re.search(r"steamcommunity\.com/(?:profiles|id)/([^/?\s]+)", q)
    if link:
        q = link.group(1)
    if not q:
        return []
    exact = [e for e in entries if e["name"].lower() == q or e["profile"].lower() == q]
    if exact:
        return exact
    return [e for e in entries if q in e["name"].lower()]


if __name__ == "__main__":
    import sys
    rows, _ = get_leaderboard(force=True)
    print(f"Read {len(rows)} top entries. Top 5:")
    for e in rows[:5]:
        print(f"  #{e['rank']}  {e['name']}  -  {e['score']:,}  ({e['url']})")
    ranks, total, _ = get_index(force=True)
    print(f"Full leaderboard: {len(ranks):,} players read (Steam says {total:,}).")
    if len(sys.argv) > 1:
        print("Lookup:", lookup_rank(sys.argv[1]) or "not on the leaderboard")
