# Betting Guide

Live-play reference for `cfb_edge.py`. Read before firing on any game. Companion docs:
`README.md` (install, diagrams, honest expectations), `CLAUDE.md` (conventions).

> **Status 2026-09-20: PAPER ONLY.** On 578 settled paper bets (2025 season backfilled +
> 2026 live) no bucket has a 95% CI above zero, the flagged spread side covers 49% against a
> 52.4% break-even, and the "STRONG" plays are the *worst* ones. `LIVE_STAKES = False` in
> `cfb_edge.py` prints that on every board. Everything below describes what the tool flags
> and paper-logs; none of it is a reason to place a real ticket until `analysis/` says so.

## 1. The signals — trust ladder

| # | Signal | Compares | Fires | Trust |
|---|---|---|---|---|
| 1 | **ATS** | FPI predicted margin vs DK spread | Δ ≥ 3 (lean) / 5 ≤ Δ < 8 (STRONG) / Δ ≥ 8 (capped at lean, ⚠overreach) | primary |
| 2 | **ML** | FPI win prob vs de-vigged DK moneyline | +8% (value) / +20% (STRONG); dogs +100..+150 never staked | primary when the spread is tiny or the dog is live |
| 3 | **line move** | DK opener vs current | ≥ 3 pts | information only — the market learned something |
| 4 | **total steam** | DK total opener vs current | ≥ 2.5 pts | information only — no totals model |

FPI is a real model with a public track record. The market is also a model, with money
behind it. When they disagree by a lot, one of them is wrong. On 795 games the answer is
the market: the FPI side covers 48.4%, and 41% when the gap is 8+ points. A big Δ is a
reason to ask what the market knows, not a reason to bet.

## 2. What to fire on

Nothing, with real money, as of 2026-09-20. The paper ledger fires on:

1. **STRONG ATS** (5 ≤ Δ < 8) on an FBS-vs-FBS game. Δ ≥ 8 is demoted to lean.
2. **ML value / STRONG ML** outside the dead zone (+100..+150) and inside −300..+400.

The old "three confirmations" (STRONG ATS + steam with + ML on the same side) was tested
on the 2025 season and did not survive: steam toward the FPI side covers 47.9%, steam
against it 52.6%. The line moving your way is not confirmation. The 2026-09-12 examples
that used to sit here (Syracuse −3.5, Δ 8.1; Middle Tennessee +13.5, Δ 9.3) both lost.

## 3. What to skip

- **⚠market-moved-against** — the line moved ≥ 1.5 pts *away* from FPI. Somebody knows
  something FPI doesn't (QB out, weather, suspension). Skip unless you know the reason and
  disagree with it.
- **FCS opponents** — never ranked, never staked, ATS *or* ML. FPI's FCS rating is generic.
- **⚠blowout-number** — spreads of 28+ are about garbage time, not team strength.
- **⚠long-dog** — ML dogs beyond +250 are capped at "value"; beyond +400 never staked.
  Variance eats small bankrolls.
- **⚠overreach** — Δ ≥ 8. FPI is most wrong exactly where it disagrees most (42.5% cover
  on 40 bets). Capped at lean, never STRONG.
- **⚠dead-zone-dog** — ML dogs +100 to +150. 30% hit, −33% flat ROI on 63 bets. Strength 0,
  never staked. Yesterday's 0-for-5 (2026-09-19) was five of these.
- **Totals** — we have no model. Steam on a total is a reason to look at the weather, not
  to bet.

## 4. Sizing

- `$Bet` = quarter-Kelly on the model's probability, **capped at 5% of bankroll**, $1 min.
  It is what the paper ledger logs. With `LIVE_STAKES = False` it is not a ticket.
- 3–5 tickets per Saturday. More than that and you're betting the noise.
- Default bankroll in the tool is $100; pass `--bankroll` for yours.
- Log every real ticket with `--bet` the moment you place it. Run `--settle` Sunday.

## 5. Soccer (`soccer_edge.py`) — the inverted ladder

- **Only small disagreements get a paper stake.** Elo vs the de-vigged DK three-way:
  +8% to +15% is STRONG 3W, +15% to +20% is 3W value, ≥ +20% is ⚠overreach and never staked.
  On 287 bets the hit rate fell in every band as the edge grew (44% → 34% → 33% → 28% → 23%).
- **Dogs beyond +250: never** (21% hit on 95). **Draws: shown, never staked** — the model's
  draw ceiling is 26%, so a draw only shows edge when the book prices it past +250.
- **⚠unrated** (either side has < 8 results in the table): never.
- Board columns: `DK H/D/A`, `Elo H/A`, `Model H/D/A`. The STRONG rows are the ones where the
  model and the market *almost* agree.

## 6. NHL props (`nhl_edge.py`) — 2026-27, paper from opening night

- **What is projected:** shots, points, goals, assists, blocks, PP points, goalie saves. Rate
  = this season shrunk to last season (20 games), 35% tilt to the last 10, × opponent
  shots/goals allowed vs league (0.80–1.20), × 1.02 at home → Poisson P(over).
- **Trust order:** shots and points (walk-forward log-loss beats naive by 0.066 / 0.045, bins
  within 2 pp) → goals / assists / PPP (same machinery, not separately tested) → **saves
  last** (barely beats naive; capped at value, ⚠saves-model).
- **Tiers:** +8% value, +15% STRONG, ≥ +30% ⚠overreach never staked (borrowed from what CFB
  and soccer both showed). Price window −250..+250. ⚠thin (< 10 games) never. Goalie flagged
  ⚠not-starter never — check the confirmed starter yourself two hours before puck drop.
- **Lines:** `.env` with `ODDS_API_KEY=…` (free tier ~500 requests/month; a 10-game night
  costs 11) or `--lines-file` CSV. Without lines the tool prints projections and logs nothing.
- **No historical prop prices exist**, so there is no ROI backtest. The ledger starts empty on
  2026-10-07 and `analysis/06` gets built when it has a few hundred props.

## 7. Discipline — the horses lessons carry over

- **No signal has beaten a market until the backtest says so** with a confidence interval
  that clears zero. On 795 games and 578 paper bets nothing does; the flagged spread side is
  below break-even. That is why stakes are paper only.
- **Simulate before you change a rule.** `analysis/04_deep_dive` shows hit % and ROI by
  edge band, price band, |spread|, home/away and calibration. A rule change that is not
  visible there is a hunch.
- **The losers count.** The analysis "settled" rule is completed game + both scores.
  Nothing filters on a result column that could hide losses.
- **Re-run the loop weekly**, both runtimes, before changing a threshold.
- Take the closing-line-value view seriously: if the plays we flag Thursday consistently
  close *further* from FPI on Saturday, the market disagrees with us and it is usually right.
