"""
Zero Hour stat tracker backend (FastAPI + SQLite).

Run:
    pip install fastapi uvicorn
    uvicorn zerohour_tracker:app --reload

Pipeline:
    your Photon client  ->  on_match_finished(result)  ->  SQLite  ->  /api + web UI

The Photon/packet side is intentionally NOT in this file. Your existing client
decodes the game's traffic; you only need to hand finished matches to
`on_match_finished()` (in-process) or POST them to /api/ingest (separate process).
"""
import sqlite3
import time
from contextlib import contextmanager
from typing import List, Optional, Union

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

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


# --------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------
class PlayerResult(BaseModel):
    player_key: str
    name: str
    team: Optional[str] = None
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    matchpoints: int = 0
    won: bool = False


class MatchResult(BaseModel):
    match_key: str                 # unique per match; re-sending the same key is ignored
    map: Optional[Union[int, str]] = None   # a map number (e.g. 5) or a name
    mode: Optional[str] = None
    ended_at: Optional[float] = None
    players: List[PlayerResult]


def save_match(m: MatchResult) -> bool:
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
    out.update(name=p["name"], last_seen=p["last_seen"], recent_matches=[{**dict(r), "map": map_name(r["map"])} for r in recent], maps=maps)
    return out


def leaderboard(sort: str = "kd", min_matches: int = 5, limit: int = 50) -> list:
    rows = []
    with db() as c:
        for r in c.execute(
            f"SELECT p.name, {AGG} FROM match_players mp JOIN players p ON p.id = mp.player_id GROUP BY p.id"
        ):
            if r["matches"] >= min_matches:
                rows.append(_finish(r))
    keys = {"kd", "wins", "win_rate", "total_matchpoints", "kills", "matches"}
    rows.sort(key=lambda d: d[sort if sort in keys else "kd"], reverse=True)
    return rows[:limit]


# --------------------------------------------------------------------------
# Hook for your Photon client
# --------------------------------------------------------------------------
def on_match_finished(result: dict) -> bool:
    """
    Call this from your Photon client when a match ends. Map whatever your client
    decodes (player IDs, kills, deaths, score, winning team...) into this shape:

        {
          "match_key": "<room/session id>",
          "map": "...", "mode": "...",
          "players": [
            {"player_key": "...", "name": "...", "team": "A",
             "kills": 12, "deaths": 7, "assists": 3, "matchpoints": 2450, "won": true},
            ...
          ]
        }
    """
    return save_match(MatchResult(**result))


# --------------------------------------------------------------------------
# API + minimal web UI
# --------------------------------------------------------------------------
app = FastAPI(title="Zero Hour Stat Tracker")
init_db()


@app.post("/api/ingest")
def ingest(match: MatchResult):
    return {"stored": save_match(match)}


@app.get("/api/players/{name}")
def api_player(name: str):
    s = player_stats(name)
    if not s:
        raise HTTPException(404, "Player not found")
    return s


@app.get("/api/players/{name}/maps")
def api_player_maps(name: str):
    s = player_stats(name)
    if not s:
        raise HTTPException(404, "Player not found")
    return s["maps"]


@app.get("/api/leaderboard")
def api_leaderboard(sort: str = "kd", min_matches: int = 5, limit: int = 50):
    return leaderboard(sort, min_matches, limit)


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Zero Hour Stats</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:760px;margin:2rem auto;padding:0 1rem}
 input,button{padding:.6rem;font-size:1rem;border-radius:6px;border:1px solid #444;background:#222;color:#eee}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:.6rem;margin:1rem 0}
 .card{background:#1c1c1c;padding:.8rem;border-radius:8px}.card b{display:block;font-size:1.4rem}
 table{width:100%;border-collapse:collapse}td,th{padding:.4rem;text-align:left;border-bottom:1px solid #333}
</style>
<h1>Zero Hour Stats</h1>
<form onsubmit="go(event)"><input id=q placeholder="Player name"> <button>Search</button></form>
<div id=out></div><h2>Leaderboard</h2><table id=lb></table>
<script>
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function go(e){e.preventDefault();const r=await fetch('/api/players/'+encodeURIComponent(q.value));
 if(!r.ok){out.textContent='Player not found';return}const s=await r.json();
 const c=(l,v)=>`<div class=card>${l}<b>${v}</b></div>`;
 out.innerHTML=`<h2>${esc(s.name)}</h2><div class=grid>${c('K/D',s.kd)}${c('Wins',s.wins)}${c('Losses',s.losses)}
 ${c('Win rate',s.win_rate+'%')}${c('Matchpoints',s.total_matchpoints)}${c('Matches',s.matches)}</div>
 <h3>By map</h3><table><tr><th>Map<th>Matches<th>K/D<th>W-L<th>Win %<th>Points</tr>
 ${s.maps.map(m=>`<tr><td>${esc(m.map)}<td>${m.matches}<td>${m.kd}<td>${m.wins}-${m.losses}<td>${m.win_rate}%<td>${m.total_matchpoints}</tr>`).join('')}</table>`}
(async()=>{const d=await (await fetch('/api/leaderboard?min_matches=1')).json();
 lb.innerHTML='<tr><th>Player<th>K/D<th>W-L<th>Points</tr>'+d.map(p=>`<tr><td>${esc(p.name)}<td>${p.kd}<td>${p.wins}-${p.losses}<td>${p.total_matchpoints}</tr>`).join('')})();
</script>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
