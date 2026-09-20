"""Unit tests for soccer_edge.py — no network. Run: python -m pytest -q tests"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cfb_edge as ce  # noqa: E402
import soccer_edge as se  # noqa: E402

UTC = dt.timezone.utc


def make_match(*, home_ml=+120, draw_ml=+240, away_ml=+220, home_ml_open=None, draw_ml_open=None,
               away_ml_open=None, home_elo=1520.0, away_elo=1500.0, home_n=20, away_n=20,
               total=2.5, total_open=None, status="pre", home_score=None, away_score=None,
               neutral=False, league="eng.1") -> se.Match:
    home = se.Side(id="h1", abbr="HOM", name="Home FC", score=home_score, ml=home_ml, ml_open=home_ml_open,
                   elo=home_elo, elo_n=home_n)
    away = se.Side(id="a1", abbr="AWY", name="Away United", score=away_score, ml=away_ml, ml_open=away_ml_open,
                   elo=away_elo, elo_n=away_n)
    m = se.Match(id="m1", league=league, league_name="Premier League", name="Away United at Home FC",
                 short="AWY @ HOM", kickoff=dt.datetime(2026, 9, 20, 14, 0, tzinfo=UTC), status=status,
                 completed=status == "post", neutral=neutral, home=home, away=away, draw_ml=draw_ml,
                 draw_ml_open=draw_ml_open, total=total, total_open=total_open, provider="DraftKings")
    if home_elo is not None and away_elo is not None:
        ph, pd_, pa = se.elo_probs(home_elo, away_elo, neutral)
        home.win_p, m.draw_p, away.win_p = ph, pd_, pa
    return m


# ---------------------------------------------------------------- odds / model math

def test_devig3_sums_to_one_and_orders():
    h, d, a = se.devig3(+120, +240, +220)
    assert h + d + a == pytest.approx(1.0)
    assert h > a > d
    assert se.devig3(None, +240, +220) == (None, None, None)


def test_ml_parses_even_and_signs():
    assert se._ml("EVEN") == 100 and se._ml("+115") == 115 and se._ml("-150") == -150
    assert se._ml(None) is None and se._ml("") is None


def test_elo_probs_parity_gives_draw_base_and_home_edge():
    ph, pd_, pa = se.elo_probs(1500, 1500, neutral=True)
    assert pd_ == pytest.approx(se.DRAW_BASE, abs=1e-9)
    assert ph == pytest.approx(pa)
    ph2, _, pa2 = se.elo_probs(1500, 1500)              # HFA applies
    assert ph2 > pa2
    ph3, pd3, pa3 = se.elo_probs(1900, 1400)            # mismatch: draw shrinks, sums to 1
    assert pd3 < se.DRAW_BASE and ph3 + pd3 + pa3 == pytest.approx(1.0) and ph3 > 0.8


def test_elo_update_zero_sum_and_gd_multiplier():
    r = {}
    se.elo_update(r, "A", "B", 3, 0, neutral=True, k=20)
    assert r["A"][0] + r["B"][0] == pytest.approx(2 * se.ELO_START)
    assert r["A"][1] == r["B"][1] == 1
    big = r["A"][0] - se.ELO_START
    r2 = {}
    se.elo_update(r2, "A", "B", 1, 0, neutral=True, k=20)
    assert big > r2["A"][0] - se.ELO_START                # 3-0 moves more than 1-0
    assert se._gd_mult(1) == 1.0 and se._gd_mult(2) == 1.5 and se._gd_mult(4) == pytest.approx(15 / 8)


def test_k_for_friendlies_is_half():
    assert se._k_for("club.friendly") == se.ELO_K_FRIENDLY and se._k_for("eng.1") == se.ELO_K


# ---------------------------------------------------------------- signals

def test_ml3_signal_inverted_tiers():
    # 2026-09-20: small edges are STRONG, big edges are demoted. fair home ~ 41%.
    m = make_match(home_elo=1600.0)                          # model ~60% -> edge ~ +45% -> overreach
    s = se.ml3_signal(m)
    assert s.kind == "ml3" and s.pick == "home" and s.strength == 0 and s.note == "⚠overreach"
    assert se.stake_for(s, 100.0) is None
    m = make_match()                                         # 1520 vs 1500: ~46% vs fair 41% -> +12% -> STRONG
    s = se.ml3_signal(m)
    assert s.pick == "home" and 8 <= s.edge < 15 and s.strength == 2 and s.price == 120
    assert s.pick_name == "Home FC" and se.stake_for(s, 100.0) is not None
    m = make_match(home_elo=1535, away_elo=1500)             # ~+17% -> value
    s = se.ml3_signal(m)
    assert 15 <= s.edge < 20 and s.strength == 1


def test_ml3_unrated_side_never_staked():
    m = make_match(away_n=3)
    s = se.ml3_signal(m)
    assert s.strength == 0 and s.note == "⚠unrated" and se.stake_for(s, 100.0) is None


def test_ml3_draw_pick_never_staked_in_practice():
    # a draw only shows model edge when the market prices it beyond +250 (DRAW_BASE caps the
    # model at 26%), and dogs beyond +250 are strength 0 -> draws are shown, never staked
    m = make_match(home_ml=+150, draw_ml=+330, away_ml=+150, home_elo=1500, away_elo=1560)
    s = se.ml3_signal(m)
    assert s.pick == "draw" and s.strength == 0 and "⚠draw-model" in se.warnings_for(s)
    assert se.stake_for(s, 100.0) is None


def test_ml3_long_dog_and_price_limits():
    m = make_match(home_ml=-400, draw_ml=+450, away_ml=+900, home_elo=1500, away_elo=1600)
    s = se.ml3_signal(m)
    assert s.pick == "away" and s.strength == 0                     # +900 beyond ML_MAX_PRICE
    m = make_match(home_ml=-250, draw_ml=+350, away_ml=+300, home_elo=1500, away_elo=1600)
    s = se.ml3_signal(m)
    assert s.pick == "away" and s.strength == 0 and s.note == "⚠long-dog"   # >+250: 21% hit, never staked


def test_ml3_none_without_prices_or_model():
    assert se.ml3_signal(make_match(home_ml=None)) is None
    m = make_match(home_elo=None, away_elo=None)
    assert se.ml3_signal(m) is None


def test_prob_and_total_move_signals():
    m = make_match(home_ml=-150, draw_ml=+300, away_ml=+400,
                   home_ml_open=+110, draw_ml_open=+250, away_ml_open=+240)
    s = se.prob_move_signal(m)
    assert s.kind == "prob-move" and s.pick == "home" and s.edge >= se.PROB_MOVE_PP
    assert se.prob_move_signal(make_match()) is None                # no opener
    t = se.total_move_signal(make_match(total=3.0, total_open=2.5))
    assert t.kind == "total-move" and t.edge == pytest.approx(0.5)
    assert se.total_move_signal(make_match(total=2.5, total_open=2.5)) is None


def test_ranked_signals_skips_started_matches_and_orders():
    live = make_match(status="in")
    pre = make_match()
    sigs = se.ranked_signals([live, pre])
    assert all(s.match is pre for s in sigs) and sigs[0].kind == "ml3"


# ---------------------------------------------------------------- grading + SQLite round trip

def test_grade3():
    assert se.grade3("home", 2, 1) == "W" and se.grade3("away", 2, 1) == "L"
    assert se.grade3("draw", 1, 1) == "W" and se.grade3("home", 1, 1) == "L"
    assert se.grade3("away", 0, 3) == "W"


def test_persist_paper_log_settle_round_trip(tmp_path):
    conn = se.db_connect(str(tmp_path / "s.db"))
    now = dt.datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
    m = make_match()
    assert se.db_persist(conn, [m], now) == 1
    sigs = se.ranked_signals([m])
    assert se.db_paper_log(conn, sigs, 100.0, now) == 1
    assert se.db_paper_log(conn, sigs, 100.0, now) == 0              # dedup
    # home wins 2-0 -> home pick is a W at +120
    m.status, m.completed, m.home.score, m.away.score = "post", True, 2, 0
    se.db_update_scores(conn, [m])
    n, staked, profit = se.db_settle_paper(conn)
    assert n == 1 and staked > 0 and profit == pytest.approx(staked * 1.2, abs=0.01)
    assert "home" in se.db_paper_summary(conn)


def test_results_store_and_elo_as_of(tmp_path):
    conn = se.db_connect(str(tmp_path / "s.db"))
    d1, d2 = dt.date(2026, 9, 1), dt.date(2026, 9, 8)
    m1 = make_match(status="post", home_score=2, away_score=0)
    assert se.store_results(conn, [m1], d1) == 1
    assert se.store_results(conn, [make_match()], d2) == 0          # not completed
    r_before = se.elo_as_of(conn, d1)
    r_after = se.elo_as_of(conn, d2)
    assert r_before == {} and r_after["h1"][0] > se.ELO_START > r_after["a1"][0]
    assert dict(conn.execute("SELECT date, n FROM results_log")) == {d1.isoformat(): 1, d2.isoformat(): 0}
    n, dr = se.draw_rate(conn)
    assert n == 1 and dr == 0.0


def test_apply_elo_marks_unrated_sides_and_board_renders():
    m = make_match(home_elo=None, away_elo=None)
    se.apply_elo([m], {"h1": [1550.0, 12]})
    assert m.home.elo == 1550.0 and m.away.elo is None and m.home.win_p is None
    assert "1550/—" in se.render_board([m], 100.0)      # one-sided rating must not crash


# ---------------------------------------------------------------- rendering

def test_board_and_top_carry_paper_banner_and_report_name():
    m = make_match()
    board = se.render_board([m], 100.0)
    top = se.render_top([m], 100.0)
    assert ce.stakes_banner() in board and ce.stakes_banner() in top
    assert "STRONG 3W" in top and "Home FC" in top
    assert se.report_path(dt.date(2026, 9, 20)).endswith("soccer-sunday-2026-09-20.md")


def test_league_of_uses_cache_and_falls_back():
    lmap = {"700": {"slug": "eng.1", "name": "English Premier League"}}
    assert se._league_of("s:600~l:700~e:1", lmap) == ("eng.1", "English Premier League")
    assert se._league_of("s:600~l:999~e:1", lmap) == ("l999", "league 999")
