# analysis/

Offline analysis of `data.db`. **Not part of the live `cfb_edge.py` tool** — these scripts
only read; they never write to `data.db` or `bets.csv`.

Each analysis ships in two flavors, kept in lockstep on purpose:

- **R** (`.R`) — `DBI`, `RSQLite`, `dplyr`, `boot`.
- **Python** (`.py`) — `pandas`, `numpy`. Same SQL, same bins, same conclusions.

If Python and R disagree on a point estimate, something is wrong. Bootstrap CIs may differ
in the last digit (different RNG streams); Wilson intervals are closed-form and must match.

## Layout

- `_shared/load_data.{py,R}` — `load_games()` (one row per settled FBS-vs-FBS game with the
  last-snapshot line and pre-game FPI) and `load_paper_bets()`; `wilson()` helper.
- `01_paper_roi_ci/` — flat-bet ROI of the paper ledger by kind × strength, bootstrap 95% CI.
- `02_fpi_calibration/` — FPI win-prob calibration; RMSE of FPI vs closer vs blend; FPI-side
  cover rate by |Δ| bucket vs the 52.4% break-even.
- `03_line_move/` — does the side the line moved toward cover? FPI-side cover rate when
  steam is with vs against the model.
- `04_deep_dive/` — where exactly does the paper ledger win and lose? Hit % (Wilson CI) and
  flat ROI by edge band, ML price band, |spread|, dog/fav, home/away, and model truth_p vs
  actual. This is the "simulate before you change a rule" script; it produced the 2026-09-20
  demotions (`SPREAD_OVERREACH_PTS`, `ML_DEAD_ZONE`) and `LIVE_STAKES = False`.
- `_out/` — CSV outputs, gitignored.

## Running

From the project root:

```powershell
pip install -r analysis/requirements-py.txt
python analysis/01_paper_roi_ci/paper_roi.py
python analysis/02_fpi_calibration/fpi_calibration.py
python analysis/03_line_move/line_move.py
python analysis/04_deep_dive/deep_dive.py

$env:PATH += ";C:\Program Files\R\R-4.4.2\bin"
Rscript -e 'install.packages(readLines("analysis/requirements-r.txt"), repos="https://cloud.r-project.org")'
Rscript analysis/01_paper_roi_ci/paper_roi.R
Rscript analysis/02_fpi_calibration/fpi_calibration.R
Rscript analysis/03_line_move/line_move.R
Rscript analysis/04_deep_dive/deep_dive.R
```

## "Settled" means

`games.completed = 1 AND home_score IS NOT NULL AND away_score IS NOT NULL`. That is the
whole filter. Losers are never dropped by a result-column filter (the survivorship lesson
from horses_worldwide). FCS games are excluded by `fbs_only=True` because FPI gives FCS
teams a generic rating — the same rule the live tool uses to refuse to rank them.

## Wiring findings back

Run all four in both runtimes → read verdicts → change the constants block at the top of
`cfb_edge.py` → bump `FINDINGS_AS_OF` → update the "Before you bet" table in `README.md` →
commit + push.
