"""Shared loader for the NHL half of analysis/ (reads nhl.db, never writes).

    from _shared.load_nhl import load_nhl_bets, load_game_logs
    bets = load_nhl_bets()          # one row per settled NHL paper prop
    logs = load_game_logs(season)   # every stored game-log row for a season, chronological

Mirrors load_nhl.R — keep them in lockstep. CFB_NHL_DB overrides the path (CI).
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

DEFAULT_DB = Path(os.environ.get("CFB_NHL_DB") or Path(__file__).resolve().parents[2] / "nhl.db")

_BETS_SQL = """
SELECT p.id, p.game_id, g.date, p.player_id, pl.name, pl.position, p.market, p.side, p.line, p.price,
       p.truth_p, p.edge, p.strength, p.stake, p.book, p.actual, p.result, p.profit
FROM paper_bets p
JOIN games g ON g.id = p.game_id
LEFT JOIN players pl ON pl.id = p.player_id
WHERE p.result IS NOT NULL
"""

_LOGS_SQL = """
SELECT game_id, player_id, season, date, team, opp, home, shots, points, goals, assists, blocked,
       pp_points, saves, shots_against, started, toi
FROM game_logs WHERE season = ? ORDER BY player_id, date, game_id
"""


def _read(sql: str, db_path, params=()) -> pd.DataFrame:
    con = sqlite3.connect(str(db_path))
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def _dec(price: pd.Series) -> np.ndarray:
    p = price.astype(float)
    return np.where(p > 0, 1 + p / 100.0, 1 + 100.0 / p.abs())


def load_nhl_bets(db_path=DEFAULT_DB) -> pd.DataFrame:
    df = _read(_BETS_SQL, db_path)
    df["pnl_flat"] = np.where(df.result == "W", _dec(df.price) - 1.0, np.where(df.result == "L", -1.0, 0.0))
    return df


def load_game_logs(season: int, db_path=DEFAULT_DB) -> pd.DataFrame:
    return _read(_LOGS_SQL, db_path, (int(season),))
