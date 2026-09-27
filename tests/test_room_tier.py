"""The middle tier: a small negative surplus means "find extra room first", not a counselor hand-off."""
import pandas as pd
import pytest

from app import engine, main

AS_OF = "2026-09-01"
SHIFTED = ["MBR-0022", "MBR-0029", "MBR-0036", "MBR-0039"]
ROOM_MESSAGE = (
    "Right now there's not quite enough room to save safely, but you're very close. "
    "Let's focus on finding some extra room first."
)


# ---------------------------------------------------------------- the threshold, at its boundary
@pytest.mark.parametrize(
    "surplus, income, expected",
    [
        (-150.00, 2000.0, True),    # exactly $150: within the dollar limit
        (-150.01, 2000.0, False),   # a cent over, and 7.5% of income: not marginal
        (-199.99, 4000.0, True),    # just under 5% of $4,000
        (-200.00, 4000.0, False),   # exactly 5%: the percentage rule is "under"
        (-150.01, 10000.0, True),   # over $150 but only 1.5% of a large income
        (-243.73, 3876.73, False),  # 6.3% of income and over $150 (MBR-0016's June window)
        (0.0, 3000.0, False),       # no shortfall at all
        (500.0, 3000.0, False),     # a positive surplus is never marginal
        (-1200.55, 2406.33, False), # MBR-0062
    ],
)
def test_marginal_shortfall_boundary(surplus, income, expected):
    assert engine.is_marginal_shortfall(surplus, income) is expected


def _debts_for_test_member(tables, late: bool):
    """Debts with MBR-0026's rows replaced by one debt that is current, or 60+ days late."""
    debts = tables["Debts"]
    others = debts[debts["Member_ID"] != "MBR-0026"]
    row = debts[debts["Member_ID"] == "MBR-0026"].iloc[[0]].copy()
    row["Payment_Status"] = "60+ days late" if late else "Current"
    return pd.concat([others, row], ignore_index=True)


def _fake_facts(surplus, income, debt_min=500.0):
    return {"surplus": surplus, "monthly_income": income, "debt_minimums": debt_min}


@pytest.mark.parametrize(
    "surplus, income, late, expect_handoff, expect_room",
    [
        (-150.00, 2000.0, False, False, True),    # small shortfall, nothing late: find room
        (-150.01, 2000.0, False, True, False),    # just past the boundary: full hand-off
        (-199.99, 4000.0, False, False, True),
        (-200.00, 4000.0, False, True, False),
        (-100.00, 4000.0, True, True, False),     # small shortfall but a 60+ day late debt: hand-off
        (300.0, 4000.0, False, False, False),     # a healthy surplus: normal recommendation
        (300.0, 4000.0, True, True, False),       # late debt alone is still a hand-off
    ],
)
def test_handoff_and_room_at_the_boundary(monkeypatch, tables, surplus, income, late, expect_handoff, expect_room):
    monkeypatch.setattr(engine, "safe_to_save", lambda m, a, t=None: _fake_facts(surplus, income))
    t = {**tables, "Debts": _debts_for_test_member(tables, late)}
    assert engine.handoff_needed("MBR-0026", AS_OF, t)[0] is expect_handoff
    assert engine.room_needed("MBR-0026", AS_OF, t)[0] is expect_room


def test_a_small_shortfall_with_a_late_debt_names_the_late_debt_as_the_reason(monkeypatch, tables):
    monkeypatch.setattr(engine, "safe_to_save", lambda m, a, t=None: _fake_facts(-80.0, 3000.0))
    t = {**tables, "Debts": _debts_for_test_member(tables, late=True)}
    assert engine.handoff_needed("MBR-0026", AS_OF, t) == (True, "A debt is 60+ days late.")


# ---------------------------------------------------------------- the real dataset
def test_exactly_these_members_shift_from_handoff_to_find_room(raw, tables):
    p = engine.portfolio_check(AS_OF, raw, tables)
    assert sorted(p["find_room_members"]) == SHIFTED
    assert p["find_room_count"] == 4
    assert p["recommended_count"] == 38                                    # savings recommendations unchanged
    assert p["recommended_count"] + p["find_room_count"] + p["handoff_count"] == 100
    assert p["handoff_by_reason"]["Monthly spending plus debt minimums exceed income."] == 46
    assert p["handoff_by_reason"]["A debt is 60+ days late."] == 12        # 10 before, plus 0016 and 0063


@pytest.mark.parametrize("member", SHIFTED)
def test_shifted_members_are_no_longer_handed_off(tables, member):
    assert engine.handoff_needed(member, AS_OF, tables)[0] is False
    assert engine.room_needed(member, AS_OF, tables)[0] is True
    assert engine.safe_to_save(member, AS_OF, tables)["surplus"] < 0


def test_mbr_0062_is_still_a_full_handoff(tables):
    f = engine.safe_to_save("MBR-0062", AS_OF, tables)
    assert f["surplus"] == pytest.approx(-1200.55, abs=0.01)
    assert engine.handoff_needed("MBR-0062", AS_OF, tables) == (True, "Monthly spending plus debt minimums exceed income.")
    assert engine.room_needed("MBR-0062", AS_OF, tables)[0] is False


def test_other_handoff_members_are_untouched(tables):
    assert engine.handoff_needed("MBR-0081", AS_OF, tables)[0] is True     # -$576
    assert engine.handoff_needed("MBR-0008", AS_OF, tables)[0] is True     # 9.7% of income
    assert engine.room_needed("MBR-0026", AS_OF, tables)[0] is False       # healthy member


# ---------------------------------------------------------------- what the member sees
@pytest.fixture()
def room_member():
    main._load(force=True)
    main._replay_state("MBR-0036", "2026-09-01")
    yield "MBR-0036"
    main._load(force=True)


def test_recommendation_shows_the_soft_message_and_no_amount(room_member):
    out = main.recommendation(member_id=room_member, as_of=None, corrupt=False)
    assert out["handoff"]["needed"] is False
    assert out["room"]["needed"] is True and out["room"]["message"] == ROOM_MESSAGE
    assert out["message"]["text"] == ROOM_MESSAGE and out["message"]["source"] == "template"
    assert out["amount"] == 0
    assert out["validator"]["passed"] is True


def test_found_money_is_the_next_action_without_saving_language(room_member):
    out = main.leaks(member_id=room_member, corrupt=False)
    assert out["room"] is True and out["handoff"] is False
    assert out["items"], "the found money card needs items to act on"
    assert "goal" not in out["message"]["text"].lower() and "sav" not in out["message"]["text"].lower()


def test_freeing_up_money_never_creates_a_savings_amount(room_member):
    item = engine.leaks(room_member, "2026-09-01", main._state["tables"])["items"][0]
    main.leaks_confirm(main.LeakConfirm(member_id=room_member, item_id=item["id"], confirmed=True))
    out = main.recommendation(member_id=room_member, as_of=None, corrupt=False)
    assert out["amount"] == 0 and out["room"]["needed"] is True
    decision = main.decide(main.Decision(member_id=room_member, action="accept"))
    assert decision["planned_amount"] == 0


def test_marginal_late_debt_member_still_gets_the_counselor_handoff():
    main._load(force=True)
    main._replay_state("MBR-0016", "2026-09-01")
    out = main.recommendation(member_id="MBR-0016", as_of=None, corrupt=False)
    assert out["handoff"]["needed"] is True and out["handoff"]["reason"] == "A debt is 60+ days late."
    assert out["room"] == {"needed": False}
    main._load(force=True)


def test_auto_contribute_does_nothing_in_the_find_room_tier(room_member):
    main.update_settings(main.SettingsUpdate(member_id=room_member, auto_contribute=True))
    main._replay_state(room_member, "2026-06-01")
    res = main.replay_advance(main.ReplayRequest(member_id=room_member))
    assert res["auto_note"] is None or res["auto_note"]["kind"] != "auto_contribute"


def test_page_shows_the_room_card_and_portfolio_row():
    from pathlib import Path

    html = (Path(main.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    assert "Let's find some extra room" in html and "Ways to find extra room" in html
    assert "Find extra room first" in html
