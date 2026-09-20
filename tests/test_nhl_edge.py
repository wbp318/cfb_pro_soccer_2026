"""Unit tests for nhl_edge.py — no network. Run: python -m pytest -q tests"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cfb_edge as ce  # noqa: E402
import nhl_edge as ne  # noqa: E402

UTC = dt.timezone.utc


def make_game(state="FUT") -> ne.GameCtx:
    g = ne.GameCtx(id=2026020001, date=dt.date(2026, 10, 7), start_utc=dt.datetime(2026, 10, 7, 23, 0, tzinfo=UTC),
                   home="TOR", away="MTL", state=state)
    g.home_factor = {k: 1.0 for k in ("shots", "points", "goals", "assists", "blocked", "pp_points", "saves")}
    g.away_factor = dict(g.home_factor)
    return g


def make_player(name="Auston Matthews", team="TOR", pos="C", rates=None, games=50) -> ne.Player:
    return ne.Player(id=8479318, name=name, team=team, position=pos,
                     rates=rates or {"shots": 4.2, "points": 1.3, "goals": 0.7}, games=games)


def make_prop(market="player_shots_on_goal", line=3.5, over=-115, under=-105, player=None, game=None) -> ne.Prop:
    p = ne.Prop(game or make_game(), player or make_player(), market, ne.MARKETS[market][0], line, over, under, "dk")
    ne.project(p)
    return p


# ---------------------------------------------------------------- distribution math

def test_poisson_cdf_and_p_over():
    assert ne.poisson_cdf(-1, 2.0) == 0.0
    assert ne.poisson_cdf(0, 2.0) == pytest.approx(0.1353, abs=1e-4)
    assert ne.poisson_cdf(100, 2.0) == pytest.approx(1.0)
    po, push = ne.p_over(3.0, 2.5)
    assert push == 0.0 and po == pytest.approx(1 - ne.poisson_cdf(2, 3.0))
    po2, push2 = ne.p_over(3.0, 3.0)                       # whole line pushes at exactly 3
    assert push2 == pytest.approx(0.2240, abs=1e-3) and po2 == pytest.approx(1 - ne.poisson_cdf(3, 3.0))


def test_project_uses_rate_times_opponent_factor():
    g = make_game()
    g.home_factor["shots"] = 1.10
    p = make_prop(game=g)
    assert p.mean == pytest.approx(4.2 * 1.10) and 0 < p.p_over < 1
    q = ne.Prop(g, make_player(rates={"points": 1.0}), "player_shots_on_goal", "shots", 3.5, -110, -110)
    ne.project(q)
    assert q.mean is None and q.p_over is None            # no rate for that stat


def test_opponent_factors_clamped_and_home_bump():
    prior = {"TOR": {"gp": 82, "sf": 33.0, "sa": 27.0, "gf": 3.5, "ga": 2.6},
             "MTL": {"gp": 82, "sf": 28.0, "sa": 36.0, "gf": 2.8, "ga": 3.6},
             "BOS": {"gp": 82, "sf": 30.0, "sa": 30.0, "gf": 3.0, "ga": 3.0}}
    hf, af = ne.opponent_factors(prior, {}, "TOR", "MTL")
    assert hf["shots"] <= ne.OPP_FACTOR_CAP[1] * ne.HOME_FACTOR and hf["shots"] > 1.0   # MTL allows a lot
    assert af["shots"] < 1.0                                                          # TOR allows little
    assert hf["saves"] < 1.0 and af["saves"] > 1.0        # goalie facing MTL sees fewer shots


# ---------------------------------------------------------------- signals

def test_prop_signal_tiers_and_overreach():
    p = make_prop(line=3.5, over=+100, under=-120)        # model P(over 3.5 | λ4.2) ≈ 0.61 vs fair ≈ 0.48 -> +27%
    s = ne.prop_signal(p)
    assert s.side == "over" and s.strength == 2 and 15 <= s.edge < 30
    p = make_prop(line=2.5, over=-200, under=+165)        # model ≈ 0.79 vs fair ≈ 0.65 -> ~+21%
    s = ne.prop_signal(p)
    assert s.side == "over" and s.strength == 2
    p = make_prop(line=5.5, over=+300, under=-400)        # +300 beyond MAX_PRICE
    s = ne.prop_signal(p)
    assert s is None or s.strength == 0
    p = make_prop(line=3.5, over=+140, under=-170)        # model 0.61 vs fair ~0.40 -> > +30% -> overreach
    s = ne.prop_signal(p)
    assert s.side == "over" and s.strength == 0 and s.note == "⚠overreach"


def test_prop_signal_thin_and_non_starter_never_staked():
    p = make_prop(player=make_player(games=4), line=3.5, over=+100, under=-120)
    s = ne.prop_signal(p)
    assert s.strength == 0 and s.note == "⚠thin" and ne.stake_for(s, 100.0) is None
    g = make_player(name="Joseph Woll", pos="G", rates={"saves": 27.5})
    p = make_prop(market="player_total_saves", line=26.5, over=-110, under=-110, player=g)
    s = ne.prop_signal(p)
    assert s.side == "over" and s.strength == 1 and s.note == "⚠saves-model"   # saves capped at value
    g.starter = False
    p = make_prop(market="player_total_saves", line=26.5, over=-110, under=-110, player=g)
    s = ne.prop_signal(p)
    assert s.strength == 0 and s.note == "⚠not-starter"


def test_ranked_signals_skips_started_games():
    live = make_prop(game=make_game(state="LIVE"), line=3.5, over=+100, under=-120)
    fut = make_prop(line=3.5, over=+100, under=-120)
    sigs = ne.ranked_signals([live, fut])
    assert len(sigs) == 1 and sigs[0].prop is fut


# ---------------------------------------------------------------- lines: matching + csv

def test_name_and_team_matching():
    assert ne._name_match("Auston Matthews", "Auston Matthews")
    assert ne._name_match("Auston Matthews", "A. Matthews")
    assert ne._name_match("Nick Suzuki", "Nicholas Suzuki")            # first initial + last name
    assert not ne._name_match("Auston Matthews", "Mitch Marner")
    assert ne._team_match("Toronto Maple Leafs", "TOR") and ne._team_match("Utah Mammoth", "UTA")
    assert not ne._team_match("Montreal Canadiens", "TOR")


def test_attach_props_matches_rostered_player_and_drops_ambiguous(tmp_path):
    g = make_game()
    players = {"TOR": [make_player()], "MTL": [make_player(name="Nick Suzuki", team="MTL")]}
    rows = [(g.id, "A. Matthews", "player_shots_on_goal", 3.5, -115, -105, "dk"),
            (None, "Nick Suzuki", "player_points", 0.5, -140, +110, "fd"),
            (g.id, "Nobody Here", "player_points", 0.5, -110, -110, "dk"),
            (g.id, "Auston Matthews", "player_total_saves", 27.5, -110, -110, "dk")]   # skater on a goalie market
    props = ne.attach_props(rows, [g], players)
    assert [(p.player.name, p.market) for p in props] == [("Auston Matthews", "player_shots_on_goal"),
                                                          ("Nick Suzuki", "player_points")]
    csv_path = tmp_path / "lines.csv"
    csv_path.write_text("player,market,line,over,under,book,game\nAuston Matthews,player_shots_on_goal,3.5,-115,-105,dk,MTL @ TOR\n",
                        encoding="utf-8")
    rows = ne.read_props_csv(str(csv_path), [g])
    assert rows == [(g.id, "Auston Matthews", "player_shots_on_goal", 3.5, -115, -105, "dk")]


def test_load_key_from_env_and_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert ne.load_key() is None
    (tmp_path / ".env").write_text('ODDS_API_KEY="abc123"\n', encoding="utf-8")
    assert ne.load_key() == "abc123"
    monkeypatch.setenv("ODDS_API_KEY", "fromenv")
    assert ne.load_key() == "fromenv"


# ---------------------------------------------------------------- rates + SQLite round trip

def test_player_rates_shrink_toward_prior_and_recent_window(tmp_path):
    conn = ne.db_connect(str(tmp_path / "n.db"))
    rows = []
    for i in range(40):                                      # prior season: 3 shots a game
        rows.append((100 + i, 1, ne.PRIOR_SEASON, f"2026-01-{i % 28 + 1:02d}", "TOR", "MTL", 1, 3, 1, 0, 1, None, 0, None, None, None, 18.0))
    for i in range(10):                                      # current season: 5 shots a game
        rows.append((200 + i, 1, ne.SEASON, f"2026-10-{i + 8:02d}", "TOR", "MTL", 1, 5, 1, 0, 1, None, 0, None, None, None, 18.0))
    conn.executemany("INSERT INTO game_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    rates, eff = ne.player_rates(conn, 1, dt.date(2026, 10, 20), "C")
    # base = w*5 + (1-w)*3 with w = 10/30 -> 3.667; recent-10 = 5 -> 0.65*3.667 + 0.35*5 = 4.13
    assert rates["shots"] == pytest.approx(4.133, abs=0.01) and eff == 30
    before, _ = ne.player_rates(conn, 1, dt.date(2026, 10, 1), "C")
    assert before["shots"] == pytest.approx(3.0)             # nothing from the current season yet
    assert "blocked" not in rates                            # game logs carry no blocks


def test_grade_prop_and_settle_summary(tmp_path):
    assert ne.grade_prop("over", 2.5, 3) == "W" and ne.grade_prop("under", 2.5, 3) == "L"
    assert ne.grade_prop("over", 3.0, 3) == "P"
    conn = ne.db_connect(str(tmp_path / "n.db"))
    now = dt.datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    g, pl = make_game(), make_player()
    conn.execute("INSERT INTO players VALUES (?,?,?,?)", (pl.id, pl.name, pl.team, pl.position))
    p = make_prop(line=3.5, over=+100, under=-120, player=pl, game=g)
    sigs = ne.ranked_signals([p])
    assert ne.db_persist(conn, [g], [p], now) == 1
    assert ne.db_paper_log(conn, sigs, 100.0, now) == 1 and ne.db_paper_log(conn, sigs, 100.0, now) == 0
    conn.execute("UPDATE paper_bets SET actual=5, result='W', profit=5.0")
    conn.commit()
    txt = ne.db_paper_summary(conn)
    assert "player_shots_on_goal" in txt and "over" in txt


def test_report_name_and_banner_in_top():
    assert ne.report_path(dt.date(2026, 10, 7)).endswith("nhl-wednesday-2026-10-07.md")
    p = make_prop(line=3.5, over=+100, under=-120)
    assert ce.stakes_banner() in ne.render_top([p], 100.0)
