"""Shared loader for the soccer half of analysis/ (reads soccer.db, never writes).

    from _shared.load_soccer import load_soccer_bets, load_soccer_matches, load_results
    bets    = load_soccer_bets()      # one row per settled soccer paper bet (3-way)
    matches = load_soccer_matches()   # one row per settled match w/ last snapshot (closer + Elo probs)
    results = load_results()          # every stored final score, chronological (for Elo refits)

Mirrors load_soccer.R — keep them in lockstep. CFB_SOCCER_DB overrides the path (CI).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_DB = Path(os.environ.get("CFB_SOCCER_DB") or Path(__file__).resolve().parents[2] / "soccer.db")

_MATCHES_SQL = """
WITH last AS (
    SELECT s.*, ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY taken_at DESC) AS rn
    FROM snapshots s
    WHERE home_ml IS NOT NULL AND draw_ml IS NOT NULL AND away_ml IS NOT NULL
)
SELECT m.id AS match_id, m.date, m.league, m.league_name, m.neutral, m.home, m.away,
       m.home_score, m.away_score,
       last.home_ml, last.draw_ml, last.away_ml, last.home_ml_open, last.draw_ml_open, last.away_ml_open,
       last.total, last.total_open, last.home_elo, last.away_elo, last.home_elo_n, last.away_elo_n,
       last.home_p, last.draw_p, last.away_p, last.backfill
FROM matches m
JOIN last ON last.match_id = m.id AND last.rn = 1
WHERE m.completed = 1 AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
"""

_BETS_SQL = """
SELECT p.id, p.match_id, m.date, m.league, p.kind, p.pick, p.side, p.price, p.truth_p, p.edge,
       p.strength, p.stake, p.result, p.profit, p.backfill
FROM paper_bets p JOIN matches m ON m.id = p.match_id
WHERE p.result IS NOT NULL
"""

_RESULTS_SQL = """
SELECT id, date, league, home_id, away_id, home_score, away_score, neutral
FROM results ORDER BY date, id
"""


def _read(sql: str, db_path) -> pd.DataFrame:
    con = sqlite3.connect(str(db_path))
    try:
        return pd.read_sql_query(sql, con)
    finally:
        con.close()


def _dec(price: pd.Series) -> np.ndarray:
    p = price.astype(float)
    return np.where(p > 0, 1 + p / 100.0, 1 + 100.0 / p.abs())


def _implied(price: pd.Series) -> np.ndarray:
    return 1.0 / _dec(price)


def load_soccer_bets(db_path=DEFAULT_DB) -> pd.DataFrame:
    df = _read(_BETS_SQL, db_path)
    df["pnl_flat"] = np.where(df.result == "W", _dec(df.price) - 1.0,
                              np.where(df.result == "L", -1.0, 0.0))
    return df


def load_soccer_matches(db_path=DEFAULT_DB, rated_only: bool = True) -> pd.DataFrame:
    df = _read(_MATCHES_SQL, db_path)
    if rated_only:
        df = df[df.home_p.notna()].copy()
    ih, id_, ia = _implied(df.home_ml), _implied(df.draw_ml), _implied(df.away_ml)
    s = ih + id_ + ia
    df["fair_home"], df["fair_draw"], df["fair_away"] = ih / s, id_ / s, ia / s
    df["outcome"] = np.where(df.home_score > df.away_score, "home",
                             np.where(df.home_score < df.away_score, "away", "draw"))
    return df.reset_index(drop=True)


def load_results(db_path=DEFAULT_DB) -> pd.DataFrame:
    return _read(_RESULTS_SQL, db_path)
