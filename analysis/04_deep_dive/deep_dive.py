"""Where exactly does the paper ledger win and lose? Flat-bet hit rate + ROI by the
dimensions a play rule can act on: edge band, ML price band, |spread|, dog/fav,
home/away, and the model's own truth_p vs what actually happened (calibration).

This is the script that motivated the 2026-09-20 rule changes (SPREAD_OVERREACH_PTS,
ML_DEAD_ZONE, LIVE_STAKES=False). Wilson 95% intervals on hit rate; ROI is flat $1.

Output: analysis/_out/deep_dive.csv + console tables.
Mirrors deep_dive.R — keep them in lockstep. Point estimates must match exactly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared.load_data import BREAK_EVEN_110, load_games, load_paper_bets, wilson

OUT_DIR = Path(__file__).resolve().parents[1] / "_out"
OUT_CSV = OUT_DIR / "deep_dive.csv"
MIN_N = 5

SPREAD_EDGE_BINS = [0, 3, 5, 8, 1000]
SPREAD_EDGE_LABELS = ["0-3", "3-5", "5-8", "8+"]
ABS_SPREAD_BINS = [-1, 3, 7, 14, 21, 28, 1000]
ABS_SPREAD_LABELS = ["0-3", "3-7", "7-14", "14-21", "21-28", "28+"]
ML_PRICE_BINS = [-100000, -200, -110, 99, 150, 200, 250, 100000]   # right-closed: +100..150 inclusive = ML_DEAD_ZONE
ML_PRICE_LABELS = ["<-200", "-200..-110", "-110..+100", "+100..150", "+150..200", "+200..250", ">+250"]
ML_EDGE_BINS = [0, 8, 15, 20, 30, 50, 100000]
ML_EDGE_LABELS = ["0-8", "8-15", "15-20", "20-30", "30-50", "50+"]
TRUTH_BINS = [0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
TRUTH_LABELS = ["<.4", ".4-.5", ".5-.6", ".6-.7", ".7-.8", ".8+"]


def prepare() -> pd.DataFrame:
    b = load_paper_bets()
    if b.empty:
        return b
    g = load_games(fbs_only=False)[["game_id", "home", "away", "home_spread", "home_fpi", "away_fpi"]]
    b = b.merge(g, on="game_id", how="left")
    b = b[(b.strength >= 1) & b.home_fpi.notna() & b.away_fpi.notna()].copy()
    b["season"] = b.date.str[:4]
    b["side_home"] = np.where(b.side == b.home, "home", "away")
    b["truth_band"] = pd.cut(b.truth_p, TRUTH_BINS, labels=TRUTH_LABELS, right=False).astype(str)
    sp = b.kind == "spread"
    b.loc[sp, "edge_band"] = pd.cut(b.loc[sp, "edge"], SPREAD_EDGE_BINS, labels=SPREAD_EDGE_LABELS,
                                    right=False).astype(str)
    b.loc[sp, "abs_spread"] = pd.cut(b.loc[sp, "home_spread"].abs(), ABS_SPREAD_BINS,
                                     labels=ABS_SPREAD_LABELS, right=False).astype(str)
    side_is_home = b.side == b.home
    b.loc[sp, "dog_fav"] = np.where((b.home_spread > 0) == side_is_home, "dog", "fav")[sp]
    ml = b.kind == "ml"
    b.loc[ml, "price_band"] = pd.cut(b.loc[ml, "price"], ML_PRICE_BINS, labels=ML_PRICE_LABELS,
                                     right=True).astype(str)
    b.loc[ml, "edge_band"] = pd.cut(b.loc[ml, "edge"], ML_EDGE_BINS, labels=ML_EDGE_LABELS,
                                    right=False).astype(str)
    return b


ORDER = {"edge_band": SPREAD_EDGE_LABELS + ML_EDGE_LABELS, "abs_spread": ABS_SPREAD_LABELS,
         "price_band": ML_PRICE_LABELS, "truth_band": TRUTH_LABELS}


def table(df: pd.DataFrame, by: str, kind: str, question: str) -> list[dict]:
    rows = []
    col = df[by].astype(str)
    if by in ORDER:
        col = pd.Categorical(col, categories=[x for x in ORDER[by] if x in set(col)], ordered=True)
    for key, grp in df.groupby(col, observed=True):
        n = len(grp)
        if n < MIN_N:
            continue
        w = int((grp.result == "W").sum())
        dec = n - int((grp.result == "P").sum())
        p, lo, hi = wilson(w, dec)
        rows.append({"kind": kind, "dimension": by, "bucket": str(key), "n": n, "wins": w,
                     "hit_rate": p, "hit_lo": lo, "hit_hi": hi, "roi_flat": float(grp.pnl_flat.mean())})
    print(f"\n{question}")
    print(f"  {'bucket':<12}{'n':>5}{'W':>5}{'hit%':>8}{'ROI':>8}   95% CI on hit")
    for r in rows:
        flag = ""
        if kind == "spread":
            flag = "  losing (CI hi < BE)" if r["hit_hi"] < BREAK_EVEN_110 else ""
        print(f"  {r['bucket']:<12}{r['n']:>5}{r['wins']:>5}{100 * r['hit_rate']:>7.1f}%"
              f"{100 * r['roi_flat']:>+7.1f}%   [{100 * r['hit_lo']:.0f}, {100 * r['hit_hi']:.0f}]{flag}")
    return rows


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    b = prepare()
    if b.empty:
        print("no settled paper bets yet — run cfb_edge.py --snapshot / --settle (or --backfill)")
        return
    rows: list[dict] = []
    for kind in ("spread", "ml"):
        d = b[b.kind == kind]
        print(f"\n=== {kind.upper()} — {len(d)} settled flagged paper bets "
              f"(FBS vs FBS, strength ≥ 1); break-even at -110 = {100 * BREAK_EVEN_110:.1f}%")
        rows += table(d, "strength", kind, "A. by strength (1 = lean/value, 2 = STRONG)")
        rows += table(d, "season", kind, "B. by season")
        rows += table(d, "edge_band", kind, "C. by edge band (does a bigger FPI-vs-DK gap win more?)")
        if kind == "spread":
            rows += table(d, "abs_spread", kind, "D. by |spread|")
            rows += table(d, "dog_fav", kind, "E. FPI side is the dog or the favourite")
        else:
            rows += table(d, "price_band", kind, "D. by moneyline price band")
        rows += table(d, "side_home", kind, "F. home vs away")
        rows += table(d, "truth_band", kind, "G. model truth_p vs actual hit rate (calibration)")
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False)
    print(f"\nwrote {OUT_CSV}")


if __name__ == "__main__":
    main()
