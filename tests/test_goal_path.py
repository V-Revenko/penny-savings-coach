"""Goal path chart data: where the safe pace leads, shown next to the target date."""
import math

import pytest

from app import engine

AS_OF = "2026-09-01"


def test_mbr_0026_reaches_goal_in_three_months_at_safe_pace(tables):
    p = engine.goal_projection("MBR-0026", AS_OF, tables)
    assert p["monthly_pace"] == pytest.approx(723.25, abs=0.01)
    assert p["remaining"] == pytest.approx(1640.13, abs=0.01)
    assert p["months_to_goal"] == math.ceil(1640.13 / 723.25) == 3
    assert p["projected_date"] == "2026-12-01"
    assert p["on_pace"] is True


def test_series_starts_at_saved_and_ends_at_target(tables):
    p = engine.goal_projection("MBR-0026", AS_OF, tables)
    assert p["series"][0]["date"] == "2026-09-01"
    assert p["series"][0]["amount"] == pytest.approx(p["saved"])
    assert p["series"][-1]["amount"] == pytest.approx(p["target"])
    amounts = [s["amount"] for s in p["series"]]
    assert amounts == sorted(amounts)  # never goes backward
    assert max(amounts) <= p["target"]


def test_no_safe_amount_means_progress_only_no_projection(tables):
    p = engine.goal_projection("MBR-0081", AS_OF, tables)  # hand-off member, pace 0
    assert p["monthly_pace"] == 0
    assert p["months_to_goal"] is None
    assert p["projected_date"] is None
    assert p["on_pace"] is False
    assert all(s["amount"] == pytest.approx(p["saved"]) for s in p["series"])


def test_checkin_savings_move_the_path_forward(tables):
    base = engine.goal_projection("MBR-0026", AS_OF, tables)
    more = engine.goal_projection("MBR-0026", AS_OF, tables, extra_saved=1000.0)
    assert more["saved"] == pytest.approx(base["saved"] + 1000.0)
    assert more["remaining"] == pytest.approx(base["remaining"] - 1000.0)
    assert more["months_to_goal"] <= base["months_to_goal"]


def test_goal_already_reached(tables):
    p = engine.goal_projection("MBR-0026", AS_OF, tables, extra_saved=10_000.0)
    assert p["remaining"] == 0
    assert p["months_to_goal"] == 0


def test_needed_monthly_is_remaining_over_months_until_target(tables):
    p = engine.goal_projection("MBR-0026", AS_OF, tables)
    assert p["months_until_target"] > 0
    assert p["needed_monthly"] == pytest.approx(p["remaining"] / p["months_until_target"])


def test_series_is_capped(tables):
    for member_id in tables["Members"]["Member_ID"]:
        p = engine.goal_projection(member_id, AS_OF, tables)
        assert len(p["series"]) <= engine.MAX_PROJECTION_MONTHS + 1


def test_api_goal_path_includes_checkin_savings():
    from app import main

    main._load(force=True)
    main._replay_state("MBR-0026", "2026-06-01")
    before = main.goal_path(member_id="MBR-0026")
    assert before["checkin_saved"] == 0
    main.replay_advance(main.ReplayRequest(member_id="MBR-0026"))  # first payday is a miss
    main.decide(main.Decision(member_id="MBR-0026", action="accept"))
    main.replay_advance(main.ReplayRequest(member_id="MBR-0026"))
    after = main.goal_path(member_id="MBR-0026")
    assert after["checkin_saved"] >= 0
    assert after["saved"] == pytest.approx(before["saved"] + after["checkin_saved"], abs=0.01)
    main._load(force=True)
