"""Settings tab and auto-contribute."""
from pathlib import Path

import pytest

from app import engine, main

MEMBER = "MBR-0026"
HANDOFF_MEMBER = "MBR-0081"
STATIC = Path(main.STATIC_DIR) / "index.html"


@pytest.fixture()
def fresh():
    main._load(force=True)  # also clears settings and stored check-ins
    main._replay_state(MEMBER, "2026-06-01")
    yield
    main._load(force=True)


def _advance(member=MEMBER):
    return main.replay_advance(main.ReplayRequest(member_id=member))


def _set_auto(on, member=MEMBER):
    return main.update_settings(main.SettingsUpdate(member_id=member, auto_contribute=on))


def test_auto_contribute_is_off_by_default(fresh):
    out = main.get_settings(member_id=MEMBER)
    assert out["auto_contribute"] is False
    assert out["window_hours"] == 1


def test_toggle_off_leaves_current_behavior_unchanged(fresh):
    res = _advance()
    assert res["auto_note"] is None
    st = main._replay_state(MEMBER)
    assert st["planned"] is None and st["notes"] == []          # nothing applied for the member
    assert main.goals(member_id=MEMBER)["deposits"][0]["outcome"] == "none"  # only the real check-in row
    # manual Save still works exactly as before
    main.decide(main.Decision(member_id=MEMBER, action="accept"))
    assert main._replay_state(MEMBER)["planned"] == pytest.approx(76.59)


def test_toggle_on_applies_the_safe_amount_and_notifies(fresh):
    _set_auto(True)
    res = _advance()  # Jun 5 -> Jun 12: the first period was a miss, so the offer is $76.59
    note = res["auto_note"]
    assert note["kind"] == "auto_contribute" and note["status"] == "active"
    assert note["amount"] == pytest.approx(76.59)
    assert note["message"] == (
        "Penny moved $76.59 toward your Home Down Payment. Change the amount or cancel within 1 hour."
    )
    st = main._replay_state(MEMBER)
    assert st["planned"] == pytest.approx(76.59)                  # exactly as if they tapped Save
    assert res["state"]["auto_note"]["id"] == note["id"]
    assert st["unread"] >= 2                                       # check-in + notification
    pending = main.goals(member_id=MEMBER)["deposits"][0]
    assert pending["outcome"] == "pending" and pending["amount"] == pytest.approx(76.59)
    # and it settles like any accepted plan at the next payday
    settled = _advance()["checkin"]
    assert settled["planned_amount"] == pytest.approx(76.59) and settled["saved_amount"] == pytest.approx(17.40)


def test_auto_amount_matches_what_save_would_have_planned(fresh):
    _set_auto(True)
    _advance()
    auto_planned = main._replay_state(MEMBER)["planned"]
    main.decide(main.Decision(member_id=MEMBER, action="accept"))
    assert main._replay_state(MEMBER)["planned"] == pytest.approx(auto_planned)


def test_handoff_blocks_auto_contribute_even_with_the_toggle_on():
    main._load(force=True)
    main._replay_state(HANDOFF_MEMBER, "2026-06-01")
    _set_auto(True, HANDOFF_MEMBER)
    tables = main._state["tables"]
    nxt = engine.next_payday(HANDOFF_MEMBER, main._replay_state(HANDOFF_MEMBER)["as_of"], tables)
    assert engine.handoff_needed(HANDOFF_MEMBER, nxt, tables)[0] is True  # precondition: hand-off at that payday
    res = _advance(HANDOFF_MEMBER)
    note = res["auto_note"]
    assert note["kind"] == "auto_paused"
    assert note["message"] == "Paused: let's talk to someone about your situation first."
    st = main._replay_state(HANDOFF_MEMBER)
    assert not st["planned"]                                       # nothing was moved
    assert main._active_auto_note(st) is None
    assert all(d["outcome"] != "pending" for d in main.goals(member_id=HANDOFF_MEMBER)["deposits"])
    main._load(force=True)


def test_canceling_within_the_window_reverses_the_contribution(fresh):
    _set_auto(True)
    note = _advance()["auto_note"]
    assert main.goals(member_id=MEMBER)["deposits"][0]["outcome"] == "pending"

    out = main.auto_cancel(main.AutoCancel(member_id=MEMBER, note_id=note["id"]))
    assert out["note"]["status"] == "canceled"
    assert out["note"]["message"] == "Canceled. Nothing was moved this payday."
    assert main._replay_state(MEMBER)["planned"] == 0.0            # reversed
    assert all(d["outcome"] != "pending" for d in main.goals(member_id=MEMBER)["deposits"])

    settled = _advance()["checkin"]                                # nothing moves at the next payday
    assert settled["planned_amount"] == 0.0 and settled["saved_amount"] == 0.0 and settled["outcome"] == "skipped"

    with pytest.raises(main.HTTPException):                        # a second cancel is refused
        main.auto_cancel(main.AutoCancel(member_id=MEMBER, note_id=note["id"]))


def test_changing_the_amount_updates_the_notification(fresh):
    _set_auto(True)
    note = _advance()["auto_note"]
    main.decide(main.Decision(member_id=MEMBER, action="adjust", amount=50.00))
    st = main._replay_state(MEMBER)
    assert st["planned"] == pytest.approx(50.00)
    changed = next(n for n in st["notes"] if n["id"] == note["id"])
    assert changed["status"] == "changed" and changed["amount"] == pytest.approx(50.00)


def test_declining_with_not_now_cancels_the_automatic_contribution(fresh):
    _set_auto(True)
    note = _advance()["auto_note"]
    main.decide(main.Decision(member_id=MEMBER, action="not_now"))
    assert main._replay_state(MEMBER)["planned"] == 0.0
    assert next(n for n in main._replay_state(MEMBER)["notes"] if n["id"] == note["id"])["status"] == "canceled"


def test_a_new_payday_closes_the_previous_window(fresh):
    _set_auto(True)
    note = _advance()["auto_note"]
    _advance()
    assert next(n for n in main._replay_state(MEMBER)["notes"] if n["id"] == note["id"])["status"] == "done"


def test_setting_survives_a_replay_reset_but_not_a_full_demo_reset(fresh):
    _set_auto(True)
    main._replay_state(MEMBER, "2026-06-01")
    assert main.get_settings(member_id=MEMBER)["auto_contribute"] is True
    main._load(force=True)                                         # what "Reset demo" does
    assert main.get_settings(member_id=MEMBER)["auto_contribute"] is False


def test_settings_page_has_general_rows_and_the_goals_toggle():
    html = STATIC.read_text(encoding="utf-8")
    for row in ("Notifications", "Linked accounts", "Security"):
        assert f"grey('{row}')" in html
    assert 'class="set-row off" aria-disabled="true"' in html      # grayed out, no handler
    assert "Automatically contribute the suggested amount toward your goal." in html
    assert "Penny will move the suggested amount automatically. You'll be notified and have 1 hour to change the amount or cancel." in html
    assert 'id="autoToggle"' in html and "checked" in html
    assert "One hour is the intended real-world window." in html
    assert "Change the amount" in html and "cancelAuto(" in html   # notification actions
