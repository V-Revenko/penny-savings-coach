"""Goals tab: the goal, and a deposit history read from the coach_checkin table."""
from pathlib import Path

import pytest

from app import data, engine, main

MEMBER = "MBR-0026"
STATIC = Path(main.STATIC_DIR) / "index.html"


@pytest.fixture()
def fresh():
    main._load(force=True)  # also clears stored check-ins
    main._replay_state(MEMBER, "2026-06-01")
    yield
    main._load(force=True)


def test_empty_state_after_reset(fresh):
    out = main.goals(member_id=MEMBER)
    assert out["deposits"] == []
    assert out["goal"]["type"] == "Home Down Payment"
    assert out["goal"]["target"] == pytest.approx(5250.63)
    assert out["goal"]["saved"] == pytest.approx(3610.50)
    assert out["goal"]["remaining"] == pytest.approx(1640.13)
    assert out["goal"]["progress"] == pytest.approx(3610.50 / 5250.63, abs=1e-4)


def test_empty_state_message_is_in_the_page():
    html = STATIC.read_text(encoding="utf-8")
    assert "No deposits yet. Accept a recommendation on the Coach tab to get started." in html


def test_populated_history_newest_first_with_outcomes(fresh):
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # Jun 12: a real miss
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # Jun 19: a partial save
    out = main.goals(member_id=MEMBER)
    dates = [d["date"] for d in out["deposits"]]
    assert dates == ["2026-06-19", "2026-06-12"]  # newest first
    partial, miss = out["deposits"]
    assert partial["outcome"] == "partial" and partial["amount"] == pytest.approx(17.40)
    assert miss["outcome"] == "none" and miss["amount"] == 0


def test_accepting_every_offer_gives_this_history_for_mbr_0026(fresh):
    """Saving the full offer each payday: what lands is capped by that week's real leftover cash."""
    for _ in range(6):
        main.decide(main.Decision(member_id=MEMBER, action="accept"))
        main.replay_advance(main.ReplayRequest(member_id=MEMBER))
    got = [(d["date"], d["amount"], d["planned"], d["outcome"]) for d in reversed(main.goals(member_id=MEMBER)["deposits"])]
    assert got == [
        ("2026-06-12", 0.0, 153.18, "none"),      # spent $862.28 more than came in: nothing left to move
        ("2026-06-19", 17.40, 76.59, "partial"),  # offer stepped down after the miss
        ("2026-06-26", 153.18, 153.18, "full"),
        ("2026-07-03", 153.18, 153.18, "full"),
        ("2026-07-10", 0.0, 100.57, "none"),
        ("2026-07-17", 50.28, 50.28, "full"),
    ]


def test_approved_plan_shows_immediately_and_is_replaced_at_next_payday(fresh):
    assert main.goals(member_id=MEMBER)["deposits"] == []
    main.decide(main.Decision(member_id=MEMBER, action="accept"))
    first = main.goals(member_id=MEMBER)["deposits"]
    assert len(first) == 1 and first[0]["outcome"] == "pending"
    # Pending row is dated for the upcoming payday (when it will actually land), not today.
    assert first[0]["amount"] == pytest.approx(153.18) and first[0]["date"] == "2026-06-12"

    main.decide(main.Decision(member_id=MEMBER, action="adjust", amount=100.00))  # changing the amount updates it
    assert main.goals(member_id=MEMBER)["deposits"][0]["amount"] == pytest.approx(100.00)

    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # real result replaces the plan
    after = main.goals(member_id=MEMBER)["deposits"]
    assert len(after) == 1 and after[0]["outcome"] == "none" and after[0]["date"] == "2026-06-12"


def test_not_now_adds_no_history_row(fresh):
    main.decide(main.Decision(member_id=MEMBER, action="not_now"))
    assert main.goals(member_id=MEMBER)["deposits"] == []


def test_short_rows_explain_planned_versus_moved_in_the_page():
    html = STATIC.read_text(encoding="utf-8")
    assert "was actually left over that week, so that's what moved" in html


@pytest.mark.skipif(not data.db_available(), reason="gesa.db not built")
def test_history_comes_from_the_coach_checkin_table(fresh):
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))
    rows = data.read_checkins(MEMBER)
    assert len(rows) == 1
    assert rows[0]["payday_date"] == "2026-06-12"
    assert rows[0]["planned_amount"] == pytest.approx(153.18)
    assert main.goals(member_id=MEMBER)["source"] == "coach_checkin"


@pytest.mark.skipif(not data.db_available(), reason="gesa.db not built")
def test_reset_clears_stored_history(fresh):
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))
    assert data.read_checkins(MEMBER)
    main._replay_state(MEMBER, "2026-06-01")  # what the presenter's reset does
    assert data.read_checkins(MEMBER) == []
    assert main.goals(member_id=MEMBER)["deposits"] == []


def test_confirmed_found_money_appears_once_savings_land(fresh):
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # miss: nothing landed
    item = engine.leaks(MEMBER, main._replay_state(MEMBER)["as_of"], main._state["tables"])["items"][0]
    main.leaks_confirm(main.LeakConfirm(member_id=MEMBER, item_id=item["id"], confirmed=True))
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # partial save landed
    deposits = main.goals(member_id=MEMBER)["deposits"]
    landed, missed = deposits
    assert missed["found_money"] == []
    assert [f["merchant"] for f in landed["found_money"]] == [item["merchant"]]
    assert landed["found_money"][0]["monthly_amount"] == pytest.approx(item["monthly_amount"], abs=0.01)


def test_goals_tab_is_in_the_bar_between_coach_and_settings():
    html = STATIC.read_text(encoding="utf-8")
    labels = [html.index(f"<span>{n}</span>") for n in ("Accounts", "Transfer", "Coach", "Goals", "Settings")]
    assert labels == sorted(labels)
    assert "<span>More</span>" not in html
    assert 'id="tabbar-goals" onclick="show(\'goals\')"' in html
    assert 'id="tabbar-settings" onclick="show(\'settings\')"' in html
    assert 'onclick="show(\'coach\')"><svg' in html
    # Accounts and Transfer stay disabled
    for name in ("Accounts", "Transfer"):
        tab = html[html.index(f"<span>{name}</span>") - 700 : html.index(f"<span>{name}</span>")]
        assert 'class="tabitem" disabled' in tab
