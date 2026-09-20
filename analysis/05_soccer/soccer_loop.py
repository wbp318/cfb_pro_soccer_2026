"""The soccer analysis loop: does Elo-vs-DraftKings 3-way earn anything, and are the
soccer constants in soccer_edge.py the right priors?

  A. Paper ROI by pick x strength, flat $1, bootstrap 95% CI (twin of 01 for soccer).
  B. Slices a rule can act on: edge band, price band (Wilson CI on hit, flat ROI).
  C. Elo calibration on settled snapshots: binned model P(home) / P(draw) vs observed,
     and log-loss of the model vs the de-vigged closer (is the market sharper?).
  D. Refit on the results table: replay Elo for a grid of ELO_HFA, then score every
     DRAW_BASE by 3-way log-likelihood on the held-out second half. Prints the best
     pair next to the constants the tool is using.

Output: analysis/_out/soccer_roi.csv, soccer_slices.csv, soccer_calibration.csv, soccer_fit.csv.
Mirrors soccer_loop.R — keep in lockstep. Point estimates must match; bootstrap CIs may
differ in the last digit.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared.load_data import wilson
from _shared.load_soccer import load_results, load_soccer_bets, load_soccer_matches

OUT_DIR = Path(__file__).resolve().parents[1] / "_out"
MIN_BETS = 10
MIN_N = 5
BOOT_REPS = 5000
SEED = 20260920

# the constants under test (mirror soccer_edge.py — a change there must be reflected here)
ELO_START, ELO_K, ELO_K_FRIENDLY = 1500.0, 20.0, 10.0
ELO_HFA_LIVE, DRAW_BASE_LIVE = 60.0, 0.26
HFA_GRID = [0.0, 30.0, 60.0, 90.0, 120.0]
DRAW_GRID = [0.20, 0.22, 0.24, 0.26, 0.28, 0.30, 0.32]
HOLDOUT_FRAC = 0.5                    # score the second half of the results table

EDGE_BINS, EDGE_LABELS = [0, 8, 15, 20, 30, 50, 100000], ["0-8", "8-15", "15-20", "20-30", "30-50", "50+"]
PRICE_BINS = [-100000, -200, -110, 99, 150, 200, 250, 100000]
PRICE_LABELS = ["<-200", "-200..-110", "-110..+100", "+100..150", "+150..200", "+200..250", ">+250"]
PROB_BINS, PROB_LABELS = [0, .2, .3, .4, .5, .6, .7, .8, 1.01], ["<.2", ".2-.3", ".3-.4", ".4-.5", ".5-.6", ".6-.7", ".7-.8", ".8+"]


def verdict(lo: float, hi: float) -> str:
    return "PROFITABLE (95% CI > 0)" if lo > 0 else "losing (95% CI < 0)" if hi < 0 else "inconclusive"


def boot_ci(x: np.ndarray, rng: np.random.Generator) -> tuple[float, float]:
    idx = rng.integers(0, len(x), size=(BOOT_REPS, len(x)))
    means = x[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


# ---------------------------------------------------------------- A. paper ROI

def section_a(bets: pd.DataFrame, rng) -> pd.DataFrame:
    print(f"A. Paper ROI — {len(bets):,} settled soccer paper bets, backfilled {int(bets['backfill'].sum()):,}, "
          f"flat ROI {100 * bets.pnl_flat.mean():+.1f}%\n")
    groups = [("ALL", "all", bets)]
    groups += [(p, str(s), g) for (p, s), g in bets.groupby(["pick", "strength"])]
    groups += [(p, "any", g) for p, g in bets.groupby("pick")]
    groups += [("ALL", str(s), g) for s, g in bets.groupby("strength")]
    rows = []
    for pick, strength, g in groups:
        if len(g) < MIN_BETS:
            continue
        lo, hi = boot_ci(g.pnl_flat.to_numpy(), rng)
        rows.append({"pick": pick, "strength": strength, "bets": len(g), "wins": int((g.result == "W").sum()),
                     "hit_rate": float((g.result == "W").mean()), "roi_mean": float(g.pnl_flat.mean()),
                     "roi_ci_lo": lo, "roi_ci_hi": hi, "verdict": verdict(lo, hi)})
    out = pd.DataFrame(rows).sort_values("roi_ci_lo", ascending=False)
    print(f"  {'pick':<7}{'str':>4}{'bets':>6}{'wins':>6}{'hit%':>7}{'ROI':>8}{'CI lo':>8}{'CI hi':>8}  verdict")
    for r in out.itertuples():
        print(f"  {r.pick:<7}{r.strength:>4}{r.bets:>6}{r.wins:>6}{100 * r.hit_rate:>6.1f}%{100 * r.roi_mean:>+7.1f}%"
              f"{100 * r.roi_ci_lo:>+7.1f}%{100 * r.roi_ci_hi:>+7.1f}%  {r.verdict}")
    return out


# ---------------------------------------------------------------- B. slices

def slice_table(df: pd.DataFrame, col: str, order: list[str], title: str) -> list[dict]:
    rows = []
    print(f"\n{title}")
    print(f"  {'bucket':<12}{'n':>5}{'W':>5}{'hit%':>8}{'ROI':>8}   95% CI on hit")
    for key in [k for k in order if k in set(df[col])]:
        g = df[df[col] == key]
        if len(g) < MIN_N:
            continue
        w = int((g.result == "W").sum())
        p, lo, hi = wilson(w, len(g))
        rows.append({"dimension": col, "bucket": key, "n": len(g), "wins": w, "hit_rate": p, "hit_lo": lo,
                     "hit_hi": hi, "roi_flat": float(g.pnl_flat.mean())})
        print(f"  {key:<12}{len(g):>5}{w:>5}{100 * p:>7.1f}%{100 * g.pnl_flat.mean():>+7.1f}%   [{100 * lo:.0f}, {100 * hi:.0f}]")
    return rows


def section_b(bets: pd.DataFrame) -> pd.DataFrame:
    b = bets[bets.strength >= 1].copy()
    b["edge_band"] = pd.cut(b.edge, EDGE_BINS, labels=EDGE_LABELS, right=False).astype(str)
    b["price_band"] = pd.cut(b.price, PRICE_BINS, labels=PRICE_LABELS, right=True).astype(str)
    b["strength_s"] = b.strength.astype(str)
    rows = slice_table(b, "edge_band", EDGE_LABELS, "B1. by edge band (does a bigger Elo-vs-DK gap win more?)")
    rows += slice_table(b, "price_band", PRICE_LABELS, "B2. by price band")
    rows += slice_table(b, "pick", ["home", "draw", "away"], "B3. by pick")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- C. calibration

def section_c(m: pd.DataFrame) -> pd.DataFrame:
    print(f"\nC. Elo calibration on {len(m):,} settled matches with a closer and a rating on both sides")
    rows = []
    for col, label, hit in (("home_p", "P(home win)", "home"), ("draw_p", "P(draw)", "draw"), ("away_p", "P(away win)", "away")):
        print(f"  {label}: model bin -> observed rate (Wilson 95%)")
        band = pd.cut(m[col], PROB_BINS, labels=PROB_LABELS, right=False).astype(str)
        for key in [k for k in PROB_LABELS if k in set(band)]:
            g = m[band == key]
            if len(g) < MIN_N:
                continue
            k = int((g.outcome == hit).sum())
            p, lo, hi = wilson(k, len(g))
            pred = float(g[col].mean())
            rows.append({"outcome": hit, "bin": key, "n": len(g), "pred": pred, "obs": p, "obs_lo": lo, "obs_hi": hi})
            print(f"    {key:<7} n={len(g):>5}  pred {100 * pred:5.1f}%  obs {100 * p:5.1f}% [{100 * lo:5.1f},{100 * hi:5.1f}]  Δ{100 * (p - pred):+5.1f}pp")
    # log-loss: model vs de-vigged closer
    eps = 1e-9
    mp = np.select([m.outcome == "home", m.outcome == "draw"], [m.home_p, m.draw_p], m.away_p).astype(float)
    fp = np.select([m.outcome == "home", m.outcome == "draw"], [m.fair_home, m.fair_draw], m.fair_away).astype(float)
    ll_model, ll_market = -np.log(np.clip(mp, eps, 1)).mean(), -np.log(np.clip(fp, eps, 1)).mean()
    print(f"  3-way log-loss (lower is better): Elo model {ll_model:.4f} · de-vigged closer {ll_market:.4f} "
          f"→ {'the closer is sharper' if ll_market < ll_model else 'the model is sharper'} by {abs(ll_model - ll_market):.4f}")
    rows.append({"outcome": "logloss", "bin": "model", "n": len(m), "pred": ll_model, "obs": ll_market,
                 "obs_lo": np.nan, "obs_hi": np.nan})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- D. refit

def replay(results: pd.DataFrame, hfa: float) -> np.ndarray:
    """Chronological Elo replay; returns the pre-match expected home score for every row."""
    r: dict[str, float] = {}
    e_out = np.empty(len(results))
    k_col = np.where(results.league.str.contains("friendly"), ELO_K_FRIENDLY, ELO_K)
    for i, (h, a, hs, as_, neu, k) in enumerate(zip(results.home_id, results.away_id, results.home_score,
                                                     results.away_score, results.neutral, k_col)):
        rh, ra = r.get(h, ELO_START), r.get(a, ELO_START)
        e = 1.0 / (1.0 + 10.0 ** (-(rh - ra + (0.0 if neu else hfa)) / 400.0))
        e_out[i] = e
        s = 1.0 if hs > as_ else 0.0 if hs < as_ else 0.5
        gd = abs(hs - as_)
        mult = 1.0 if gd <= 1 else 1.5 if gd == 2 else (11.0 + gd) / 8.0
        d = k * mult * (s - e)
        r[h], r[a] = rh + d, ra - d
    return e_out


def three_way(e: np.ndarray, draw_base: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    pd_ = draw_base * 4.0 * e * (1.0 - e)
    ph = np.maximum(0.0, e - pd_ / 2.0)
    pa = np.maximum(0.0, 1.0 - e - pd_ / 2.0)
    s = ph + pd_ + pa
    return ph / s, pd_ / s, pa / s


def section_d(results: pd.DataFrame) -> pd.DataFrame:
    n = len(results)
    start = int(n * HOLDOUT_FRAC)
    out = np.select([results.home_score > results.away_score, results.home_score < results.away_score],
                    ["home", "away"], "draw")[start:]
    print(f"\nD. Refit ELO_HFA x DRAW_BASE on the results table — {n:,} matches, scoring the last {n - start:,} "
          f"(3-way log-likelihood per match, higher is better)")
    rows = []
    for hfa in HFA_GRID:
        e = replay(results, hfa)[start:]
        for db in DRAW_GRID:
            ph, pdr, pa = three_way(e, db)
            p = np.where(out == "home", ph, np.where(out == "draw", pdr, pa))
            ll = float(np.log(np.clip(p, 1e-9, 1)).mean())
            rows.append({"hfa": hfa, "draw_base": db, "loglik": ll})
    fit = pd.DataFrame(rows)
    best = fit.loc[fit.loglik.idxmax()]
    live = fit[(fit.hfa == ELO_HFA_LIVE) & (fit.draw_base == DRAW_BASE_LIVE)].iloc[0]
    print(f"  {'HFA':>5}  " + "".join(f"{db:>8.2f}" for db in DRAW_GRID) + "   (DRAW_BASE)")
    for hfa in HFA_GRID:
        row = fit[fit.hfa == hfa].sort_values("draw_base")
        print(f"  {hfa:>5.0f}  " + "".join(f"{v:>8.4f}" for v in row.loglik))
    print(f"  best: HFA {best.hfa:.0f}, DRAW_BASE {best.draw_base:.2f} (loglik {best.loglik:.4f}); "
          f"live: HFA {ELO_HFA_LIVE:.0f}, DRAW_BASE {DRAW_BASE_LIVE:.2f} (loglik {live.loglik:.4f}); "
          f"gap {best.loglik - live.loglik:.4f} per match")
    return fit


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    rng = np.random.default_rng(SEED)
    bets = load_soccer_bets()
    if bets.empty:
        print("no settled soccer paper bets yet — run soccer_edge.py --snapshot / --settle (or --backfill)")
        return
    section_a(bets, rng).to_csv(OUT_DIR / "soccer_roi.csv", index=False)
    section_b(bets).to_csv(OUT_DIR / "soccer_slices.csv", index=False)
    m = load_soccer_matches()
    if not m.empty:
        section_c(m).to_csv(OUT_DIR / "soccer_calibration.csv", index=False)
    results = load_results()
    if len(results) >= 200:
        section_d(results).to_csv(OUT_DIR / "soccer_fit.csv", index=False)
    print(f"\nwrote {OUT_DIR / 'soccer_*.csv'}")


if __name__ == "__main__":
    main()
