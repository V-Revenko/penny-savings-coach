"""Ask Penny: a free-text chat about the member's own recommendation."""
import pytest

from app import engine, explain, validate

AS_OF = "2026-09-01"


@pytest.fixture(scope="module")
def facts(tables):
    f = engine.safe_to_save("MBR-0026", AS_OF, tables)
    f["offer_amount"] = f["safe_per_paycheck"]
    return f


def _refuse_to_call(system, user):
    raise AssertionError("the model must not be called for this question")


# --------------------------------------------------------------------------- #
# Classification, checked before any model call
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question",
    [
        "I can't pay my rent this month",
        "I'm overwhelmed and a debt collector keeps calling",
        "I just lost my job, what do I do",
        "I can't afford my bills anymore",
    ],
)
def test_hardship_is_detected(question):
    assert explain.classify_question(question) == "hardship"


@pytest.mark.parametrize("question", ["Can I talk to a person?", "I'd like to speak with a counselor", "Is there a real person I can talk to"])
def test_counselor_request_is_detected(question):
    assert explain.classify_question(question) == "counselor_request"


def test_ordinary_question_is_normal():
    assert explain.classify_question("Why this amount?") == "normal"
    assert explain.classify_question("What is a good goal?") == "normal"


def test_the_two_calculation_chips_have_their_own_types():
    assert explain.classify_question("How did you figure this out?") == "explain_calc"
    assert explain.classify_question("How do you calculate this?") == "explain_calc"
    assert explain.classify_question("What if I can't save this week?") == "skip_week"
    # hardship still wins over both
    assert explain.classify_question("I can't pay rent and can't save this week") == "hardship"


# --------------------------------------------------------------------------- #
# Hardship / counselor request short-circuit the model entirely
# --------------------------------------------------------------------------- #
def test_hardship_never_calls_the_model_and_shows_counselor(facts):
    out = explain.ask_penny(facts, "Encouraging", "I can't pay rent this month", call=_refuse_to_call)
    assert out["category"] == "hardship"
    assert out["show_counselor"] is True
    assert out["source"] == "template"
    assert out["validator"]["passed"] is True
    assert "counselor" in out["message"].lower()


def test_talk_to_a_person_never_calls_the_model_and_shows_counselor(facts):
    out = explain.ask_penny(facts, "Direct", "Talk to a person", call=_refuse_to_call)
    assert out["category"] == "counselor_request"
    assert out["show_counselor"] is True
    assert out["source"] == "template"


# --------------------------------------------------------------------------- #
# Normal questions: validated against this member's own facts (a wider set
# than the recommendation message allows)
# --------------------------------------------------------------------------- #
def test_honest_answer_quoting_surplus_and_buffer_passes(facts):
    def honest(system, user):
        return (
            f"You have about ${facts['surplus']:.2f} left over most months, so moving "
            f"${facts['safe_per_paycheck']:.2f} still keeps a {int(facts['buffer'] * 100)}% cushion."
        )

    out = explain.ask_penny(facts, "Direct", "Why this amount?", call=honest)
    assert out["source"] == "ai"
    assert out["validator"]["passed"] is True
    assert out["show_counselor"] is False


def test_answer_quoting_window_months_passes(facts):
    def honest(system, user):
        return f"I looked at your last {facts['window_months']} months of transactions to figure this out."

    out = explain.ask_penny(facts, "Direct", "How was my starting point chosen?", call=honest)
    assert out["source"] == "ai"
    assert out["validator"]["passed"] is True


def test_invented_number_is_blocked(facts):
    def liar(system, user):
        return "You could actually move $9,999.00 this payday without any risk."

    out = explain.ask_penny(facts, "Direct", "Why this amount?", call=liar)
    assert out["source"] == "template"
    assert out["message"] == explain.CHAT_FALLBACK
    assert out["validator"]["blocked"][0]["value"] == 9999.0
    assert out["show_counselor"] is False


def test_investment_advice_is_blocked_even_with_correct_numbers(facts):
    def pushy(system, user):
        return f"With ${facts['safe_per_paycheck']:.2f} free, you should invest it in an index fund."

    out = explain.ask_penny(facts, "Direct", "What should I do with this?", call=pushy)
    assert out["source"] == "template"
    assert "invest" in out["validator"]["advice_terms"] or "index fund" in out["validator"]["advice_terms"]


def test_too_many_sentences_is_blocked(facts):
    def rambling(system, user):
        return "One. Two. Three. Four."

    out = explain.ask_penny(facts, "Direct", "Why this amount?", call=rambling)
    assert out["source"] == "template"
    assert out["validator"]["sentences"] == 4


def test_api_failure_falls_back_to_safe_message(facts):
    def boom(system, user):
        raise ConnectionError("down")

    out = explain.ask_penny(facts, "Direct", "Why this amount?", call=boom)
    assert out["source"] == "template"
    assert out["message"] == explain.CHAT_FALLBACK
    assert "ConnectionError" in out["error"]


def test_corrupt_toggle_blocks_a_good_answer(facts):
    def honest(system, user):
        return f"You have ${facts['surplus']:.2f} left over most months."

    out = explain.ask_penny(facts, "Direct", "Why this amount?", call=honest, corrupt=True)
    assert out["source"] == "template"
    assert out["validator"]["passed"] is False


# --------------------------------------------------------------------------- #
# validate_chat_message directly
# --------------------------------------------------------------------------- #
def test_chat_validator_allows_surplus_and_buffer_unlike_the_recommendation_validator(facts):
    msg = f"Your surplus is ${facts['surplus']:.2f} with a {int(facts['buffer'] * 100)}% buffer."
    chat_report = validate.validate_chat_message(msg, facts)
    rec_report = validate.validate_message(msg, facts)
    assert chat_report["passed"] is True
    assert rec_report["passed"] is False  # surplus/buffer are not in the restricted recommendation allowlist


def test_chat_validator_blocks_unrelated_number(facts):
    report = validate.validate_chat_message("Your rate is 4.5%.", facts)
    assert report["passed"] is False


# --------------------------------------------------------------------------- #
# API wiring
# --------------------------------------------------------------------------- #
def test_api_ask_endpoint(tables):
    from app import main

    main._load(force=True)
    main._replay_state("MBR-0026", "2026-09-01")
    result = main.ask(main.AskRequest(member_id="MBR-0026", question="Why this amount?"))
    assert result["answer"]
    assert result["validator"]["passed"] in (True, False)
    assert result["category"] in ("normal", "hardship", "counselor_request")


def test_api_ask_rejects_empty_question():
    from fastapi import HTTPException

    from app import main

    main._load(force=True)
    main._replay_state("MBR-0026", "2026-09-01")
    with pytest.raises(HTTPException):
        main.ask(main.AskRequest(member_id="MBR-0026", question="   "))
