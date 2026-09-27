"""Explainer: Penny (the LLM) writes the coach message from a facts dict. It never calculates.

Provider is isolated here so it can be swapped. If the API call fails, or the
validator blocks the AI message, we fall back to a templated message built
from the same facts and label it "offline template".
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from app.engine import paydays_to_goal, round_money
from app.validate import corrupt_one_number, required_amount_key, validate_chat_message, validate_message

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")  # exposes ANTHROPIC_API_KEY; never print or log it

MODEL = os.environ.get("COACH_MODEL", "claude-haiku-4-5")
MAX_TOKENS = 200

TONE_GUIDE = {
    "Encouraging": "warm and supportive, like a friend who believes in them",
    "Direct": "plain and matter-of-fact, no fluff, no pep talk",
    "Educational": "calm and clear, briefly connect the action to the reason",
}

COMMON_RULES = """Rules:
- 30 words maximum. 2 sentences maximum.
- Use only numbers from the facts, exactly as written. Never calculate, round, or invent a number.
  No percentages, no fractions, no "halfway" or "two-thirds".
- Plain words a 6th grader knows. No banking words: surplus, buffer, discretionary, allocation, cash flow.
- Never shame. No hype, at most one exclamation mark. No advice about products or investing.
  Do not promise results.
- Match the coaching tone given.
- Use plain punctuation only: periods and commas. Never use an em dash or en dash (no "—", no "–", no "--").
- Output only the message, no quotes, no preamble."""

SYSTEM_PROMPT = f"""You are Penny, a savings coach for a credit union member who is stressed about money.
You write one short nudge they can absorb in ten seconds.

Structure, in this order: the dollar amount, then the action (move it to their goal), then the reason.
Normally start with "You can safely move $X to your [goal], because [reason]." The choice is the
member's, so never command ("Move $X", "Put $X"). You may add one short clause about goal progress.
Nothing else. Exception: when the task says the member's last payday was missed or only partly saved,
follow the task's instructions for the opening and tone instead.

{COMMON_RULES}
- Use the reason exactly as given. Do not add or change the reason."""

CHECKIN_SYSTEM_PROMPT = f"""You are Penny, a savings coach checking in with a credit union member on payday.
You write one short message about what happened since last payday.

Outcome rules:
- full: they saved the whole planned amount. Celebrate briefly, name the saved amount.
- partial: they saved part of it. Name the saved amount as a win. Never mention what was missed.
- none: nothing was saved. No guilt at all. Say it is okay, then say the next amount is already set ("so I've set your next amount to $X"). A statement, never a question or an offer.
- skipped: they chose "not now" last time. No guilt. Say the next amount is already set ("I've set your next amount to $X"). A statement, never a question or an offer.

{COMMON_RULES}"""

LEAKS_SYSTEM_PROMPT = f"""You are Penny, a savings coach who spotted some patterns in a credit union
member's recent spending: fees and possible duplicate subscriptions. You write one short, upbeat,
judgment-free headline about it. Never say "wasting money", "mistake", or blame the member. Frame
it as money that could work harder for their goal, not a scolding.

{COMMON_RULES}"""

CHAT_SYSTEM_PROMPT = """You are Penny, answering one question a credit union member typed about their
own savings recommendation. Use ONLY the numbers in the facts dict, copied exactly as written.
Never calculate, round, combine, or invent a number. Never state a date, deadline, or timeline
unless that exact date is in the facts, and none is given here. If answering would need a number
or date that is not in the facts, say plainly that you can't work that out, and point them to the
"How this was calculated" panel (the (i) icon on the recommendation) or a free counselor.

Rules:
- Plain language, 3 sentences maximum.
- No investment or product recommendations. No promised outcomes, no guarantees.
- Stay only on this member's own numbers and situation. If the question is off topic, give a
  short, friendly redirect back to their savings plan.
- Never shame. Match the coaching tone given.
- Do not reassure about the member's finances (no "plenty of time", "solid cushion", "on track")
  unless a fact in the list says so. Say only what the numbers show.
- Use plain punctuation only: periods and commas. Never use an em dash or en dash.
- Output only the answer, no quotes, no preamble."""

# --------------------------------------------------------------------------- #
# Facts -> prompt
# --------------------------------------------------------------------------- #
def offer_amount(facts: dict) -> float:
    return float(facts[required_amount_key(facts)])


def display_facts(facts: dict) -> dict:
    """Only the allowlisted numbers, pre-rounded, plus the goal name and reason."""
    return {
        "amount_to_move_this_payday": f"${round_money(offer_amount(facts)):,.2f}",
        "goal": facts["goal_type"],
        "goal_saved_so_far": f"${round_money(facts['goal_saved']):,.2f}",
        "goal_target": f"${round_money(facts['goal_target']):,.2f}",
        "goal_remaining": f"${round_money(facts['goal_remaining']):,.2f}",
        "reason": facts.get("reason"),
        **_after_checkin_facts(facts),
    }


def _after_checkin_facts(facts: dict) -> dict:
    """What just happened last payday, so the new recommendation can connect to it."""
    outcome = facts.get("after_outcome")
    if outcome not in ("none", "partial"):
        return {}
    out = {"last_payday_result": "nothing could be saved" if outcome == "none" else "only part of the plan was saved"}
    if outcome == "partial" and isinstance(facts.get("saved_amount"), (int, float)):
        out["saved_last_payday"] = f"${round_money(facts['saved_amount']):,.2f}"
    return out


# After a missed or partial payday the tone stays gentle: no upbeat progress language.
UPBEAT_PATTERN = re.compile(
    r"building nicely|great progress|doing great|on a roll|crushing it|amazing|fantastic|awesome|keep it up|momentum|proud",
    re.IGNORECASE,
)


def _tone_line(tone: str) -> str:
    return f"Coaching tone: {tone} ({TONE_GUIDE.get(tone, 'plain and kind')})"


def build_user_prompt(facts: dict, tone: str, handoff_reason: str | None = None) -> str:
    if handoff_reason:
        shown = {"goal": facts["goal_type"], "situation": handoff_reason}
        task = (
            "Do NOT suggest a savings amount and do not use any numbers. Write a warm, no-guilt "
            "message that says now is a time to hold steady, and that a real person, a free "
            "counselor at the credit union, will help make a plan."
        )
    else:
        shown = display_facts(facts)
        task = (
            "Write: 'You can safely move [amount] to your [goal] this payday, because [reason].' "
            "in natural words, then optionally one short clause about progress. If you mention "
            "progress, quote goal_saved_so_far or goal_remaining exactly. Never say a fraction, "
            "percent, 'halfway', or 'two-thirds'."
        )
        after = facts.get("after_outcome")
        if after == "none":
            task = (
                "The member's last payday didn't work out: nothing could be saved. Do not open with "
                "'You can safely move'. The check-in just above this message already told the member "
                "this amount was set, so present it as already decided and do not re-explain or "
                "re-justify it. Say it like: \"Your next amount is [amount], a smaller step toward "
                "your [goal] while things settle.\" Do not mention the missed amount, and do NOT use "
                "upbeat language (no 'building nicely', 'great progress', 'doing great'). No progress "
                "clause, no exclamation marks."
            )
        elif after == "partial":
            task = (
                "The member's last payday only partly worked out. Do not open with 'You can safely move'. "
                "Open like: \"Let's try [amount] this payday toward your [goal].\" Then name "
                "saved_last_payday exactly as a real start. Never mention what was missed, and do NOT use "
                "upbeat language (no 'building nicely', 'great progress', 'doing great'). No exclamation marks."
            )
        if facts.get("suggested_exceeds_income") and not after:
            task += (
                " This member's stored plan asked for more than a month's pay, which is a signal, "
                "not a mistake. Present this amount as a reachable step that still moves them "
                "forward. Do not mention the stored plan or any other amount, and never say the "
                "goal is wrong or too big."
            )
    return f"{_tone_line(tone)}\n\nFacts (use only these, exactly as written):\n{json.dumps(shown, indent=2)}\n\n{task}"


def build_leaks_prompt(facts: dict, tone: str, handoff: bool = False) -> str:
    shown = {
        "amount_found_per_month": f"${round_money(facts['leaks_total_monthly']):,.2f}",
        "fee_count": sum(1 for i in facts["items"] if i["kind"] == "fee"),
        "duplicate_count": sum(1 for i in facts["items"] if i["kind"] == "duplicate"),
    }
    if handoff:
        task = (
            "Write one short headline: 'Penny found [amount_found_per_month] a month you might be "
            "able to free up.' Then one short, neutral clause inviting them to take a look below. "
            "This member is about to talk with a counselor, not save toward a goal right now: do "
            "not mention goals, saving, or moving money anywhere. No specifics, no merchant names, "
            "no other numbers."
        )
    else:
        task = (
            "Write one short headline: 'Penny found [amount_found_per_month] a month.' Then one short, "
            "neutral clause inviting them to take a look below (no specifics, no merchant names, no "
            "other numbers)."
        )
    return f"{_tone_line(tone)}\n\nFacts (use only these, exactly as written):\n{json.dumps(shown, indent=2)}\n\n{task}"


def leaks_item_note(item: dict) -> str:
    """Judgment-free, per-item wording. Deterministic, never AI-generated."""
    if item["kind"] == "fee":
        plural = "times" if item["count"] != 1 else "time"
        return f"Charged {item['count']} {plural} in the last 3 months."
    word = "two" if item["count"] == 2 else str(item["count"])
    return f"Looks like {word} charges from {item['merchant']} might overlap."


def build_checkin_prompt(facts: dict, tone: str) -> str:
    shown = {
        "outcome": facts["outcome"],
        "planned_amount": f"${round_money(facts['planned_amount']):,.2f}",
        "saved_amount": f"${round_money(facts['saved_amount']):,.2f}",
        "next_payday_amount": f"${round_money(facts['offer_amount']):,.2f}",
        "goal": facts["goal_type"],
    }
    must = {
        "full": "Name saved_amount exactly.",
        "partial": "Name saved_amount exactly. Do not name planned_amount.",
        "none": "Do not name planned_amount. State that you've already set the next amount, naming next_payday_amount exactly (for example: \"so I've set your next amount to X\"). Do not ask a question.",
        "skipped": "State that you've already set the next amount, naming next_payday_amount exactly. Do not ask a question.",
    }[facts["outcome"]]
    return f"{_tone_line(tone)}\n\nFacts (use only these, exactly as written):\n{json.dumps(shown, indent=2)}\n\nWrite the check-in for outcome '{facts['outcome']}'. {must}"


# --------------------------------------------------------------------------- #
# Offline templates
# --------------------------------------------------------------------------- #
# Style reference: the two best validated Haiku 4.5 outputs for MBR-0026
# (Encouraging tone, 2026-09-01). The templates below mirror their shape:
# amount, action, reason, then one short progress clause using an exact fact.
STYLE_REFERENCE = (
    "You can safely move $166.90 to your Home Down Payment, because your spending was lower "
    "than usual last month. You're at $3,610.50 saved, and that's getting close!",
    "You can safely move $166.90 to your Home Down Payment this payday, because your spending "
    "was lower than usual last month. You're at $3,610.50 saved!",
)


def template_message(facts: dict, handoff_reason: str | None = None) -> str:
    d = display_facts(facts)
    if handoff_reason:
        return (
            "Right now the numbers are tight, and that is not a failure. "
            "A real person, a free counselor at the credit union, will help you make a plan."
        )
    if offer_amount(facts) <= 0:
        return (
            f"This payday, hold steady on your {d['goal']} goal. "
            "Your lowest recent month was lean, so we won't plan on money that may not come."
        )
    after = facts.get("after_outcome")
    if after == "none":
        return f"Your next amount is {d['amount_to_move_this_payday']}, a smaller step toward your {d['goal']} while things settle."
    if after == "partial" and isinstance(facts.get("saved_amount"), (int, float)):
        saved = f"${round_money(facts['saved_amount']):,.2f}"
        return f"Let's try {d['amount_to_move_this_payday']} this payday toward your {d['goal']}. {saved} last time was a real start."
    if facts.get("suggested_exceeds_income"):
        return (
            f"You can safely move {d['amount_to_move_this_payday']} to your {d['goal']}, "
            f"because {d['reason']}. A step you can really make still moves you forward."
        )
    return (
        f"You can safely move {d['amount_to_move_this_payday']} to your {d['goal']} this payday, "
        f"because {d['reason']}. You're at {d['goal_saved_so_far']} saved, with {d['goal_remaining']} to go."
    )


def template_checkin(facts: dict) -> str:
    saved = f"${round_money(facts['saved_amount']):,.2f}"
    offer = f"${round_money(facts['offer_amount']):,.2f}"
    goal = facts["goal_type"]
    if facts["offer_amount"] <= 0 and facts["outcome"] in ("none", "skipped"):
        return "Nothing moved this time, and that's okay. There's no safe amount to set aside right now."
    return {
        "full": f"You moved {saved} to your {goal}. That's the whole plan, nice and steady.",
        "partial": f"You moved {saved} to your {goal} this time. That's a real win, and every bit counts.",
        "none": f"Nothing moved this time, and that's okay. I've set your next amount to {offer}.",
        "skipped": f"No worries about last time. I've set your next amount to {offer}, a safe step toward your {goal}.",
    }[facts["outcome"]]


# --------------------------------------------------------------------------- #
# Provider call (the only place the Anthropic SDK is used)
# --------------------------------------------------------------------------- #
def _request_kwargs(model: str) -> dict:
    # Haiku 4.5 rejects output_config.effort; newer models accept it.
    if model.startswith("claude-haiku"):
        return {}
    return {"output_config": {"effort": "low"}}


def call_model(system: str, user: str) -> str:
    """Return the model's text. Raises on any API failure or refusal."""
    import anthropic  # imported lazily so tests run without the SDK configured

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        **_request_kwargs(MODEL),
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model refused")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise RuntimeError("empty response")
    return text


# --------------------------------------------------------------------------- #
# Generation with validation and fallback
# --------------------------------------------------------------------------- #
def _generate(system: str, user: str, facts: dict, fallback: str, *, handoff: bool, required, call, corrupt: bool) -> dict:
    ai_text: str | None = None
    error: str | None = None
    try:
        ai_text = call(system, user)
    except Exception as exc:  # noqa: BLE001 - any provider failure falls back to the template
        error = f"{type(exc).__name__}: {exc}"

    report = None
    if ai_text is not None:
        if corrupt:
            ai_text = corrupt_one_number(ai_text)
        report = validate_message(ai_text, facts, handoff=handoff, required=required)
        if report["passed"]:
            return {"message": ai_text, "source": "ai", "model": MODEL, "validator": report, "ai_message": ai_text, "error": None}
        error = "validator blocked the AI message"

    fallback_report = validate_message(fallback, facts, handoff=handoff, required=required)
    return {
        "message": fallback,
        "source": "template",
        "model": None,
        "validator": report or fallback_report,
        "template_validator": fallback_report,
        "ai_message": ai_text,
        "error": error,
    }


def coach_message(facts: dict, tone: str, handoff_reason: str | None = None, call=None, corrupt: bool = False) -> dict:
    """Penny's recommendation. ``corrupt=True`` is the demo toggle."""
    handoff = handoff_reason is not None
    call = call or call_model  # looked up at call time so tests can swap the provider
    if facts.get("after_outcome") in ("none", "partial"):
        inner = call

        def call(system, user):  # noqa: F811 - wraps the provider with a tone guard
            text = inner(system, user)
            if UPBEAT_PATTERN.search(text):
                raise ValueError("upbeat wording right after a missed or partial payday")
            return text

    return _generate(
        SYSTEM_PROMPT,
        build_user_prompt(facts, tone, handoff_reason),
        facts,
        template_message(facts, handoff_reason),
        handoff=handoff,
        required=None if handoff else required_amount_key(facts),
        call=call,
        corrupt=corrupt,
    )


def checkin_facts(facts: dict, planned: float, saved: float, offer: float, outcome: str) -> dict:
    """Facts for a payday check-in: the new engine facts plus what happened last period."""
    return {**facts, "planned_amount": float(planned), "saved_amount": float(saved), "offer_amount": float(offer), "outcome": outcome}


def template_leaks(facts: dict, handoff: bool = False) -> str:
    total = f"${round_money(facts['leaks_total_monthly']):,.2f}"
    if handoff:
        return f"Penny found {total} a month you might be able to free up. Take a look below, no pressure to change anything."
    return f"Penny found {total} a month. Take a look below, no pressure to change anything."


def leaks_message(facts: dict, tone: str, call=call_model, corrupt: bool = False, handoff: bool = False) -> dict:
    """Penny's Tier 3 'found money' headline. Item-level wording is templated, not AI-generated.

    ``handoff=True`` frames this as ways to free up money while talking to a
    counselor: no goal mention, no push to save.
    """
    return _generate(
        LEAKS_SYSTEM_PROMPT,
        build_leaks_prompt(facts, tone, handoff),
        facts,
        template_leaks(facts, handoff),
        handoff=False,
        required="leaks_total_monthly",
        call=call,
        corrupt=corrupt,
    )


def checkin_message(facts: dict, tone: str, call=call_model, corrupt: bool = False) -> dict:
    """Penny's payday check-in (Tier 2). ``facts`` must come from ``checkin_facts``."""
    required = "saved_amount" if facts["outcome"] in ("full", "partial") else "offer_amount"
    return _generate(
        CHECKIN_SYSTEM_PROMPT,
        build_checkin_prompt(facts, tone),
        facts,
        template_checkin(facts),
        handoff=False,
        required=required,
        call=call,
        corrupt=corrupt,
    )


# --------------------------------------------------------------------------- #
# Ask Penny (chat)
# --------------------------------------------------------------------------- #
# Checked before the model ever runs, so these never depend on the AI behaving.
HARDSHIP_PATTERN = re.compile(
    r"can'?t pay|cannot pay|can'?t afford|cant afford|behind on (my )?(rent|bills|payments)|"
    r"evict|overwhelm|debt collector|collections? (calling|call)|lost my job|laid off|job loss|"
    r"drowning( in debt)?|repossess|struggling to (pay|afford)|no (money|food)|can'?t make rent",
    re.IGNORECASE,
)
COUNSELOR_REQUEST_PATTERN = re.compile(
    r"talk to a (person|human|counselor)|speak (to|with) (a person|someone|a counselor)|"
    r"real person|human being|talk to someone",
    re.IGNORECASE,
)

HARDSHIP_REPLY = (
    "That sounds like a lot to carry right now, and you do not have to sort it out alone. "
    "A free financial counselor at the credit union can help you make a plan."
)
COUNSELOR_REPLY = (
    "Of course. A free financial counselor at the credit union can talk this through with you, no cost."
)
CHAT_FALLBACK = (
    "I want to get this exactly right for you, so I will not guess. Tap the (i) on your recommendation "
    "to see how it was calculated, or talk with a free counselor anytime."
)


EXPLAIN_CALC_PATTERN = re.compile(
    r"how (did|do|does) (you|penny) (figure|calculate|work|get|come)|how (is|was) (this|that|it|the amount) "
    r"(calculated|worked out|figured)|show (me )?the math|walk me through",
    re.IGNORECASE,
)
SKIP_WEEK_PATTERN = re.compile(
    r"(can'?t|cannot|not able to|unable to) save (this|the|any)? ?(week|payday)|skip (this|a|the) (week|payday)|"
    r"miss(ing)? (a|this|the) (week|payday)",
    re.IGNORECASE,
)


def classify_question(question: str) -> str:
    """hardship | counselor_request | explain_calc | skip_week | normal, checked before any model call."""
    if HARDSHIP_PATTERN.search(question):
        return "hardship"
    if COUNSELOR_REQUEST_PATTERN.search(question):
        return "counselor_request"
    if EXPLAIN_CALC_PATTERN.search(question):
        return "explain_calc"
    if SKIP_WEEK_PATTERN.search(question):
        return "skip_week"
    return "normal"


def _usd(x: float) -> str:
    return f"${round_money(x):,.2f}"


def _plain_number(x: float) -> str:
    """4.33 -> '4.33', 2.0 -> '2', 1.5 -> '1.5'."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def explain_calc_answer(facts: dict) -> str:
    """The guaranteed, non-generic answer to "How did you figure this out?": this member's own numbers,
    straight from the engine's facts. Debt minimums are shown separately because they never appear
    in the transactions, so they are not part of the spending figure."""
    months = facts.get("window_months", 3)
    lowest = str(facts.get("income_basis", "")).startswith("lowest month")
    if lowest:
        earned = f"your lowest month brought in about {_usd(facts['monthly_income'])}"
    else:
        earned = f"you brought in about {_usd(facts['monthly_income'])} a month"
    opening = (
        f"Over the last {months} months {earned} and spent about {_usd(facts['monthly_outflow'])}, "
        f"plus debt payments of {_usd(facts['debt_minimums'])} that don't show up as transactions."
    )
    if facts["surplus"] <= 0:
        return (
            f"{opening} That is about {_usd(abs(facts['surplus']))} less than you need each month, "
            "so there is no safe amount to set aside right now."
        )
    safe = _usd(facts["safe_per_paycheck"])
    offer = facts.get("offer_amount")
    now = f" (you're on {_usd(offer)} for now)" if isinstance(offer, (int, float)) and offer < facts["safe_per_paycheck"] - 0.005 else ""
    return (
        f"{opening} That leaves about {_usd(facts['surplus'])} extra. "
        f"I keep {round(facts['buffer'] * 100)}% of that aside as a safety cushion, then split what's left across "
        f"your {_plain_number(facts['paychecks_per_month'])} paychecks a month, which comes to {safe} each payday{now}."
    )


def skip_week_answer(facts: dict) -> str:
    """"What if I can't save this week?" answered only with what the engine can show: paydays to finish
    the goal at the current amount, and one more if this payday is skipped. No reassurance that no
    number supports."""
    remaining = facts["goal_remaining"]
    offer = offer_amount(facts)
    if remaining <= 0:
        return "That's okay. You've already reached your goal, so there's nothing to save this week."
    if offer <= 0:
        return "That's okay. There isn't a safe amount to set aside right now, so there's nothing to skip."
    n = paydays_to_goal(remaining, offer)
    return (
        "That's okay. Tapping Not now doesn't change your amount for next payday. "
        f"At {_usd(offer)} a payday you'd reach your {facts['goal_type']} in about {n} paydays, "
        f"and skipping this one would make it about {n + 1}."
    )


def chat_display_facts(facts: dict) -> dict:
    """The fuller set of numbers Penny may draw on to answer a question about her own math."""
    return {
        "amount_offered_this_payday": f"${round_money(offer_amount(facts)):,.2f}",
        "goal": facts["goal_type"],
        "goal_saved_so_far": f"${round_money(facts['goal_saved']):,.2f}",
        "goal_target": f"${round_money(facts['goal_target']):,.2f}",
        "goal_remaining": f"${round_money(facts['goal_remaining']):,.2f}",
        "reason": facts.get("reason"),
        "window_months": facts.get("window_months", 3),
        "window_start": facts["window_start"],
        "window_end": facts["window_end"],
        "pay_cycle": facts["pay_cycle"],
        "income_basis": facts["income_basis"],
        "average_monthly_income": f"${round_money(facts['monthly_income']):,.2f}",
        "average_monthly_spending": f"${round_money(facts['monthly_outflow']):,.2f}",
        "monthly_debt_minimum_payments": f"${round_money(facts['debt_minimums']):,.2f}",
        "monthly_amount_left_over": f"${round_money(facts['surplus']):,.2f}",
        "safety_cushion_kept_aside_percent": f"{round(facts['buffer'] * 100)}%",
    }


def build_chat_prompt(facts: dict, tone: str, question: str, handoff_reason: str | None = None) -> str:
    shown = chat_display_facts(facts)
    if handoff_reason:
        shown["current_situation"] = handoff_reason
    return (
        f"{_tone_line(tone)}\n\nFacts (use only these, exactly as written):\n{json.dumps(shown, indent=2)}"
        f"\n\nThe member typed this question: \"{question}\"\n\nAnswer it."
    )


def _explain_calc_required(facts: dict) -> list[str]:
    return [_usd(facts["monthly_income"]), _usd(facts["monthly_outflow"]), _usd(facts["debt_minimums"])] + (
        [_usd(facts["surplus"]), _usd(facts["safe_per_paycheck"])] if facts["surplus"] > 0 else []
    )


def _explain_calc(facts: dict, tone: str, call, corrupt: bool) -> dict:
    """The AI may rephrase the guaranteed text. If it fails validation, or drops any of the member's
    numbers, the exact code-generated text is shown instead."""
    exact = explain_calc_answer(facts)
    ai_text: str | None = None
    error: str | None = None
    try:
        ai_text = call(
            CHAT_SYSTEM_PROMPT,
            f"{_tone_line(tone)}\n\nRephrase this answer in your own plain words. Keep every dollar amount exactly "
            f"as written and keep it to 3 sentences. Do not add anything.\n\nAnswer to rephrase:\n{exact}",
        )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    report = None
    if ai_text is not None:
        if corrupt:
            ai_text = corrupt_one_number(ai_text)
        report = validate_chat_message(ai_text, facts)
        missing = [n for n in _explain_calc_required(facts) if n not in ai_text]
        if report["passed"] and not missing:
            return {"message": ai_text, "source": "ai", "model": MODEL, "validator": report,
                    "ai_message": ai_text, "error": None, "category": "explain_calc", "show_counselor": False}
        error = "validator blocked the AI answer" if not report["passed"] else "AI answer dropped a number"
    return {
        "message": exact, "source": "template", "model": None,
        "validator": report or validate_chat_message(exact, facts),
        "ai_message": ai_text, "error": error, "category": "explain_calc", "show_counselor": False,
    }


def ask_penny(
    facts: dict,
    tone: str,
    question: str,
    handoff_reason: str | None = None,
    call=None,
    corrupt: bool = False,
) -> dict:
    """Penny answers a free-text question about her own recommendation.

    Hardship and "talk to a person" are caught before any model call, so they
    never depend on the AI behaving. Every other answer is validated against
    this member's own facts (a wider set than the recommendation message
    allows, since explaining the math is the point) and falls back to a safe,
    numberless message that points to the panel or a counselor.
    """
    call = call or call_model  # looked up at call time so tests can swap the provider
    category = classify_question(question)
    if category == "explain_calc":
        return _explain_calc(facts, tone, call, corrupt)
    if category == "skip_week":
        message = skip_week_answer(facts)
        return {"message": message, "source": "template", "model": None, "validator": validate_chat_message(message, facts),
                "ai_message": None, "error": None, "category": category, "show_counselor": False}
    if category in ("hardship", "counselor_request"):
        message = HARDSHIP_REPLY if category == "hardship" else COUNSELOR_REPLY
        report = validate_chat_message(message, facts)
        return {
            "message": message,
            "source": "template",
            "model": None,
            "validator": report,
            "ai_message": None,
            "error": None,
            "category": category,
            "show_counselor": True,
        }

    ai_text: str | None = None
    error: str | None = None
    try:
        ai_text = call(CHAT_SYSTEM_PROMPT, build_chat_prompt(facts, tone, question, handoff_reason))
    except Exception as exc:  # noqa: BLE001 - any provider failure falls back to the safe message
        error = f"{type(exc).__name__}: {exc}"

    report = None
    if ai_text is not None:
        if corrupt:
            ai_text = corrupt_one_number(ai_text)
        report = validate_chat_message(ai_text, facts)
        if report["passed"]:
            return {
                "message": ai_text,
                "source": "ai",
                "model": MODEL,
                "validator": report,
                "ai_message": ai_text,
                "error": None,
                "category": "normal",
                "show_counselor": False,
            }
        error = "validator blocked the AI answer"

    fallback_report = validate_chat_message(CHAT_FALLBACK, facts)
    return {
        "message": CHAT_FALLBACK,
        "source": "template",
        "model": None,
        "validator": report or fallback_report,
        "ai_message": ai_text,
        "error": error,
        "category": "normal",
        "show_counselor": False,
    }
