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
from typing import List, Optional, Union

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# The database and stat code lives in zerohour_db.py (so the Discord bot can use it without FastAPI).
from zerohour_db import (  # noqa: F401
    DB_PATH, MAP_NAMES, init_db, leaderboard, map_name, player_stats, save_match,
)

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
