# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Three outlier finders, one philosophy, **all paper only**:

| tool | market | model | lines | DB |
|---|---|---|---|---|
| `cfb_edge.py` | college football spreads / moneylines | ESPN FPI predictor | DraftKings via ESPN | `data.db` |
| `soccer_edge.py` | every soccer league ESPN lists, 3-way ML | self-built Elo from a year of ESPN results | DraftKings via ESPN | `soccer.db` |
| `nhl_edge.py` | NHL player props (SOG, PTS, G, A, BLK, PPP, goalie SV) | Poisson projections from NHL public game logs | The Odds API (`ODDS_API_KEY`) or a CSV | `nhl.db` |

Each flags where model and market disagree, paper-logs every flagged play, settles it from
finals, and hands the ledger to `analysis/` (Python + R twins) which is the **only** thing
allowed to change a rule constant. Sister project of `../horses_worldwide`; read its
CLAUDE.md for the shared philosophy. `README.md` (canonical, holds all Mermaid diagrams),
`betting_guide.md` (play rules), `CHANGELOG.md` (every rule change with its evidence).

## Commands

```sh
pip install -r requirements.txt -r analysis/requirements-py.txt -r requirements-dev.txt

# football (default date = next Saturday)
python cfb_edge.py --top 15
python cfb_edge.py --snapshot --report              # persist + paper-log + reports/<weekday>-<date>.md
python cfb_edge.py --date 2026-09-19 --settle       # Sunday: grade paper + bets.csv (date = the slate settled)
python cfb_edge.py --date 2025-11-15 --backfill     # closers + pre-game FPI for a finished week
python cfb_edge.py --bet <id> --kind ml --side "Kansas" --price 180 --stake 4
python cfb_gui.py                                   # stdlib browser dashboard over cfb_edge

# soccer (default date = today)
python soccer_edge.py --build-elo                   # once, ~3 min; re-runs fetch only missing days
python soccer_edge.py --snapshot --report --top 15  # every league; --league eng.1,esp.1 to filter
python soccer_edge.py --date 2026-09-20 --settle

# NHL (season opens 2026-10-07)
python nhl_edge.py --build                          # rosters + game logs both seasons, ~2 min
python nhl_edge.py --calibrate                      # walk-forward projection test, no lines needed
python nhl_edge.py --date 2026-10-07 --projections  # no key needed
python nhl_edge.py --date 2026-10-07 --snapshot --report   # needs ODDS_API_KEY in .env, or --lines-file x.csv
python nhl_edge.py --settle

# checks — run all four before every push
ruff check cfb_edge.py cfb_gui.py soccer_edge.py nhl_edge.py analysis tests
python -m pytest -q tests                           # 78 cases, no network
python -m pytest -q tests/test_soccer_edge.py -k draw   # one file / one test
python analysis/05_soccer/soccer_loop.py && "C:/Program Files/R/R-4.4.2/bin/Rscript" analysis/05_soccer/soccer_loop.R
```

`Rscript` is not on PATH. CI (`.github/workflows/ci.yml`) runs py_compile, ruff (rule set
pinned in `ruff.toml`; fix the code, never relax the lint), pytest, `--help` for all four
entry points, schema bootstrap of scratch DBs, and all six analysis scripts in both runtimes
against empty DBs via `CFB_DB` / `CFB_SOCCER_DB` / `CFB_NHL_DB`. Every new module or script
must be wired into every one of those steps.

## Architecture

**One contract per sport, read-only signals.** `cfb_edge` has `Game`/`TeamSide`, `soccer_edge`
has `Match`/`Side`, `nhl_edge` has `GameCtx`/`Player`/`Prop`. Adapters populate them; signal
functions and renderers only read them. A new data source means a new adapter, never a change
to a signal.

**Shared plumbing lives in `cfb_edge` and is imported, never copied:** odds math
(`american_to_decimal`, `devig_pair`, `kelly_fraction`), `stake_for` (duck-typed on
`truth_p` / `price` / `strength`), `stakes_banner()`, `LIVE_STAKES`, `_profit`, `UA`,
`LOCAL_TZ`. `cfb_gui.py` likewise must never recompute a signal; add logic to `cfb_edge.py`
and call it.

**Each tool is one file with section headers** (odds math → adapters → model → signals →
SQLite → rendering/report → main). Don't split them without asking.

**Signal shape is identical across sports:** `strength` 2/1/0 with a label, `edge`,
`truth_p`, `price`; stakes fire only on strength ≥ 1 with a `truth_p`. Every demotion sets
strength and a ⚠note. Market-only signals (line move, prob move, total move) are strength 1,
informational, never staked. Football also has `just_win_signal` (kind `just-win`): the
opposite question, favourites FPI and DK agree on at -250..-110, +EV, gap <= +20%; report
section 0b, own paper bucket, no track record yet.

**Backfill is honest by construction.** Football: ESPN freezes `current` at the closer and
the predictor at game morning. Soccer: ratings are never stored; `elo_as_of(date)` replays the
`results` table, so a past date sees only what was known then. Backfilled rows carry
`backfill=1` so analysis can split them. "Settled" everywhere means `completed=1 AND both
scores present` — never a result-column filter that could drop losers.

**SQLite:** `SCHEMA` is `CREATE IF NOT EXISTS`; new columns go in `MIGRATIONS` and are ALTERed
onto existing DBs. Never drop a column. `snapshots`/`props` are append-only time series; the
analysis loaders take the last row per game as the closer.

**The analysis loop is the source of truth for every rule constant.** Six scripts, each a
Python + R pair sharing SQL, bins and the closed-form Wilson interval: `01`–`04` football
(`data.db`), `05` soccer, `06` NHL. `01` groups by `kind`, so `just-win` shows up as its own row once it has settled bets. Point estimates must match to the digit; only bootstrap
CIs may differ in the last place. To change a constant: run both runtimes, confirm they
agree, edit the constants block, bump `FINDINGS_AS_OF`, update the README status table
(football "Before you bet", soccer "Honest status", NHL calibration table), add a
`CHANGELOG.md` entry citing the run, commit, push, cut a `rules-<date>` release. `05` and `06`
re-implement the Elo replay and the projection recipe on purpose (both runtimes need them);
if you change `elo_update` or the shrinkage/recent-tilt/Poisson recipe in a tool, change
the analysis twin too.

## What the data has said so far (don't re-litigate without a new run)

- Across all three sports the closer is sharper than the model, and **the bigger the
  model-vs-market gap, the worse the bet.** Football Δ8+ covers 42.5%; soccer hit rate falls
  from 44% (edge 8–15%) to 23% (50%+). This is why:
  - football demotes Δ ≥ 8 (`SPREAD_OVERREACH_PTS`) and never stakes ML dogs +100..+150;
  - soccer tiers are **inverted**: +8–15% STRONG, +15–20% value, ≥ +20% strength 0;
    dogs > +250 never; draws can only show edge past +250 so they never reach a stake;
  - NHL borrows a ≥ +30% overreach demotion as a prior.
- `LIVE_STAKES = False` for all three. Flip it only when a bucket's 95% CI in the ROI
  script clears zero. Stakes are still computed and paper-logged so the sample grows.
- NHL `--calibrate` (walk-forward, 2025-26): shots and points beat naive clearly and are
  calibrated; goalie saves barely beat naive, hence `SAVES_MAX_STRENGTH = 1`. No historical
  prop prices exist, so NHL ROI is untested until the season.
- Football demotions that predate the loop and stay: FCS side (generic FPI rating; the first
  bug put UT Martin +41.5 on top of the board), |spread| ≥ 28, steam against FPI, dogs > +250
  capped, price window −300..+400, 5% bankroll cap after quarter-Kelly.

## Gotchas

- **ESPN 403s a full Chrome User-Agent** (Akamai). `{"User-Agent": "Mozilla/5.0"}` works.
  DraftKings' own sportsbook API also 403s; that is why NHL props need The Odds API.
- **ESPN soccer `all/scoreboard` emits `null` entries inside a match's `odds` list**; skip
  non-dict entries or a whole day's build fails.
- **NHL `stats/rest/en/team` lists every defunct franchise**; use `standings/now` for the
  32 clubs. Boxscores have blocked shots but not power-play points (game logs are the
  reverse); `--settle` fills each from the right source.
- **`--settle` needs `--date` of the slate being settled** for football and soccer; the
  default date is the *next* slate.
- **`settle_bets` matches the ledger side by prefix/substring** (`_side_is_home`) and leaves
  ambiguous names unsettled with a stderr note. The first version never matched short names
  and graded every ticket as the away side.
- **Windows console is cp1252.** `main()` and the analysis loaders reconfigure stdout to
  UTF-8 because output uses Δ, ≥, →, ·. Don't remove it. When patching docs from Python,
  those glyphs are why substring matches silently fail; prefer `Edit` or match on ASCII.
- **`bets.backfill` is a pandas method name.** Use `df["backfill"]`.
- In R, `dbGetQuery(..., params = list())` errors; the NHL loader only passes params when
  they exist.
- Kickoffs render in America/Chicago. Only DraftKings is exposed by ESPN.

## Conventions

- Every commit gets pushed in the same step. Remote: `github.com/wbp318/cfb_soccer_nhl_2026_2027`
  (renamed twice on 2026-09-20; GitHub redirects old names). The local folder stays
  `C:\Users\wbp31\cfb_2026` on purpose: the scheduled task and the Claude memory dir point at it.
- `main` is protected: no force-push, CI must pass (admin can bypass). Rewrite history on a
  branch and open a PR.
- Releases: every report run is a release tagged `<weekday>-<date>`, `soccer-<weekday>-<date>`
  or `nhl-<weekday>-<date>` (same-day refresh adds `-HHMM`, earlier release stays); rule
  changes are `rules-<date>`; new analysis scripts are `analysis-0N-<sport>-<date>`. The release body
  IS the full report (`--notes-file reports/<file>.md`) or the full changelog entry; never just
  attach the file. Use `--target main`, feed stdin from `/dev/null`.
- `CHANGELOG.md` gets an entry for every rule/constant change and every fix, citing the
  analysis run. Weekly report releases are not changelog entries.
- Every Mermaid block must parse; the scratch check is `node check.mjs README.md
  analysis/README.md` with `mermaid@11` + `jsdom` installed in a temp folder. Update the
  diagrams whenever a script, demotion, test count or module is added.
- Never commit `data.db`, `soccer.db`, `nhl.db`, `soccer_leagues.json`, `bets.csv`, `.env`,
  `snapshot.log`, `analysis/_out/`. Never print, paste, or go looking for the Odds API key in
  other repos; the user puts it in `.env`.
- License is proprietary, all rights reserved (see `LICENSE`); the repo is public on purpose.
  Never open-source it or soften the license text.
- Honesty in the README is load-bearing. Don't soften "inconclusive" into "promising", and
  don't promise winners; report the sample size and the interval.
