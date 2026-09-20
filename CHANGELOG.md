# Changelog

All notable changes to `cfb_edge.py` and the analysis loop. Rule changes cite the analysis
run that justified them; nothing in the constants block changes without one. Weekly report
releases (`<weekday>-<date>` tags) are not listed here; see the GitHub releases page.

## [2026-09-20b] — Pro soccer module, repo renamed to cfb_pro_soccer_2026

### Added
- **`soccer_edge.py`**: pro soccer outlier finder for every league ESPN lists (219 in the
  catalogue) and DraftKings prices. ESPN publishes no predictor for soccer, so the model is a
  self-built Elo table: `--build-elo` stores a year of final scores in `soccer.db` and
  ratings are replayed chronologically (K 20, half for friendlies, HFA 60, goal-difference
  multiplier), never stored, so a backfill uses exactly the rating known on that date.
  Elo → home/draw/away (draw base 26% at parity, shrinking as 4E(1−E)) vs the de-vigged
  DraftKings three-way line. Signals: `ml3` (+8% value / +20% STRONG), `prob-move`,
  `total-move`. Demotions: unrated side (< 8 results) → never staked; draw picks capped at
  value; dogs > +250 capped; outside −300..+400 never. Shares `LIVE_STAKES`, `stake_for`
  and the PAPER ONLY banner with `cfb_edge`. Flags mirror the football tool
  (`--snapshot --report --settle --backfill --top --league --elo-show --paper-show`).
- `tests/test_soccer_edge.py`: 18 cases, no network. CI compiles, lints, tests, `--help`s
  and schema-bootstraps the soccer module too.
- `reports/soccer-<weekday>-<date>.md` report format.

### Changed
- Repo renamed **`cfb_2026` → `cfb_pro_soccer_2026`** (GitHub redirects the old URL). The
  local folder keeps its name so the scheduled task and memory paths still resolve.
- README: title, clone URL, quick start, overview diagram (soccer subgraph), new "Pro
  soccer" section with its own signal diagram and flag table, roadmap and file table.

### Honest status
- No soccer analysis run exists. Every soccer constant is a prior. The paper ledger in
  `soccer.db` and a future `analysis/05_soccer_*` pair decide whether Elo-vs-DK is anything.
- First backfill sample, 2026-09-12/13 (229 paper bets across all leagues, closers + Elo as
  of that day): flat ROI **−14.7%** on $629 staked. STRONG (str 2): 88 bets, 35 W, +3.1%.
  Value (str 1): 141 bets, 44 W, −35%. Two days is not a verdict; it is the reason the
  module is paper only.

## [2026-09-20] — Paper only, overreach cap, ML dead zone, 2025 backfill, analysis/04

Triggered by going 0-for-5 on 2026-09-19 with five ML dogs from the top of the board.

### Fixed
- **bets.csv grading was wrong for every home-side ticket.** `settle_bets` compared the short
  side name typed at `--bet` ("Missouri State") to ESPN's full name ("Missouri State Bears"),
  never matched, and graded every ticket as the away team. The 9/19 ledger showed 3-2 and
  +$16; it was 0-5 and −$24. New `_side_is_home` matches by prefix/substring and refuses to
  grade (leaves the row unsettled, prints to stderr) when the name matches both teams or
  neither. Two regression tests. The 9/12 tickets were graded correctly by luck (all the
  home-side picks lost anyway).

### Added
- **2025 season backfilled** into `data.db`: 16 Saturdays, 775 games, 766 with a DraftKings
  closer and a pre-game FPI projection, flagged `backfill=1`. The paper ledger went from 114
  settled bets to 578. ESPN still serves last season's closers and frozen predictor, so this
  is reproducible with `--date <2025 Saturday> --backfill` on a fresh DB.
- **`analysis/04_deep_dive`** (Python + R, CI-wired): hit % with Wilson 95% CI and flat ROI
  by strength, season, edge band, ML price band, |spread|, dog/fav, home/away, and the
  model's own `truth_p` vs actual hit rate. Point estimates match to the digit across
  runtimes (44 rows). This is the "simulate before you change a rule" script.
- **`stakes_banner()`** printed at the top of the picks board, the top-N board and the report.
- **`CHANGELOG.md`** (this file).

### Changed (rules — from the 2026-09-20 analysis run, 795 games / 578 paper bets)
- **`LIVE_STAKES = False`.** No bucket has a 95% CI above zero; the flagged spread side
  covers 49.3% against a 52.4% break-even. Every board says `STAKES: PAPER ONLY`. Stakes are
  still computed and paper-logged so the sample keeps growing.
- **`SPREAD_OVERREACH_PTS = 8.0`.** Δ ≥ 8 is capped at lean and tagged ⚠overreach. Cover
  rate by edge band: Δ3-5 52.6%, Δ5-8 47.6%, Δ8+ 42.5%. The bigger the FPI-vs-DK gap, the
  more often the market is right.
- **`ML_DEAD_ZONE = (100, 150)`.** Moneyline dogs priced +100 to +150 get strength 0 and
  ⚠dead-zone-dog: 30.2% hit, −33% flat ROI on 63 bets, the worst bucket in the sample.
  All five 9/19 tickets were in it.
- **Line-move gating tested and rejected.** "Steam with FPI" covers 47.9% (n=117), steam
  against 52.6% (n=196). The `[steam with]` tag stays informational; the betting guide no
  longer calls it a confirmation.
- `FINDINGS_AS_OF = "2026-09-20"`.

### Docs
- README "Before you bet" rewritten against the new sample; signals diagram gains the two
  demotions and the `LIVE_STAKES` gate; analysis and CI diagrams show four scripts and 47
  tests. `betting_guide.md` opens with the paper-only status and drops the "three
  confirmations" play. `CLAUDE.md` and `analysis/README.md` updated to match.

### Tests
- 42 → 47 cases: `_side_is_home`, short-name home ML regression, overreach cap, ML dead
  zone, paper-only banner.

## [2026-09-14] — Analysis loop re-run, README table

- Constants unchanged; "Before you bet" table refreshed on 96 games / 70 paper bets.

## [2026-09-09] — First analysis loop, CI, branch protection

- `analysis/01`–`03` in Python and R; `FINDINGS_AS_OF` introduced; `main` protected.
- FCS demotion: either side without an FPI rating is never ranked or staked.
