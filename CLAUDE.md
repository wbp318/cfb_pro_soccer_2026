# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A single-file Python tool (`cfb_edge.py`) that pulls the Saturday college football slate
from ESPN's public endpoints, compares the DraftKings line to ESPN FPI's game projection,
flags outliers, tracks open→current line movement, suggests quarter-Kelly stakes, and
persists everything to SQLite for an honest backtest. Sister project of
`../horses_worldwide` — read its CLAUDE.md for the shared philosophy. **`README.md` has
usage and the diagrams; `betting_guide.md` has the play rules.**

## Common commands

```sh
pip install -r requirements.txt
python cfb_edge.py                        # board + top-10, next Saturday
python cfb_edge.py --top 15               # ranked outliers only
python cfb_edge.py --snapshot --report    # persist + paper-log + reports/<weekday>-<date>.md
python cfb_edge.py --date 2026-09-05 --backfill   # seed DB from a finished week
python cfb_edge.py --settle               # Sunday: grade paper + real bets
python cfb_edge.py --paper-show / --bets-show
python cfb_edge.py --bet <id> --kind spread --side "Team" --line 3.5 --price -110 --stake 5
python cfb_gui.py                         # local browser dashboard, same functions

python analysis/01_paper_roi_ci/paper_roi.py      # and the .R twin via Rscript
```

CI (`.github/workflows/ci.yml`) runs on every push: py_compile, `ruff check` (fix the code,
never relax the lint), `--help`, schema bootstrap on a scratch DB, and all four analysis
scripts in both Python and R against that empty DB via the `CFB_DB` env var. Run
`ruff check cfb_edge.py cfb_gui.py analysis tests` and `python -m pytest -q tests` before pushing.
`ruff.toml` pins the rule set (E4/E7/E9/F) so a ruff upgrade in CI can't move the goalposts.

**Unit tests** live in `tests/test_cfb_edge.py` (47 cases, no network): odds math, every
signal function including the demotions (FCS, blowout, steam-against, long-dog, overreach,
ML dead zone), the paper-only banner, Kelly cap,
ranking order, `_grade`/`_profit` for spread/ML/total, a full SQLite persist → paper-log →
settle round trip on a tmp DB, the column migration, the bets.csv ledger, the picks-board filter, the day-aware report name, and Wilson-interval
parity with the analysis loader. When you change a threshold or add a demotion, add a case.
Also verify by running the board for next Saturday and one `--backfill`
of a past Saturday, then running all four analysis scripts in **both** runtimes and checking
the point estimates match. `Rscript` is at `C:\Program Files\R\R-4.4.2\bin` (not on PATH).

## Architecture

**Everything is in `cfb_edge.py`** (~1,000 lines). `cfb_gui.py` is a stdlib `http.server`
dashboard that imports it; it must never recompute a signal or duplicate a rule — add
logic to `cfb_edge.py` and have the GUI call it. Its tests are `tests/test_cfb_gui.py`.
Sections of `cfb_edge.py`: odds math → ESPN adapters →
signals → SQLite → bets ledger → rendering → report → main. Don't split it without asking.

**Data flow:** `fetch_scoreboard` → `enrich_games` (parallel core-odds + predictor per game)
→ `apply_powerindex` → `list[Game]` → `spread_signal / ml_signal / spread_move_signal /
total_move_signal` → `render_board` / `render_top` / `write_report` / `db_persist` /
`db_paper_log`.

**The unified contract is `Game` / `TeamSide`.** Any new source (a keyed multi-book odds API,
another rating system) must populate these; signals and rendering only read them.

**Signals in trust order:** ATS (FPI margin vs spread) → ML (FPI win prob vs de-vigged
moneyline) → line move / total move (market-only, informational, never staked).
`Signal.strength` is 2/1/0; stakes only fire on ≥1 and only when `truth_p` exists.

**Demotions are deliberate, keep them:**
- Either side missing an FPI rating (= FCS) → strength 0. FPI assigns FCS teams a generic
  rating, so the "edge" is noise. This was the first bug: without it, UT Martin +41.5 and
  Colgate +23.5 were the top plays on the board.
- |spread| ≥ 28 → cap at lean. The normal-margin cover model with SD 13.5 overstates edges
  on blowout numbers.
- Line moved ≥ 1.5 pts against FPI → drop one tier and print ⚠market-moved-against.
- ML dogs longer than +250 → cap at "ML value"; > +400 or < −300 → no stake.
- Δ ≥ 8 (`SPREAD_OVERREACH_PTS`) → cap at lean, ⚠overreach. Added 2026-09-20 from the 2025
  backfill: Δ8+ covered 42.5% on 40 bets. The bigger the gap, the more the market knows.
- ML dogs +100..+150 (`ML_DEAD_ZONE`) → strength 0, ⚠dead-zone-dog. 30% hit on 63 bets.
- `LIVE_STAKES = False` → every board/report prints `STAKES: PAPER ONLY`. Flip it only when
  a bucket's 95% CI in `analysis/01` clears zero. Stakes are still computed and paper-logged.
- Every ticket capped at 5% of bankroll after quarter-Kelly.

**The 2025 season is backfilled** (16 Saturdays, 2025-08-23 → 2025-12-06, done 2026-09-20)
so the paper ledger has ~500 backfilled bets behind the live ones. ESPN still serves closers
and the frozen pre-game predictor for last season; re-run `--date <2025 Saturday> --backfill`
on a fresh DB to rebuild it.

**Backfill = closing line + pre-game FPI.** For finished games ESPN keeps `open`, freezes
`current` at the closer, and the predictor's `lastModified` is game morning. `--backfill`
persists that and paper-logs with `backfill=1` so analysis can split live vs backfilled.
"Settled" in analysis = `completed=1 AND both scores present` — never a filter that could
silently drop losers (the horses survivorship lesson).

**SQLite migrations:** `SCHEMA` is `CREATE IF NOT EXISTS`; add new columns to `MIGRATIONS`
and `_migrate_columns` ALTERs them onto existing DBs. Never drop a column.

**The analysis loop** (`analysis/`, Python + R twins) is the only source of truth for the
constants block at the top of `cfb_edge.py` (`SPREAD_OUTLIER_PTS`, `MARGIN_SD`, `STEAM_PTS`,
…). To refresh: run all four scripts in both runtimes, confirm they agree, change constants,
bump `FINDINGS_AS_OF`, update the "Before you bet" table in README.md, commit + push.

## Gotchas

- **ESPN 403s a full Chrome User-Agent** (Akamai). `UA = {"User-Agent": "Mozilla/5.0"}` works.
  Verified 2026-09-09.
- **Windows console is cp1252.** `main()` and `analysis/_shared/load_data.py` reconfigure
  stdout/stderr to UTF-8 because the output uses Δ, ≥, →. Don't remove it.
- **`bets.backfill` is a pandas method name.** Use `df["backfill"]`.
- **`settle_bets` matches the ledger side by prefix/substring** against the full ESPN name
  (`_side_is_home`). Ambiguous or unknown names are left unsettled with a stderr note. The
  first version compared "Missouri State" to "Missouri State Bears", never matched, and graded
  every ticket as the away side (fixed 2026-09-20).
- **Only DraftKings** is exposed by ESPN. Line shopping across books needs a keyed API.
- Kickoffs are rendered in America/Chicago.

## Conventions

- `README.md` is canonical and holds the Mermaid diagrams; keep the "Before you bet" table
  current with the latest analysis run.
- `CHANGELOG.md` gets an entry for every rule/constant change and every fix, citing the
  analysis run that justified it. Weekly report releases are not changelog entries.
- Every commit gets pushed in the same step. Remote: `github.com/wbp318/cfb_2026`.
- `main` is protected (set 2026-09-09): no force-push, no deletion, the three CI checks must
  pass; the owner (admin) can bypass the check requirement. Never `push --force` to main;
  if history needs rewriting, do it on a branch and open a PR.
- Do not commit `data.db`, `bets.csv`, `snapshot.log`, `analysis/_out/` (gitignored).
- `.gitattributes` marks every language linguist-detectable on purpose.
- Honesty in the README is load-bearing. Don't soften "inconclusive" into "promising".
