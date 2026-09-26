#!/usr/bin/env python3
# Copyright (c) 2026 William Brooks Parker. All rights reserved. Proprietary — see LICENSE.
"""
cfb_edge.py — college football outlier finder (single-file, no API keys).

Compares the DraftKings line (via ESPN) against ESPN's FPI game predictor and
FPI power ratings, flags the games where the model and the market disagree
most, tracks open->current line movement, sizes 1/4-Kelly tickets, snapshots
everything to SQLite for an honest backtest, and keeps a bets.csv ledger that
settles itself from final scores.

Sister project of horses_worldwide/edge_finder.py — same philosophy:
  * one file, section headers navigate it
  * every signal is persisted so it can be backtested later
  * the README's "honest expectations" callout is load-bearing

Sections:
  ---- models / odds math ----
  ---- ESPN adapters ----
  ---- signals ----
  ---- SQLite persistence ----
  ---- bets ledger ----
  ---- rendering ----
  ---- report ----
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
from zoneinfo import ZoneInfo

import requests

DEFAULT_DB = "data.db"
BETS_CSV = "bets.csv"
REPORTS_DIR = "reports"
LOCAL_TZ = ZoneInfo("America/Chicago")
# NB: a full Chrome UA string gets a 403 from ESPN's Akamai edge (fingerprint
# mismatch); a plain token passes. Verified 2026-09-09 — don't "improve" this.
UA = {"User-Agent": "Mozilla/5.0"}

ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/football/"
                   "college-football/scoreboard")
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football"
ESPN_POWERINDEX = ("https://site.web.api.espn.com/apis/fitt/v3/sports/football/"
                   "college-football/powerindex")

# ---- decision constants (see README "Before you bet") ----
# Std-dev of (actual margin - closing spread) in FBS. 13-14 pts is the
# long-run figure; used to turn a point-edge into a cover probability.
MARGIN_SD = 13.5
SPREAD_PRICE = -110               # assumed juice when ESPN doesn't give one
SPREAD_OUTLIER_PTS = 3.0          # |FPI margin - market margin| to flag
SPREAD_STRONG_PTS = 5.0
ML_EDGE_PCT = 8.0                 # (FPI p - devig p)/devig p, in %
ML_STRONG_PCT = 20.0
ML_MAX_PRICE = 400                # never suggest a ML dog longer than +400
ML_MIN_PRICE = -300               # never suggest a ML fav shorter than -300
STEAM_PTS = 1.5                   # open->current spread move to call "steam"
KEY_NUMBERS = (3, 7, 10, 14)
KELLY_FRACTION = 0.25
MIN_TICKET = 1.0
MAX_TICKET_PCT = 0.05             # hard cap: 5% of bankroll per ticket
LINE_MOVE_PTS = 3.0               # open->current spread move worth surfacing on its own
BLOWOUT_SPREAD = 28.0             # numbers this big: cover model is unreliable, cap at lean
SPREAD_OVERREACH_PTS = 8.0        # |FPI - DK| this big: the market knows something, cap at lean
ML_DEAD_ZONE = (100, 150)         # ML dogs in this band never staked (30% hit, -33% ROI on 63 bets)
LIVE_STAKES = False               # no bucket has a 95% CI above zero -> paper only, no real money
HFA_PTS = 2.5                     # for the rating-diff cross-check only
# "just win" board: favourites FPI and the market agree on, at a price you can hold.
# Deliberately the opposite of the outlier board — the data says big gaps lose.
JUST_WIN_MIN_P = 0.60             # FPI must give the side at least this to win outright
JUST_WIN_PRICE = (-250, -110)     # moneyline window; shorter than -250 pays nothing, plus money = coin flip
JUST_WIN_MAX_EDGE_PCT = ML_STRONG_PCT   # past a STRONG-sized gap the market knows something; drop it
JUST_WIN_N = 5                    # the top table in the report

FINDINGS_AS_OF = "2026-09-20"


# =====================================================================
# ---- models / odds math ----
# =====================================================================

@dataclass
class TeamSide:
    id: str
    abbr: str
    name: str
    record: Optional[str] = None
    rank: Optional[int] = None           # AP rank if any
    score: Optional[int] = None
    fpi: Optional[float] = None
    fpi_rank: Optional[int] = None
    off_eff: Optional[float] = None
    def_eff: Optional[float] = None
    # market
    ml: Optional[int] = None             # american, current
    ml_open: Optional[int] = None
    spread: Optional[float] = None       # this side's number (neg = favored)
    spread_open: Optional[float] = None
    spread_price: Optional[int] = None
    # model
    fpi_win_p: Optional[float] = None    # 0..1
    fpi_margin: Optional[float] = None   # predicted points this side wins by


@dataclass
class Game:
    id: str
    name: str
    short: str
    kickoff: dt.datetime                 # aware UTC
    status: str                          # pre / in / post
    completed: bool
    neutral: bool
    conf_game: bool
    home: TeamSide
    away: TeamSide
    provider: Optional[str] = None
    total: Optional[float] = None
    total_open: Optional[float] = None
    over_price: Optional[int] = None
    under_price: Optional[int] = None
    matchup_quality: Optional[float] = None
    notes: list[str] = field(default_factory=list)

    @property
    def kick_local(self) -> dt.datetime:
        return self.kickoff.astimezone(LOCAL_TZ)


def american_to_decimal(a: Optional[int]) -> Optional[float]:
    if a is None:
        return None
    return 1 + (a / 100.0 if a > 0 else 100.0 / abs(a))


def implied_prob(a: Optional[int]) -> Optional[float]:
    d = american_to_decimal(a)
    return None if not d else 1.0 / d


def devig_pair(a: Optional[int], b: Optional[int]) -> tuple[Optional[float], Optional[float]]:
    """Two-way market -> fair probabilities (multiplicative de-vig)."""
    pa, pb = implied_prob(a), implied_prob(b)
    if pa is None or pb is None:
        return None, None
    s = pa + pb
    return pa / s, pb / s


def kelly_fraction(p: float, decimal_odds: float) -> float:
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    return max(0.0, (p * b - (1 - p)) / b)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def cover_probability(model_margin: float, market_margin: float) -> float:
    """P(model side covers) given the model's margin vs the market's."""
    return norm_cdf((model_margin - market_margin) / MARGIN_SD)


def fmt_spread(x: Optional[float]) -> str:
    if x is None:
        return "—"
    if x == 0:
        return "PK"
    return f"{x:+g}"


def fmt_ml(x: Optional[int]) -> str:
    return "—" if x is None else f"{x:+d}"


def _num(s) -> Optional[float]:
    try:
        if s is None or s == "" or s == "EVEN":
            return 0.0 if s == "EVEN" else None
        return float(str(s).replace("PK", "0").replace("+", ""))
    except ValueError:
        return None


def _int(s) -> Optional[int]:
    v = _num(s)
    return None if v is None else int(round(v))


# =====================================================================
# ---- ESPN adapters ----
# =====================================================================

def _get(url: str, params: Optional[dict] = None, timeout: int = 20) -> dict:
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def next_saturday(today: Optional[dt.date] = None) -> dt.date:
    d = today or dt.datetime.now(LOCAL_TZ).date()
    return d + dt.timedelta(days=(5 - d.weekday()) % 7)


def fetch_scoreboard(date: dt.date) -> list[Game]:
    """All FBS+FCS games on a date (groups=80 = FBS slate incl. FCS visitors)."""
    d = _get(ESPN_SCOREBOARD, {"dates": date.strftime("%Y%m%d"), "groups": 80, "limit": 300})
    games: list[Game] = []
    for ev in d.get("events", []):
        comp = ev["competitions"][0]
        sides = {}
        for c in comp["competitors"]:
            t = c["team"]
            rec = (c.get("records") or [{}])[0].get("summary")
            rank = c.get("curatedRank", {}).get("current")
            side = TeamSide(
                id=str(t["id"]), abbr=t.get("abbreviation", "?"),
                name=t.get("displayName", t.get("name", "?")), record=rec,
                rank=int(rank) if rank and int(rank) <= 25 else None,
                score=_int(c.get("score")) if comp["status"]["type"]["state"] != "pre" else None,
            )
            sides[c["homeAway"]] = side
        st = comp["status"]["type"]
        g = Game(
            id=str(ev["id"]), name=ev.get("name", ""), short=ev.get("shortName", ""),
            kickoff=dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")),
            status=st.get("state", "pre"), completed=bool(st.get("completed")),
            neutral=bool(comp.get("neutralSite")), conf_game=bool(comp.get("conferenceCompetition")),
            home=sides["home"], away=sides["away"],
        )
        # scoreboard odds (current only) — fallback if the core odds call fails
        for o in comp.get("odds", []) or []:
            g.provider = o.get("provider", {}).get("name")
            g.total = _num(o.get("overUnder"))
            sp = _num(o.get("spread"))
            if sp is not None:
                g.home.spread, g.away.spread = sp, -sp
            g.home.ml = _int(o.get("homeTeamOdds", {}).get("moneyLine"))
            g.away.ml = _int(o.get("awayTeamOdds", {}).get("moneyLine"))
            break
        games.append(g)
    games.sort(key=lambda x: (x.kickoff, x.name))
    return games


def _apply_core_odds(g: Game, d: dict) -> None:
    items = d.get("items") or []
    if not items:
        return
    it = items[0]
    g.provider = it.get("provider", {}).get("name", g.provider)
    g.total = _num(((it.get("current") or {}).get("total") or {}).get("american")) or _num(it.get("overUnder"))
    g.total_open = _num(((it.get("open") or {}).get("total") or {}).get("american"))
    g.over_price = _int(((it.get("current") or {}).get("over") or {}).get("american")) or _int(it.get("overOdds"))
    g.under_price = _int(((it.get("current") or {}).get("under") or {}).get("american")) or _int(it.get("underOdds"))
    for key, side in (("homeTeamOdds", g.home), ("awayTeamOdds", g.away)):
        o = it.get(key) or {}
        cur, opn = o.get("current") or {}, o.get("open") or {}
        side.spread = _num((cur.get("pointSpread") or {}).get("american"))
        side.spread_open = _num((opn.get("pointSpread") or {}).get("american"))
        side.spread_price = _int((cur.get("spread") or {}).get("american")) or _int(o.get("spreadOdds"))
        side.ml = _int((cur.get("moneyLine") or {}).get("american")) or _int(o.get("moneyLine"))
        side.ml_open = _int((opn.get("moneyLine") or {}).get("american"))
    if g.home.spread is None and g.away.spread is not None:
        g.home.spread = -g.away.spread
    if g.away.spread is None and g.home.spread is not None:
        g.away.spread = -g.home.spread


def _apply_predictor(g: Game, d: dict) -> None:
    for key, side in (("homeTeam", g.home), ("awayTeam", g.away)):
        stats = {s["name"]: s.get("value") for s in (d.get(key) or {}).get("statistics", [])}
        if "gameProjection" in stats:
            side.fpi_win_p = stats["gameProjection"] / 100.0
        if "teamPredPtDiff" in stats:
            side.fpi_margin = stats["teamPredPtDiff"]
        if "matchupQuality" in stats:
            g.matchup_quality = stats["matchupQuality"]


def enrich_games(games: list[Game], workers: int = 8) -> None:
    """Pull open/current odds + FPI predictor for every game (parallel)."""
    def one(g: Game) -> None:
        base = f"{ESPN_CORE}/events/{g.id}/competitions/{g.id}"
        try:
            _apply_core_odds(g, _get(f"{base}/odds", {"limit": 50}))
        except Exception as e:  # noqa: BLE001
            g.notes.append(f"odds: {type(e).__name__}")
        try:
            _apply_predictor(g, _get(f"{base}/predictor"))
        except Exception as e:  # noqa: BLE001
            g.notes.append(f"predictor: {type(e).__name__}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, games))


def fetch_powerindex() -> dict[str, dict]:
    """team_id -> {fpi, fpi_rank, off_eff, def_eff}. ~138 FBS teams."""
    out: dict[str, dict] = {}
    page = 1
    while True:
        d = _get(ESPN_POWERINDEX, {"region": "us", "lang": "en", "limit": 100, "page": page})
        cat_names = {c["name"]: c["names"] for c in d.get("categories", [])}
        for t in d.get("teams", []):
            rec = {}
            for c in t.get("categories", []):
                names = cat_names.get(c["name"], [])
                vals = dict(zip(names, c.get("values", [])))
                if c["name"] == "fpi":
                    rec["fpi"] = vals.get("fpi")
                    rec["fpi_rank"] = vals.get("fpirank")
                elif c["name"] == "efficiencies":
                    rec["off_eff"] = vals.get("offefficiency")
                    rec["def_eff"] = vals.get("defefficiency")
            out[str(t["team"]["id"])] = rec
        pages = d.get("pagination", {}).get("pages", 1)
        if page >= pages:
            break
        page += 1
    return out


def apply_powerindex(games: list[Game], pi: dict[str, dict]) -> None:
    for g in games:
        for s in (g.home, g.away):
            r = pi.get(s.id)
            if r:
                s.fpi, s.fpi_rank = r.get("fpi"), r.get("fpi_rank")
                s.off_eff, s.def_eff = r.get("off_eff"), r.get("def_eff")


# =====================================================================
# ---- signals ----
# =====================================================================

@dataclass
class Signal:
    game: Game
    kind: str            # "spread" | "ml" | "total-move"
    side: TeamSide | None
    label: str           # human tag
    strength: int        # 2 strong, 1 lean, 0 info
    edge: float          # pts for spread, % for ml, pts for total move
    truth_p: Optional[float] = None
    price: Optional[int] = None
    line: Optional[float] = None
    steam: str = ""      # "with" / "against" / ""
    key_note: str = ""


def market_margin_home(g: Game) -> Optional[float]:
    """Market's expected home margin (= -home spread)."""
    return None if g.home.spread is None else -g.home.spread


def rating_margin_home(g: Game) -> Optional[float]:
    """Cross-check: raw FPI rating diff + HFA (predictor already includes this)."""
    if g.home.fpi is None or g.away.fpi is None:
        return None
    return g.home.fpi - g.away.fpi + (0.0 if g.neutral else HFA_PTS)


def spread_move_home(g: Game) -> Optional[float]:
    """Positive = line moved toward home (home became more favored)."""
    if g.home.spread is None or g.home.spread_open is None:
        return None
    return g.home.spread_open - g.home.spread


def crosses_key(a: Optional[float], b: Optional[float]) -> str:
    if a is None or b is None:
        return ""
    lo, hi = sorted((abs(a), abs(b)))
    ks = [k for k in KEY_NUMBERS if lo <= k <= hi and lo != hi]
    return f"crosses {','.join(map(str, ks))}" if ks else ""


def spread_signal(g: Game) -> Optional[Signal]:
    mm, fm = market_margin_home(g), g.home.fpi_margin
    if mm is None or fm is None:
        return None
    diff = fm - mm                      # >0 -> FPI likes home vs the number
    side = g.home if diff > 0 else g.away
    edge = abs(diff)
    p = cover_probability(fm, mm) if diff > 0 else 1 - cover_probability(fm, mm)
    strength = 2 if edge >= SPREAD_STRONG_PTS else 1 if edge >= SPREAD_OUTLIER_PTS else 0
    mv = spread_move_home(g)
    steam = ""
    if mv is not None and abs(mv) >= STEAM_PTS:
        steam = "with" if (mv > 0) == (diff > 0) else "against"
    # demotions — see README "Before you bet"
    if g.home.fpi is None or g.away.fpi is None:
        strength = 0            # FCS side: FPI uses a generic rating, the "edge" is noise
    if abs(mm) >= BLOWOUT_SPREAD:
        strength = min(strength, 1)
    if edge >= SPREAD_OVERREACH_PTS:
        strength = min(strength, 1)  # 2025 sample: Δ8+ covered 42.5% — bigger gap, more wrong
    if steam == "against":
        strength = max(strength - 1, 0)
    label = {2: "STRONG ATS", 1: "ATS lean", 0: ""}[strength]
    return Signal(g, "spread", side, label, strength, edge, p,
                  side.spread_price or SPREAD_PRICE, side.spread, steam,
                  crosses_key(mm, fm))


def ml_signal(g: Game) -> Optional[Signal]:
    ph, pa = devig_pair(g.home.ml, g.away.ml)
    if ph is None or g.home.fpi_win_p is None:
        return None
    best: Optional[Signal] = None
    for side, fair in ((g.home, ph), (g.away, pa)):
        if side.fpi_win_p is None or side.ml is None:
            continue
        edge = (side.fpi_win_p - fair) / fair * 100.0
        if edge <= 0:
            continue
        strength = 2 if edge >= ML_STRONG_PCT else 1 if edge >= ML_EDGE_PCT else 0
        if side.ml > ML_MAX_PRICE or side.ml < ML_MIN_PRICE:
            strength = 0
        if side.ml > 250:
            strength = min(strength, 1)
        if ML_DEAD_ZONE[0] <= side.ml <= ML_DEAD_ZONE[1]:
            strength = 0        # +100..+150 dogs hit 30.2% on 63 bets: worst bucket in the sample
        if g.home.fpi is None or g.away.fpi is None:
            strength = 0        # FCS side: same rule as ATS — never ranked, never staked
        label = {2: "STRONG ML", 1: "ML value", 0: ""}[strength]
        s = Signal(g, "ml", side, label, strength, edge, side.fpi_win_p, side.ml)
        if best is None or s.edge > best.edge:
            best = s
    return best


def spread_move_signal(g: Game) -> Optional[Signal]:
    """Raw market movement, model-agnostic. Big moves = news (QB, injury, weather)."""
    mv = spread_move_home(g)
    if mv is None or abs(mv) < LINE_MOVE_PTS:
        return None
    side = g.home if mv > 0 else g.away
    return Signal(g, "spread-move", side, "line move", 1, abs(mv), None, None, side.spread,
                  "", f"{fmt_spread(side.spread_open)}→{fmt_spread(side.spread)}")


def total_move_signal(g: Game) -> Optional[Signal]:
    if g.total is None or g.total_open is None or g.total == g.total_open:
        return None
    mv = g.total - g.total_open
    strength = 1 if abs(mv) >= 2.5 else 0
    return Signal(g, "total-move", None, "total steam" if strength else "", strength,
                  mv, None, None, g.total)


def stake_for(sig: Signal, bankroll: float, frac: float = KELLY_FRACTION) -> Optional[float]:
    """1/4-Kelly ticket; None if no positive expectation."""
    if sig.truth_p is None or sig.price is None or sig.strength == 0:
        return None
    k = kelly_fraction(sig.truth_p, american_to_decimal(sig.price))
    amt = k * frac * bankroll
    amt = min(amt, bankroll * MAX_TICKET_PCT)
    if amt < MIN_TICKET * 0.5:
        return None
    return max(MIN_TICKET, round(amt, 0))


def findings_warnings(sig: Signal) -> list[str]:
    """Inline caveats. Mirrors edge_finder.findings_warnings — constants only."""
    w = []
    g = sig.game
    if sig.steam == "against":
        w.append("⚠market-moved-against")
    if sig.kind == "spread" and sig.edge >= SPREAD_OVERREACH_PTS:
        w.append("⚠overreach")
    if sig.kind == "ml" and sig.price is not None and ML_DEAD_ZONE[0] <= sig.price <= ML_DEAD_ZONE[1]:
        w.append("⚠dead-zone-dog")
    if sig.kind == "spread" and sig.side and sig.side.fpi is None:
        w.append("⚠non-FBS side")
    if sig.kind == "ml" and sig.price and sig.price > 250:
        w.append("⚠long-dog")
    if sig.kind == "spread" and abs(sig.line or 0) >= 28:
        w.append("⚠blowout-number")
    if g.home.fpi_win_p is None:
        w.append("⚠no-FPI")
    return w


def just_win_signal(g: Game) -> Optional[Signal]:
    """The favourite FPI expects to win outright, with the market on the same side, priced
    inside JUST_WIN_PRICE and disagreeing with FPI by no more than a STRONG-sized gap.
    Strength is always 1: this is a separate paper bucket ("just-win") the analysis loop has
    not graded yet. edge is the relative % gap, truth_p the FPI win prob."""
    if g.home.fpi is None or g.away.fpi is None or g.home.fpi_win_p is None:
        return None                     # FBS vs FBS only, same rule as every other signal
    ph, pa = devig_pair(g.home.ml, g.away.ml)
    if ph is None:
        return None
    side, fair = (g.home, ph) if g.home.fpi_win_p >= 0.5 else (g.away, pa)
    p = side.fpi_win_p
    if p is None or side.ml is None or p < JUST_WIN_MIN_P or fair < 0.5:
        return None
    if not (JUST_WIN_PRICE[0] <= side.ml <= JUST_WIN_PRICE[1]):
        return None
    edge = (p - fair) / fair * 100.0
    if edge <= 0 or edge > JUST_WIN_MAX_EDGE_PCT:
        return None
    if p * american_to_decimal(side.ml) <= 1.0:
        return None                     # positive EV at the *vigged* price, not just vs fair
    mv = spread_move_home(g)
    steam = ""
    if mv is not None and abs(mv) >= STEAM_PTS:
        steam = "with" if (mv > 0) == (side is g.home) else "against"
    if steam == "against":
        return None
    return Signal(g, "just-win", side, "JUST WIN", 1, edge, p, side.ml, side.spread, steam,
                  f"fair {fair*100:.0f}%")


def just_win_board(games: list[Game]) -> list[Signal]:
    """Every qualifying just-win side, most likely winner first (ties: bigger edge)."""
    sigs = [s for g in games if g.status == "pre" for s in (just_win_signal(g),) if s]
    return sorted(sigs, key=lambda s: (-s.truth_p, -s.edge))


def just_win_roi(s: Signal) -> float:
    """Expected return per $1 at FPI's win probability, in %."""
    return (s.truth_p * american_to_decimal(s.price) - 1.0) * 100.0


# =====================================================================
# ---- SQLite persistence ----
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  id TEXT PRIMARY KEY, name TEXT, date TEXT, kickoff_utc TEXT, neutral INTEGER,
  home_id TEXT, home TEXT, away_id TEXT, away TEXT,
  home_score INTEGER, away_score INTEGER, completed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT, taken_at TEXT,
  provider TEXT, home_spread REAL, home_spread_open REAL, total REAL, total_open REAL,
  home_ml INTEGER, away_ml INTEGER, home_fpi_p REAL, home_fpi_margin REAL,
  home_fpi REAL, away_fpi REAL
);
CREATE TABLE IF NOT EXISTS paper_bets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT, logged_at TEXT, kind TEXT,
  side_id TEXT, side TEXT, line REAL, price INTEGER, truth_p REAL, edge REAL,
  strength INTEGER, stake REAL, result TEXT, profit REAL, backfill INTEGER DEFAULT 0
);
"""


# Migration pattern (same as horses): SCHEMA is CREATE IF NOT EXISTS; new
# columns get appended here and ALTER TABLE'd onto existing DBs. Never drop.
MIGRATIONS = {
    "paper_bets": [("backfill", "INTEGER DEFAULT 0")],
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def db_connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    _migrate_columns(conn)
    return conn


def db_persist(conn: sqlite3.Connection, games: list[Game], now: dt.datetime) -> int:
    n = 0
    for g in games:
        conn.execute(
            "INSERT INTO games(id,name,date,kickoff_utc,neutral,home_id,home,away_id,away,"
            "home_score,away_score,completed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET home_score=excluded.home_score,"
            "away_score=excluded.away_score,completed=excluded.completed",
            (g.id, g.name, g.kick_local.date().isoformat(), g.kickoff.isoformat(), int(g.neutral),
             g.home.id, g.home.name, g.away.id, g.away.name,
             g.home.score, g.away.score, int(g.completed)))
        if g.home.spread is not None or g.home.fpi_win_p is not None:
            conn.execute(
                "INSERT INTO snapshots(game_id,taken_at,provider,home_spread,home_spread_open,"
                "total,total_open,home_ml,away_ml,home_fpi_p,home_fpi_margin,home_fpi,away_fpi)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (g.id, now.isoformat(), g.provider, g.home.spread, g.home.spread_open,
                 g.total, g.total_open, g.home.ml, g.away.ml, g.home.fpi_win_p,
                 g.home.fpi_margin, g.home.fpi, g.away.fpi))
            n += 1
    conn.commit()
    return n


def db_paper_log(conn: sqlite3.Connection, sigs: list[Signal], bankroll: float,
                 now: dt.datetime, backfill: bool = False) -> int:
    """Paper-log every flagged, stakeable play. With backfill=True, completed games
    are logged too (their 'current' line is the closer and FPI is the pre-game
    number — ESPN keeps both), flagged backfill=1 so the analysis can split them."""
    n = 0
    for s in sigs:
        if s.strength == 0 or s.side is None or s.truth_p is None:
            continue
        if s.game.status != "pre" and not backfill:
            continue
        dup = conn.execute("SELECT 1 FROM paper_bets WHERE game_id=? AND kind=? AND side_id=?",
                           (s.game.id, s.kind, s.side.id)).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO paper_bets(game_id,logged_at,kind,side_id,side,line,price,truth_p,edge,"
            "strength,stake,backfill) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.game.id, now.isoformat(), s.kind, s.side.id, s.side.name, s.line, s.price,
             s.truth_p, s.edge, s.strength, stake_for(s, bankroll) or 0.0, int(backfill)))
        n += 1
    conn.commit()
    return n


def _grade(kind: str, side_is_home: bool, line: Optional[float], hs: int, as_: int) -> str:
    margin = (hs - as_) if side_is_home else (as_ - hs)
    if kind in ("ml", "just-win"):
        return "W" if margin > 0 else "L" if margin < 0 else "P"
    if kind == "spread":
        adj = margin + (line or 0.0)
        return "W" if adj > 0 else "L" if adj < 0 else "P"
    if kind in ("over", "under"):
        tot = hs + as_
        if tot == line:
            return "P"
        return "W" if ((tot > line) == (kind == "over")) else "L"
    return "?"


def _profit(result: str, stake: float, price: int) -> float:
    if result == "W":
        return round(stake * (american_to_decimal(price) - 1), 2)
    if result == "L":
        return -stake
    return 0.0


def db_settle_paper(conn: sqlite3.Connection) -> tuple[int, float, float]:
    rows = conn.execute(
        "SELECT p.id,p.kind,p.side_id,p.line,p.price,p.stake,g.home_id,g.home_score,g.away_score "
        "FROM paper_bets p JOIN games g ON g.id=p.game_id "
        "WHERE p.result IS NULL AND g.completed=1 AND g.home_score IS NOT NULL").fetchall()
    n, staked, profit = 0, 0.0, 0.0
    for pid, kind, side_id, line, price, stake, home_id, hs, as_ in rows:
        res = _grade(kind, side_id == home_id, line, hs, as_)
        pr = _profit(res, stake or 0.0, price or SPREAD_PRICE)
        conn.execute("UPDATE paper_bets SET result=?,profit=? WHERE id=?", (res, pr, pid))
        n += 1
        staked += stake or 0.0
        profit += pr
    conn.commit()
    return n, staked, profit


def db_paper_summary(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT kind,strength,COUNT(*),SUM(result='W'),SUM(result='L'),SUM(result='P'),"
        "SUM(stake),SUM(profit) FROM paper_bets WHERE result IS NOT NULL "
        "GROUP BY kind,strength ORDER BY kind,strength DESC").fetchall()
    pend = conn.execute("SELECT COUNT(*) FROM paper_bets WHERE result IS NULL").fetchone()[0]
    out = [f"paper bets — settled by kind/strength (pending: {pend})",
           f"{'kind':<11}{'str':>4}{'n':>5}{'W':>4}{'L':>4}{'P':>4}{'staked':>9}{'profit':>9}{'ROI':>8}"]
    for kind, st, n, w, lost, p, staked, prof in rows:
        roi = (prof / staked * 100) if staked else 0.0
        out.append(f"{kind:<11}{st:>4}{n:>5}{w or 0:>4}{lost or 0:>4}{p or 0:>4}"
                   f"{staked or 0:>9.2f}{prof or 0:>9.2f}{roi:>+7.1f}%")
    if len(out) == 2:
        out.append("(none settled yet — run --snapshot before kickoff and --settle after)")
    return "\n".join(out)


# =====================================================================
# ---- bets ledger (real money) ----
# =====================================================================

BET_COLS = ["logged_at", "date", "game_id", "matchup", "kind", "side", "line", "price",
            "stake", "result", "profit", "settled_at", "note"]


def log_bet(game: Game, kind: str, side: str, line: Optional[float], price: int,
            stake: float, note: str = "", path: str = BETS_CSV) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BET_COLS)
        if new:
            w.writeheader()
        w.writerow({"logged_at": dt.datetime.now(LOCAL_TZ).isoformat(timespec="minutes"),
                    "date": game.kick_local.date().isoformat(), "game_id": game.id,
                    "matchup": game.short, "kind": kind, "side": side, "line": line,
                    "price": price, "stake": stake, "result": "", "profit": "",
                    "settled_at": "", "note": note})


def _side_is_home(side: str, home: str, away: str) -> Optional[bool]:
    """Match a ledger side ("Missouri State", "Missouri State Bears", "home") to the
    home/away team. Prefix and substring both count so the short name the user types
    matches the full ESPN display name. Ambiguous (matches both) or no match -> None,
    never a silent guess: the first version of this compared the short name to the
    full name, never matched, and graded every ticket as the away side."""
    s = side.strip().lower()
    if s == "home":
        return True
    if s == "away":
        return False
    h, a = home.lower(), away.lower()
    hm = s == h or h.startswith(s) or s in h
    am = s == a or a.startswith(s) or s in a
    if hm and not am:
        return True
    if am and not hm:
        return False
    return None


def settle_bets(conn: sqlite3.Connection, path: str = BETS_CSV) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    n = 0
    for r in rows:
        if r.get("result"):
            continue
        g = conn.execute("SELECT home_id,home,away,home_score,away_score,completed FROM games "
                         "WHERE id=?", (r["game_id"],)).fetchone()
        if not g or not g[5] or g[3] is None:
            continue
        home_id, home, away, hs, as_, _ = g
        kind = r["kind"].lower()
        if kind in ("over", "under"):
            res = _grade(kind, True, _num(r["line"]), hs, as_)
        else:
            side_is_home = _side_is_home(r["side"], home, away)
            if side_is_home is None:
                print(f"settle: can't match side {r['side']!r} to {away} / {home} "
                      f"(game {r['game_id']}); left unsettled", file=sys.stderr)
                continue
            res = _grade(kind, side_is_home, _num(r["line"]), hs, as_)
        r["result"] = res
        r["profit"] = f"{_profit(res, float(r['stake']), int(float(r['price']))):.2f}"
        r["settled_at"] = dt.datetime.now(LOCAL_TZ).isoformat(timespec="minutes")
        n += 1
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BET_COLS)
        w.writeheader()
        w.writerows(rows)
    return n


def show_bets(path: str = BETS_CSV) -> str:
    if not os.path.exists(path):
        return "no bets logged yet (use --bet)"
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = [f"{'date':<11}{'matchup':<16}{'kind':<7}{'side':<22}{'line':>6}{'price':>6}"
           f"{'stake':>7}{'res':>4}{'profit':>8}{'run':>8}"]
    run = staked = 0.0
    for r in rows:
        p = float(r["profit"]) if r["profit"] else 0.0
        run += p
        if r["result"]:
            staked += float(r["stake"])
        out.append(f"{r['date']:<11}{r['matchup'][:15]:<16}{r['kind']:<7}{r['side'][:21]:<22}"
                   f"{r['line'] or '':>6}{r['price']:>6}{float(r['stake']):>7.2f}"
                   f"{r['result'] or '·':>4}{(r['profit'] or '·'):>8}{run:>+8.2f}")
    roi = (run / staked * 100) if staked else 0.0
    out.append(f"\nsettled stake ${staked:.2f} · net {run:+.2f} · ROI {roi:+.1f}%")
    return "\n".join(out)


# =====================================================================
# ---- rendering ----
# =====================================================================

def _c(s: str, color: str, on: bool = True) -> str:
    if not on:
        return s
    codes = {"green": "32", "cyan": "36", "red": "31", "dim": "2", "bold": "1", "yellow": "33"}
    return f"\033[{codes[color]}m{s}\033[0m"


def _tag_color(s: Signal) -> str:
    if s.steam == "against":
        return "red"
    return {2: "green", 1: "cyan"}.get(s.strength, "dim")


def team_label(t: TeamSide) -> str:
    r = f"#{t.rank} " if t.rank else ""
    return f"{r}{t.abbr}"


def render_board(games: list[Game], bankroll: float, color: bool,
                 only_flagged: bool = False) -> str:
    hdr = (f"{'Kick CT':<12}{'Matchup':<24}{'DK spread (open)':<21}{'FPI mrg':>8}{'Δ':>6}{'Cov%':>6}"
           f"  {'ML home/away':<14}{'FPI%':>6}  {'Total (open)':<13}  Tag / $Bet")
    out = [hdr, "-" * len(hdr)]
    for g in games:
        ss, ms, ts, lm = spread_signal(g), ml_signal(g), total_move_signal(g), spread_move_signal(g)
        flagged = any(s and s.strength for s in (ss, ms, ts, lm))
        if only_flagged and not flagged:
            continue
        kick = g.kick_local.strftime("%a %I:%M%p").replace(":00", "").lower()
        mu = f"{team_label(g.away)} @ {team_label(g.home)}{' (N)' if g.neutral else ''}"
        sp_open = fmt_spread(g.home.spread_open)
        sp_cur = fmt_spread(g.home.spread)
        sp = f"{g.home.abbr} {sp_cur}" + (f" ({sp_open})" if g.home.spread_open not in (None, g.home.spread) else "")
        fm = "—" if g.home.fpi_margin is None else f"{g.home.fpi_margin:+.1f}"
        delta = "—" if not ss else f"{ss.edge:+.1f}"[1:]
        cov = "—" if not ss else f"{ss.truth_p*100:.0f}"
        ml = f"{fmt_ml(g.home.ml)}/{fmt_ml(g.away.ml)}"
        fp = "—" if g.home.fpi_win_p is None else f"{g.home.fpi_win_p*100:.0f}"
        tot = "—" if g.total is None else f"{g.total:g}" + (
            f" ({g.total_open:g})" if g.total_open not in (None, g.total) else "")
        tags = []
        for s in (ss, ms, ts, lm):
            if not s or not s.strength:
                continue
            txt = s.label
            if s.side:
                txt += f" {s.side.abbr}"
                if s.kind == "spread":
                    txt += f" {fmt_spread(s.line)}"
                elif s.kind == "spread-move":
                    txt += f" {s.key_note}"
                elif s.kind == "ml":
                    txt += f" {fmt_ml(s.price)} (+{s.edge:.0f}%)"
            else:
                txt += f" {'▲' if s.edge > 0 else '▼'}{abs(s.edge):g}"
            if s.steam:
                txt += f" [steam {s.steam}]"
            if s.key_note:
                txt += f" [{s.key_note}]"
            st = stake_for(s, bankroll)
            if st:
                txt += f" ${st:.0f}"
            txt += " " + " ".join(findings_warnings(s))
            tags.append(_c(txt.strip(), _tag_color(s), color))
        row = (f"{kick:<12}{mu:<24}{sp:<21}{fm:>8}{delta:>6}{cov:>6}  {ml:<14}{fp:>6}  {tot:<13}  "
               + " · ".join(tags))
        if g.status != "pre":
            row += _c(f"  [{g.status} {g.away.score}-{g.home.score}]", "dim", color)
        out.append(row)
    return "\n".join(out)


def ranked_signals(games: list[Game], include_done: bool = False) -> list[Signal]:
    sigs: list[Signal] = []
    for g in games:
        if g.status != "pre" and not include_done:
            continue
        for s in (spread_signal(g), ml_signal(g), total_move_signal(g), spread_move_signal(g)):
            if s and s.strength:
                sigs.append(s)
    # tier: strength desc, then steam-with first, then edge (normalized) desc
    def key(s: Signal):
        steam_rank = {"with": 0, "": 1, "against": 2}[s.steam]
        norm = s.edge / {"spread": SPREAD_STRONG_PTS, "ml": ML_STRONG_PCT}.get(s.kind, 3.0)
        return (-s.strength, steam_rank, -norm)
    return sorted(sigs, key=key)


MAX_PICKS = 5                     # betting_guide §4: 3-5 tickets per Saturday


def pick_signals(games: list[Game], bankroll: float, n: int = MAX_PICKS) -> list[tuple[Signal, float]]:
    """The 'good picks' board: ranked model signals that clear every rule in betting_guide.md.

    ATS / ML only (never market-only signals), strength >= 1, a positive quarter-Kelly stake,
    no ⚠ warning (market-moved-against, long-dog, blowout-number, non-FBS), one ticket per
    game (the higher-ranked signal wins), at most `n` tickets. Empty list = no play today.
    """
    out: list[tuple[Signal, float]] = []
    seen: set[str] = set()
    for s in ranked_signals(games):
        if s.kind not in ("spread", "ml") or s.game.id in seen:
            continue
        if findings_warnings(s):
            continue
        st = stake_for(s, bankroll)
        if not st:
            continue
        out.append((s, st))
        seen.add(s.game.id)
        if len(out) >= n:
            break
    return out


def _pick_row(s: Signal) -> tuple[str, str, str]:
    """(play, why, confirmations) for one pick — shared by the terminal and the report."""
    g = s.game
    if s.kind == "spread":
        play = f"{s.side.name} {fmt_spread(s.line)} ({fmt_ml(s.price)})"
        why = (f"FPI margin {g.home.fpi_margin:+.1f} home vs market {market_margin_home(g):+.1f}"
               f" → Δ{s.edge:.1f}, cover {s.truth_p*100:.0f}%")
    else:
        fair = devig_pair(g.home.ml, g.away.ml)[0 if s.side is g.home else 1]
        play = f"{s.side.name} ML {fmt_ml(s.price)}"
        why = f"FPI {s.truth_p*100:.0f}% vs fair {fair*100:.0f}% → +{s.edge:.0f}%"
    conf = " ".join(x for x in (f"[steam {s.steam}]" if s.steam else "",
                                f"[{s.key_note}]" if s.key_note else "") if x)
    return play, why, conf


def stakes_banner() -> str:
    """One line that says whether the analysis loop has cleared real money. Printed on
    every board/report so nobody mistakes a paper stake for a recommendation."""
    if LIVE_STAKES:
        return f"STAKES: live — a bucket cleared the analysis loop as of {FINDINGS_AS_OF}"
    return (f"STAKES: PAPER ONLY as of {FINDINGS_AS_OF} — no bucket has a 95% CI above zero. "
            "$Bet is what the paper ledger logs, not a recommendation to bet real money.")


def render_picks(games: list[Game], bankroll: float, color: bool) -> str:
    picks = pick_signals(games, bankroll)
    if not picks:
        return stakes_banner() + "\nPicks board: no play clears every rule today — that is a valid answer."
    out = [stakes_banner(),
           f"Picks board — {len(picks)} ticket(s) that clear every rule — bankroll ${bankroll:.0f}, 1/4 Kelly"]
    for i, (s, st) in enumerate(picks, 1):
        g = s.game
        kick = g.kick_local.strftime("%a %I:%M%p").lower()
        play, why, conf = _pick_row(s)
        out.append(_c(f"{i:>2}. {s.label:<11}", _tag_color(s), color) +
                   f"{kick:<12}{g.short:<14} {play}  {why}  ${st:.0f} {conf}")
    return "\n".join(out)


def render_top(games: list[Game], bankroll: float, color: bool, n: int = 12) -> str:
    sigs = ranked_signals(games)[:n]
    if not sigs:
        return "no flagged outliers on this slate"
    out = [stakes_banner(),
           f"Top {len(sigs)} outliers — FPI vs DraftKings — bankroll ${bankroll:.0f}, 1/4 Kelly"]
    for i, s in enumerate(sigs, 1):
        g = s.game
        kick = g.kick_local.strftime("%a %I:%M%p").lower()
        if s.kind == "spread":
            what = (f"{s.side.name} {fmt_spread(s.line)} ({fmt_ml(s.price)})  "
                    f"FPI margin {g.home.fpi_margin:+.1f} home vs market {market_margin_home(g):+.1f}"
                    f" → Δ{s.edge:.1f} pts, cover {s.truth_p*100:.0f}%")
        elif s.kind == "ml":
            fair = devig_pair(g.home.ml, g.away.ml)[0 if s.side is g.home else 1]
            what = (f"{s.side.name} ML {fmt_ml(s.price)}  FPI {s.truth_p*100:.0f}% vs fair "
                    f"{fair*100:.0f}% → +{s.edge:.0f}%")
        elif s.kind == "spread-move":
            what = (f"{s.side.name} moved {s.key_note} ({s.edge:g} pts toward them) — "
                    f"market news, FPI margin {g.home.fpi_margin:+.1f} home")
        else:
            what = f"total {g.total_open:g} → {g.total:g} ({s.edge:+g}) — no model, movement only"
        st = stake_for(s, bankroll)
        bet = f"  ${st:.0f}" if st else ""
        extras = " ".join(x for x in (f"[steam {s.steam}]" if s.steam else "",
                                      f"[{s.key_note}]" if s.key_note and s.kind != "spread-move" else "",
                                      *findings_warnings(s)) if x)
        out.append(_c(f"{i:>2}. {s.label:<11}", _tag_color(s), color) +
                   f"{kick:<12}{g.short:<14} {what}{bet} {extras}")
    return "\n".join(out)


def _just_win_row(s: Signal) -> tuple[str, str, str, str, str, str]:
    """(kick, play, ml, fpi %, fair %, spread) — shared by the terminal and the report."""
    g = s.game
    kick = g.kick_local.strftime("%I:%M %p").lstrip("0")
    opp = g.away if s.side is g.home else g.home
    where = "vs" if s.side is g.home else "at"
    return (kick, f"{s.side.name} {where} {opp.name}", fmt_ml(s.price), f"{s.truth_p*100:.0f}%",
            s.key_note.replace("fair ", ""), f"{s.side.name} {fmt_spread(s.side.spread)}")


def render_just_win(games: list[Game], color: bool, n: int = JUST_WIN_N) -> str:
    board = just_win_board(games)
    if not board:
        return "Just-win board: no favourite clears the window today"
    out = [f"Just-win board — favourites FPI and DK agree on, {fmt_ml(JUST_WIN_PRICE[0])} to "
           f"{fmt_ml(JUST_WIN_PRICE[1])}, FPI ≥ {JUST_WIN_MIN_P*100:.0f}% — top {n} starred"]
    for i, s in enumerate(board, 1):
        kick, play, ml, p, fair, sp = _just_win_row(s)
        tag = "★" if i <= n else " "
        out.append(_c(f"{tag}{i:>2}. ", "green", color and i <= n) +
                   f"{kick:<9}{play:<48} ML {ml:>5}  FPI {p} vs fair {fair}  "
                   f"EV {just_win_roi(s):+.1f}%  {sp}{'  [steam with]' if s.steam else ''}")
    return "\n".join(out)


# =====================================================================
# ---- report ----
# =====================================================================

def report_path(date: dt.date) -> str:
    """reports/saturday-<date>.md on Saturdays (the historical name); reports/<weekday>-<date>.md otherwise."""
    return os.path.join(REPORTS_DIR, f"{date.strftime('%A').lower()}-{date.isoformat()}.md")


def write_report(games: list[Game], bankroll: float, date: dt.date, now: dt.datetime,
                 paper_summary: str) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = report_path(date)
    sigs = ranked_signals(games)
    picks = pick_signals(games, bankroll)
    pre = [g for g in games if g.status == "pre"]
    with_fpi = [g for g in pre if g.home.fpi_margin is not None and g.home.spread is not None]
    L = [f"# CFB Outlier Report — {date.strftime('%A, %B %d, %Y')}",
         "",
         f"**Generated:** {now.strftime('%Y-%m-%d %I:%M %p %Z')}  ",
         f"**Bankroll assumption:** ${bankroll:.0f} · 1/4 Kelly · ${MIN_TICKET:.0f} minimum ticket  ",
         f"**Slate:** {len(games)} games, {len(pre)} not yet kicked, {len(with_fpi)} with both a DK line and an FPI projection  ",
         "**Lines:** DraftKings via ESPN (open → current). **Model:** ESPN FPI game predictor (win prob + predicted margin).",
         "",
         f"> **{stakes_banner()}**",
         ">",
         "> **Honest expectations.** On 578 settled paper bets (2025 season backfilled + 2026 live) the "
         "flagged spread side covers 49.3% ATS against a 52.4% break-even, and the bigger the FPI-vs-DK gap "
         "the worse it does (Δ8+: 42.5%). Every flagged play is written to `paper_bets` in `data.db`; the "
         "paper ledger (below) and `analysis/` are the only things that can turn stakes back on.",
         "",
         "## 0. Picks board",
         "",
         "Tickets that clear **every** rule in `betting_guide.md`: FBS vs FBS, ATS or ML only, "
         "no ⚠ flag, positive quarter-Kelly stake, one per game, max 5. Everything else on this "
         "page is context.",
         "",
         "| # | Tag | Kick (CT) | Game | Play | Why | $Bet | Confirmations |",
         "|---|---|---|---|---|---|---|---|"]
    for i, (s, st) in enumerate(picks, 1):
        play, why, conf = _pick_row(s)
        L.append(f"| {i} | **{s.label}** | {s.game.kick_local.strftime('%I:%M %p').lstrip('0')} | "
                 f"{s.game.short} | {play} | {why} | ${st:.0f} | {conf or '—'} |")
    if not picks:
        L.append("| — | no ticket clears every rule today | | | | | | |")
    board = just_win_board(games)
    L += ["",
          "## 0b. Just-win board — winners at a decent line",
          "",
          f"The opposite question from the rest of this page: not *where does FPI disagree with "
          f"the market* but *who is going to win, at a price worth holding*. A side qualifies when "
          f"FPI gives it ≥ {JUST_WIN_MIN_P*100:.0f}% to win, the market also has it favoured, the "
          f"moneyline sits between {fmt_ml(JUST_WIN_PRICE[0])} and {fmt_ml(JUST_WIN_PRICE[1])}, "
          f"FPI is ahead of the de-vigged price by at most +{JUST_WIN_MAX_EDGE_PCT:.0f}% (bigger "
          f"gaps are where the ledger bleeds), it is +EV at the vigged price, both teams are FBS "
          f"and the line has not moved against it. Ranked by FPI win probability. *EV* is expected return per $1 at FPI's "
          f"number. Logged to `paper_bets` as kind `just-win` so `analysis/01` can grade the "
          f"bucket on its own; it has **no track record yet**.",
          "",
          f"### Top {JUST_WIN_N}",
          "",
          "| # | Kick (CT) | Pick | ML | FPI win % | Market fair % | EV | Spread |",
          "|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(board[:JUST_WIN_N], 1):
        kick, play, ml, p, fair, sp = _just_win_row(s)
        L.append(f"| {i} | {kick} | **{play}** | {ml} | {p} | {fair} | {just_win_roi(s):+.1f}% | {sp} |")
    if not board:
        L.append("| — | no favourite clears the window today | | | | | | |")
    if len(board) > JUST_WIN_N:
        L += ["", "### The rest of the board", "",
              "| # | Kick (CT) | Pick | ML | FPI win % | Market fair % | EV | Spread |",
              "|---|---|---|---|---|---|---|---|"]
        for i, s in enumerate(board[JUST_WIN_N:], JUST_WIN_N + 1):
            kick, play, ml, p, fair, sp = _just_win_row(s)
            L.append(f"| {i} | {kick} | {play} | {ml} | {p} | {fair} | {just_win_roi(s):+.1f}% | {sp} |")
    L += ["",
         "## 1. Ranked outliers",
         "",
         "| # | Tag | Kick (CT) | Game | Play | Model vs market | Cover/Win % | Steam | $Bet | Flags |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(sigs, 1):
        g = s.game
        kick = g.kick_local.strftime("%I:%M %p").lstrip("0")
        if s.kind == "spread":
            play = f"{s.side.name} {fmt_spread(s.line)} ({fmt_ml(s.price)})"
            mvm = f"FPI {g.home.fpi_margin:+.1f} vs mkt {market_margin_home(g):+.1f} (home) → Δ{s.edge:.1f}"
            pct = f"{s.truth_p*100:.0f}%"
        elif s.kind == "ml":
            fair = devig_pair(g.home.ml, g.away.ml)[0 if s.side is g.home else 1]
            play = f"{s.side.name} ML {fmt_ml(s.price)}"
            mvm = f"FPI {s.truth_p*100:.0f}% vs fair {fair*100:.0f}% → +{s.edge:.0f}%"
            pct = f"{s.truth_p*100:.0f}%"
        elif s.kind == "spread-move":
            play = f"{s.side.name} {s.key_note}"
            mvm = f"line moved {s.edge:g} pts toward them — news, not model"
            pct = "—"
        else:
            play = f"Total {g.total:g} (opened {g.total_open:g})"
            mvm = f"moved {s.edge:+g} — movement only, no model"
            pct = "—"
        st = stake_for(s, bankroll)
        flags = " ".join(findings_warnings(s) + ([f"[{s.key_note}]"] if s.key_note and s.kind != "spread-move" else []))
        L.append(f"| {i} | **{s.label}** | {kick} | {g.short} | {play} | {mvm} | {pct} | "
                 f"{s.steam or '—'} | {('$%.0f' % st) if st else '—'} | {flags or '—'} |")
    if not sigs:
        L.append("| — | no flagged outliers | | | | | | | | |")
    L += ["",
          "**How to read it.** *Δ* is |FPI predicted margin − market margin| in points. "
          "*Cover %* converts that gap to a cover probability assuming a 13.5-pt margin std-dev. "
          "*Steam* says whether the DK line has moved ≥1.5 pts since open **with** the model (market agreeing) "
          "or **against** it (market disagreeing — usually the market knows something: injury, weather, QB). "
          "*$Bet* is quarter-Kelly on the model's probability; it is a ceiling, not a target.",
          "",
          "## 2. Full board",
          "",
          "| Kick (CT) | Game | DK spread (open) | FPI home margin | Δ | ML H/A | FPI home % | Total (open) | Status |",
          "|---|---|---|---|---|---|---|---|---|"]
    for g in games:
        ss = spread_signal(g)
        kick = g.kick_local.strftime("%I:%M %p").lstrip("0")
        sp = f"{g.home.abbr} {fmt_spread(g.home.spread)}" + (
            f" ({fmt_spread(g.home.spread_open)})" if g.home.spread_open not in (None, g.home.spread) else "")
        tot = "—" if g.total is None else f"{g.total:g}" + (
            f" ({g.total_open:g})" if g.total_open not in (None, g.total) else "")
        status = "pre" if g.status == "pre" else f"{g.status} {g.away.score}-{g.home.score}"
        L.append(f"| {kick} | {team_label(g.away)} @ {team_label(g.home)}{' (N)' if g.neutral else ''} | {sp} | "
                 f"{'—' if g.home.fpi_margin is None else f'{g.home.fpi_margin:+.1f}'} | "
                 f"{'—' if not ss else f'{ss.edge:.1f}'} | {fmt_ml(g.home.ml)}/{fmt_ml(g.away.ml)} | "
                 f"{'—' if g.home.fpi_win_p is None else f'{g.home.fpi_win_p*100:.0f}%'} | {tot} | {status} |")
    L += ["", "## 3. Paper ledger to date", "", "```", paper_summary, "```", "",
          "## 4. Discipline", "",
          "- Max 3–5 tickets per Saturday. Quarter-Kelly stakes are ceilings.",
          "- Skip anything flagged ⚠market-moved-against unless you know *why* the line moved and disagree.",
          "- Prefer plays where the line crosses a key number (3, 7) in your favor; avoid buying through one.",
          "- Log every real ticket with `--bet` the moment you place it; run `--settle` Sunday morning.",
          "- Re-run `--snapshot` Saturday morning so the closing-line snapshot is in the DB for CLV tracking.",
          ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


# =====================================================================
# ---- main ----
# =====================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="College football outlier finder (FPI vs DraftKings).")
    p.add_argument("--date", help="YYYY-MM-DD (default: next Saturday)")
    p.add_argument("--top", type=int, metavar="N", help="ranked outliers only")
    p.add_argument("--flagged", action="store_true", help="board rows with a tag only")
    p.add_argument("--picks", action="store_true", help="picks board: only tickets that clear every rule")
    p.add_argument("--bankroll", type=float, default=100.0)
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--snapshot", action="store_true", help="persist lines/FPI to data.db + paper-log flagged plays")
    p.add_argument("--settle", action="store_true", help="refresh scores, settle paper + real bets")
    p.add_argument("--backfill", action="store_true",
                   help="with --date in the past: snapshot closers + pre-game FPI, paper-log, settle")
    p.add_argument("--paper-show", action="store_true", help="paper-bet ledger summary")
    p.add_argument("--report", action="store_true", help="write reports/<weekday>-<date>.md")
    p.add_argument("--db", default=DEFAULT_DB)
    # real-money ledger
    p.add_argument("--bet", metavar="GAME_ID", help="log a placed bet (with --kind/--side/--line/--price/--stake)")
    p.add_argument("--kind", choices=["spread", "ml", "over", "under"])
    p.add_argument("--side", help="team name/abbr (ignored for over/under)")
    p.add_argument("--line", type=float)
    p.add_argument("--price", type=int, default=SPREAD_PRICE)
    p.add_argument("--stake", type=float)
    p.add_argument("--note", default="")
    p.add_argument("--bets-show", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if os.name == "nt":
        os.system("")  # enable ANSI colors in the Windows console
    a = build_parser().parse_args(argv)
    color = not a.no_color and sys.stdout.isatty()
    now = dt.datetime.now(LOCAL_TZ)
    date = dt.date.fromisoformat(a.date) if a.date else next_saturday()

    if a.bets_show:
        print(show_bets())
        return 0
    if a.paper_show:
        print(db_paper_summary(db_connect(a.db)))
        return 0

    print(f"fetching {date} slate…", file=sys.stderr)
    games = fetch_scoreboard(date)
    enrich_games(games)
    try:
        apply_powerindex(games, fetch_powerindex())
    except Exception as e:  # noqa: BLE001
        print(f"powerindex unavailable: {e}", file=sys.stderr)
    print(f"{len(games)} games · {sum(g.home.spread is not None for g in games)} with DK line · "
          f"{sum(g.home.fpi_margin is not None for g in games)} with FPI predictor", file=sys.stderr)

    if a.bet:
        g = next((x for x in games if x.id == a.bet), None)
        if not g or not a.kind or a.stake is None:
            print("--bet needs a game id from this slate plus --kind and --stake", file=sys.stderr)
            return 2
        side = a.side or a.kind
        log_bet(g, a.kind, side, a.line, a.price, a.stake, a.note)
        print(f"logged: {g.short} {a.kind} {side} {a.line} @ {a.price} for ${a.stake:.2f}")
        return 0

    conn = db_connect(a.db)
    if a.snapshot or a.settle or a.report:
        n = db_persist(conn, games, now)
        print(f"snapshot: {n} line rows → {a.db}", file=sys.stderr)
    if a.backfill:
        db_persist(conn, games, now)
        n = db_paper_log(conn, ranked_signals(games, include_done=True), a.bankroll, now, backfill=True)
        print(f"backfill {date}: paper-logged {n} flagged plays", file=sys.stderr)
        a.settle = True
    elif a.snapshot:
        n = db_paper_log(conn, ranked_signals(games), a.bankroll, now)
        m = db_paper_log(conn, just_win_board(games), a.bankroll, now)
        print(f"paper-logged {n} new flagged plays + {m} just-win sides", file=sys.stderr)
    if a.settle:
        n, staked, profit = db_settle_paper(conn)
        print(f"settled {n} paper bets: staked {staked:.2f}, profit {profit:+.2f}")
        m = settle_bets(conn)
        print(f"settled {m} real bets in {BETS_CSV}")
        print(db_paper_summary(conn))
        return 0

    if a.picks:
        print(render_picks(games, a.bankroll, color))
    elif a.top:
        print(render_top(games, a.bankroll, color, a.top))
        print()
        print(render_just_win(games, color))
    else:
        print(render_board(games, a.bankroll, color, only_flagged=a.flagged))
        print()
        print(render_top(games, a.bankroll, color, 10))
        print()
        print(render_picks(games, a.bankroll, color))
        print()
        print(render_just_win(games, color))
    if a.report:
        path = write_report(games, a.bankroll, date, now, db_paper_summary(conn))
        print(f"\nreport → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
