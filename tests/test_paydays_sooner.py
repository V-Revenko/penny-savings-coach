"""Adjust slider: how many paydays sooner the full safe amount finishes the goal."""
import re
from pathlib import Path

import pytest

from app import engine, main

STATIC = Path(main.STATIC_DIR) / "index.html"
MEMBER = "MBR-0026"


def test_paydays_to_goal_is_remaining_over_amount_rounded_up():
    assert engine.paydays_to_goal(1640.13, 153.18) == 11   # 10.71 -> 11
    assert engine.paydays_to_goal(1640.13, 100.00) == 17   # 16.40 -> 17
    assert engine.paydays_to_goal(300.00, 100.00) == 3     # exact: no extra payday from float noise
    assert engine.paydays_to_goal(0, 100.00) == 0
    assert engine.paydays_to_goal(500.00, 0) is None


def test_full_amount_gets_there_sooner_than_a_lower_one():
    out = engine.paydays_sooner(1640.13, 153.18, 100.00)
    assert out["paydays_at_full"] == 11
    assert out["paydays_at_chosen"] == 17
    assert out["sooner_by"] == 6


def test_no_comparison_when_the_chosen_amount_is_the_full_amount():
    assert engine.paydays_sooner(1640.13, 153.18, 153.18)["sooner_by"] is None
    assert engine.paydays_sooner(1640.13, 153.18, 153.179)["sooner_by"] is None  # within a cent


def test_no_comparison_when_both_finish_on_the_same_payday():
    # 1640.13 / 150 = 10.93 and / 153.18 = 10.71: both take 11 paydays
    assert engine.paydays_sooner(1640.13, 153.18, 150.00)["sooner_by"] is None


def test_no_comparison_at_zero_and_amounts_above_full_are_capped():
    assert engine.paydays_sooner(1640.13, 153.18, 0)["sooner_by"] is None
    over = engine.paydays_sooner(1640.13, 153.18, 999.0)
    assert over["chosen_amount"] == 153.18 and over["sooner_by"] is None


def test_one_payday_sooner_is_singular_in_the_page_text():
    html = STATIC.read_text(encoding="utf-8")
    assert "payday${d.sooner_by === 1 ? '' : 's'} sooner than at" in html
    assert "you'd reach your goal about" in html


def test_api_compares_against_the_full_offer():
    main._load(force=True)
    main._replay_state(MEMBER, "2026-06-01")
    out = main.paydays_sooner(member_id=MEMBER, amount=100.0)
    assert out["full_amount"] == pytest.approx(153.18)
    assert out["chosen_amount"] == pytest.approx(100.0)
    assert out["paydays_at_full"] == 11 and out["paydays_at_chosen"] == 17 and out["sooner_by"] == 6
    assert main.paydays_sooner(member_id=MEMBER, amount=153.18)["sooner_by"] is None
    main._load(force=True)


def test_slider_updates_the_line_live_and_the_line_is_encouraging():
    html = STATIC.read_text(encoding="utf-8")
    assert 'id="soonerLine"' in html and "updateSooner(this.value)" in html
    sentence = html[html.index("At ${money(d.full_amount)}") : html.index("sooner than at")]
    for discouraging in ("only", "unfortunately", "too low", "slow", "behind", "worse"):
        assert discouraging not in sentence.lower()


def test_not_now_is_a_single_tap_with_no_confirmation():
    html = STATIC.read_text(encoding="utf-8")
    assert not re.search(r"\bconfirm\(|are you sure|Are you sure", html)
    assert html.count("onclick=\"decide('not_now')\"") >= 1
