#!/usr/bin/env python3
# Copyright (c) 2026 William Brooks Parker. All rights reserved. Proprietary — see LICENSE.
"""
soccer_edge.py — pro soccer outlier finder, every league ESPN/DraftKings prices.

Sister module of cfb_edge.py, same philosophy, one difference: ESPN has no predictor
for soccer, so the model side is an Elo table this file builds itself from a year of
ESPN results across every league (--build-elo). Elo -> home/draw/away probabilities
-> compared with the de-vigged DraftKings three-way moneyline. Everything is
persisted to soccer.db and paper-logged so the analysis loop can grade it honestly.

Reuses cfb_edge for the odds math, Kelly sizing, the PAPER ONLY banner and LIVE_STAKES.

Sections:
  ---- constants / models ----
  ---- ESPN adapters ----
  ---- Elo ----
  ---- signals ----
  ---- SQLite ----
  ---- rendering / report ----
  ---- main ----
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

import requests

import cfb_edge as ce

SOCCER_DB = "soccer.db"
LEAGUE_CACHE = "soccer_leagues.json"
REPORTS_DIR = ce.REPORTS_DIR
UA = ce.UA
LOCAL_TZ = ce.LOCAL_TZ

ESPN_ALL = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
ESPN_LEAGUES = "https://sports.core.api.espn.com/v2/sports/soccer/leagues"
ESPN_ODDS = ("https://sports.core.api.espn.com/v2/sports/soccer/leagues/{league}/events/{id}/"
             "competitions/{id}/odds/100")

# ---- decision constants (soccer). Untested until the paper ledger says otherwise. ----
ELO_START = 1500.0
ELO_K = 20.0                      # league match
ELO_K_FRIENDLY = 10.0             # friendlies / pre-season: half weight
ELO_HFA = 60.0                    # home advantage in Elo points (neutral sites: 0)
ELO_MIN_MATCHES = 8               # fewer rated matches on either side -> unrated, never staked
DRAW_BASE = 0.26                  # P(draw) when the sides are equal; shrinks as they diverge
ML_EDGE_PCT = ce.ML_EDGE_PCT      # same tiers as CFB ML: +8% value, +20% STRONG
ML_STRONG_PCT = ce.ML_STRONG_PCT
ML_MAX_PRICE = ce.ML_MAX_PRICE
ML_MIN_PRICE = ce.ML_MIN_PRICE
ML_LONG_DOG = 250                 # beyond this a dog is capped at "value"
PROB_MOVE_PP = 5.0                # open->current implied-prob move worth surfacing
TOTAL_MOVE = 0.5                  # goals
ELO_BUILD_SINCE = "2025-07-01"    # default first day of results for --build-elo
FINDINGS_AS_OF = "2026-09-20"     # no soccer analysis run yet — every constant above is a prior


# =====================================================================
# ---- models ----
# =====================================================================

@dataclass
class Side:
    id: str
    abbr: str
    name: str
    score: Optional[int] = None
    ml: Optional[int] = None
    ml_open: Optional[int] = None
    spread: Optional[float] = None
    spread_open: Optional[float] = None
    spread_price: Optional[int] = None
    elo: Optional[float] = None
    elo_n: int = 0
    win_p: Optional[float] = None        # model probability this side wins


@dataclass
class Match:
    id: str
    league: str                          # ESPN slug, e.g. eng.1
    league_name: str
    name: str
    short: str
    kickoff: dt.datetime
    status: str                          # pre / in / post
    completed: bool
    neutral: bool
    home: Side
    away: Side
    draw_ml: Optional[int] = None
    draw_ml_open: Optional[int] = None
    draw_p: Optional[float] = None       # model
    total: Optional[float] = None
    total_open: Optional[float] = None
    provider: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def kick_local(self) -> dt.datetime:
        return self.kickoff.astimezone(LOCAL_TZ)

    def side(self, key: str) -> Optional[Side]:
        return self.home if key == "home" else self.away if key == "away" else None


@dataclass
class Signal:
    match: Match
    kind: str                            # ml3 / prob-move / total-move
    pick: str                            # home / draw / away
    label: str
    strength: int
    edge: float
    truth_p: Optional[float]
    price: Optional[int]
    line: Optional[float] = None
    note: str = ""

    @property
    def pick_name(self) -> str:
        return "Draw" if self.pick == "draw" else self.match.side(self.pick).name


def devig3(h: Optional[int], d: Optional[int], a: Optional[int]) -> tuple[Optional[float], ...]:
    ps = [ce.implied_prob(x) for x in (h, d, a)]
    if any(p is None for p in ps):
        return None, None, None
    s = sum(ps)
    return tuple(p / s for p in ps)


def elo_expected(home_elo: float, away_elo: float, neutral: bool = False) -> float:
    dr = home_elo - away_elo + (0.0 if neutral else ELO_HFA)
    return 1.0 / (1.0 + 10.0 ** (-dr / 400.0))


def elo_probs(home_elo: float, away_elo: float, neutral: bool = False) -> tuple[float, float, float]:
    """Elo expected score -> (home, draw, away). Draw mass is DRAW_BASE at parity and
    shrinks as 4E(1-E); the remainder is split so the expected score is preserved."""
    e = elo_expected(home_elo, away_elo, neutral)
    pd_ = DRAW_BASE * 4.0 * e * (1.0 - e)
    ph = max(0.0, e - pd_ / 2.0)
    pa = max(0.0, 1.0 - e - pd_ / 2.0)
    s = ph + pd_ + pa
    return ph / s, pd_ / s, pa / s


def _ml(s) -> Optional[int]:
    if s is None or s == "":
        return None
    if str(s).upper() == "EVEN":
        return 100
    v = ce._num(s)
    return None if v is None else int(round(v))


# =====================================================================
# ---- ESPN adapters ----
# =====================================================================

def _get(url: str, params: Optional[dict] = None, timeout: int = 30) -> dict:
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def league_map(path: str = LEAGUE_CACHE, refresh: bool = False) -> dict[str, dict]:
    """ESPN league id -> {slug, name}. Cached on disk; 219 refs resolved in parallel."""
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    items = _get(ESPN_LEAGUES, {"limit": 1000}).get("items", [])

    def one(it):
        try:
            j = _get(it["$ref"])
            return str(j.get("id")), {"slug": j.get("slug"), "name": j.get("name")}
        except Exception:
            return None
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(16) as ex:
        for r in ex.map(one, items):
            if r and r[0] != "None":
                out[r[0]] = r[1]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def _league_of(uid: str, lmap: dict[str, dict]) -> tuple[str, str]:
    lid = next((p[2:] for p in uid.split("~") if p.startswith("l:")), "")
    info = lmap.get(lid)
    return (info["slug"], info["name"]) if info else (f"l{lid}", f"league {lid}")


def fetch_slate(date: dt.date, lmap: Optional[dict] = None) -> list[Match]:
    """Every soccer match ESPN lists on a date, all leagues, with scoreboard odds."""
    lmap = lmap or league_map()
    d = _get(ESPN_ALL, {"dates": date.strftime("%Y%m%d"), "limit": 1000})
    out: list[Match] = []
    for ev in d.get("events", []):
        comp = ev["competitions"][0]
        st = comp.get("status", {}).get("type", {})
        sides = {}
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            sides[c.get("homeAway")] = Side(id=str(t.get("id")), abbr=t.get("abbreviation", "?"),
                                            name=t.get("displayName", t.get("name", "?")),
                                            score=ce._int(c.get("score")))
        if "home" not in sides or "away" not in sides:
            continue
        slug, lname = _league_of(ev.get("uid", ""), lmap)
        m = Match(id=str(ev["id"]), league=slug, league_name=lname, name=ev.get("name", ""),
                  short=ev.get("shortName", ""),
                  kickoff=dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")),
                  status=st.get("state", "pre"), completed=bool(st.get("completed")),
                  neutral=bool(comp.get("neutralSite")), home=sides["home"], away=sides["away"])
        for o in comp.get("odds") or []:
            if not isinstance(o, dict):          # ESPN emits null entries on some days
                continue
            m.provider = (o.get("provider") or {}).get("name")
            m.home.ml = _ml((o.get("homeTeamOdds") or {}).get("moneyLine"))
            m.away.ml = _ml((o.get("awayTeamOdds") or {}).get("moneyLine"))
            m.draw_ml = _ml((o.get("drawOdds") or {}).get("moneyLine"))
            m.total = ce._num(o.get("overUnder"))
            break
        out.append(m)
    out.sort(key=lambda x: (x.kickoff, x.league))
    return out


def _odds_block(b: dict, key: str) -> dict:
    return (b.get(key) or {}) if isinstance(b, dict) else {}


def enrich_odds(matches: list[Match], workers: int = 16) -> None:
    """Per-match DraftKings record from the core API: open / current / close for the
    three-way line, the Asian spread and the total. Finished matches take 'close' as
    current so a backfill snapshot is the closer."""
    def one(m: Match):
        try:
            o = _get(ESPN_ODDS.format(league=m.league, id=m.id))
        except Exception:
            return
        cur_key = "close" if m.completed else "current"
        for side_key, blk in (("home", "homeTeamOdds"), ("away", "awayTeamOdds")):
            s = m.side(side_key)
            tb = o.get(blk) or {}
            cur, opn = _odds_block(tb, cur_key) or _odds_block(tb, "current"), _odds_block(tb, "open")
            s.ml = _ml((cur.get("moneyLine") or {}).get("american")) or s.ml
            s.ml_open = _ml((opn.get("moneyLine") or {}).get("american"))
            s.spread = ce._num((cur.get("pointSpread") or {}).get("american"))
            s.spread_open = ce._num((opn.get("pointSpread") or {}).get("american"))
            s.spread_price = _ml((cur.get("spread") or {}).get("american"))
        top_cur = _odds_block(o, cur_key) or _odds_block(o, "current")
        top_open = _odds_block(o, "open")
        m.draw_ml = _ml((top_cur.get("draw") or {}).get("american")) or m.draw_ml
        m.draw_ml_open = _ml((top_open.get("draw") or {}).get("american"))
        m.total = ce._num((top_cur.get("total") or {}).get("american")) or m.total
        m.total_open = ce._num((top_open.get("total") or {}).get("american"))
        m.provider = (o.get("provider") or {}).get("name") or m.provider
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, matches))


# =====================================================================
# ---- Elo ----
# =====================================================================

def _k_for(league: str) -> float:
    return ELO_K_FRIENDLY if "friendly" in league or "club.friendly" in league else ELO_K


def _gd_mult(gd: int) -> float:
    gd = abs(gd)
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return (11.0 + gd) / 8.0


def elo_update(ratings: dict[str, list], home_id: str, away_id: str, hs: int, as_: int,
               neutral: bool, k: float) -> None:
    """ratings[team] = [elo, n]. World-Football-Elo style: K x goal-diff multiplier."""
    h = ratings.setdefault(home_id, [ELO_START, 0])
    a = ratings.setdefault(away_id, [ELO_START, 0])
    e = elo_expected(h[0], a[0], neutral)
    s = 1.0 if hs > as_ else 0.0 if hs < as_ else 0.5
    delta = k * _gd_mult(hs - as_) * (s - e)
    h[0] += delta
    a[0] -= delta
    h[1] += 1
    a[1] += 1


def elo_as_of(conn: sqlite3.Connection, date: dt.date) -> dict[str, list]:
    """Replay every stored result strictly before `date` (chronological)."""
    ratings: dict[str, list] = {}
    rows = conn.execute("SELECT home_id,away_id,home_score,away_score,neutral,league FROM results "
                        "WHERE date < ? ORDER BY date, id", (date.isoformat(),))
    for hid, aid, hs, as_, neu, lg in rows:
        elo_update(ratings, hid, aid, hs, as_, bool(neu), _k_for(lg))
    return ratings


def apply_elo(matches: list[Match], ratings: dict[str, list]) -> None:
    for m in matches:
        for s in (m.home, m.away):
            r = ratings.get(s.id)
            s.elo, s.elo_n = (r[0], r[1]) if r else (None, 0)
        if m.home.elo is not None and m.away.elo is not None:
            ph, pd_, pa = elo_probs(m.home.elo, m.away.elo, m.neutral)
            m.home.win_p, m.draw_p, m.away.win_p = ph, pd_, pa


def store_results(conn: sqlite3.Connection, matches: list[Match], date: dt.date) -> int:
    n = 0
    for m in matches:
        if not m.completed or m.home.score is None or m.away.score is None:
            continue
        conn.execute("INSERT OR REPLACE INTO results(id,date,league,home_id,home,away_id,away,"
                     "home_score,away_score,neutral) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (m.id, date.isoformat(), m.league, m.home.id, m.home.name, m.away.id,
                      m.away.name, m.home.score, m.away.score, int(m.neutral)))
        n += 1
    conn.execute("INSERT OR REPLACE INTO results_log(date, n) VALUES (?,?)", (date.isoformat(), n))
    conn.commit()
    return n


def build_elo(conn: sqlite3.Connection, since: dt.date, until: dt.date,
              lmap: Optional[dict] = None, log=print) -> int:
    """Fetch every day's results from `since` to `until` (inclusive) that is not already
    in results_log, store them. Ratings are replayed from the table, never stored."""
    lmap = lmap or league_map()
    done = {r[0] for r in conn.execute("SELECT date FROM results_log")}
    total = 0
    d = since
    while d <= until:
        if d.isoformat() not in done:
            try:
                ms = fetch_slate(d, lmap)
            except Exception as e:                    # noqa: BLE001 — one bad day must not kill a 450-day build
                log(f"  {d}: fetch failed ({e}); skipped")
                d += dt.timedelta(days=1)
                continue
            n = store_results(conn, ms, d)
            total += n
            log(f"  {d}: {n} results")
        d += dt.timedelta(days=1)
    return total


def draw_rate(conn: sqlite3.Connection) -> tuple[int, float]:
    n, dr = conn.execute("SELECT COUNT(*), AVG(home_score = away_score) FROM results").fetchone()
    return int(n or 0), float(dr or 0.0)


# =====================================================================
# ---- signals ----
# =====================================================================

def ml3_signal(m: Match) -> Optional[Signal]:
    """Model (Elo) three-way probability vs the de-vigged DraftKings three-way line."""
    fh, fd, fa = devig3(m.home.ml, m.draw_ml, m.away.ml)
    if fh is None or m.home.win_p is None:
        return None
    cands = (("home", m.home.win_p, fh, m.home.ml), ("draw", m.draw_p, fd, m.draw_ml),
             ("away", m.away.win_p, fa, m.away.ml))
    best: Optional[Signal] = None
    for pick, p, fair, price in cands:
        edge = (p - fair) / fair * 100.0
        if edge <= 0:
            continue
        strength = 2 if edge >= ML_STRONG_PCT else 1 if edge >= ML_EDGE_PCT else 0
        note = ""
        if price > ML_MAX_PRICE or price < ML_MIN_PRICE:
            strength = 0
        if price > ML_LONG_DOG:
            strength = min(strength, 1)
            note = "⚠long-dog"
        if pick == "draw":
            strength = min(strength, 1)       # draw mass is the model's weakest assumption
        if m.home.elo_n < ELO_MIN_MATCHES or m.away.elo_n < ELO_MIN_MATCHES:
            strength = 0                      # unrated side: same rule as FCS in CFB
            note = "⚠unrated"
        label = {2: "STRONG 3W", 1: "3W value", 0: ""}[strength]
        s = Signal(m, "ml3", pick, label, strength, edge, p, price, None, note)
        if best is None or s.edge > best.edge:
            best = s
    return best


def prob_move_signal(m: Match) -> Optional[Signal]:
    """Market-only: open->current implied-probability move on the home three-way price."""
    if m.home.ml is None or m.home.ml_open is None or m.away.ml is None or m.away.ml_open is None:
        return None
    if m.draw_ml is None or m.draw_ml_open is None:
        return None
    cur = devig3(m.home.ml, m.draw_ml, m.away.ml)
    opn = devig3(m.home.ml_open, m.draw_ml_open, m.away.ml_open)
    if cur[0] is None or opn[0] is None:
        return None
    moves = {"home": (cur[0] - opn[0]) * 100, "draw": (cur[1] - opn[1]) * 100, "away": (cur[2] - opn[2]) * 100}
    pick = max(moves, key=moves.get)
    mv = moves[pick]
    if mv < PROB_MOVE_PP:
        return None
    return Signal(m, "prob-move", pick, "prob move", 1, mv, None, None, None,
                  f"{ce.fmt_ml(m.side(pick).ml_open if pick != 'draw' else m.draw_ml_open)}→"
                  f"{ce.fmt_ml(m.side(pick).ml if pick != 'draw' else m.draw_ml)} ({mv:+.1f}pp)")


def total_move_signal(m: Match) -> Optional[Signal]:
    if m.total is None or m.total_open is None or abs(m.total - m.total_open) < TOTAL_MOVE:
        return None
    mv = m.total - m.total_open
    return Signal(m, "total-move", "home", "total move", 1, mv, None, None, m.total,
                  f"{m.total_open:g}→{m.total:g}")


def ranked_signals(matches: list[Match]) -> list[Signal]:
    sigs: list[Signal] = []
    for m in matches:
        if m.status != "pre":
            continue
        for s in (ml3_signal(m), prob_move_signal(m), total_move_signal(m)):
            if s and s.strength:
                sigs.append(s)
    order = {"ml3": 0, "prob-move": 1, "total-move": 2}
    sigs.sort(key=lambda s: (-s.strength, order[s.kind], -s.edge))
    return sigs


def stake_for(sig: Signal, bankroll: float) -> Optional[float]:
    return ce.stake_for(sig, bankroll)      # duck-typed: truth_p / price / strength


def warnings_for(sig: Signal) -> list[str]:
    w = [sig.note] if sig.note.startswith("⚠") else []
    if sig.pick == "draw" and sig.kind == "ml3":
        w.append("⚠draw-model")
    return w


# =====================================================================
# ---- SQLite ----
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  id TEXT PRIMARY KEY, league TEXT, league_name TEXT, name TEXT, date TEXT, kickoff_utc TEXT,
  neutral INTEGER, home_id TEXT, home TEXT, away_id TEXT, away TEXT,
  home_score INTEGER, away_score INTEGER, completed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, taken_at TEXT, provider TEXT,
  home_ml INTEGER, draw_ml INTEGER, away_ml INTEGER,
  home_ml_open INTEGER, draw_ml_open INTEGER, away_ml_open INTEGER,
  home_spread REAL, home_spread_open REAL, total REAL, total_open REAL,
  home_elo REAL, away_elo REAL, home_elo_n INTEGER, away_elo_n INTEGER,
  home_p REAL, draw_p REAL, away_p REAL, backfill INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_snap_match ON snapshots(match_id, taken_at);
CREATE TABLE IF NOT EXISTS paper_bets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, logged_at TEXT, kind TEXT,
  pick TEXT, side TEXT, price INTEGER, truth_p REAL, edge REAL, strength INTEGER,
  stake REAL, result TEXT, profit REAL, backfill INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS results (
  id TEXT PRIMARY KEY, date TEXT, league TEXT, home_id TEXT, home TEXT, away_id TEXT, away TEXT,
  home_score INTEGER, away_score INTEGER, neutral INTEGER
);
CREATE INDEX IF NOT EXISTS ix_results_date ON results(date);
CREATE TABLE IF NOT EXISTS results_log (date TEXT PRIMARY KEY, n INTEGER);
"""
MIGRATIONS: dict[str, list[tuple[str, str]]] = {}


def db_connect(path: str = SOCCER_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def db_persist(conn: sqlite3.Connection, matches: list[Match], now: dt.datetime,
               backfill: bool = False) -> int:
    n = 0
    for m in matches:
        conn.execute(
            "INSERT INTO matches(id,league,league_name,name,date,kickoff_utc,neutral,home_id,home,"
            "away_id,away,home_score,away_score,completed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET home_score=excluded.home_score, "
            "away_score=excluded.away_score, completed=excluded.completed",
            (m.id, m.league, m.league_name, m.name, m.kick_local.date().isoformat(),
             m.kickoff.isoformat(), int(m.neutral), m.home.id, m.home.name, m.away.id, m.away.name,
             m.home.score, m.away.score, int(m.completed)))
        if m.home.ml is None and m.home.win_p is None:
            continue
        conn.execute(
            "INSERT INTO snapshots(match_id,taken_at,provider,home_ml,draw_ml,away_ml,home_ml_open,"
            "draw_ml_open,away_ml_open,home_spread,home_spread_open,total,total_open,home_elo,"
            "away_elo,home_elo_n,away_elo_n,home_p,draw_p,away_p,backfill) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.id, now.isoformat(), m.provider, m.home.ml, m.draw_ml, m.away.ml, m.home.ml_open,
             m.draw_ml_open, m.away.ml_open, m.home.spread, m.home.spread_open, m.total, m.total_open,
             m.home.elo, m.away.elo, m.home.elo_n, m.away.elo_n, m.home.win_p, m.draw_p,
             m.away.win_p, int(backfill)))
        n += 1
    conn.commit()
    return n


def db_paper_log(conn: sqlite3.Connection, sigs: list[Signal], bankroll: float,
                 now: dt.datetime, backfill: bool = False) -> int:
    n = 0
    for s in sigs:
        if s.kind != "ml3" or s.strength == 0 or s.truth_p is None:
            continue
        if conn.execute("SELECT 1 FROM paper_bets WHERE match_id=? AND kind=? AND pick=?",
                        (s.match.id, s.kind, s.pick)).fetchone():
            continue
        conn.execute("INSERT INTO paper_bets(match_id,logged_at,kind,pick,side,price,truth_p,edge,"
                     "strength,stake,backfill) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (s.match.id, now.isoformat(), s.kind, s.pick, s.pick_name, s.price, s.truth_p,
                      s.edge, s.strength, stake_for(s, bankroll) or 0.0, int(backfill)))
        n += 1
    conn.commit()
    return n


def grade3(pick: str, hs: int, as_: int) -> str:
    if hs == as_:
        return "W" if pick == "draw" else "L"
    winner = "home" if hs > as_ else "away"
    return "W" if pick == winner else "L"


def db_settle_paper(conn: sqlite3.Connection) -> tuple[int, float, float]:
    rows = conn.execute("SELECT p.id,p.pick,p.price,p.stake,m.home_score,m.away_score FROM paper_bets p "
                        "JOIN matches m ON m.id=p.match_id WHERE p.result IS NULL AND m.completed=1 "
                        "AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL").fetchall()
    n, staked, profit = 0, 0.0, 0.0
    for pid, pick, price, stake, hs, as_ in rows:
        res = grade3(pick, hs, as_)
        pr = ce._profit(res, stake, price)
        conn.execute("UPDATE paper_bets SET result=?,profit=? WHERE id=?", (res, pr, pid))
        n += 1
        staked += stake
        profit += pr
    conn.commit()
    return n, staked, profit


def db_paper_summary(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT pick,strength,COUNT(*),SUM(result='W'),SUM(result='L'),SUM(stake),"
                        "SUM(profit) FROM paper_bets WHERE result IS NOT NULL GROUP BY pick,strength "
                        "ORDER BY strength DESC, pick").fetchall()
    pend = conn.execute("SELECT COUNT(*) FROM paper_bets WHERE result IS NULL").fetchone()[0]
    out = [f"soccer paper bets — settled by pick/strength (pending: {pend})",
           f"{'pick':<8}{'str':>4}{'n':>5}{'W':>4}{'L':>4}{'staked':>9}{'profit':>9}{'ROI':>8}"]
    for pick, st, n, w, l_, stk, pr in rows:
        roi = (pr / stk * 100) if stk else 0.0
        out.append(f"{pick:<8}{st:>4}{n:>5}{w:>4}{l_:>4}{stk:>9.2f}{pr:>9.2f}{roi:>+7.1f}%")
    if not rows:
        out.append("(nothing settled yet)")
    return "\n".join(out)


def db_update_scores(conn: sqlite3.Connection, matches: list[Match]) -> None:
    for m in matches:
        conn.execute("UPDATE matches SET home_score=?,away_score=?,completed=? WHERE id=?",
                     (m.home.score, m.away.score, int(m.completed), m.id))
    conn.commit()


# =====================================================================
# ---- rendering / report ----
# =====================================================================

def _pct(p: Optional[float]) -> str:
    return "—" if p is None else f"{100 * p:.0f}%"


def render_board(matches: list[Match], bankroll: float, only_flagged: bool = False) -> str:
    hdr = (f"{'Kick CT':<13}{'League':<14}{'Match':<38}{'DK H/D/A':<18}{'Elo H/A':<12}"
           f"{'Model H/D/A':<14}{'Total':<7} Tag / $Bet")
    out = [ce.stakes_banner(), hdr, "-" * len(hdr)]
    for m in matches:
        s = ml3_signal(m)
        pm, tm = prob_move_signal(m), total_move_signal(m)
        flagged = any(x and x.strength for x in (s, pm, tm))
        if only_flagged and not flagged:
            continue
        kick = m.kick_local.strftime("%a %I:%M%p").replace(":00", "").lower()
        dk = f"{ce.fmt_ml(m.home.ml)}/{ce.fmt_ml(m.draw_ml)}/{ce.fmt_ml(m.away.ml)}"
        elo = "/".join("—" if x.elo is None else f"{x.elo:.0f}" for x in (m.home, m.away))
        model = f"{_pct(m.home.win_p)}/{_pct(m.draw_p)}/{_pct(m.away.win_p)}"
        tot = "—" if m.total is None else f"{m.total:g}"
        tag = ""
        if s and s.strength:
            st = stake_for(s, bankroll)
            tag = f"{s.label} {s.pick_name} {ce.fmt_ml(s.price)} (+{s.edge:.0f}%)" + (f" ${st:.0f}" if st else "")
            tag += " " + " ".join(warnings_for(s))
        elif s and s.note:
            tag = s.note
        if pm:
            tag += f" [{pm.pick} {pm.note}]"
        if tm:
            tag += f" [total {tm.note}]"
        score = ""
        if m.status != "pre" and m.home.score is not None:
            score = f" {m.home.score}-{m.away.score}" + (" FT" if m.completed else " live")
        out.append(f"{kick:<13}{m.league[:13]:<14}{(m.short or m.name)[:36] + score:<38}{dk:<18}{elo:<12}"
                   f"{model:<14}{tot:<7} {tag}".rstrip())
    return "\n".join(out)


def render_top(matches: list[Match], bankroll: float, n: int = 12) -> str:
    sigs = ranked_signals(matches)[:n]
    if not sigs:
        return ce.stakes_banner() + "\nno flagged soccer outliers on this slate"
    out = [ce.stakes_banner(), f"Top {len(sigs)} soccer outliers — Elo vs DraftKings 3-way — bankroll ${bankroll:.0f}, 1/4 Kelly"]
    for i, s in enumerate(sigs, 1):
        m = s.match
        kick = m.kick_local.strftime("%a %I:%M%p").lower()
        if s.kind == "ml3":
            fair = devig3(m.home.ml, m.draw_ml, m.away.ml)[("home", "draw", "away").index(s.pick)]
            st = stake_for(s, bankroll)
            what = (f"{s.pick_name} {ce.fmt_ml(s.price)}  model {100 * s.truth_p:.0f}% vs fair {100 * fair:.0f}% "
                    f"→ +{s.edge:.0f}%  " + (f"${st:.0f} " if st else "") + " ".join(warnings_for(s)))
        elif s.kind == "prob-move":
            what = f"{s.pick_name} {s.note} — market news, no model"
        else:
            what = f"total {s.note} — movement only"
        out.append(f"{i:>2}. {s.label:<10} {kick:<12}{m.league[:10]:<11}{(m.short or m.name)[:30]:<31} {what}".rstrip())
    return "\n".join(out)


def report_path(date: dt.date) -> str:
    return os.path.join(REPORTS_DIR, f"soccer-{date.strftime('%A').lower()}-{date.isoformat()}.md")


def write_report(matches: list[Match], bankroll: float, date: dt.date, now: dt.datetime,
                 paper_summary: str, elo_n: int) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = report_path(date)
    sigs = ranked_signals(matches)
    pre = [m for m in matches if m.status == "pre"]
    priced = [m for m in pre if m.home.ml is not None]
    rated = [m for m in priced if m.home.win_p is not None]
    leagues = sorted({m.league_name for m in priced})
    L = [f"# Soccer Outlier Report — {date.strftime('%A, %B %d, %Y')}", "",
         f"**Generated:** {now.strftime('%Y-%m-%d %I:%M %p %Z')}  ",
         f"**{ce.stakes_banner()}**  ",
         f"**Slate:** {len(matches)} matches across every league ESPN lists, {len(pre)} not yet kicked, "
         f"{len(priced)} with a DraftKings three-way price, {len(rated)} with an Elo rating on both sides  ",
         f"**Model:** self-built Elo from {elo_n:,} ESPN results (K={ELO_K:g}, HFA={ELO_HFA:g}, "
         f"draw base {DRAW_BASE:.2f}). No soccer analysis run has happened yet: every threshold is a prior.",
         "", f"**Leagues priced today ({len(leagues)}):** " + ", ".join(leagues), "",
         "## 1. Ranked outliers", "",
         "| # | Tag | Kick (CT) | League | Match | Play | Model vs fair | $Bet (paper) | Flags |",
         "|---|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(sigs, 1):
        m = s.match
        kick = m.kick_local.strftime("%a %I:%M %p")
        if s.kind == "ml3":
            fair = devig3(m.home.ml, m.draw_ml, m.away.ml)[("home", "draw", "away").index(s.pick)]
            st = stake_for(s, bankroll)
            play, mv = f"{s.pick_name} {ce.fmt_ml(s.price)}", f"{100 * s.truth_p:.0f}% vs {100 * fair:.0f}% (+{s.edge:.0f}%)"
            bet = f"${st:.0f}" if st else "—"
        else:
            play, mv, bet = s.pick_name if s.kind == "prob-move" else "total", s.note, "—"
        L.append(f"| {i} | **{s.label}** | {kick} | {m.league_name} | {m.short or m.name} | {play} | {mv} | {bet} | "
                 f"{' '.join(warnings_for(s)) or '—'} |")
    if not sigs:
        L.append("| — | — | — | — | no flagged outliers | | | | |")
    L += ["", "## 2. Full board", "", "```", render_board(matches, bankroll), "```", "",
          "## 3. Soccer paper ledger to date", "", "```", paper_summary, "```", "",
          "> Paper only. The soccer module has no analysis run behind it yet; the ledger above is the "
          "first thing that will say whether Elo-vs-DK is anything. Draw picks are capped at value "
          "because the draw split is the model's weakest assumption."]
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
    p = argparse.ArgumentParser(description="pro soccer outlier finder — Elo vs DraftKings 3-way, every league")
    p.add_argument("--date", help="YYYY-MM-DD (default: today, America/Chicago)")
    p.add_argument("--league", help="comma-separated ESPN slugs to keep, e.g. eng.1,esp.1")
    p.add_argument("--top", type=int, default=0, help="print only the top-N ranked outliers")
    p.add_argument("--flagged", action="store_true", help="board rows with a tag only")
    p.add_argument("--bankroll", type=float, default=100.0)
    p.add_argument("--db", default=SOCCER_DB)
    p.add_argument("--snapshot", action="store_true", help="persist lines/Elo + paper-log flagged plays")
    p.add_argument("--report", action="store_true", help="write reports/soccer-<weekday>-<date>.md")
    p.add_argument("--settle", action="store_true", help="refresh scores for --date, settle paper bets, store results")
    p.add_argument("--backfill", action="store_true", help="past --date: closers + Elo-as-of, paper-log, settle")
    p.add_argument("--build-elo", action="store_true", help="fetch results from --since to --date and store them")
    p.add_argument("--since", default=ELO_BUILD_SINCE, help="first day for --build-elo")
    p.add_argument("--paper-show", action="store_true")
    p.add_argument("--elo-show", type=int, default=0, metavar="N", help="print the top-N Elo table")
    a = p.parse_args(argv)

    now = dt.datetime.now(LOCAL_TZ)
    date = dt.date.fromisoformat(a.date) if a.date else now.date()
    conn = db_connect(a.db)

    if a.paper_show:
        print(db_paper_summary(conn))
        return 0
    if a.build_elo:
        since = dt.date.fromisoformat(a.since)
        print(f"building results table {since} → {date} …")
        n = build_elo(conn, since, date)
        tot, dr = draw_rate(conn)
        print(f"stored {n:,} new results; table has {tot:,}; empirical draw rate {100 * dr:.1f}% "
              f"(DRAW_BASE is {DRAW_BASE:.2f} at parity)")
        return 0
    if a.elo_show:
        r = elo_as_of(conn, date + dt.timedelta(days=1))
        names = dict(conn.execute("SELECT home_id, home FROM results UNION SELECT away_id, away FROM results"))
        for i, (tid, (elo, n)) in enumerate(sorted(r.items(), key=lambda kv: -kv[1][0])[:a.elo_show], 1):
            print(f"{i:>3}. {names.get(tid, tid):<32} {elo:7.1f}  ({n} matches)")
        return 0

    print(f"fetching soccer slate for {date} (all leagues)…", file=sys.stderr)
    lmap = league_map()
    matches = fetch_slate(date, lmap)
    if a.league:
        keep = {x.strip() for x in a.league.split(",")}
        matches = [m for m in matches if m.league in keep]
    enrich_odds(matches)
    ratings = elo_as_of(conn, date)
    apply_elo(matches, ratings)
    priced = sum(1 for m in matches if m.home.ml is not None)
    rated = sum(1 for m in matches if m.home.win_p is not None)
    print(f"{len(matches)} matches · {priced} with a DK 3-way price · {rated} with Elo on both sides "
          f"· {len({m.league for m in matches})} leagues", file=sys.stderr)

    if a.settle or a.backfill:
        store_results(conn, matches, date)
    if a.backfill:
        db_persist(conn, matches, now, backfill=True)
        n = db_paper_log(conn, ranked_signals_all(matches), a.bankroll, now, backfill=True)
        print(f"backfill: paper-logged {n} plays with closers + Elo as of {date}")
    if a.snapshot and not a.backfill:
        n = db_persist(conn, matches, now)
        k = db_paper_log(conn, ranked_signals(matches), a.bankroll, now)
        print(f"snapshot: {n} line rows, {k} new paper plays → {a.db}")
    if a.settle or a.backfill:
        db_update_scores(conn, matches)
        n, staked, profit = db_settle_paper(conn)
        print(f"settled {n} paper bets: staked {staked:.2f}, profit {profit:+.2f}")
        print(db_paper_summary(conn))
        if a.settle and not a.backfill:
            return 0

    if a.top:
        print(render_top(matches, a.bankroll, a.top))
    else:
        print(render_board(matches, a.bankroll, only_flagged=a.flagged))
        print()
        print(render_top(matches, a.bankroll, 10))
    if a.report:
        path = write_report(matches, a.bankroll, date, now, db_paper_summary(conn),
                            conn.execute("SELECT COUNT(*) FROM results").fetchone()[0])
        print(f"\nreport → {path}")
    return 0


def ranked_signals_all(matches: list[Match]) -> list[Signal]:
    """Backfill variant: finished matches count (their 'current' odds are the closers)."""
    sigs = [s for m in matches for s in (ml3_signal(m),) if s and s.strength]
    sigs.sort(key=lambda s: (-s.strength, -s.edge))
    return sigs


if __name__ == "__main__":
    sys.exit(main())
