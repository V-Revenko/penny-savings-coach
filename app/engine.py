"""Savings engine. ALL arithmetic lives here.

Every function is pure: it takes the canonical tables, a member_id and an
``as_of`` date, and returns plain dicts. Money is kept unrounded inside the
facts dict; round to cents only when displaying (``round_money``).
"""
from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd

from app import data as data_module

BUFFER = 0.25
WINDOW_MONTHS = 3

PAYCHECKS_PER_MONTH: dict[str, float] = {
    "Weekly": 52 / 12,
    "Biweekly": 26 / 12,
    "Semi-monthly": 2.0,
    "Monthly": 1.0,
    # Irregular: computed from the data (income transactions in window / 3).
}

HANDOFF_STATUS = "60+ days late"

# The "because" part of the insight sentence. Chosen by rules in code, never by
# the model. Order matters: the first rule that fires wins.
REASON_CLOSE_TO_GOAL = "you're close to your goal"
REASON_SPENDING_LOWER = "your spending was lower than usual last month"
REASON_PAY_STEADY = "your pay has been steady the last 3 months"
REASON_BILLS_COVERED = "your bills and debt payments are covered"
REASONS = (REASON_CLOSE_TO_GOAL, REASON_SPENDING_LOWER, REASON_PAY_STEADY, REASON_BILLS_COVERED)

CLOSE_TO_GOAL_PROGRESS = 0.75   # saved / target
SPENDING_LOWER_RATIO = 0.90     # last month outflow vs mean of the two before it
PAY_STEADY_TOLERANCE = 0.10     # every month within +/-10% of the mean

# Tier 2 replay
REPLAY_START = "2026-06-01"
ZERO_SAVE_STEP_DOWN = 0.5       # after a missed payday, offer half of what was planned


def pick_reason(
    income_by_month: list[float],
    outflow_by_month: list[float],
    goal_saved: float,
    goal_target: float,
    surplus: float,
    pay_cycle: str,
) -> str | None:
    """Pick one reason from REASONS. None when there is nothing safe to save."""
    if surplus <= 0:
        return None
    if goal_target > 0 and goal_saved / goal_target >= CLOSE_TO_GOAL_PROGRESS:
        return REASON_CLOSE_TO_GOAL
    earlier = outflow_by_month[:-1]
    if earlier and sum(earlier) > 0 and outflow_by_month[-1] < SPENDING_LOWER_RATIO * (sum(earlier) / len(earlier)):
        return REASON_SPENDING_LOWER
    mean_income = sum(income_by_month) / len(income_by_month)
    if (
        pay_cycle != "Irregular"
        and mean_income > 0
        and all(abs(m - mean_income) <= PAY_STEADY_TOLERANCE * mean_income for m in income_by_month)
    ):
        return REASON_PAY_STEADY
    return REASON_BILLS_COVERED


def _as_timestamp(as_of: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(as_of).normalize()


def window_bounds(as_of) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The 3 full calendar months before as_of, as [start, end)."""
    end = _as_timestamp(as_of).replace(day=1)
    start = end - pd.DateOffset(months=WINDOW_MONTHS)
    return start, end


def _tables(tables):
    return tables if tables is not None else data_module.load_tables()


def _member_row(tables, member_id: str, table: str) -> pd.Series:
    rows = tables[table][tables[table]["Member_ID"] == member_id]
    if rows.empty:
        raise KeyError(f"{member_id} not found in {table}")
    return rows.iloc[0]


def _optional_float(row: pd.Series, column: str) -> float | None:
    if column not in row.index:
        return None
    value = row[column]
    return None if pd.isna(value) else float(value)


def window_transactions(tables, member_id: str, as_of) -> pd.DataFrame:
    """The member's transactions inside the 3-month window (the engine's source rows)."""
    start, end = window_bounds(as_of)
    tx = tables["Transactions"]
    mask = (
        (tx["Member_ID"] == member_id)
        & (tx["Transaction_Date"] >= start)
        & (tx["Transaction_Date"] < end)
    )
    return tx.loc[mask]


def _monthly_totals(frame: pd.DataFrame, start: pd.Timestamp) -> list[float]:
    """Sum of Amount per calendar month across the whole window (missing months = 0)."""
    months = [(start + pd.DateOffset(months=i)).to_period("M") for i in range(WINDOW_MONTHS)]
    by_month = frame.groupby(frame["Transaction_Date"].dt.to_period("M"))["Amount"].sum()
    return [float(by_month.get(m, 0.0)) for m in months]


# --------------------------------------------------------------------------- #
# Stored estimates vs our calculation (shown side by side, never used in math)
# --------------------------------------------------------------------------- #
def months_to_payoff(balance: float | None, apr: float | None, payment: float) -> int | None:
    """Months to pay off a balance at the minimum payment, with monthly compounding.

    None when the balance is unknown or the payment does not cover the interest.
    """
    if balance is None or payment <= 0:
        return None
    if balance <= 0:
        return 0
    r = (apr or 0.0) / 12
    if r == 0:
        return math.ceil(balance / payment)
    if payment <= r * balance:
        return None
    return math.ceil(-math.log(1 - r * balance / payment) / math.log(1 + r))


def debt_comparisons(debts: pd.DataFrame) -> list[dict]:
    out = []
    for _, d in debts.iterrows():
        balance = _optional_float(d, "Current_Balance")
        apr = _optional_float(d, "APR")
        payment = float(d["Minimum_Payment"])
        stored = _optional_float(d, "Months_Remaining_Est")
        out.append(
            {
                "debt_type": str(d["Debt_Type"]) if "Debt_Type" in d.index else "Debt",
                "balance": balance,
                "apr": apr,
                "minimum_payment": payment,
                "payment_status": str(d["Payment_Status"]),
                "months_remaining_ours": months_to_payoff(balance, apr, payment),
                "months_remaining_stored": int(stored) if stored is not None else None,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
def safe_to_save(member_id: str, as_of, tables: dict[str, pd.DataFrame] | None = None) -> dict:
    """Compute how much the member can safely move to their goal this payday.

    Returns a facts dict with every intermediate number and the source
    Transaction_IDs, so the explainer and validator can reference them.
    """
    tables = _tables(tables)
    start, end = window_bounds(as_of)
    member = _member_row(tables, member_id, "Members")
    goal = _member_row(tables, member_id, "Goals")
    pay_cycle = str(member["Pay_Cycle"])

    window_tx = window_transactions(tables, member_id, as_of)
    income_tx = window_tx[window_tx["Amount"] > 0]
    spend_tx = window_tx[window_tx["Amount"] < 0]

    income_by_month = _monthly_totals(income_tx, start)
    outflow_by_month = [abs(v) for v in _monthly_totals(spend_tx, start)]

    if pay_cycle == "Irregular":
        monthly_income = min(income_by_month)  # conservative for gig income
        income_basis = "lowest month (Irregular pay cycle)"
    else:
        monthly_income = sum(income_by_month) / WINDOW_MONTHS
        income_basis = f"mean of {WINDOW_MONTHS} months"
    monthly_outflow = sum(outflow_by_month) / WINDOW_MONTHS

    debts = tables["Debts"][tables["Debts"]["Member_ID"] == member_id]
    debt_minimums = float(debts["Minimum_Payment"].sum())

    surplus = monthly_income - monthly_outflow - debt_minimums
    safe_monthly = max(0.0, surplus * (1 - BUFFER))

    if pay_cycle == "Irregular":
        paychecks_per_month = len(income_tx) / WINDOW_MONTHS
    else:
        paychecks_per_month = PAYCHECKS_PER_MONTH[pay_cycle]

    goal_target = float(goal["Target_Amount"])
    goal_saved = float(goal["Current_Saved"])
    goal_remaining = max(0.0, goal_target - goal_saved)
    uncapped = safe_monthly / paychecks_per_month if paychecks_per_month > 0 else 0.0
    safe_per_paycheck = min(uncapped, goal_remaining)
    reason = pick_reason(income_by_month, outflow_by_month, goal_saved, goal_target, surplus, pay_cycle)

    # Stored estimates: kept for "Our calculation vs Stored estimate", never used above.
    stored_suggested = _optional_float(goal, "Suggested_Monthly_Contribution")
    stored_income = _optional_float(member, "Monthly_Net_Income")
    suggested_exceeds_income = (
        stored_suggested is not None and stored_income is not None and stored_suggested > stored_income
    )

    return {
        "member_id": member_id,
        "as_of": _as_timestamp(as_of).date().isoformat(),
        "window_start": start.date().isoformat(),
        "window_end": (end - pd.Timedelta(days=1)).date().isoformat(),
        "window_months": WINDOW_MONTHS,
        "pay_cycle": pay_cycle,
        "income_basis": income_basis,
        "income_by_month": income_by_month,
        "outflow_by_month": outflow_by_month,
        "monthly_income": monthly_income,
        "monthly_outflow": monthly_outflow,
        "debt_minimums": debt_minimums,
        "surplus": surplus,
        "buffer": BUFFER,
        "safe_monthly": safe_monthly,
        "paychecks_per_month": paychecks_per_month,
        "income_transactions_in_window": int(len(income_tx)),
        "safe_per_paycheck_uncapped": uncapped,
        "safe_per_paycheck": safe_per_paycheck,
        "goal_type": str(goal["Goal_Type"]),
        "goal_target": goal_target,
        "goal_saved": goal_saved,
        "goal_remaining": goal_remaining,
        "reason": reason,
        "coaching_tone": str(goal["Coaching_Tone"]),
        "opening_balance": float(_member_row(tables, member_id, "Accounts")["Opening_Balance"]),
        "stored_suggested_monthly": stored_suggested,
        "stored_monthly_income": stored_income,
        "suggested_exceeds_income": suggested_exceeds_income,
        "debts": debt_comparisons(debts),
        "source_transaction_ids": window_tx["Transaction_ID"].tolist(),
    }


# A negative surplus this small is "not quite enough room", not a crisis: the member gets a softer
# path (find some extra room first) instead of a full counselor hand-off.
SMALL_SHORTFALL_DOLLARS = 150.0   # a shortfall up to this many dollars a month...
SMALL_SHORTFALL_PCT = 0.05        # ...or under this share of monthly income


def is_marginal_shortfall(surplus: float, monthly_income: float) -> bool:
    """True for a negative surplus within $150 a month, or under 5% of monthly income."""
    if surplus >= 0:
        return False
    shortfall = -surplus
    return shortfall <= SMALL_SHORTFALL_DOLLARS or (monthly_income > 0 and shortfall < SMALL_SHORTFALL_PCT * monthly_income)


def handoff_needed(member_id: str, as_of, tables: dict[str, pd.DataFrame] | None = None) -> tuple[bool, str | None]:
    """True (with a reason) when the member should see a counselor instead of a savings push.

    A small shortfall alone is NOT a hand-off (see ``room_needed``). A larger shortfall,
    debt minimums above income, or any debt 60+ days late still is.
    """
    tables = _tables(tables)
    facts = safe_to_save(member_id, as_of, tables)
    debts = tables["Debts"][tables["Debts"]["Member_ID"] == member_id]
    late = bool(debts["Payment_Status"].eq(HANDOFF_STATUS).any())

    if facts["surplus"] < 0:
        if not is_marginal_shortfall(facts["surplus"], facts["monthly_income"]):
            return True, "Monthly spending plus debt minimums exceed income."
        if late:  # a small shortfall plus a late debt: the late debt is the real reason
            return True, "A debt is 60+ days late."
    if facts["debt_minimums"] > facts["monthly_income"]:
        return True, "Debt minimum payments exceed monthly income."
    if late:
        return True, "A debt is 60+ days late."
    return False, None


ROOM_REASON = "Not quite enough room to save safely: the monthly shortfall is small."


def room_needed(member_id: str, as_of, tables: dict[str, pd.DataFrame] | None = None) -> tuple[bool, str | None]:
    """The middle tier: not a hand-off, but no safe amount to save yet (a small negative surplus).

    The next step is finding extra room (fees, duplicate subscriptions), not saving.
    """
    tables = _tables(tables)
    if handoff_needed(member_id, as_of, tables)[0]:
        return False, None
    if safe_to_save(member_id, as_of, tables)["surplus"] < 0:
        return True, ROOM_REASON
    return False, None


# --------------------------------------------------------------------------- #
# Tier 2: replay
# --------------------------------------------------------------------------- #
def _income_dates(tables, member_id: str) -> pd.Series:
    tx = tables["Transactions"]
    m = tx[(tx["Member_ID"] == member_id) & (tx["Amount"] > 0)]
    return m["Transaction_Date"].drop_duplicates().sort_values()


def first_payday_on_or_after(member_id: str, as_of, tables=None) -> str | None:
    tables = _tables(tables)
    d = _as_timestamp(as_of)
    dates = _income_dates(tables, member_id)
    dates = dates[dates >= d]
    return None if dates.empty else dates.iloc[0].date().isoformat()


def next_payday(member_id: str, as_of, tables=None) -> str | None:
    """The member's next income transaction strictly after as_of, or None at end of data."""
    tables = _tables(tables)
    d = _as_timestamp(as_of)
    dates = _income_dates(tables, member_id)
    dates = dates[dates > d]
    return None if dates.empty else dates.iloc[0].date().isoformat()


def period_net_cash_flow(member_id: str, start, end, tables=None) -> dict:
    """Net cash flow (income minus spending) for [start, end), with the rows used."""
    tables = _tables(tables)
    s, e = _as_timestamp(start), _as_timestamp(end)
    tx = tables["Transactions"]
    rows = tx[(tx["Member_ID"] == member_id) & (tx["Transaction_Date"] >= s) & (tx["Transaction_Date"] < e)]
    income = float(rows.loc[rows["Amount"] > 0, "Amount"].sum())
    spending = float(-rows.loc[rows["Amount"] < 0, "Amount"].sum())
    return {
        "period_start": s.date().isoformat(),
        "period_end": e.date().isoformat(),
        "income": income,
        "spending": spending,
        "net": income - spending,
        "transaction_ids": rows["Transaction_ID"].tolist(),
    }


def actual_saved(planned: float, net_cash_flow: float) -> float:
    """What really got saved last period: min(planned, max(0, net cash flow))."""
    return min(float(planned), max(0.0, float(net_cash_flow)))


def checkin_outcome(planned: float, saved: float) -> str:
    """full | partial | none | skipped (member chose 'not now')."""
    if planned <= 0:
        return "skipped"
    if saved >= planned - 0.005:
        return "full"
    if saved > 0:
        return "partial"
    return "none"


def next_offer(safe_per_paycheck: float, planned: float, saved: float) -> float:
    """The amount to offer next payday. After a missed save, step down, no guilt."""
    if planned > 0 and saved <= 0:
        return min(safe_per_paycheck, planned * ZERO_SAVE_STEP_DOWN)
    return safe_per_paycheck


# --------------------------------------------------------------------------- #
# Portfolio check: engine + hand-off rules across every member
# --------------------------------------------------------------------------- #
ADULT_AGE = 18  # for the account-age-vs-member-age signal


def portfolio_check(as_of, raw: dict[str, pd.DataFrame], tables: dict[str, pd.DataFrame] | None = None) -> dict:
    """Run safe_to_save and handoff_needed for every member, plus per-member data signals.

    ``raw`` is the raw workbook (``data.read_workbook()``) for the Credit
    Profiles and Age columns, which are not part of the canonical schema.
    These are signals that a person should review, never failures: nothing
    here blocks the engine for any individual member.
    """
    tables = _tables(tables)
    member_ids = tables["Members"]["Member_ID"].tolist()

    credit = raw.get("Credit Profiles")
    credit_by_member = credit.set_index("Member_ID") if credit is not None else None
    band_ranges = (
        credit.groupby("Profile_Band")["Credit_Score"].agg(["min", "max"])
        if credit is not None and {"Profile_Band", "Credit_Score"}.issubset(credit.columns)
        else None
    )
    age_by_member = raw["Members"].set_index("Member_ID")["Age"] if "Age" in raw["Members"].columns else None

    recommended: list[str] = []
    find_room: list[str] = []
    handoff_by_reason: dict[str, int] = {}
    signal_members: dict[str, set[str]] = {
        "credit_band_vs_score": set(),
        "account_age_vs_member_age": set(),
        "stored_debt_months_vs_ours": set(),
        "minimums_over_half_income": set(),
    }

    for member_id in member_ids:
        f = safe_to_save(member_id, as_of, tables)
        needed, reason = handoff_needed(member_id, as_of, tables)
        if needed:
            handoff_by_reason[reason] = handoff_by_reason.get(reason, 0) + 1
        elif f["surplus"] < 0:
            find_room.append(member_id)  # the middle tier: find extra room first
        else:
            recommended.append(member_id)

        if f["monthly_income"] > 0 and f["debt_minimums"] > 0.5 * f["monthly_income"]:
            signal_members["minimums_over_half_income"].add(member_id)
        if any(d["months_remaining_stored"] is not None and d["months_remaining_ours"] != d["months_remaining_stored"] for d in f["debts"]):
            signal_members["stored_debt_months_vs_ours"].add(member_id)

        if credit_by_member is not None and member_id in credit_by_member.index:
            row = credit_by_member.loc[member_id]
            if band_ranges is not None:
                others = band_ranges.drop(index=row["Profile_Band"], errors="ignore")
                if ((others["min"] <= row["Credit_Score"]) & (row["Credit_Score"] <= others["max"])).any():
                    signal_members["credit_band_vs_score"].add(member_id)
            if age_by_member is not None and member_id in age_by_member.index:
                oldest_years = float(row["Oldest_Account_Months"]) / 12
                if oldest_years > max(0.0, float(age_by_member.loc[member_id]) - ADULT_AGE):
                    signal_members["account_age_vs_member_age"].add(member_id)

    return {
        "as_of": _as_timestamp(as_of).date().isoformat(),
        "member_count": len(member_ids),
        "recommended_count": len(recommended),
        "find_room_count": len(find_room),
        "find_room_members": find_room,
        "handoff_count": sum(handoff_by_reason.values()),
        "handoff_by_reason": handoff_by_reason,
        "needs_attention": {k: len(v) for k, v in signal_members.items()},
    }


# --------------------------------------------------------------------------- #
# Tier 3: found money (leaks)
# --------------------------------------------------------------------------- #
FEES_CATEGORY = "Fees"


def _group_leak_items(frame: pd.DataFrame, kind: str) -> list[dict]:
    if frame.empty:
        return []
    items = []
    for merchant, g in frame.groupby("Merchant"):
        items.append(
            {
                "id": f"{kind}:{merchant}",
                "kind": kind,
                "merchant": str(merchant),
                "monthly_amount": float(g["Amount"].abs().sum()) / WINDOW_MONTHS,
                "count": int(len(g)),
                "transaction_ids": g["Transaction_ID"].tolist(),
            }
        )
    return sorted(items, key=lambda i: -i["monthly_amount"])


def leaks(member_id: str, as_of, tables: dict[str, pd.DataFrame] | None = None) -> dict:
    """Tier 3: monthly average of Fees plus flagged duplicate subscriptions, with source rows.

    Items are grouped by merchant. Amounts are signals from the data, not
    accusations: two transactions flagged as possible duplicates from the same
    merchant "might overlap" -- the member confirms or dismisses each item.
    """
    tables = _tables(tables)
    start, end = window_bounds(as_of)
    window_tx = window_transactions(tables, member_id, as_of)

    fee_items = _group_leak_items(window_tx[window_tx["Category"] == FEES_CATEGORY], "fee")
    duplicate_items = _group_leak_items(window_tx[window_tx["Possible_Duplicate_Flag"]], "duplicate")
    items = fee_items + duplicate_items

    fees_monthly = sum(i["monthly_amount"] for i in fee_items)
    duplicates_monthly = sum(i["monthly_amount"] for i in duplicate_items)

    return {
        "member_id": member_id,
        "as_of": _as_timestamp(as_of).date().isoformat(),
        "window_start": start.date().isoformat(),
        "window_end": (end - pd.Timedelta(days=1)).date().isoformat(),
        "items": items,
        "fees_monthly": fees_monthly,
        "duplicates_monthly": duplicates_monthly,
        "leaks_total_monthly": fees_monthly + duplicates_monthly,
    }


# --------------------------------------------------------------------------- #
# Goal path: where the member is heading at their safe pace (chart data)
# --------------------------------------------------------------------------- #
MAX_PROJECTION_MONTHS = 60


def goal_projection(member_id: str, as_of, tables=None, extra_saved: float = 0.0) -> dict:
    """Month-by-month path toward the goal at the member's safe monthly pace.

    ``extra_saved`` is what the member has saved through check-ins since the
    workbook's Current_Saved. Pure and neutral: it never says the member is
    "behind", it reports the date the safe pace reaches the goal next to the
    target date so the UI can show both.
    """
    tables = _tables(tables)
    facts = safe_to_save(member_id, as_of, tables)
    goal = _member_row(tables, member_id, "Goals")
    start = _as_timestamp(as_of).replace(day=1)
    target_date = pd.Timestamp(goal["Target_Date"]) if "Target_Date" in goal.index and not pd.isna(goal["Target_Date"]) else None

    target = facts["goal_target"]
    saved = facts["goal_saved"] + float(extra_saved)
    remaining = max(0.0, target - saved)
    pace = facts["safe_monthly"]

    if remaining <= 0:
        months_to_goal: int | None = 0
    elif pace > 0:
        months_to_goal = math.ceil(remaining / pace)
    else:
        months_to_goal = None  # no safe amount right now: show progress only, no projection

    months_until_target = None
    needed_monthly = None
    if target_date is not None:
        months_until_target = max(0, (target_date.year - start.year) * 12 + (target_date.month - start.month))
        if months_until_target > 0:
            needed_monthly = remaining / months_until_target

    projected_date = start + pd.DateOffset(months=months_to_goal) if months_to_goal is not None else None
    on_pace = (
        projected_date is not None and target_date is not None and projected_date <= target_date.replace(day=1) + pd.DateOffset(months=1)
    )

    horizon = max(months_to_goal or 0, months_until_target or 0, 1)
    horizon = min(horizon, MAX_PROJECTION_MONTHS)
    series = [
        {
            "date": (_as_timestamp(as_of) + pd.DateOffset(months=i)).date().isoformat(),  # i=0 is today
            "amount": min(target, saved + (pace * i if pace > 0 else 0.0)),
        }
        for i in range(horizon + 1)
    ]

    return {
        "as_of": _as_timestamp(as_of).date().isoformat(),
        "goal_type": facts["goal_type"],
        "target": target,
        "saved": saved,
        "remaining": remaining,
        "monthly_pace": pace,
        "months_to_goal": months_to_goal,
        "projected_date": projected_date.date().isoformat() if projected_date is not None else None,
        "target_date": target_date.date().isoformat() if target_date is not None else None,
        "months_until_target": months_until_target,
        "needed_monthly": needed_monthly,
        "on_pace": on_pace,
        "series": series,
        "capped": (months_to_goal or 0) > MAX_PROJECTION_MONTHS,
    }


def paydays_to_goal(remaining: float, per_payday: float) -> int | None:
    """Paydays needed to finish the goal saving ``per_payday`` each time (remaining / amount, rounded up)."""
    if remaining <= 0:
        return 0
    if per_payday <= 0:
        return None
    return math.ceil(round(remaining / per_payday, 9))


def paydays_sooner(remaining: float, full_amount: float, chosen_amount: float) -> dict:
    """How many paydays sooner the full safe amount finishes the goal than the chosen amount.

    ``sooner_by`` is None when there is nothing to compare: the chosen amount is
    the full amount (or more), is zero, or both finish on the same payday.
    """
    chosen = min(max(chosen_amount, 0.0), full_amount)
    full_paydays = paydays_to_goal(remaining, full_amount)
    chosen_paydays = paydays_to_goal(remaining, chosen)
    sooner = None
    if (
        full_paydays is not None
        and chosen_paydays is not None
        and chosen < full_amount - 0.005
        and chosen_paydays > full_paydays
    ):
        sooner = chosen_paydays - full_paydays
    return {
        "full_amount": full_amount,
        "chosen_amount": chosen,
        "paydays_at_full": full_paydays,
        "paydays_at_chosen": chosen_paydays,
        "sooner_by": sooner,
    }


def round_money(value: float) -> float:
    """Round to cents. Use only at output time."""
    return round(float(value) + 0.0, 2)
