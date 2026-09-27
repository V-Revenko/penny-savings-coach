from datetime import date

import pytest

from app import engine, explain, validate

AS_OF = date(2026, 9, 1)


@pytest.fixture(scope="module")
def facts(tables):
    return engine.safe_to_save("MBR-0026", AS_OF, tables)


def test_extract_numbers_handles_formats():
    found = validate.extract_numbers("Move $166.90 (of $1,640.13 left) with a 25% buffer, -$576.01 short, $50 flat.")
    assert [(f["kind"], f["value"]) for f in found] == [
        ("dollar", 166.90),
        ("dollar", 1640.13),
        ("percent", 25.0),
        ("dollar", 576.01),
        ("dollar", 50.0),
    ]


def test_message_with_allowlisted_numbers_passes(facts):
    msg = "This payday, move $166.90 to your Home Down Payment. You have $1,640.13 to go of $5,250.63."
    report = validate.validate_message(msg, facts)
    assert report["passed"] is True
    assert report["numbers_checked"] == 3
    assert {c["matched"] for c in report["checks"]} == {"safe_per_paycheck", "goal_remaining", "goal_target"}


def test_wrong_dollar_amount_is_blocked(facts):
    msg = "This payday, move $200.00 to your goal. You have $1,640.13 to go."
    report = validate.validate_message(msg, facts)
    assert report["passed"] is False
    assert len(report["blocked"]) == 1
    assert report["blocked"][0]["value"] == 200.0
    assert "blocked" in report["summary"].lower()


def test_real_fact_outside_allowlist_is_blocked(facts):
    """The surplus is a real fact, but quoting it as the savings amount is misleading."""
    surplus = round(facts["surplus"], 2)
    msg = f"This payday, move ${surplus:,.2f} to your Home Down Payment."
    report = validate.validate_message(msg, facts)
    assert report["passed"] is False
    assert report["blocked"][0]["value"] == pytest.approx(surplus)
    assert report["missing_amount"] is True


def test_percentages_are_never_allowed(facts):
    msg = "This payday, move $166.90 to your goal. We kept a 25% cushion."
    assert validate.validate_message(msg, facts)["passed"] is False


def test_message_without_safe_amount_is_blocked(facts):
    msg = "Great job. You have $1,640.13 to go on your Home Down Payment."
    report = validate.validate_message(msg, facts)
    assert report["passed"] is False
    assert report["missing_amount"] is True
    assert not report["blocked"]


def test_handoff_message_needs_no_amount(tables):
    f = engine.safe_to_save("MBR-0081", AS_OF, tables)
    msg = "Right now the numbers are tight. A free counselor can help you make a plan."
    assert validate.validate_message(msg, f, handoff=True)["passed"] is True
    # But it still may not quote non-allowlisted numbers.
    assert validate.validate_message("You are $576.01 short each month.", f, handoff=True)["passed"] is False


def test_tolerance_is_one_cent(facts):
    assert validate.validate_message("Move $166.91 to your goal.", facts)["passed"] is True
    assert validate.validate_message("Move $166.92 to your goal.", facts)["passed"] is False


def test_style_report_flags_jargon_and_length():
    s = validate.style_report("Your surplus is fine! Great! " + "word " * 40)
    assert s["banned_terms"] == ["surplus"]
    assert s["words"] > validate.MAX_WORDS
    assert s["ok"] is False
    assert validate.style_report("This payday, move $166.90 to your goal, because your pay has been steady.")["ok"] is True


def test_corrupt_one_number_demo_toggle(facts):
    msg = explain.template_message(facts)
    assert validate.validate_message(msg, facts)["passed"] is True
    bad = validate.corrupt_one_number(msg)
    assert bad != msg
    assert validate.validate_message(bad, facts)["passed"] is False


def test_template_message_always_validates(tables):
    for member_id in ("MBR-0026", "MBR-0081", "MBR-0055"):
        f = engine.safe_to_save(member_id, AS_OF, tables)
        needed, reason = engine.handoff_needed(member_id, AS_OF, tables)
        msg = explain.template_message(f, reason if needed else None)
        report = validate.validate_message(msg, f, handoff=needed)
        assert report["passed"] is True, (member_id, msg, report["summary"])
        assert report["style"]["ok"] is True, (member_id, msg, report["style"])


def test_coach_message_falls_back_when_api_fails(facts):
    def boom(system, user):
        raise ConnectionError("no network")

    out = explain.coach_message(facts, "Direct", call=boom)
    assert out["source"] == "template"
    assert "ConnectionError" in out["error"]
    assert out["validator"]["passed"] is True
    assert "$166.90" in out["message"]


def test_coach_message_blocks_bad_ai_number(facts):
    def liar(system, user):
        return "This payday, move $999.00 to your Home Down Payment. You have $1,640.13 left."

    out = explain.coach_message(facts, "Direct", call=liar)
    assert out["source"] == "template"
    assert out["error"] == "validator blocked the AI message"
    assert out["validator"]["passed"] is False
    assert out["validator"]["blocked"][0]["value"] == 999.0


def test_coach_message_blocks_surplus_quoted_as_savings(facts):
    def confused(system, user):
        return "This payday, move $964.34 to your Home Down Payment, because your spending was lower than usual last month."

    out = explain.coach_message(facts, "Direct", call=confused)
    assert out["source"] == "template"
    assert out["validator"]["passed"] is False
    assert out["validator"]["blocked"][0]["value"] == 964.34


def test_coach_message_accepts_good_ai_number(facts):
    def honest(system, user):
        return "This payday, move $166.90 to your Home Down Payment, because your spending was lower than usual last month."

    out = explain.coach_message(facts, "Direct", call=honest)
    assert out["source"] == "ai"
    assert out["validator"]["passed"] is True


def test_corrupt_toggle_blocks_good_ai_message(facts):
    def honest(system, user):
        return "This payday, move $166.90 to your goal."

    out = explain.coach_message(facts, "Direct", call=honest, corrupt=True)
    assert out["source"] == "template"
    assert out["ai_message"] == "This payday, move $176.90 to your goal."
    assert out["validator"]["passed"] is False


def test_prompt_gives_reason_and_forbids_calculating(facts):
    assert "Never" in explain.SYSTEM_PROMPT and "calculate" in explain.SYSTEM_PROMPT
    prompt = explain.build_user_prompt(facts, "Direct")
    assert "$166.90" in prompt
    assert facts["reason"] in prompt
    assert "Direct" in prompt
    # Non-allowlisted numbers are never shown to the model.
    assert "964.34" not in prompt
