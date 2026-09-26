"""Unit tests for cfb_edge.py. No network — every Game is built by hand.

Run:  python -m pytest -q
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cfb_edge as ce

UTC = dt.timezone.utc


# ---------------------------------------------------------------- helpers

def make_game(*, home_spread=-3.5, home_spread_open=None, home_ml=-160, away_ml=+140,
              fpi_home_p=0.62, fpi_home_margin=6.0, home_fpi=10.0, away_fpi=5.0,
              total=52.5, total_open=None, status="pre", home_score=None, away_score=None,
              neutral=False) -> ce.Game:
    home = ce.TeamSide(id="1", abbr="HOM", name="Home U", fpi=home_fpi,
                       ml=home_ml, spread=home_spread, spread_open=home_spread_open,
                       spread_price=-110, fpi_win_p=fpi_home_p, fpi_margin=fpi_home_margin,
                       score=home_score)
    away = ce.TeamSide(id="2", abbr="AWY", name="Away State", fpi=away_fpi,
                       ml=away_ml, spread=None if home_spread is None else -home_spread,
                       spread_open=None if home_spread_open is None else -home_spread_open,
                       spread_price=-110,
                       fpi_win_p=None if fpi_home_p is None else 1 - fpi_home_p,
                       fpi_margin=None if fpi_home_margin is None else -fpi_home_margin,
                       score=away_score)
    return ce.Game(id="g1", name="Away State at Home U", short="AWY @ HOM",
                   kickoff=dt.datetime(2026, 9, 12, 16, 0, tzinfo=UTC), status=status,
                   completed=status == "post", neutral=neutral, conf_game=False,
                   home=home, away=away, provider="DraftKings", total=total,
                   total_open=total_open)


# ---------------------------------------------------------------- odds math

def test_american_to_decimal():
    assert ce.american_to_decimal(+100) == pytest.approx(2.0)
    assert ce.american_to_decimal(-110) == pytest.approx(1.9091, abs=1e-4)
    assert ce.american_to_decimal(+250) == pytest.approx(3.5)
    assert ce.american_to_decimal(None) is None


def test_implied_prob_and_devig():
    assert ce.implied_prob(+100) == pytest.approx(0.5)
    ph, pa = ce.devig_pair(-110, -110)
    assert ph == pytest.approx(0.5) and pa == pytest.approx(0.5)
    ph, pa = ce.devig_pair(-200, +170)
    assert ph + pa == pytest.approx(1.0)
    assert ph > pa
    assert ce.devig_pair(None, +170) == (None, None)


def test_kelly_fraction():
    # 60% at even money -> f = (0.6*1 - 0.4)/1 = 0.2
    assert ce.kelly_fraction(0.6, 2.0) == pytest.approx(0.2)
    # no edge -> 0, never negative
    assert ce.kelly_fraction(0.5, 2.0) == 0.0
    assert ce.kelly_fraction(0.3, 2.0) == 0.0


def test_cover_probability_symmetric_and_monotone():
    assert ce.cover_probability(0.0, 0.0) == pytest.approx(0.5)
    p_plus = ce.cover_probability(6.0, 0.0)
    p_minus = ce.cover_probability(-6.0, 0.0)
    assert p_plus == pytest.approx(1 - p_minus)
    assert ce.cover_probability(13.5, 0.0) == pytest.approx(0.8413, abs=1e-3)  # one SD


def test_fmt_helpers_and_parsers():
    assert ce.fmt_spread(-3.5) == "-3.5"
    assert ce.fmt_spread(0) == "PK"
    assert ce.fmt_spread(None) == "—"
    assert ce.fmt_ml(+150) == "+150"
    assert ce._num("+3.5") == 3.5
    assert ce._num("PK") == 0.0
    assert ce._num("EVEN") == 0.0
    assert ce._num("") is None
    assert ce._int("-110") == -110


def test_next_saturday():
    assert ce.next_saturday(dt.date(2026, 9, 9)) == dt.date(2026, 9, 12)   # Wednesday
    assert ce.next_saturday(dt.date(2026, 9, 12)) == dt.date(2026, 9, 12)  # Saturday stays
    assert ce.next_saturday(dt.date(2026, 9, 13)) == dt.date(2026, 9, 19)  # Sunday rolls


def test_crosses_key():
    assert ce.crosses_key(2.5, 7.5) == "crosses 3,7"
    assert ce.crosses_key(-2.5, -7.5) == "crosses 3,7"   # sign-agnostic (abs)
    assert ce.crosses_key(4.0, 5.0) == ""
    assert ce.crosses_key(None, 5.0) == ""


# ---------------------------------------------------------------- signals

def test_spread_signal_strong_home_side():
    g = make_game(home_spread=-3.5, fpi_home_margin=10.0)       # FPI likes home by 6.5 more
    s = ce.spread_signal(g)
    assert s is not None and s.kind == "spread"
    assert s.side is g.home and s.strength == 2 and s.label == "STRONG ATS"
    assert s.edge == pytest.approx(6.5)
    assert s.line == -3.5 and s.truth_p > 0.5
    assert s.key_note == "crosses 7,10"    # market 3.5 -> FPI 10


def test_spread_signal_lean_away_side():
    g = make_game(home_spread=-7.0, fpi_home_margin=3.5)        # FPI: home only by 3.5
    s = ce.spread_signal(g)
    assert s.side is g.away and s.strength == 1 and s.label == "ATS lean"
    assert s.line == +7.0


def test_spread_signal_below_threshold_is_info_only():
    g = make_game(home_spread=-3.5, fpi_home_margin=5.0)
    s = ce.spread_signal(g)
    assert s is not None and s.strength == 0 and s.label == ""


def test_spread_signal_none_without_line_or_fpi():
    assert ce.spread_signal(make_game(home_spread=None)) is None
    assert ce.spread_signal(make_game(fpi_home_margin=None)) is None


def test_fcs_side_is_demoted_to_zero():
    g = make_game(home_spread=-40.5, fpi_home_margin=25.0, away_fpi=None)
    s = ce.spread_signal(g)
    assert s.edge == pytest.approx(15.5)
    assert s.strength == 0     # would be STRONG on points; FCS rule kills it


def test_blowout_number_caps_at_lean():
    g = make_game(home_spread=-30.5, fpi_home_margin=20.0)
    s = ce.spread_signal(g)
    assert s.edge == pytest.approx(10.5) and s.strength == 1


def test_steam_with_and_against():
    # line moved 2 pts toward home; FPI likes home -> "with"
    g = make_game(home_spread=-5.5, home_spread_open=-3.5, fpi_home_margin=12.0)
    s = ce.spread_signal(g)
    assert s.steam == "with" and s.strength == 2
    # line moved 2 pts toward away; FPI still likes home -> "against", demoted one tier
    g = make_game(home_spread=-3.5, home_spread_open=-5.5, fpi_home_margin=10.0)
    s = ce.spread_signal(g)
    assert s.steam == "against" and s.strength == 1
    assert "⚠market-moved-against" in ce.findings_warnings(s)


def test_ml_signal_picks_positive_edge_side():
    # fair home ~ 53%, FPI says 70% -> +32% -> STRONG ML on home
    g = make_game(home_ml=-115, away_ml=-105, fpi_home_p=0.70)
    s = ce.ml_signal(g)
    assert s.kind == "ml" and s.side is g.home and s.strength == 2
    assert s.price == -115 and s.truth_p == pytest.approx(0.70)


def test_ml_signal_long_dog_caps_and_price_limits():
    g = make_game(home_ml=-400, away_ml=+320, fpi_home_p=0.55)    # dog fair ~22%, FPI 45%
    s = ce.ml_signal(g)
    assert s.side is g.away and s.strength == 1                   # >+250 caps at value
    assert "⚠long-dog" in ce.findings_warnings(s)
    g = make_game(home_ml=-700, away_ml=+500, fpi_home_p=0.30)    # +500 beyond ML_MAX_PRICE
    assert ce.ml_signal(g).strength == 0


def test_ml_signal_none_when_no_edge_or_no_prices():
    g = make_game(home_ml=-160, away_ml=+140, fpi_home_p=0.58)    # fair home ~ 0.60
    assert ce.ml_signal(g) is None or ce.ml_signal(g).side is g.away
    assert ce.ml_signal(make_game(home_ml=None)) is None


def test_move_signals():
    g = make_game(home_spread=-9.5, home_spread_open=-3.5, fpi_home_margin=5.0)
    lm = ce.spread_move_signal(g)
    assert lm.kind == "spread-move" and lm.side is g.home and lm.edge == 6.0
    assert lm.truth_p is None and ce.stake_for(lm, 100) is None   # never staked
    assert ce.spread_move_signal(make_game(home_spread=-4.5, home_spread_open=-3.5)) is None
    tm = ce.total_move_signal(make_game(total=57.5, total_open=60.5))
    assert tm.kind == "total-move" and tm.edge == -3.0 and tm.strength == 1
    assert ce.total_move_signal(make_game(total=57.5, total_open=57.5)) is None


def test_stake_for_quarter_kelly_and_cap():
    s = ce.spread_signal(make_game(home_spread=-3.5, fpi_home_margin=10.0))
    st = ce.stake_for(s, 100)
    assert st is not None and 1 <= st <= 100 * ce.MAX_TICKET_PCT
    assert ce.stake_for(s, 10_000) == 10_000 * ce.MAX_TICKET_PCT   # cap binds
    info = ce.spread_signal(make_game(home_spread=-3.5, fpi_home_margin=5.0))
    assert ce.stake_for(info, 100) is None                          # strength 0


def test_ranked_signals_orders_strong_first_and_skips_started_games():
    strong = make_game(home_spread=-3.5, fpi_home_margin=10.0)
    lean = make_game(home_spread=-7.0, fpi_home_margin=3.5)
    lean.id = "g2"
    done = make_game(home_spread=-3.5, fpi_home_margin=10.0, status="post", home_score=30, away_score=10)
    done.id = "g3"
    sigs = ce.ranked_signals([lean, strong, done])
    assert sigs[0].game is strong and sigs[0].strength == 2
    assert all(s.game is not done for s in sigs)
    assert any(s.game is done for s in ce.ranked_signals([done], include_done=True))


# ---------------------------------------------------------------- grading

@pytest.mark.parametrize("kind,home,line,hs,as_,exp", [
    ("ml", True, None, 24, 20, "W"),
    ("ml", False, None, 24, 20, "L"),
    ("ml", True, None, 20, 20, "P"),
    ("spread", True, -3.5, 24, 20, "W"),     # home -3.5 wins by 4
    ("spread", True, -3.5, 23, 20, "L"),     # wins by 3, fails to cover
    ("spread", False, +3.5, 23, 20, "W"),    # away +3.5 loses by 3
    ("spread", True, -3.0, 23, 20, "P"),
    ("over", True, 44.5, 24, 21, "W"),
    ("under", True, 44.5, 24, 21, "L"),
    ("over", True, 45.0, 24, 21, "P"),
])
def test_grade(kind, home, line, hs, as_, exp):
    assert ce._grade(kind, home, line, hs, as_) == exp


def test_profit():
    assert ce._profit("W", 10, -110) == pytest.approx(9.09, abs=0.01)
    assert ce._profit("W", 10, +150) == pytest.approx(15.0)
    assert ce._profit("L", 10, -110) == -10
    assert ce._profit("P", 10, -110) == 0.0


# ---------------------------------------------------------------- sqlite round trip

def test_db_roundtrip_persist_paper_settle(tmp_path):
    db = tmp_path / "t.db"
    conn = ce.db_connect(str(db))
    now = dt.datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    g = make_game(home_spread=-3.5, fpi_home_margin=10.0)
    assert ce.db_persist(conn, [g], now) == 1
    n = ce.db_paper_log(conn, ce.ranked_signals([g]), 100, now)
    assert n >= 1
    # logging again is idempotent
    assert ce.db_paper_log(conn, ce.ranked_signals([g]), 100, now) == 0
    # nothing settles while the game is pre
    assert ce.db_settle_paper(conn)[0] == 0
    # final: home wins 30-10 -> home -3.5 covers, home ML wins
    g.status, g.completed = "post", True
    g.home.score, g.away.score = 30, 10
    ce.db_persist(conn, [g], now + dt.timedelta(hours=8))
    settled, staked, profit = ce.db_settle_paper(conn)
    assert settled == n and staked > 0 and profit > 0
    rows = conn.execute("SELECT kind, result FROM paper_bets").fetchall()
    assert all(r == "W" for _, r in rows)
    assert "spread" in ce.db_paper_summary(conn)


def test_migrations_add_missing_columns(tmp_path):
    db = tmp_path / "old.db"
    import sqlite3
    raw = sqlite3.connect(str(db))
    raw.execute("CREATE TABLE paper_bets (id INTEGER PRIMARY KEY, game_id TEXT)")
    raw.commit()
    raw.close()
    conn = ce.db_connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_bets)")}
    assert "backfill" in cols


# ---------------------------------------------------------------- bets.csv ledger

def test_bets_csv_log_and_settle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conn = ce.db_connect(str(tmp_path / "t.db"))
    g = make_game(status="post", home_score=27, away_score=20)
    ce.db_persist(conn, [g], dt.datetime.now(UTC))
    ce.log_bet(g, "spread", "Away State", +3.5, -110, 5.0)
    ce.log_bet(g, "ml", "Home U", None, -160, 8.0)
    ce.log_bet(g, "under", "under", 50.5, -105, 4.0)
    assert ce.settle_bets(conn) == 3
    txt = ce.show_bets()
    # away +3.5 lost by 7 -> L ; home ML -> W ; under 50.5 with 47 -> W
    assert txt.count(" L ") >= 1 and txt.count(" W ") >= 2
    assert os.path.exists(ce.BETS_CSV)


# ---------------------------------------------------------------- analysis helper parity

def test_wilson_matches_known_value():
    from analysis._shared.load_data import wilson
    p, lo, hi = wilson(7, 13)
    assert p == pytest.approx(7 / 13)
    assert (lo, hi) == pytest.approx((0.2914, 0.7680), abs=1e-3)


# ---------------------------------------------------------------- picks board / day-aware report

def test_ml_signal_fcs_side_is_never_staked():
    g = make_game(home_ml=-160, away_ml=+140, fpi_home_p=0.75, away_fpi=None)    # would be STRONG ML
    assert ce.ml_signal(g).strength == 0
    assert ce.stake_for(ce.ml_signal(g), 100.0) is None


def test_pick_signals_filters_to_clean_stakeable_tickets():
    clean = make_game(home_spread=-3.5, fpi_home_margin=11.0)                      # STRONG ATS, no flags
    against = make_game(home_spread=-3.5, home_spread_open=-6.5, fpi_home_margin=11.0)  # steam against
    against.id = "g2"
    fcs = make_game(home_spread=-40.5, fpi_home_margin=25.0, away_fpi=None)
    fcs.id = "g3"
    started = make_game(home_spread=-3.5, fpi_home_margin=11.0, status="in")
    started.id = "g4"
    picks = ce.pick_signals([against, fcs, started, clean], 100.0)
    assert [s.game.id for s, _ in picks] == ["g1"]              # one ticket per game, only the clean one
    s, st = picks[0]
    assert s.kind == "spread" and s.strength == 2 and st == ce.stake_for(s, 100.0)
    assert ce.pick_signals([against, fcs], 100.0) == []
    assert "no play" in ce.render_picks([against], 100.0, False)
    assert "Home U -3.5" in ce.render_picks([clean], 100.0, False)


def test_pick_signals_respects_max_and_report_is_day_aware(tmp_path, monkeypatch):
    games = []
    for i in range(8):
        g = make_game(home_spread=-3.5, fpi_home_margin=11.0)
        g.id = f"p{i}"
        games.append(g)
    assert len(ce.pick_signals(games, 100.0)) == ce.MAX_PICKS
    monkeypatch.setattr(ce, "REPORTS_DIR", str(tmp_path))
    now = dt.datetime(2026, 9, 11, 12, 0, tzinfo=ce.LOCAL_TZ)
    fri = ce.write_report(games, 100.0, dt.date(2026, 9, 11), now, "ledger")
    sat = ce.write_report(games, 100.0, dt.date(2026, 9, 12), now, "ledger")
    assert fri.endswith("friday-2026-09-11.md") and sat.endswith("saturday-2026-09-12.md")
    txt = open(fri, encoding="utf-8").read()
    assert "Friday, September 11, 2026" in txt and "## 0. Picks board" in txt
    assert txt.count("| **STRONG ATS** |") >= ce.MAX_PICKS


def test_side_is_home_short_names():
    h, a = "Missouri State Bears", "Marshall Thundering Herd"
    assert ce._side_is_home("Missouri State", h, a) is True
    assert ce._side_is_home("Marshall", h, a) is False
    assert ce._side_is_home("Missouri State Bears", h, a) is True
    assert ce._side_is_home("home", h, a) is True and ce._side_is_home("away", h, a) is False
    # ambiguous (matches both) or unknown -> None, never a silent away grade
    assert ce._side_is_home("Texas", "Texas Longhorns", "Texas State Bobcats") is None
    assert ce._side_is_home("Nobody", h, a) is None
    assert ce._side_is_home("Florida International", "Florida Atlantic Owls",
                            "Florida International Panthers") is False


def test_settle_bets_short_home_name_grades_home(tmp_path, monkeypatch):
    # regression: 9/19/2026 ledger graded a home ML dog that lost as a W because the
    # short side name never matched the full home name and fell through to "away"
    monkeypatch.chdir(tmp_path)
    conn = ce.db_connect(str(tmp_path / "t.db"))
    g = make_game(status="post", home_score=24, away_score=30)   # home lost
    ce.db_persist(conn, [g], dt.datetime.now(UTC))
    ce.log_bet(g, "ml", "Home", None, +160, 5.0)                 # short form of "Home U"
    assert ce.settle_bets(conn) == 1
    assert " L " in ce.show_bets()


# ---------------------------------------------------------------- 2026-09-20 demotions

def test_spread_overreach_caps_at_lean():
    # Δ 8+ is where FPI is most often wrong (42.5% cover, n=40): never STRONG
    g = make_game(home_spread=-3.5, fpi_home_margin=12.0)          # Δ 8.5
    s = ce.spread_signal(g)
    assert s.edge == pytest.approx(8.5) and s.strength == 1
    assert "⚠overreach" in ce.findings_warnings(s)
    g = make_game(home_spread=-3.5, fpi_home_margin=10.0)          # Δ 6.5 still STRONG
    assert ce.spread_signal(g).strength == 2


def test_ml_dead_zone_dog_never_staked():
    # +100..+150 dogs: 28.8% hit on 59 bets. Edge is there on paper, strength is 0.
    g = make_game(home_ml=-150, away_ml=+130, fpi_home_p=0.45)     # away fair ~41%, FPI 55%
    s = ce.ml_signal(g)
    assert s.side is g.away and s.edge > ce.ML_EDGE_PCT and s.strength == 0
    assert "⚠dead-zone-dog" in ce.findings_warnings(s)
    assert ce.stake_for(s, 100.0) is None
    g = make_game(home_ml=-200, away_ml=+170, fpi_home_p=0.45)     # +170 outside the zone
    assert ce.ml_signal(g).strength >= 1


def test_stakes_banner_says_paper_only():
    assert ce.LIVE_STAKES is False
    b = ce.stakes_banner()
    assert "PAPER ONLY" in b and ce.FINDINGS_AS_OF in b
    assert b in ce.render_top([make_game(fpi_home_margin=10.0)], 100.0, color=False)


def test_just_win_board_favourites_at_a_holdable_price(tmp_path):
    # FPI 70% home, DK -198/+160 -> fair ~65%: in the window, small gap -> qualifies
    ok = make_game(home_spread=-5.5, home_ml=-198, away_ml=+160, fpi_home_p=0.70)
    s = ce.just_win_signal(ok)
    assert s and s.kind == "just-win" and s.side is ok.home and s.strength == 1
    assert 0 < s.edge < ce.JUST_WIN_MAX_EDGE_PCT and s.price == -198
    assert ce.just_win_roi(s) > 0
    # too short, plus money, gap too big, FPI too low, steam against, FCS side: all out
    assert ce.just_win_signal(make_game(home_ml=-400, away_ml=+300, fpi_home_p=0.85)) is None
    assert ce.just_win_signal(make_game(home_ml=+105, away_ml=-125, fpi_home_p=0.62)) is None
    assert ce.just_win_signal(make_game(home_ml=-130, away_ml=+110, fpi_home_p=0.80)) is None
    assert ce.just_win_signal(make_game(home_ml=-150, away_ml=+130, fpi_home_p=0.58)) is None
    assert ce.just_win_signal(make_game(home_spread=-3.5, home_spread_open=-6.5,
                                        home_ml=-198, away_ml=+160, fpi_home_p=0.70)) is None
    assert ce.just_win_signal(make_game(home_ml=-198, away_ml=+160, fpi_home_p=0.70,
                                        away_fpi=None)) is None
    # board ranks by win prob, report carries the section, ledger grades it like a moneyline
    two = make_game(home_ml=-140, away_ml=+120, fpi_home_p=0.64)
    two.id = "g2"
    board = ce.just_win_board([two, ok])
    assert [x.game.id for x in board] == ["g1", "g2"]
    assert "Home U vs Away State" in ce.render_just_win([two, ok], False)
    assert ce._grade("just-win", True, None, 21, 17) == "W"
    conn = ce.db_connect(str(tmp_path / "t.db"))
    now = dt.datetime(2026, 9, 26, 8, 0, tzinfo=ce.LOCAL_TZ)
    assert ce.db_paper_log(conn, board, 100.0, now) == 2
    assert conn.execute("SELECT COUNT(*) FROM paper_bets WHERE kind='just-win'").fetchone()[0] == 2
    monkey_dir = tmp_path / "reports"
    ce.REPORTS_DIR = str(monkey_dir)
    path = ce.write_report([two, ok], 100.0, dt.date(2026, 9, 26), now, "")
    text = open(path, encoding="utf-8").read()
    assert "## 0b. Just-win board" in text and "**Home U vs Away State**" in text
    ce.REPORTS_DIR = "reports"
    # FPI only a hair above fair: +edge but the vig makes it -EV, so it is out
    assert ce.just_win_signal(make_game(home_ml=-230, away_ml=+190, fpi_home_p=0.67)) is None
