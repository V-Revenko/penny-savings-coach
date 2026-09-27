"""Reference table from CLAUDE.md, as_of = 2026-09-01 (window June..August 2026).

If the code disagrees with these numbers, investigate before changing the test.
"""
from datetime import date

import pytest

from app import engine

AS_OF = date(2026, 9, 1)

REFERENCE = {
    # member: (pay_cycle, income, outflow, debt_mins, surplus, safe_mo, safe_paycheck, goal_remaining)
    "MBR-0026": ("Weekly", 4424.33, 2970.46, 489.54, 964.34, 723.25, 166.90, 1640.13),
    "MBR-0081": ("Biweekly", 3077.01, 1977.36, 1675.66, -576.01, 0.0, 0.0, 9578.78),
}


@pytest.mark.parametrize("member_id", list(REFERENCE))
def test_reference_table(tables, member_id):
    pay_cycle, income, outflow, mins, surplus, safe_mo, safe_pc, remaining = REFERENCE[member_id]
    f = engine.safe_to_save(member_id, AS_OF, tables)

    assert f["pay_cycle"] == pay_cycle
    assert f["window_start"] == "2026-06-01"
    assert f["window_end"] == "2026-08-31"
    assert f["monthly_income"] == pytest.approx(income, abs=0.01)
    assert f["monthly_outflow"] == pytest.approx(outflow, abs=0.01)
    assert f["debt_minimums"] == pytest.approx(mins, abs=0.01)
    assert f["surplus"] == pytest.approx(surplus, abs=0.01)
    assert f["safe_monthly"] == pytest.approx(safe_mo, abs=0.01)
    assert f["safe_per_paycheck"] == pytest.approx(safe_pc, abs=0.01)
    assert f["goal_remaining"] == pytest.approx(remaining, abs=0.01)


def test_paychecks_per_month_weekly(tables):
    f = engine.safe_to_save("MBR-0026", AS_OF, tables)
    assert f["paychecks_per_month"] == pytest.approx(52 / 12)
    assert f["safe_per_paycheck"] == pytest.approx(f["safe_monthly"] / (52 / 12))


def test_facts_include_intermediates_and_sources(tables):
    f = engine.safe_to_save("MBR-0026", AS_OF, tables)
    assert len(f["income_by_month"]) == 3
    assert len(f["outflow_by_month"]) == 3
    assert f["source_transaction_ids"]
    assert all(t.startswith("TXN-") for t in f["source_transaction_ids"])
    assert f["surplus"] == pytest.approx(f["monthly_income"] - f["monthly_outflow"] - f["debt_minimums"])
    assert f["safe_monthly"] == pytest.approx(max(0, f["surplus"] * (1 - engine.BUFFER)))


def test_handoff(tables):
    needed, reason = engine.handoff_needed("MBR-0026", AS_OF, tables)
    assert needed is False and reason is None

    needed, reason = engine.handoff_needed("MBR-0081", AS_OF, tables)
    assert needed is True
    assert reason


def test_window_is_three_full_months_before_as_of():
    start, end = engine.window_bounds(date(2026, 9, 15))
    assert start.date() == date(2026, 6, 1)
    assert end.date() == date(2026, 9, 1)


def test_irregular_uses_lowest_month(tables):
    """MBR-0055 is the Irregular-income demo member (CLAUDE.md Tier 4).

    Note: CLAUDE.md says MBR-0055 has a positive surplus. That is true under the
    mean-income rule (~$680/mo) but NOT under the required lowest-month rule
    (July 2026 has a single $1,391.34 paycheck, surplus ~ -$1,468). Only
    MBR-0054 has a positive lowest-month surplus among Irregular members.
    """
    f = engine.safe_to_save("MBR-0055", AS_OF, tables)
    assert f["pay_cycle"] == "Irregular"
    assert f["income_basis"].startswith("lowest month")
    assert f["monthly_income"] == pytest.approx(min(f["income_by_month"]))
    assert f["monthly_income"] < sum(f["income_by_month"]) / 3
    assert f["paychecks_per_month"] == pytest.approx(f["income_transactions_in_window"] / 3)
    assert f["safe_monthly"] == 0.0


def test_safe_per_paycheck_capped_at_goal_remaining(tables):
    f = engine.safe_to_save("MBR-0026", AS_OF, tables)
    assert f["safe_per_paycheck"] <= f["goal_remaining"]


def test_unknown_member_raises(tables):
    with pytest.raises(KeyError):
        engine.safe_to_save("MBR-9999", AS_OF, tables)
