"""The recommendation right after a missed or partial payday connects to what just happened."""
import re
from pathlib import Path

import pytest

from app import engine, explain, main, validate

MEMBER = "MBR-0026"
STATIC = Path(main.STATIC_DIR) / "index.html"
UPBEAT = re.compile(r"building nicely|great progress|doing great|on a roll|amazing|fantastic|keep it up|momentum", re.I)


@pytest.fixture()
def offline(monkeypatch):
    """No network: Penny's provider fails, so the deterministic templates are what we compare."""
    def boom(system, user):
        raise ConnectionError("offline")

    monkeypatch.setattr(explain, "call_model", boom)
    main._load(force=True)
    main._replay_state(MEMBER, "2026-06-01")
    yield
    main._load(force=True)


def test_message_after_none_differs_from_a_normal_payday(offline):
    normal = main.recommendation(member_id=MEMBER, as_of=None, corrupt=False)["message"]["text"]

    main.decide(main.Decision(member_id=MEMBER, action="accept"))
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # Jun 12: nothing could be saved
    after = main.recommendation(member_id=MEMBER, as_of=None, corrupt=False)

    assert after["message"]["text"] != normal
    assert normal.startswith("You can safely move $153.18")
    assert after["message"]["text"] == (
        "Your next amount is $76.59, a smaller step toward your Home Down Payment while things settle."
    )
    assert not UPBEAT.search(after["message"]["text"])
    assert after["validator"]["passed"] is True  # every number in it is a real fact


def test_message_after_partial_names_the_start_not_the_miss(offline):
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # none
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # partial: $17.40 of $76.59
    after = main.recommendation(member_id=MEMBER, as_of=None, corrupt=False)
    text = after["message"]["text"]
    assert text == "Let's try $153.18 this payday toward your Home Down Payment. $17.40 last time was a real start."
    assert "76.59" not in text  # never names what was missed
    assert not UPBEAT.search(text)
    assert after["validator"]["passed"] is True


def test_full_save_keeps_the_normal_upbeat_capable_message(offline):
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # Jun 26: full save
    text = main.recommendation(member_id=MEMBER, as_of=None, corrupt=False)["message"]["text"]
    assert text.startswith("You can safely move $")


def test_upbeat_ai_wording_after_a_miss_falls_back_to_the_template(offline):
    facts = engine.safe_to_save(MEMBER, "2026-06-12", main._state["tables"])
    facts["offer_amount"] = 76.59
    facts["after_outcome"] = "none"

    def upbeat(system, user):
        return "Let's try $76.59 this time, and you're building nicely toward your Home Down Payment."

    out = explain.coach_message(facts, "Encouraging", call=upbeat)
    assert out["source"] == "template"
    assert "building nicely" not in out["message"]


def test_prompt_tells_penny_to_acknowledge_a_smaller_step_and_skip_upbeat_language():
    facts = {"goal_type": "Home Down Payment", "goal_saved": 3610.5, "goal_target": 5250.63, "goal_remaining": 1640.13,
             "offer_amount": 76.59, "reason": "your bills and debt payments are covered", "after_outcome": "none"}
    prompt = explain.build_user_prompt(facts, "Encouraging")
    assert "smaller step" in prompt and "upbeat" in prompt.lower()
    assert "You can safely move" in prompt  # only inside the instruction not to open with it
    normal = explain.build_user_prompt({**facts, "after_outcome": None}, "Encouraging")
    assert "smaller step" not in normal


def test_checkin_states_the_next_amount_is_already_set_not_a_question(offline):
    main.decide(main.Decision(member_id=MEMBER, action="accept"))
    checkin = main.replay_advance(main.ReplayRequest(member_id=MEMBER))["checkin"]
    assert checkin["outcome"] == "none" and checkin["next_offer"] == pytest.approx(76.59)
    cf = explain.checkin_facts(
        engine.safe_to_save(MEMBER, "2026-06-12", main._state["tables"]), 153.18, 0.0, 76.59, "none"
    )
    text = explain.template_checkin(cf)
    assert text == "Nothing moved this time, and that's okay. I've set your next amount to $76.59."
    assert "?" not in text and "let's try" not in text.lower() and "how about" not in text.lower()
    skipped = explain.template_checkin({**cf, "outcome": "skipped"})
    assert "I've set your next amount to $76.59" in skipped and "?" not in skipped


def test_checkin_prompt_tells_penny_the_amount_is_already_set():
    facts = {"outcome": "none", "planned_amount": 153.18, "saved_amount": 0.0, "offer_amount": 76.59, "goal_type": "Home Down Payment"}
    prompt = explain.build_checkin_prompt(facts, "Encouraging")
    assert "already set" in prompt and "Do not ask a question" in prompt
    assert "already set" in explain.CHECKIN_SYSTEM_PROMPT


def test_outcome_labels_are_full_phrases():
    html = STATIC.read_text(encoding="utf-8")
    assert "none: ['No savings last time', 'muted']" in html
    assert "skipped: ['Skipped last time', 'muted']" in html
    assert "none this time" not in html
