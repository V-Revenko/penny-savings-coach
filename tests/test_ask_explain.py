"""Ask Penny: "How did you figure this out?" and "What if I can't save this week?" get guaranteed,
number-backed answers."""
import pytest

from app import engine, explain, validate

AS_OF = "2026-09-01"


@pytest.fixture(scope="module")
def facts(tables):
    f = engine.safe_to_save("MBR-0026", AS_OF, tables)
    f["offer_amount"] = f["safe_per_paycheck"]
    return f


def _boom(system, user):
    raise ConnectionError("down")


def test_explain_answer_is_the_members_own_numbers(facts):
    text = explain.explain_calc_answer(facts)
    assert text == (
        "Over the last 3 months you brought in about $4,424.33 a month and spent about $2,970.46, "
        "plus debt payments of $489.54 that don't show up as transactions. "
        "That leaves about $964.34 extra. "
        "I keep 25% of that aside as a safety cushion, then split what's left across your 4.33 paychecks a month, "
        "which comes to $166.90 each payday."
    )
    # every figure is the engine's, formatted as currency
    assert f"${facts['surplus']:,.2f}" in text and f"${facts['safe_per_paycheck']:,.2f}" in text
    assert validate.validate_chat_message(text, facts)["passed"] is True


def test_the_chip_returns_that_answer_when_the_ai_is_down(facts):
    out = explain.ask_penny(facts, "Direct", "How did you figure this out?", call=_boom)
    assert out["message"] == explain.explain_calc_answer(facts)
    assert out["category"] == "explain_calc"
    assert "$964.34" in out["message"] and "$166.90" in out["message"]  # surplus and safe amount, not an apology
    assert "exactly right" not in out["message"]                          # not the generic fallback


def test_ai_may_rephrase_when_it_keeps_every_number(facts):
    def rephrase(system, user):
        return (
            "In the last 3 months you earned about $4,424.33 a month and spent about $2,970.46, with $489.54 "
            "going to debt. That left about $964.34, and after a 25% cushion it works out to $166.90 each payday."
        )

    out = explain.ask_penny(facts, "Direct", "How did you figure this out?", call=rephrase)
    assert out["source"] == "ai" and out["validator"]["passed"] is True


def test_a_rephrase_that_fails_validation_falls_back_to_the_exact_text(facts):
    def wrong_number(system, user):
        return "You earned about $4,424.33 a month, spent $2,970.46, and can move $999.00 each payday."

    out = explain.ask_penny(facts, "Direct", "How did you figure this out?", call=wrong_number)
    assert out["source"] == "template"
    assert out["message"] == explain.explain_calc_answer(facts)
    assert out["validator"]["blocked"][0]["value"] == 999.0  # the presenter can see what was blocked


def test_a_rephrase_that_drops_a_number_falls_back_too(facts):
    def vague(system, user):
        return "You had money left over each month, and I set aside a cushion before splitting it up."

    out = explain.ask_penny(facts, "Direct", "How did you figure this out?", call=vague)
    assert out["source"] == "template" and out["message"] == explain.explain_calc_answer(facts)


def test_corrupt_toggle_blocks_the_rephrase_and_shows_the_exact_text(facts):
    exact = explain.explain_calc_answer(facts)
    out = explain.ask_penny(facts, "Direct", "How did you figure this out?", call=lambda s, u: exact, corrupt=True)
    assert out["source"] == "template" and out["message"] == exact and out["ai_message"] != exact


def test_answer_for_a_member_with_no_surplus_does_not_claim_extra_money(tables):
    f = engine.safe_to_save("MBR-0081", AS_OF, tables)
    f["offer_amount"] = f["safe_per_paycheck"]
    text = explain.explain_calc_answer(f)
    assert "extra" not in text and "no safe amount" in text
    assert f"${abs(f['surplus']):,.2f}" in text
    assert validate.validate_chat_message(text, f)["passed"] is True


def test_answer_names_the_lowest_month_for_irregular_income(tables):
    f = engine.safe_to_save("MBR-0054", AS_OF, tables)  # Irregular pay
    f["offer_amount"] = f["safe_per_paycheck"]
    assert "your lowest month brought in about" in explain.explain_calc_answer(f)


def test_current_amount_is_mentioned_when_it_differs_from_the_safe_amount(facts):
    stepped = {**facts, "offer_amount": 76.59}
    assert "(you're on $76.59 for now)" in explain.explain_calc_answer(stepped)


# ---------------------------------------------------------------- "What if I can't save this week?"
def test_skip_week_answer_uses_only_calculated_numbers(facts):
    out = explain.ask_penny(facts, "Direct", "What if I can't save this week?", call=_boom)
    n = engine.paydays_to_goal(facts["goal_remaining"], facts["safe_per_paycheck"])
    assert out["message"] == (
        "That's okay. Tapping Not now doesn't change your amount for next payday. "
        f"At $166.90 a payday you'd reach your Home Down Payment in about {n} paydays, "
        f"and skipping this one would make it about {n + 1}."
    )
    assert (n, n + 1) == (10, 11)
    assert out["validator"]["passed"] is True and out["category"] == "skip_week"


def test_skip_week_answer_makes_no_unsupported_reassurance(facts, tables):
    for f in (facts, engine.safe_to_save("MBR-0081", AS_OF, tables)):
        f = {**f, "offer_amount": f["safe_per_paycheck"]}
        text = explain.skip_week_answer(f).lower()
        for claim in validate.UNSUPPORTED_CLAIMS:
            assert claim not in text


def test_skip_week_when_nothing_is_safe_or_goal_is_met(tables):
    f = engine.safe_to_save("MBR-0081", AS_OF, tables)
    f["offer_amount"] = 0.0
    assert "nothing to skip" in explain.skip_week_answer(f)
    done = {**f, "goal_remaining": 0.0, "offer_amount": 50.0}
    assert "already reached your goal" in explain.skip_week_answer(done)


def test_free_form_answers_cannot_claim_plenty_of_time_or_a_solid_cushion(facts):
    for claim in ("you'd still have plenty of time", "you have a solid cushion", "you're on track"):
        def reassure(system, user, claim=claim):
            return f"Missing one week is fine, {claim}."

        out = explain.ask_penny(facts, "Direct", "Will I be okay?", call=reassure)
        assert out["source"] == "template", claim
        assert out["validator"]["unsupported_claims"], claim


def test_chat_prompt_forbids_unsupported_reassurance():
    assert "plenty of time" in explain.CHAT_SYSTEM_PROMPT and "solid cushion" in explain.CHAT_SYSTEM_PROMPT
