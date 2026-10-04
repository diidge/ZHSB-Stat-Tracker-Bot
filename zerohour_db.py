"""
Zero Hour stat database and calculations (SQLite only, no web packages needed).

The Discord bot imports this file directly. zerohour_tracker.py (the web API) uses it too.
"""
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

DB_PATH = "zerohour_stats.db"

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_key  TEXT UNIQUE NOT NULL,   -- stable ID from the game (Steam ID / user ID)
    name        TEXT NOT NULL,          -- latest display name
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key   TEXT UNIQUE NOT NULL,   -- session/room ID, used to de-duplicate
    map         TEXT,
    mode        TEXT,
    ended_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS match_players (
    match_id    INTEGER NOT NULL REFERENCES matches(id),
    player_id   INTEGER NOT NULL REFERENCES players(id),
    team        TEXT,
    kills       INTEGER NOT NULL DEFAULT 0,
    deaths      INTEGER NOT NULL DEFAULT 0,
    assists     INTEGER NOT NULL DEFAULT 0,
    matchpoints INTEGER NOT NULL DEFAULT 0,
    won         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (match_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_mp_player ON match_players(player_id);
CREATE INDEX IF NOT EXISTS idx_players_name ON players(name COLLATE NOCASE);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(SCHEMA)


# --------------------------------------------------------------------------
# Map names
# --------------------------------------------------------------------------
# Numbering taken from the order of the map list on zhsb.info. Not yet confirmed
# against real match data from the client: if a name looks wrong, fix it here.
# The database keeps whatever the client sent, and names are applied when stats
# are shown, so correcting this table also corrects old matches.
MAP_NAMES = {
    0: "Residential House", 1: "Terror House", 2: "Hotel Trouble",
    3: "Breaking Meth", 4: "Embassy Raid", 5: "Bank Heist",
    6: "Military Airport", 7: "Cafe Fourteen", 8: "Abandoned Hospital",
    9: "M.V. Meghna", 10: "Red Wedding", 11: "Rat's Den",
    12: "Prison Break", 13: "Khan Manzil", 14: "Show Time",
    15: "Paradise City", 16: "Storm Harbor", 17: "Critical Response",
    18: "Cocktail Crisis", 997: "Breaking Meth (Night)", 998: "Training Ground",
}


def map_name(raw) -> str:
    """Turn a stored map value (number or text) into a readable name."""
    if raw is None or str(raw).strip() == "":
        return "Unknown"
    s = str(raw).strip()
    if s.isdigit():
        return MAP_NAMES.get(int(s), f"Map {s}")  # unknown numbers shown as "Map 42"
    return s  # already a name


def save_match(m) -> bool:
    """Store a match. Returns False if it was already recorded."""
    now = time.time()
    with db() as c:
        if c.execute("SELECT 1 FROM matches WHERE match_key=?", (m.match_key,)).fetchone():
            return False
        cur = c.execute(
            "INSERT INTO matches (match_key, map, mode, ended_at) VALUES (?,?,?,?)",
            (m.match_key, None if m.map is None else str(m.map), m.mode, m.ended_at or now),
        )
        match_id = cur.lastrowid
        for p in m.players:
            c.execute(
                """INSERT INTO players (player_key, name, first_seen, last_seen)
                   VALUES (?,?,?,?)
                   ON CONFLICT(player_key) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen""",
                (p.player_key, p.name, now, now),
            )
            pid = c.execute("SELECT id FROM players WHERE player_key=?", (p.player_key,)).fetchone()["id"]
            c.execute(
                """INSERT INTO match_players
                   (match_id, player_id, team, kills, deaths, assists, matchpoints, won)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (match_id, pid, p.team, p.kills, p.deaths, p.assists, p.matchpoints, int(p.won)),
            )
    return True


# --------------------------------------------------------------------------
# Stat calculations
# --------------------------------------------------------------------------
AGG = """
    COUNT(*)                         AS matches,
    SUM(mp.won)                      AS wins,
    COUNT(*) - SUM(mp.won)           AS losses,
    SUM(mp.kills)                    AS kills,
    SUM(mp.deaths)                   AS deaths,
    SUM(mp.assists)                  AS assists,
    SUM(mp.matchpoints)              AS total_matchpoints
"""


def _finish(row: sqlite3.Row) -> dict:
    d = dict(row)
    deaths = d["deaths"] or 0
    d["kd"] = round((d["kills"] or 0) / deaths, 2) if deaths else float(d["kills"] or 0)
    d["win_rate"] = round(100 * (d["wins"] or 0) / d["matches"], 1) if d["matches"] else 0.0
    d["avg_matchpoints"] = round((d["total_matchpoints"] or 0) / d["matches"], 1) if d["matches"] else 0.0
    return d


def _map_stats(c, player_id: int) -> list:
    """Per-map breakdown for one player, most-played map first."""
    rows = c.execute(
        f"""SELECT m.map AS raw_map, {AGG}
            FROM match_players mp JOIN matches m ON m.id = mp.match_id
            WHERE mp.player_id = ?
            GROUP BY m.map""",
        (player_id,),
    ).fetchall()
    # Convert numbers to names, merging rows that end up with the same name
    # (e.g. one match stored as "5" and another as "Bank Heist").
    merged = {}
    for r in rows:
        d = dict(r)
        name = map_name(d.pop("raw_map"))
        if name in merged:
            for k, v in d.items():
                merged[name][k] = (merged[name][k] or 0) + (v or 0)
        else:
            merged[name] = {"map": name, **d}
    result = [_finish(d) for d in merged.values()]
    result.sort(key=lambda d: d["matches"], reverse=True)
    return result


def player_stats(name: str) -> Optional[dict]:
    with db() as c:
        p = c.execute("SELECT id, name, player_key, last_seen FROM players WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        if not p:
            return None
        row = c.execute(f"SELECT {AGG} FROM match_players mp WHERE mp.player_id=?", (p["id"],)).fetchone()
        recent = c.execute(
            """SELECT m.map, m.mode, m.ended_at, mp.kills, mp.deaths, mp.assists, mp.matchpoints, mp.won
               FROM match_players mp JOIN matches m ON m.id = mp.match_id
               WHERE mp.player_id=? ORDER BY m.ended_at DESC LIMIT 10""",
            (p["id"],),
        ).fetchall()
        maps = _map_stats(c, p["id"])
    out = _finish(row)
    out.update(name=p["name"], player_key=p["player_key"], last_seen=p["last_seen"], recent_matches=[{**dict(r), "map": map_name(r["map"])} for r in recent], maps=maps)
    return out


def find_players(query: str, limit: int = 11) -> list:
    """Players whose name matches. An exact name (any capitals) wins; otherwise any name containing the text."""
    q = query.strip()
    if not q:
        return []
    cols = "SELECT name, player_key, last_seen FROM players"
    with db() as c:
        rows = c.execute(f"{cols} WHERE name = ? COLLATE NOCASE ORDER BY last_seen DESC LIMIT ?", (q, limit)).fetchall()
        if not rows:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = c.execute(
                f"{cols} WHERE name LIKE ? ESCAPE '\\' ORDER BY last_seen DESC LIMIT ?", (f"%{escaped}%", limit)
            ).fetchall()
    return [dict(r) for r in rows]


def leaderboard(sort: str = "kd", min_matches: int = 5, limit: int = 50) -> list:
    rows = []
    with db() as c:
        for r in c.execute(
            f"SELECT p.name, p.player_key, {AGG} FROM match_players mp JOIN players p ON p.id = mp.player_id GROUP BY p.id"
        ):
            if r["matches"] >= min_matches:
                rows.append(_finish(r))
    keys = {"kd", "wins", "win_rate", "total_matchpoints", "kills", "matches"}
    rows.sort(key=lambda d: d[sort if sort in keys else "kd"], reverse=True)
    return rows[:limit]


init_db()  # make sure the tables exist (safe to run every time)
