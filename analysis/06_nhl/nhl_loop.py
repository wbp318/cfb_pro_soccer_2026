"""The NHL prop analysis loop: is the projection any good, and (once the ledger fills) does
betting it against posted lines make money?

  A. Paper ROI by market x side x strength, flat $1, bootstrap 95% CI. Empty until the
     season opens (2026-10-07); prints and moves on.
  B. Slices a rule can act on: edge band, market, side (Wilson CI on hit, flat ROI).
  C. Walk-forward projection calibration on the stored game logs (same recipe as
     nhl_edge.calibrate: shrinkage toward league average, recent-10 tilt, Poisson): log-loss
     vs the naive league-average model and a reliability table for shots / points / saves.
     This runs today on 46k player-games and is the pre-season evidence.

Output: analysis/_out/nhl_roi.csv, nhl_slices.csv, nhl_calibration.csv.
Mirrors nhl_loop.R — keep in lockstep. Point estimates must match; bootstrap CIs may differ
in the last digit.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared.load_data import wilson
from _shared.load_nhl import load_game_logs, load_nhl_bets

OUT_DIR = Path(__file__).resolve().parents[1] / "_out"
MIN_BETS = 10
MIN_N = 5
BOOT_REPS = 5000
SEED = 20261007

# mirror nhl_edge.py — a change there must be reflected here
PRIOR_SEASON = 20252026
SHRINK_GAMES = 20.0
RECENT_GAMES = 10
RECENT_WEIGHT = 0.35
MIN_PRIOR = 10
LINES = {"shots": 2.5, "points": 0.5, "saves": 27.5}

EDGE_BINS, EDGE_LABELS = [0, 8, 15, 30, 50, 100000], ["0-8", "8-15", "15-30", "30-50", "50+"]


def verdict(lo: float, hi: float) -> str:
    return "PROFITABLE (95% CI > 0)" if lo > 0 else "losing (95% CI < 0)" if hi < 0 else "inconclusive"


def boot_ci(x: np.ndarray, rng: np.random.Generator) -> tuple[float, float]:
    idx = rng.integers(0, len(x), size=(BOOT_REPS, len(x)))
    means = x[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def poisson_cdf(k: int, lam: float) -> float:
    if k < 0:
        return 0.0
    term = math.exp(-lam)
    total = term
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def p_over(lam: float, line: float) -> float:
    return 1.0 - poisson_cdf(math.floor(line), lam)


# ---------------------------------------------------------------- A + B (ledger)

def section_a(bets: pd.DataFrame, rng) -> pd.DataFrame:
    print(f"A. Paper ROI — {len(bets):,} settled NHL paper props, flat ROI {100 * bets.pnl_flat.mean():+.1f}%\n")
    groups = [("ALL", "all", "all", bets)]
    groups += [(m, s, str(st), g) for (m, s, st), g in bets.groupby(["market", "side", "strength"])]
    groups += [(m, "any", "any", g) for m, g in bets.groupby("market")]
    groups += [("ALL", s, "any", g) for s, g in bets.groupby("side")]
    rows = []
    for market, side, strength, g in groups:
        if len(g) < MIN_BETS:
            continue
        lo, hi = boot_ci(g.pnl_flat.to_numpy(), rng)
        rows.append({"market": market, "side": side, "strength": strength, "bets": len(g),
                     "wins": int((g.result == "W").sum()), "hit_rate": float((g.result == "W").mean()),
                     "roi_mean": float(g.pnl_flat.mean()), "roi_ci_lo": lo, "roi_ci_hi": hi, "verdict": verdict(lo, hi)})
    out = pd.DataFrame(rows).sort_values("roi_ci_lo", ascending=False)
    print(f"  {'market':<26}{'side':<6}{'str':>4}{'bets':>6}{'wins':>6}{'hit%':>7}{'ROI':>8}{'CI lo':>8}{'CI hi':>8}  verdict")
    for r in out.itertuples():
        print(f"  {r.market:<26}{r.side:<6}{r.strength:>4}{r.bets:>6}{r.wins:>6}{100 * r.hit_rate:>6.1f}%{100 * r.roi_mean:>+7.1f}%"
              f"{100 * r.roi_ci_lo:>+7.1f}%{100 * r.roi_ci_hi:>+7.1f}%  {r.verdict}")
    return out


def slice_table(df: pd.DataFrame, col: str, order: list[str], title: str) -> list[dict]:
    rows = []
    print(f"\n{title}")
    print(f"  {'bucket':<26}{'n':>5}{'W':>5}{'hit%':>8}{'ROI':>8}   95% CI on hit")
    for key in [k for k in order if k in set(df[col])]:
        g = df[df[col] == key]
        if len(g) < MIN_N:
            continue
        w = int((g.result == "W").sum())
        p, lo, hi = wilson(w, len(g) - int((g.result == "P").sum()))
        rows.append({"dimension": col, "bucket": key, "n": len(g), "wins": w, "hit_rate": p, "hit_lo": lo, "hit_hi": hi,
                     "roi_flat": float(g.pnl_flat.mean())})
        print(f"  {key:<26}{len(g):>5}{w:>5}{100 * p:>7.1f}%{100 * g.pnl_flat.mean():>+7.1f}%   [{100 * lo:.0f}, {100 * hi:.0f}]")
    return rows


def section_b(bets: pd.DataFrame) -> pd.DataFrame:
    b = bets[bets.strength >= 1].copy()
    b["edge_band"] = pd.cut(b.edge, EDGE_BINS, labels=EDGE_LABELS, right=False).astype(str)
    rows = slice_table(b, "edge_band", EDGE_LABELS, "B1. by edge band (does a bigger projection-vs-line gap win more?)")
    rows += slice_table(b, "market", sorted(b.market.unique()), "B2. by market")
    rows += slice_table(b, "side", ["over", "under"], "B3. by side")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- C (calibration)

def section_c(logs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for stat, line in LINES.items():
        if stat == "saves":
            d = logs[(logs.started == 1) & logs.saves.notna()]
        else:
            d = logs[logs.toi.notna() & logs[stat].notna()]
        if d.empty:
            continue
        lg = float(d[stat].mean())
        pn = p_over(lg, line)
        bins: dict[int, list] = {}
        ll_m = ll_n = 0.0
        n = 0
        for _, grp in d.groupby("player_id", sort=False):
            vs = grp[stat].to_numpy(dtype=float)
            for i in range(MIN_PRIOR, len(vs)):
                hist = vs[:i]
                w = len(hist) / (len(hist) + SHRINK_GAMES)
                lam = w * hist.mean() + (1 - w) * lg
                rv = hist[-RECENT_GAMES:]
                if len(rv) >= 5:
                    lam = (1 - RECENT_WEIGHT) * lam + RECENT_WEIGHT * rv.mean()
                po = p_over(lam, line)
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
        print(f"\nC. {stat.upper()} over {line:g} — {n:,} player-games ({PRIOR_SEASON}), walk-forward, no prior season")
        print(f"  log-loss: model {ll_m / n:.4f} · naive league-average {ll_n / n:.4f} → "
              f"{'model better' if ll_m < ll_n else 'NAIVE better'} by {abs(ll_m - ll_n) / n:.4f}")
        print(f"  {'P(over) bin':<12}{'n':>7}{'pred':>8}{'obs':>8}{'Δpp':>7}")
        for b in sorted(bins):
            c, sp, sy = bins[b]
            print(f"  {b / 10:.1f}-{(b + 1) / 10:.1f}     {c:>7}{100 * sp / c:>7.1f}%{100 * sy / c:>7.1f}%{100 * (sy - sp) / c:>+6.1f}")
            rows.append({"stat": stat, "line": line, "bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": c, "pred": sp / c, "obs": sy / c})
        rows.append({"stat": stat, "line": line, "bin": "logloss", "n": n, "pred": ll_m / n, "obs": ll_n / n})
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    rng = np.random.default_rng(SEED)
    bets = load_nhl_bets()
    if bets.empty:
        print("no settled NHL paper props yet (season opens 2026-10-07) — skipping A and B")
    else:
        section_a(bets, rng).to_csv(OUT_DIR / "nhl_roi.csv", index=False)
        section_b(bets).to_csv(OUT_DIR / "nhl_slices.csv", index=False)
    logs = load_game_logs(PRIOR_SEASON)
    if logs.empty:
        print("no game logs in nhl.db — run nhl_edge.py --build")
        return
    section_c(logs).to_csv(OUT_DIR / "nhl_calibration.csv", index=False)
    print(f"\nwrote {OUT_DIR / 'nhl_*.csv'}")


if __name__ == "__main__":
    main()
