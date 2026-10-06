"""
Checks one uploaded match and turns it into the shape zerohour_db.save_match expects.
Kept separate from the web endpoint so it can also be used by reingest.py (no extra libraries needed).
"""
import re
import time
from types import SimpleNamespace

KEY_OK = re.compile(r"^(steam:|epic:)?[A-Za-z0-9]{8,40}$")
STEAM64 = re.compile(r"^(steam:)?(7656\d{13})$")


def _int(v, lo, hi, name):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{name} must be a whole number between {lo} and {hi}")
    return v


def parse_match(data: dict) -> SimpleNamespace:
    """Checks one match and returns it in the shape zerohour_db.save_match expects. Raises ValueError if it looks wrong."""
    if not isinstance(data, dict):
        raise ValueError("the upload must be a JSON object")
    key = data.get("match_key")
    if not isinstance(key, str) or not 6 <= len(key) <= 80:
        raise ValueError("match_key must be a text of 6-80 characters")
    players_in = data.get("players")
    if not isinstance(players_in, list) or not 1 <= len(players_in) <= 64:
        raise ValueError("a match needs between 1 and 64 players")
    mode = data.get("mode")
    if mode is not None and (not isinstance(mode, str) or len(mode) > 40):
        raise ValueError("mode must be short text")
    map_ = data.get("map")
    if map_ is not None and (isinstance(map_, bool) or not isinstance(map_, (int, str)) or len(str(map_)) > 60):
        raise ValueError("map must be a number or short text")
    ended = data.get("ended_at")
    if ended is not None:
        if isinstance(ended, bool) or not isinstance(ended, (int, float)) or not 1.5e9 < ended < time.time() + 86400:
            raise ValueError("ended_at must be a normal timestamp")

    players = []
    for p in players_in:
        if not isinstance(p, dict):
            raise ValueError("each player must be an object")
        pk = p.get("player_key")
        if not isinstance(pk, str) or not KEY_OK.match(pk):
            raise ValueError("player_key must be a Steam ID or an 'epic:...' ID")
        m = STEAM64.match(pk)
        if m:
            pk = m.group(2)                      # Steam IDs are stored as the plain 17 digits, as the bot expects
        name = p.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 80:
            raise ValueError("name must be text of 1-80 characters")
        team = p.get("team")
        if team is not None and (not isinstance(team, str) or len(team) > 20):
            raise ValueError("team must be short text")
        players.append(SimpleNamespace(
            player_key=pk, name=name, team=team,
            kills=_int(p.get("kills", 0), -100, 500, "kills"),      # kills can go below zero after team kills
            deaths=_int(p.get("deaths", 0), 0, 500, "deaths"),
            assists=_int(p.get("assists", 0), 0, 500, "assists"),
            matchpoints=_int(p.get("matchpoints", 0), 0, 100000, "matchpoints"),
            won=bool(p.get("won", False)),
        ))
    return SimpleNamespace(match_key=key, map=map_, mode=mode, ended_at=ended, players=players)


