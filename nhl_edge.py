#!/usr/bin/env python3
# Copyright (c) 2026 William Brooks Parker. All rights reserved. Proprietary — see LICENSE.
"""
nhl_edge.py — NHL player-prop outlier finder (2026-27 season).

Props covered: skater shots on goal, points, goals, assists, blocked shots, power-play
points; goalie saves. Model side is built from the NHL's public API (api-web.nhle.com,
keyless): every player's game log is stored in nhl.db and turned into a per-game rate
with shrinkage toward last season, scaled by the opponent's pace/allowance, then into a
Poisson probability of clearing the posted line. Market side is The Odds API
(ODDS_API_KEY in the environment or a gitignored .env) or a CSV of lines you type.

Same discipline as cfb_edge / soccer_edge: every flagged prop is paper-logged, settled
from the boxscore, and nothing is a real-money recommendation until analysis says so
(LIVE_STAKES is shared with cfb_edge). --calibrate walks forward through last season so
the projection is tested before the first puck drops.

Sections:
  ---- constants / models ----
  ---- NHL API adapters ----
  ---- projections ----
  ---- prop lines (Odds API / CSV) ----
  ---- signals ----
  ---- SQLite ----
  ---- calibration ----
  ---- rendering / report ----
  ---- main ----
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import requests

import cfb_edge as ce

NHL_DB = "nhl.db"
REPORTS_DIR = ce.REPORTS_DIR
LOCAL_TZ = ce.LOCAL_TZ
UA = ce.UA

NHL_WEB = "https://api-web.nhle.com/v1"
NHL_STATS = "https://api.nhle.com/stats/rest/en"
ODDS_API = "https://api.the-odds-api.com/v4"
ODDS_SPORT = "icehockey_nhl"

SEASON = 20262027                 # the season we are projecting
PRIOR_SEASON = 20252026           # the season the prior comes from
SEASON_START = dt.date(2026, 10, 7)

# ---- decision constants (NHL props). Every one is a prior until analysis/06 says otherwise. ----
MARKETS = {                       # Odds API market -> (stat column, who, label)
    "player_shots_on_goal": ("shots", "skater", "SOG"),
    "player_points": ("points", "skater", "PTS"),
    "player_goals": ("goals", "skater", "G"),
    "player_assists": ("assists", "skater", "A"),
    "player_blocked_shots": ("blocked", "skater", "BLK"),
    "player_power_play_points": ("pp_points", "skater", "PPP"),
    "player_total_saves": ("saves", "goalie", "SV"),
}
SHRINK_GAMES = 20.0               # games of prior-season weight a current-season rate has to overcome
MIN_GAMES = 10                    # fewer total games behind a rate -> ⚠thin, never staked
RECENT_GAMES = 10                 # rolling window that gets extra weight (form + role changes)
RECENT_WEIGHT = 0.35
OPP_FACTOR_CAP = (0.80, 1.20)     # opponent pace/allowance multiplier is clamped
HOME_FACTOR = 1.02                # skaters shoot a touch more at home
EDGE_PCT = 8.0                    # model P(side) over de-vigged fair, %: value
EDGE_STRONG_PCT = 15.0            # STRONG
EDGE_OVERREACH_PCT = 30.0         # >= this: strength 0. Prior from CFB + soccer: the biggest gaps lose most
SAVES_MAX_STRENGTH = 1            # goalie saves: --calibrate barely beats naive (log-loss 0.613 vs 0.616) -> cap at value
MAX_PRICE = 250                   # never stake a prop side longer than +250 or shorter than -250
MIN_PRICE = -250
FINDINGS_AS_OF = "2026-09-20"     # no NHL analysis run yet (season opens 2026-10-07) — all priors


# =====================================================================
# ---- models ----
# =====================================================================

@dataclass
class Player:
    id: int
    name: str
    team: str
    position: str                  # C L R D G
    rates: dict[str, float] = field(default_factory=dict)      # stat -> per-game mean (unadjusted)
    games: int = 0                 # games behind the rate (both seasons, weighted)
    starter: Optional[bool] = None # goalies: projected starter?


@dataclass
class GameCtx:
    id: int
    date: dt.date
    start_utc: dt.datetime
    home: str
    away: str
    state: str                     # FUT / PRE / LIVE / OFF / FINAL
    home_factor: dict[str, float] = field(default_factory=dict)   # stat -> opp multiplier for HOME players
    away_factor: dict[str, float] = field(default_factory=dict)

    @property
    def kick_local(self) -> dt.datetime:
        return self.start_utc.astimezone(LOCAL_TZ)

    @property
    def short(self) -> str:
        return f"{self.away} @ {self.home}"


@dataclass
class Prop:
    game: GameCtx
    player: Player
    market: str                    # Odds API key
    stat: str
    line: float
    over: Optional[int]
    under: Optional[int]
    book: str = "?"
    mean: Optional[float] = None   # projected λ
    p_over: Optional[float] = None


@dataclass
class Signal:
    prop: Prop
    kind: str                      # "prop"
    side: str                      # over / under
    label: str
    strength: int
    edge: float
    truth_p: Optional[float]
    price: Optional[int]
    note: str = ""

    @property
    def what(self) -> str:
        p = self.prop
        return f"{p.player.name} {self.side} {p.line:g} {MARKETS[p.market][2]}"


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam)."""
    if k < 0:
        return 0.0
    if lam <= 0:
        return 1.0
    term = math.exp(-lam)
    total = term
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def p_over(lam: float, line: float) -> tuple[float, float]:
    """(P(over), P(push)) for a prop line. Half lines never push."""
    k = math.floor(line)
    if abs(line - k) < 1e-9:           # whole number: push at exactly k
        push = poisson_cdf(k, lam) - poisson_cdf(k - 1, lam)
        over = 1.0 - poisson_cdf(k, lam)
        return over, push
    return 1.0 - poisson_cdf(k, lam), 0.0


def devig2(a: Optional[int], b: Optional[int]) -> tuple[Optional[float], Optional[float]]:
    return ce.devig_pair(a, b)


def load_key() -> Optional[str]:
    k = os.environ.get("ODDS_API_KEY")
    if k:
        return k.strip()
    for path in (".env", os.path.expanduser("~/.odds_api_key")):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ODDS_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
                    if path.endswith(".odds_api_key") and line and "=" not in line:
                        return line
    return None


# =====================================================================
# ---- NHL API adapters ----
# =====================================================================

def _get(url: str, params: Optional[dict] = None, timeout: int = 30) -> dict:
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_schedule(date: dt.date) -> list[GameCtx]:
    d = _get(f"{NHL_WEB}/schedule/{date.isoformat()}")
    out = []
    for day in d.get("gameWeek", []):
        if day.get("date") != date.isoformat():
            continue
        for g in day.get("games", []):
            if g.get("gameType") != 2:               # regular season only
                continue
            out.append(GameCtx(id=int(g["id"]), date=date,
                               start_utc=dt.datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00")),
                               home=g["homeTeam"]["abbrev"], away=g["awayTeam"]["abbrev"],
                               state=g.get("gameState", "FUT")))
    return out


def fetch_teams() -> list[str]:
    """The 32 active clubs (the stats /team list includes every defunct franchise)."""
    d = _get(f"{NHL_WEB}/standings/now")
    return sorted({t["teamAbbrev"]["default"] for t in d.get("standings", [])})


def fetch_roster(team: str, season: int = SEASON) -> list[Player]:
    d = _get(f"{NHL_WEB}/roster/{team}/{season}")
    out = []
    for grp in ("forwards", "defensemen", "goalies"):
        for p in d.get(grp, []):
            name = f"{p['firstName']['default']} {p['lastName']['default']}"
            out.append(Player(id=int(p["id"]), name=name, team=team, position=p.get("positionCode", "?")))
    return out


def fetch_game_log(player_id: int, season: int, game_type: int = 2) -> list[dict]:
    try:
        d = _get(f"{NHL_WEB}/player/{player_id}/game-log/{season}/{game_type}")
    except requests.HTTPError:
        return []
    return d.get("gameLog", [])


def fetch_team_summary(season: int) -> dict[str, dict]:
    """Per-team pace: shots for/against per game, goals for/against per game."""
    d = _get(f"{NHL_STATS}/team/summary", {"limit": -1, "cayenneExp": f"seasonId={season} and gameTypeId=2"})
    id2abbr = {t["id"]: t["triCode"] for t in _get(f"{NHL_STATS}/team").get("data", [])}
    out = {}
    for t in d.get("data", []):
        ab = id2abbr.get(t.get("teamId"))
        if not ab:
            continue
        out[ab] = {"gp": t.get("gamesPlayed", 0), "sf": t.get("shotsForPerGame"), "sa": t.get("shotsAgainstPerGame"),
                   "gf": t.get("goalsForPerGame"), "ga": t.get("goalsAgainstPerGame")}
    return out


def fetch_boxscore_stats(game_id: int) -> tuple[dict[int, dict], Optional[str]]:
    """(player_id -> actual stats, gameState) for a finished game. Power-play points are not
    in the boxscore; settle falls back to the player's game log for that stat."""
    d = _get(f"{NHL_WEB}/gamecenter/{game_id}/boxscore")
    out = {}
    for side in ("homeTeam", "awayTeam"):
        grp = d.get("playerByGameStats", {}).get(side, {})
        for p in grp.get("forwards", []) + grp.get("defense", []):
            out[int(p["playerId"])] = {"shots": p.get("sog", 0), "points": p.get("points", 0), "goals": p.get("goals", 0),
                                       "assists": p.get("assists", 0), "blocked": p.get("blockedShots", 0)}
        for g in grp.get("goalies", []):
            out[int(g["playerId"])] = {"saves": g.get("saves", 0), "shots_against": g.get("shotsAgainst", 0),
                                       "started": bool(g.get("starter"))}
    return out, d.get("gameState")


# =====================================================================
# ---- projections ----
# =====================================================================

def _log_rows(player: Player, season: int, logs: list[dict]) -> list[tuple]:
    rows = []
    for g in logs:
        if player.position == "G":
            rows.append((int(g["gameId"]), player.id, season, g["gameDate"], g.get("teamAbbrev"), g.get("opponentAbbrev"),
                         g.get("homeRoadFlag") == "H", None, None, None, None, None, None,
                         g.get("shotsAgainst", 0) - g.get("goalsAgainst", 0), g.get("shotsAgainst", 0),
                         int(g.get("gamesStarted", 0) or 0), _toi_min(g.get("toi"))))
        else:
            rows.append((int(g["gameId"]), player.id, season, g["gameDate"], g.get("teamAbbrev"), g.get("opponentAbbrev"),
                         g.get("homeRoadFlag") == "H", g.get("shots", 0), g.get("points", 0), g.get("goals", 0),
                         g.get("assists", 0), None, g.get("powerPlayPoints", 0), None, None, None, _toi_min(g.get("toi"))))
    return rows


def _toi_min(s: Optional[str]) -> Optional[float]:
    if not s or ":" not in s:
        return None
    m, sec = s.split(":")
    return int(m) + int(sec) / 60.0


def build_logs(conn: sqlite3.Connection, seasons: tuple[int, ...] = (PRIOR_SEASON, SEASON), workers: int = 12,
               log=print) -> int:
    """Rosters for every team this season -> game logs for both seasons -> nhl.db."""
    teams = fetch_teams()
    players: list[Player] = []
    for t in teams:
        try:
            players += fetch_roster(t)
        except requests.HTTPError:
            log(f"  roster {t}: not available yet")
    for p in players:
        conn.execute("INSERT OR REPLACE INTO players(id,name,team,position) VALUES (?,?,?,?)",
                     (p.id, p.name, p.team, p.position))
    conn.commit()
    log(f"  {len(players)} rostered players on {len(teams)} teams")

    def one(p: Player):
        rows = []
        for s in seasons:
            rows += _log_rows(p, s, fetch_game_log(p.id, s))
        return rows
    n = 0
    with ThreadPoolExecutor(workers) as ex:
        for rows in ex.map(one, players):
            conn.executemany("INSERT OR REPLACE INTO game_logs(game_id,player_id,season,date,team,opp,home,shots,points,"
                             "goals,assists,blocked,pp_points,saves,shots_against,started,toi) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            n += len(rows)
    conn.execute("INSERT OR REPLACE INTO build_log(built_at, players, rows) VALUES (?,?,?)",
                 (dt.datetime.now(LOCAL_TZ).isoformat(timespec="minutes"), len(players), n))
    conn.commit()
    return n


def player_rates(conn: sqlite3.Connection, player_id: int, as_of: dt.date, position: str) -> tuple[dict[str, float], float]:
    """Shrunk per-game rates as of a date: current season games + recent window vs prior season.
    Returns (rates, effective games). Blocked shots only exist in boxscores, not game logs:
    the rate is None until the settle path has stored some."""
    cols = ["saves", "shots_against", "started"] if position == "G" else ["shots", "points", "goals", "assists", "pp_points", "blocked"]
    rows = conn.execute(f"SELECT season, date, {','.join(cols)} FROM game_logs WHERE player_id=? AND date<? "
                        "ORDER BY date", (player_id, as_of.isoformat())).fetchall()
    prior = [r for r in rows if r[0] == PRIOR_SEASON]
    cur = [r for r in rows if r[0] == SEASON]
    if position == "G":                       # goalies: rate per START
        prior = [r for r in prior if r[4]]
        cur = [r for r in cur if r[4]]
    rates: dict[str, float] = {}
    for i, c in enumerate(cols):
        if c == "started":
            continue
        pv = [r[2 + i] for r in prior if r[2 + i] is not None]
        cv = [r[2 + i] for r in cur if r[2 + i] is not None]
        rv = cv[-RECENT_GAMES:]
        if not pv and not cv:
            continue
        prior_mean = sum(pv) / len(pv) if pv else (sum(cv) / len(cv))
        cur_mean = sum(cv) / len(cv) if cv else prior_mean
        w = len(cv) / (len(cv) + SHRINK_GAMES)
        base = w * cur_mean + (1 - w) * prior_mean
        if len(rv) >= 5:
            base = (1 - RECENT_WEIGHT) * base + RECENT_WEIGHT * (sum(rv) / len(rv))
        rates[c] = base
    eff = len(cur) + min(len(prior), SHRINK_GAMES)
    return rates, eff


def opponent_factors(summary_prior: dict, summary_cur: dict, home: str, away: str) -> tuple[dict, dict]:
    """Multipliers for HOME players' stats (vs away's allowance) and AWAY players' stats."""
    def blend(team, key):
        p = (summary_prior.get(team) or {}).get(key)
        c = summary_cur.get(team) or {}
        gp = c.get("gp") or 0
        cv = c.get(key)
        if cv is None:
            return p
        if p is None:
            return cv
        w = gp / (gp + 20.0)
        return w * cv + (1 - w) * p
    lg_sf = [v["sf"] for v in summary_prior.values() if v.get("sf")]
    lg_sa = [v["sa"] for v in summary_prior.values() if v.get("sa")]
    lg_ga = [v["ga"] for v in summary_prior.values() if v.get("ga")]
    avg_sf = sum(lg_sf) / len(lg_sf) if lg_sf else 30.0
    avg_sa = sum(lg_sa) / len(lg_sa) if lg_sa else 30.0
    avg_ga = sum(lg_ga) / len(lg_ga) if lg_ga else 3.0
    lo, hi = OPP_FACTOR_CAP

    def clamp(x):
        return max(lo, min(hi, x))

    def factors(opp, own, at_home):
        sa = blend(opp, "sa") or avg_sa          # opp shots allowed / game
        ga = blend(opp, "ga") or avg_ga          # opp goals allowed / game
        sf_opp = blend(opp, "sf") or avg_sf      # opp shots taken (for goalie saves)
        hf = HOME_FACTOR if at_home else 1.0
        shot_f = clamp(sa / avg_sa) * hf
        scor_f = clamp(ga / avg_ga) * hf
        return {"shots": shot_f, "goals": scor_f, "assists": scor_f, "points": scor_f, "pp_points": scor_f,
                "blocked": clamp(sf_opp / avg_sf), "saves": clamp(sf_opp / avg_sf)}
    return factors(away, home, True), factors(home, away, False)


def project(prop: Prop) -> None:
    r = prop.player.rates.get(prop.stat)
    if r is None:
        return
    g = prop.game
    f = (g.home_factor if prop.player.team == g.home else g.away_factor).get(prop.stat, 1.0)
    prop.mean = r * f
    po, push = p_over(prop.mean, prop.line)
    prop.p_over = po / (1.0 - push) if push < 1 else 0.5      # conditional on no push


# =====================================================================
# ---- prop lines ----
# =====================================================================

def fetch_props_oddsapi(key: str, games: list[GameCtx], books: str = "draftkings,fanduel,betmgm,caesars",
                        log=print) -> list[tuple]:
    """(game_id, player_name, market, line, over, under, book) from The Odds API."""
    events = _get(f"{ODDS_API}/sports/{ODDS_SPORT}/events", {"apiKey": key})
    by_teams = {}
    for ev in events:
        by_teams[(ev.get("home_team", ""), ev.get("away_team", ""))] = ev["id"]
    out = []
    used = 0
    for g in games:
        ev_id = None
        for (h, a), eid in by_teams.items():
            if _team_match(h, g.home) and _team_match(a, g.away):
                ev_id = eid
                break
        if not ev_id:
            continue
        try:
            r = requests.get(f"{ODDS_API}/sports/{ODDS_SPORT}/events/{ev_id}/odds",
                             params={"apiKey": key, "regions": "us", "oddsFormat": "american",
                                     "markets": ",".join(MARKETS), "bookmakers": books}, headers=UA, timeout=30)
            used = r.headers.get("x-requests-used", used)
            r.raise_for_status()
            d = r.json()
        except requests.HTTPError as e:
            log(f"  odds api {g.short}: {e}")
            continue
        for bk in d.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk["key"] not in MARKETS:
                    continue
                lines: dict[tuple, dict] = {}
                for o in mk.get("outcomes", []):
                    k = (o.get("description"), o.get("point"))
                    lines.setdefault(k, {})[o.get("name")] = o.get("price")
                for (pname, pt), sides in lines.items():
                    if pt is None:
                        continue
                    out.append((g.id, pname, mk["key"], float(pt), sides.get("Over"), sides.get("Under"), bk["key"]))
    log(f"  odds api: {len(out)} prop sides · requests used this month: {used}")
    return out


TEAM_WORDS = {"NJD": "devils", "NYI": "islanders", "NYR": "rangers", "PHI": "flyers", "PIT": "penguins", "BOS": "bruins",
              "BUF": "sabres", "MTL": "canadiens", "OTT": "senators", "TOR": "maple leafs", "CAR": "hurricanes",
              "FLA": "panthers", "TBL": "lightning", "WSH": "capitals", "CHI": "blackhawks", "DET": "red wings",
              "NSH": "predators", "STL": "blues", "CGY": "flames", "COL": "avalanche", "EDM": "oilers", "VAN": "canucks",
              "ANA": "ducks", "DAL": "stars", "LAK": "kings", "SJS": "sharks", "CBJ": "blue jackets", "MIN": "wild",
              "WPG": "jets", "VGK": "golden knights", "SEA": "kraken", "UTA": "mammoth"}


def _team_match(full_name: str, abbr: str) -> bool:
    w = TEAM_WORDS.get(abbr, abbr.lower())
    return w in full_name.lower()


def read_props_csv(path: str, games: list[GameCtx]) -> list[tuple]:
    """player,market,line,over,under[,book[,game]] — game as 'AWAY @ HOME' or blank (matched by roster)."""
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gid = None
            if r.get("game"):
                for g in games:
                    if g.short.replace(" ", "").lower() == r["game"].replace(" ", "").lower():
                        gid = g.id
            out.append((gid, r["player"].strip(), r["market"].strip(), float(r["line"]), ce._int(r.get("over")),
                        ce._int(r.get("under")), (r.get("book") or "csv").strip()))
    return out


def attach_props(rows: list[tuple], games: list[GameCtx], players: dict[str, list[Player]]) -> list[Prop]:
    """Match (game, player name) to rostered players on the two teams; unmatched rows are dropped."""
    gmap = {g.id: g for g in games}
    out = []
    for gid, pname, market, line, over, under, book in rows:
        if market not in MARKETS:
            continue
        cands = []
        for g in ([gmap[gid]] if gid in gmap else games):
            for p in players.get(g.home, []) + players.get(g.away, []):
                if _name_match(p.name, pname):
                    cands.append((g, p))
        if len(cands) != 1:
            continue
        g, p = cands[0]
        stat, who, _ = MARKETS[market]
        if (who == "goalie") != (p.position == "G"):
            continue
        out.append(Prop(g, p, market, stat, line, over, under, book))
    return out


def _name_match(roster: str, market: str) -> bool:
    a = _norm(roster)
    b = _norm(market)
    if a == b:
        return True
    ra, rb = a.split(), b.split()
    return bool(ra and rb) and ra[-1] == rb[-1] and ra[0][0] == rb[0][0]


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace(".", "").replace("-", " ").replace("'", "").split())


# =====================================================================
# ---- signals ----
# =====================================================================

def prop_signal(p: Prop) -> Optional[Signal]:
    if p.p_over is None or p.over is None or p.under is None:
        return None
    fo, fu = devig2(p.over, p.under)
    if fo is None:
        return None
    best: Optional[Signal] = None
    for side, model, fair, price in (("over", p.p_over, fo, p.over), ("under", 1 - p.p_over, fu, p.under)):
        edge = (model - fair) / fair * 100.0
        if edge <= 0:
            continue
        strength = 2 if edge >= EDGE_STRONG_PCT else 1 if edge >= EDGE_PCT else 0
        note = ""
        if edge >= EDGE_OVERREACH_PCT:
            strength, note = 0, "⚠overreach"
        if price > MAX_PRICE or price < MIN_PRICE:
            strength = 0
        if p.player.games < MIN_GAMES:
            strength, note = 0, "⚠thin"
        if p.stat == "saves":
            strength = min(strength, SAVES_MAX_STRENGTH)
            note = note or "⚠saves-model"
        if p.player.position == "G" and p.player.starter is False:
            strength, note = 0, "⚠not-starter"
        label = {2: "STRONG PROP", 1: "prop value", 0: ""}[strength]
        s = Signal(p, "prop", side, label, strength, edge, model, price, note)
        if best is None or s.edge > best.edge:
            best = s
    return best


def ranked_signals(props: list[Prop]) -> list[Signal]:
    sigs = [s for p in props if p.game.state in ("FUT", "PRE") for s in (prop_signal(p),) if s and s.strength]
    sigs.sort(key=lambda s: (-s.strength, -s.edge))
    return sigs


def stake_for(sig: Signal, bankroll: float) -> Optional[float]:
    return ce.stake_for(sig, bankroll)


# =====================================================================
# ---- SQLite ----
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (id INTEGER PRIMARY KEY, name TEXT, team TEXT, position TEXT);
CREATE TABLE IF NOT EXISTS game_logs (
  game_id INTEGER, player_id INTEGER, season INTEGER, date TEXT, team TEXT, opp TEXT, home INTEGER,
  shots INTEGER, points INTEGER, goals INTEGER, assists INTEGER, blocked INTEGER, pp_points INTEGER,
  saves INTEGER, shots_against INTEGER, started INTEGER, toi REAL,
  PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS ix_logs_player ON game_logs(player_id, date);
CREATE TABLE IF NOT EXISTS build_log (built_at TEXT PRIMARY KEY, players INTEGER, rows INTEGER);
CREATE TABLE IF NOT EXISTS games (
  id INTEGER PRIMARY KEY, date TEXT, start_utc TEXT, home TEXT, away TEXT, state TEXT,
  home_score INTEGER, away_score INTEGER, completed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS props (
  id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, player_id INTEGER, taken_at TEXT, book TEXT,
  market TEXT, line REAL, over INTEGER, under INTEGER, mean REAL, p_over REAL
);
CREATE TABLE IF NOT EXISTS paper_bets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, player_id INTEGER, logged_at TEXT, kind TEXT,
  market TEXT, side TEXT, line REAL, price INTEGER, truth_p REAL, edge REAL, strength INTEGER, stake REAL,
  book TEXT, actual REAL, result TEXT, profit REAL
);
"""
MIGRATIONS: dict[str, list[tuple[str, str]]] = {}


def db_connect(path: str = NHL_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def db_players(conn: sqlite3.Connection) -> dict[str, list[Player]]:
    out: dict[str, list[Player]] = {}
    for pid, name, team, pos in conn.execute("SELECT id,name,team,position FROM players"):
        out.setdefault(team, []).append(Player(int(pid), name, team, pos))
    return out


def db_persist(conn: sqlite3.Connection, games: list[GameCtx], props: list[Prop], now: dt.datetime) -> int:
    for g in games:
        conn.execute("INSERT INTO games(id,date,start_utc,home,away,state) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                     (g.id, g.date.isoformat(), g.start_utc.isoformat(), g.home, g.away, g.state))
    n = 0
    for p in props:
        conn.execute("INSERT INTO props(game_id,player_id,taken_at,book,market,line,over,under,mean,p_over) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (p.game.id, p.player.id, now.isoformat(), p.book, p.market, p.line, p.over, p.under, p.mean, p.p_over))
        n += 1
    conn.commit()
    return n


def db_paper_log(conn: sqlite3.Connection, sigs: list[Signal], bankroll: float, now: dt.datetime) -> int:
    n = 0
    for s in sigs:
        if s.strength == 0 or s.truth_p is None:
            continue
        p = s.prop
        if conn.execute("SELECT 1 FROM paper_bets WHERE game_id=? AND player_id=? AND market=?",
                        (p.game.id, p.player.id, p.market)).fetchone():
            continue
        conn.execute("INSERT INTO paper_bets(game_id,player_id,logged_at,kind,market,side,line,price,truth_p,edge,"
                     "strength,stake,book) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (p.game.id, p.player.id, now.isoformat(), "prop", p.market, s.side, p.line, s.price, s.truth_p,
                      s.edge, s.strength, stake_for(s, bankroll) or 0.0, p.book))
        n += 1
    conn.commit()
    return n


def grade_prop(side: str, line: float, actual: float) -> str:
    if actual == line:
        return "P"
    return "W" if ((actual > line) == (side == "over")) else "L"


def db_settle(conn: sqlite3.Connection, log=print) -> tuple[int, float, float]:
    """Pull boxscores for every game with a pending paper bet; grade from actual stats and
    store the boxscore rows as game logs (this is also how blocked shots get a rate)."""
    pend = conn.execute("SELECT DISTINCT game_id FROM paper_bets WHERE result IS NULL").fetchall()
    n, staked, profit = 0, 0.0, 0.0
    for (gid,) in pend:
        try:
            stats, state = fetch_boxscore_stats(gid)
        except requests.HTTPError:
            continue
        if state not in ("OFF", "FINAL"):
            continue
        for pid, st in stats.items():
            conn.execute("UPDATE game_logs SET blocked=? WHERE game_id=? AND player_id=?", (st.get("blocked"), gid, pid))
        for bid, pid, market, side, line, price, stake in conn.execute(
                "SELECT id,player_id,market,side,line,price,stake FROM paper_bets WHERE game_id=? AND result IS NULL",
                (gid,)).fetchall():
            stat = MARKETS[market][0]
            st = stats.get(pid)
            if st is not None and stat == "pp_points":
                st = {**st, "pp_points": _gamelog_stat(pid, gid, "powerPlayPoints")}
            if st is None or st.get(stat) is None:
                actual = 0.0            # scratched / did not play: books void, we grade as a push
                res = "P"
            else:
                actual = float(st[stat])
                res = grade_prop(side, line, actual)
            pr = ce._profit(res, stake, price)
            conn.execute("UPDATE paper_bets SET actual=?,result=?,profit=? WHERE id=?", (actual, res, pr, bid))
            n += 1
            staked += stake
            profit += pr
        conn.execute("UPDATE games SET completed=1, state=? WHERE id=?", (state, gid))
    conn.commit()
    return n, staked, profit


def _gamelog_stat(pid: int, gid: int, key: str) -> Optional[float]:
    for g in fetch_game_log(pid, SEASON):
        if int(g.get("gameId", 0)) == gid:
            return g.get(key)
    return None


def db_paper_summary(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT market,side,strength,COUNT(*),SUM(result='W'),SUM(result='L'),SUM(result='P'),"
                        "SUM(stake),SUM(profit) FROM paper_bets WHERE result IS NOT NULL "
                        "GROUP BY market,side,strength ORDER BY strength DESC, market, side").fetchall()
    pend = conn.execute("SELECT COUNT(*) FROM paper_bets WHERE result IS NULL").fetchone()[0]
    out = [f"NHL prop paper bets — settled by market/side/strength (pending: {pend})",
           f"{'market':<26}{'side':<6}{'str':>4}{'n':>5}{'W':>4}{'L':>4}{'P':>4}{'staked':>9}{'profit':>9}{'ROI':>8}"]
    for mk, side, st, n, w, l_, p, stk, pr in rows:
        roi = (pr / stk * 100) if stk else 0.0
        out.append(f"{mk:<26}{side:<6}{st:>4}{n:>5}{w:>4}{l_:>4}{p:>4}{stk:>9.2f}{pr:>9.2f}{roi:>+7.1f}%")
    if not rows:
        out.append("(nothing settled yet)")
    return "\n".join(out)


# =====================================================================
# ---- calibration ----
# =====================================================================

def calibrate(conn: sqlite3.Connection, season: int = PRIOR_SEASON, min_prior: int = 10,
              stats: tuple[str, ...] = ("shots", "points", "saves"), log=print) -> dict:
    """Walk forward through one season of stored game logs. For each player-game after the
    player's first `min_prior` games, project λ from games strictly before it (same shrinkage
    as live, prior season = nothing, so this is the HARDER version of the live model) and
    score P(X ≥ line) at the median-ish line for that stat. Reports log-loss vs a naive
    'league-average' model and a reliability table. No prop prices exist for last season,
    so this validates the projection, not the ROI."""
    lines = {"shots": 2.5, "points": 0.5, "goals": 0.5, "assists": 0.5, "saves": 27.5, "pp_points": 0.5}
    report = {}
    for stat in stats:
        is_g = stat == "saves"
        cond = "started=1" if is_g else "toi IS NOT NULL"
        rows = conn.execute(f"SELECT player_id, date, {stat} FROM game_logs WHERE season=? AND {stat} IS NOT NULL "
                            f"AND {cond} ORDER BY player_id, date", (season,)).fetchall()
        by_p: dict[int, list] = {}
        for pid, d, v in rows:
            by_p.setdefault(pid, []).append(v)
        allv = [v for vs in by_p.values() for v in vs]
        lg = sum(allv) / len(allv) if allv else 0.0
        line = lines[stat]
        bins = {}
        ll_m = ll_n = 0.0
        n = 0
        for pid, vs in by_p.items():
            for i in range(min_prior, len(vs)):
                hist = vs[:i]
                w = len(hist) / (len(hist) + SHRINK_GAMES)
                lam = w * (sum(hist) / len(hist)) + (1 - w) * lg
                rv = hist[-RECENT_GAMES:]
                if len(rv) >= 5:
                    lam = (1 - RECENT_WEIGHT) * lam + RECENT_WEIGHT * (sum(rv) / len(rv))
                po, _ = p_over(lam, line)
                pn, _ = p_over(lg, line)
                y = 1.0 if vs[i] > line else 0.0
                eps = 1e-6
                ll_m += -(y * math.log(max(po, eps)) + (1 - y) * math.log(max(1 - po, eps)))
                ll_n += -(y * math.log(max(pn, eps)) + (1 - y) * math.log(max(1 - pn, eps)))
                b = min(9, int(po * 10))
                acc = bins.setdefault(b, [0, 0.0, 0.0])
                acc[0] += 1
                acc[1] += po
                acc[2] += y
                n += 1
        if n == 0:
            continue
        report[stat] = {"n": n, "line": line, "logloss_model": ll_m / n, "logloss_naive": ll_n / n, "bins": bins}
        log(f"\n{stat.upper()} over {line:g} — {n:,} player-games ({season}), walk-forward, no prior season")
        log(f"  log-loss: model {ll_m / n:.4f} · naive league-average {ll_n / n:.4f} → "
            f"{'model better' if ll_m < ll_n else 'NAIVE better'} by {abs(ll_m - ll_n) / n:.4f}")
        log(f"  {'P(over) bin':<12}{'n':>7}{'pred':>8}{'obs':>8}{'Δpp':>7}")
        for b in sorted(bins):
            c, sp, sy = bins[b]
            log(f"  {b / 10:.1f}-{(b + 1) / 10:.1f}     {c:>7}{100 * sp / c:>7.1f}%{100 * sy / c:>7.1f}%{100 * (sy - sp) / c:>+6.1f}")
    return report


# =====================================================================
# ---- rendering / report ----
# =====================================================================

def render_top(props: list[Prop], bankroll: float, n: int = 15) -> str:
    sigs = ranked_signals(props)[:n]
    if not sigs:
        return ce.stakes_banner() + "\nno flagged NHL props on this slate"
    out = [ce.stakes_banner(), f"Top {len(sigs)} NHL prop outliers — projection vs de-vigged line — bankroll ${bankroll:.0f}, 1/4 Kelly"]
    for i, s in enumerate(sigs, 1):
        p = s.prop
        fair = devig2(p.over, p.under)[0 if s.side == "over" else 1]
        st = stake_for(s, bankroll)
        out.append(f"{i:>2}. {s.label:<12}{p.game.kick_local.strftime('%a %I:%M%p').lower():<12}{p.game.short:<12}"
                   f"{s.what:<34} {ce.fmt_ml(s.price):>5} @{p.book:<10} proj {p.mean:.2f} → model {100 * s.truth_p:.0f}% "
                   f"vs fair {100 * fair:.0f}% (+{s.edge:.0f}%) " + (f"${st:.0f} " if st else "") + s.note)
    return "\n".join(out)


def render_projections(props: list[Prop], n: int = 40) -> str:
    ps = sorted([p for p in props if p.mean is not None], key=lambda p: (p.game.kick_local, p.player.team, -p.mean))[:n]
    out = [f"{'Game':<12}{'Player':<26}{'Pos':<4}{'Market':<6}{'Line':>6}{'Proj':>7}{'P(over)':>9}{'Over/Under':>13}  Book"]
    for p in ps:
        out.append(f"{p.game.short:<12}{p.player.name[:25]:<26}{p.player.position:<4}{MARKETS[p.market][2]:<6}{p.line:>6g}"
                   f"{p.mean:>7.2f}{100 * (p.p_over or 0):>8.0f}%{ce.fmt_ml(p.over) + '/' + ce.fmt_ml(p.under):>13}  {p.book}")
    return "\n".join(out)


def report_path(date: dt.date) -> str:
    return os.path.join(REPORTS_DIR, f"nhl-{date.strftime('%A').lower()}-{date.isoformat()}.md")


def write_report(games: list[GameCtx], props: list[Prop], bankroll: float, date: dt.date, now: dt.datetime,
                 paper_summary: str, source: str) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = report_path(date)
    sigs = ranked_signals(props)
    L = [f"# NHL Prop Report — {date.strftime('%A, %B %d, %Y')}", "",
         f"**Generated:** {now.strftime('%Y-%m-%d %I:%M %p %Z')}  ", f"**{ce.stakes_banner()}**  ",
         f"**Slate:** {len(games)} games · {len(props)} prop lines matched to rostered players ({source})  ",
         f"**Model:** per-game rates from nhl.db game logs (shrink {SHRINK_GAMES:g} games to {PRIOR_SEASON}, "
         f"recent-{RECENT_GAMES} weight {RECENT_WEIGHT:.2f}) × opponent pace/allowance (clamped "
         f"{OPP_FACTOR_CAP[0]:.2f}–{OPP_FACTOR_CAP[1]:.2f}) → Poisson P(over). Tiers +{EDGE_PCT:g}% / +{EDGE_STRONG_PCT:g}%, "
         f"≥ +{EDGE_OVERREACH_PCT:g}% demoted (⚠overreach). All priors until analysis/06 runs.",
         "", "## 1. Ranked props", "",
         "| # | Tag | Game | Play | Price | Book | Proj | Model vs fair | $Bet (paper) | Flags |", "|---|---|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(sigs, 1):
        p = s.prop
        fair = devig2(p.over, p.under)[0 if s.side == "over" else 1]
        st = stake_for(s, bankroll)
        L.append(f"| {i} | **{s.label}** | {p.game.short} | {s.what} | {ce.fmt_ml(s.price)} | {p.book} | {p.mean:.2f} | "
                 f"{100 * s.truth_p:.0f}% vs {100 * fair:.0f}% (+{s.edge:.0f}%) | {f'${st:.0f}' if st else '—'} | {s.note or '—'} |")
    if not sigs:
        L.append("| — | — | — | no flagged props | | | | | | |")
    L += ["", "## 2. Projections (first 40 by game)", "", "```", render_projections(props), "```", "",
          "## 3. NHL paper ledger to date", "", "```", paper_summary, "```", "",
          "> Paper only. No NHL analysis run exists; the projection was walk-forward calibrated on last season "
          "(`--calibrate`) but no historical prop prices exist to backtest ROI. The ledger above is the first evidence."]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


# =====================================================================
# ---- main ----
# =====================================================================

def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="NHL player-prop outlier finder — projections vs the posted line")
    p.add_argument("--date", help="YYYY-MM-DD (default: today, America/Chicago)")
    p.add_argument("--bankroll", type=float, default=100.0)
    p.add_argument("--db", default=NHL_DB)
    p.add_argument("--build", action="store_true", help="rosters + game logs for both seasons -> nhl.db (~2 min)")
    p.add_argument("--calibrate", action="store_true", help="walk-forward test of the projection on last season")
    p.add_argument("--lines-file", help="CSV of prop lines (player,market,line,over,under[,book[,game]]) instead of the Odds API")
    p.add_argument("--markets", default=",".join(MARKETS), help="comma-separated Odds API markets to pull")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--projections", action="store_true", help="print the projection table (no lines needed)")
    p.add_argument("--snapshot", action="store_true", help="persist lines + projections, paper-log flagged props")
    p.add_argument("--report", action="store_true", help="write reports/nhl-<weekday>-<date>.md")
    p.add_argument("--settle", action="store_true", help="grade pending paper props from boxscores")
    p.add_argument("--paper-show", action="store_true")
    a = p.parse_args(argv)

    now = dt.datetime.now(LOCAL_TZ)
    date = dt.date.fromisoformat(a.date) if a.date else now.date()
    conn = db_connect(a.db)
    if a.paper_show:
        print(db_paper_summary(conn))
        return 0
    if a.build:
        print(f"building nhl.db: rosters for {SEASON} + game logs {PRIOR_SEASON} and {SEASON} …")
        n = build_logs(conn)
        print(f"stored {n:,} game-log rows")
        return 0
    if a.calibrate:
        calibrate(conn)
        return 0
    if a.settle:
        n, staked, profit = db_settle(conn)
        print(f"settled {n} paper props: staked {staked:.2f}, profit {profit:+.2f}")
        print(db_paper_summary(conn))
        return 0

    games = fetch_schedule(date)
    if not games:
        print(f"no NHL regular-season games on {date} (season opens {SEASON_START})")
        return 0
    players = db_players(conn)
    if not players:
        print("nhl.db has no players — run --build first", file=sys.stderr)
        return 1
    print(f"{len(games)} games on {date}: " + ", ".join(g.short for g in games), file=sys.stderr)
    sp = fetch_team_summary(PRIOR_SEASON)
    try:
        sc = fetch_team_summary(SEASON)
    except requests.HTTPError:
        sc = {}
    for g in games:
        g.home_factor, g.away_factor = opponent_factors(sp, sc, g.home, g.away)
    for g in games:
        for pl in players.get(g.home, []) + players.get(g.away, []):
            pl.rates, pl.games = player_rates(conn, pl.id, date, pl.position)

    rows, source = [], "no lines"
    if a.lines_file:
        rows, source = read_props_csv(a.lines_file, games), f"csv {a.lines_file}"
    else:
        key = load_key()
        if key:
            rows, source = fetch_props_oddsapi(key, games, log=lambda m: print(m, file=sys.stderr)), "The Odds API"
        else:
            print("no ODDS_API_KEY (env or .env) and no --lines-file: projections only", file=sys.stderr)
    props = attach_props(rows, games, players)
    for pr in props:
        project(pr)
    print(f"{len(rows)} prop sides fetched · {len(props)} matched to rostered players · source: {source}", file=sys.stderr)

    if a.projections or not props:
        # synthesize lines at the market-typical number so projections print even with no book
        synth = []
        for g in games:
            for pl in players.get(g.home, []) + players.get(g.away, []):
                for market, (stat, who, _) in MARKETS.items():
                    if (who == "goalie") != (pl.position == "G") or stat not in pl.rates:
                        continue
                    line = {"shots": 2.5, "points": 0.5, "goals": 0.5, "assists": 0.5, "blocked": 1.5,
                            "pp_points": 0.5, "saves": 27.5}[stat]
                    pr = Prop(g, pl, market, stat, line, None, None, "proj")
                    project(pr)
                    synth.append(pr)
        print(render_projections(synth, n=60))
        if not props:
            print("\n" + ce.stakes_banner())
    if props:
        print(render_top(props, a.bankroll, a.top))
    if a.snapshot and props:
        n = db_persist(conn, games, props, now)
        k = db_paper_log(conn, ranked_signals(props), a.bankroll, now)
        print(f"snapshot: {n} prop rows, {k} new paper plays → {a.db}")
    if a.report:
        path = write_report(games, props, a.bankroll, date, now, db_paper_summary(conn), source)
        print(f"\nreport → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
