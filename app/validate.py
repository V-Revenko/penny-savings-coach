"""Validator: every number in the AI message must be an allowlisted fact.

Rules:
1. Only these facts may be quoted: the amount offered this payday
   (safe_per_paycheck or offer_amount), goal_target, goal_saved, goal_remaining,
   and for check-ins planned_amount and saved_amount. Any other number BLOCKS
   the message, even if it is a real fact (e.g. the surplus, or the stored
   suggested contribution), because quoting it would mislead the member.
2. The message must contain the required amount (the offer for a
   recommendation; the saved amount or next offer for a check-in). Handoff
   messages have no required amount.
3. Tolerance $0.01.

The report also carries non-blocking style checks (length, banking jargon)
so the tone rules can be measured.
"""
from __future__ import annotations

import re

TOLERANCE = 0.01

QUOTABLE_FACTS = (
    "safe_per_paycheck",
    "offer_amount",
    "goal_target",
    "goal_saved",
    "goal_remaining",
    "planned_amount",
    "saved_amount",
    "leaks_total_monthly",
    "fees_monthly",
    "duplicates_monthly",
)

# Style rules from the brief: short, plain, no banking terms, no hype.
MAX_WORDS = 30
MAX_SENTENCES = 2
BANNED_TERMS = ("surplus", "buffer", "discretionary", "allocation", "allocate", "cash flow", "net income", "outflow", "liquidity")

# $1,234.56  $50  $ 7.5  -$576.01
_DOLLAR_RE = re.compile(r"-?\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
# 25%  25 %  25 percent  12.5%
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent\b)")


def extract_numbers(message: str) -> list[dict]:
    """Return every dollar amount and percentage in the message, in order."""
    found: list[dict] = []
    for m in _DOLLAR_RE.finditer(message):
        whole, cents = m.group(1).replace(",", ""), m.group(2) or "0"
        found.append({"kind": "dollar", "text": m.group(0), "value": float(f"{whole}.{cents}"), "pos": m.start()})
    for m in _PERCENT_RE.finditer(message):
        found.append({"kind": "percent", "text": m.group(0), "value": float(m.group(1)), "pos": m.start()})
    found.sort(key=lambda d: d["pos"])
    return found


def allowed_values(facts: dict) -> list[tuple[str, float]]:
    """The (label, value) pairs a message may quote."""
    return [(k, float(facts[k])) for k in QUOTABLE_FACTS if isinstance(facts.get(k), (int, float))]


def _same(a: float, b: float) -> bool:
    return abs(round(a, 2) - round(b, 2)) <= TOLERANCE + 1e-9


def _match(value: float, kind: str, allowed: list[tuple[str, float]]) -> str | None:
    if kind != "dollar":
        return None  # no percentage is quotable
    for label, fact in allowed:
        if _same(fact, value):
            return label
    return None


def style_report(message: str) -> dict:
    """Non-blocking checks against the tone rules."""
    words = len(message.split())
    # A terminator only counts when followed by whitespace or the end, so the
    # decimal point in "$166.90" is not a sentence break.
    sentences = len([s for s in re.split(r"[.!?]+(?=\s|$)", message) if s.strip()])
    banned = [t for t in BANNED_TERMS if re.search(rf"\b{re.escape(t)}\b", message, re.IGNORECASE)]
    exclamations = message.count("!")
    return {
        "words": words,
        "sentences": sentences,
        "banned_terms": banned,
        "exclamations": exclamations,
        "ok": words <= MAX_WORDS and sentences <= MAX_SENTENCES and not banned and exclamations <= 1,
    }


def required_amount_key(facts: dict) -> str:
    """The fact a recommendation message must contain."""
    return "offer_amount" if isinstance(facts.get("offer_amount"), (int, float)) else "safe_per_paycheck"


def validate_message(message: str, facts: dict, handoff: bool = False, required: str | None = "auto") -> dict:
    """Check every number in the message against the allowlisted facts.

    ``required`` is the fact key whose value must appear in the message
    ("auto" = the offer for a recommendation; None = nothing required).
    Returns a report the UI can show in a "How this was calculated" panel.
    """
    if required == "auto":
        required = None if handoff else required_amount_key(facts)
    if handoff and required == required_amount_key(facts):
        required = None

    allowed = allowed_values(facts)
    checks = []
    for item in extract_numbers(message):
        label = _match(item["value"], item["kind"], allowed)
        checks.append(
            {
                "kind": item["kind"],
                "text": item["text"],
                "value": item["value"],
                "matched": label,
                "passed": label is not None,
            }
        )
    blocked = [c for c in checks if not c["passed"]]

    missing_amount = False
    if required is not None:
        target = facts.get(required)
        missing_amount = not isinstance(target, (int, float)) or not any(
            c["passed"] and _same(c["value"], float(target)) for c in checks
        )

    problems = []
    if blocked:
        problems.append(f"{len(blocked)} of {len(checks)} numbers are not allowed facts")
    if missing_amount:
        problems.append(f"the required amount ({required}) is missing")
    return {
        "passed": not problems,
        "numbers_checked": len(checks),
        "checks": checks,
        "blocked": blocked,
        "missing_amount": missing_amount,
        "required": required,
        "allowed": [{"fact": k, "value": round(v, 2)} for k, v in allowed],
        "style": style_report(message),
        "summary": (
            f"All {len(checks)} numbers match the calculated facts."
            if not problems
            else "Message blocked: " + "; ".join(problems) + "."
        ),
    }


# --------------------------------------------------------------------------- #
# Ask Penny (chat): broader allowlist, since the member is asking to see the math
# --------------------------------------------------------------------------- #
# Product/outcome language Penny must never use, chat or otherwise.
ADVICE_BANNED_TERMS = (
    "invest", "investment", "stocks", "mutual fund", "index fund", "annuity",
    "certificate of deposit", "cd rate", "credit card offer", "loan offer",
    "guarantee", "guaranteed", "promise you", "promised", "will definitely",
)

# Reassurance about the member's finances that no fact supports. Penny may only say what
# the numbers show, so these are blocked in free-form answers.
UNSUPPORTED_CLAIMS = (
    "plenty of time", "solid cushion", "solid ground", "comfortable cushion", "great shape", "on track",
)

CHAT_MAX_SENTENCES = 3


def chat_allowed_values(facts: dict) -> list[tuple[str, float]]:
    """Every numeric fact, including lists (e.g. income_by_month), for chat answers.

    Unlike ``allowed_values``, this is not restricted to QUOTABLE_FACTS: a
    member asking "why this amount" is entitled to hear the surplus, buffer,
    and window that produced it.
    """
    pairs: list[tuple[str, float]] = []
    for key, value in facts.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            pairs.append((key, float(value)))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    pairs.append((f"{key}[{i}]", float(item)))
    return pairs


def _chat_match(value: float, kind: str, allowed: list[tuple[str, float]]) -> str | None:
    for label, fact in allowed:
        candidates = [fact, abs(fact)]
        if kind == "percent":
            candidates += [fact * 100, abs(fact) * 100]
        if any(_same(c, value) for c in candidates):
            return label
    return None


def validate_chat_message(message: str, facts: dict) -> dict:
    """Check a chat answer: every number must be one of this member's facts,
    at most 3 sentences, and never investment/product/outcome language.
    """
    allowed = chat_allowed_values(facts)
    checks = []
    for item in extract_numbers(message):
        label = _chat_match(item["value"], item["kind"], allowed)
        checks.append(
            {
                "kind": item["kind"],
                "text": item["text"],
                "value": item["value"],
                "matched": label,
                "passed": label is not None,
            }
        )
    blocked = [c for c in checks if not c["passed"]]
    advice_terms = [t for t in ADVICE_BANNED_TERMS if re.search(rf"\b{re.escape(t)}\b", message, re.IGNORECASE)]
    unsupported = [t for t in UNSUPPORTED_CLAIMS if re.search(rf"\b{re.escape(t)}\b", message, re.IGNORECASE)]
    sentences = len([s for s in re.split(r"[.!?]+(?=\s|$)", message) if s.strip()])
    too_long = sentences > CHAT_MAX_SENTENCES

    problems = []
    if blocked:
        problems.append(f"{len(blocked)} of {len(checks)} numbers are not in this member's facts")
    if advice_terms:
        problems.append(f"used product/outcome language: {', '.join(advice_terms)}")
    if unsupported:
        problems.append(f"made a claim no fact supports: {', '.join(unsupported)}")
    if too_long:
        problems.append(f"{sentences} sentences, more than the {CHAT_MAX_SENTENCES}-sentence limit")

    return {
        "passed": not problems,
        "numbers_checked": len(checks),
        "checks": checks,
        "blocked": blocked,
        "advice_terms": advice_terms,
        "unsupported_claims": unsupported,
        "sentences": sentences,
        "summary": (
            f"All {len(checks)} numbers match this member's facts."
            if not problems
            else "Answer blocked: " + "; ".join(problems) + "."
        ),
    }


def corrupt_one_number(message: str, delta: float = 10.0) -> str:
    """Demo toggle: change the first dollar amount so the validator catches it."""
    m = _DOLLAR_RE.search(message)
    if not m:
        return message
    whole, cents = m.group(1).replace(",", ""), m.group(2) or "0"
    value = float(f"{whole}.{cents}") + delta
    replacement = f"${value:,.2f}"
    if m.group(0).startswith("-"):
        replacement = "-" + replacement
    return message[: m.start()] + replacement + message[m.end():]
