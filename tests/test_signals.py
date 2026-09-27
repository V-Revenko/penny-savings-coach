"""Data signals: stored estimates are shown side by side, never used, never called wrong."""
from datetime import date

import pytest

from app import engine, explain, validate

AS_OF = date(2026, 9, 1)
OVER_INCOME_MEMBERS = ("MBR-0001", "MBR-0003", "MBR-0041", "MBR-0080")


def test_four_members_have_suggested_contribution_above_income(tables):
    flagged = [m for m in tables["Members"]["Member_ID"] if engine.safe_to_save(m, AS_OF, tables)["suggested_exceeds_income"]]
    assert sorted(flagged) == sorted(OVER_INCOME_MEMBERS)


def test_mbr_0041_gets_a_smaller_reachable_step(tables):
    """MBR-0041: stored suggestion $3,949.13/mo vs $2,316.67/mo income, positive surplus."""
    f = engine.safe_to_save("MBR-0041", AS_OF, tables)
    needed, _ = engine.handoff_needed("MBR-0041", AS_OF, tables)
    assert needed is False
    assert f["suggested_exceeds_income"] is True
    assert f["stored_suggested_monthly"] == pytest.approx(3949.13)
    assert f["stored_monthly_income"] == pytest.approx(2316.67)
    assert 0 < f["safe_per_paycheck"] < f["stored_suggested_monthly"]

    msg = explain.template_message(f)
    report = validate.validate_message(msg, f)
    assert report["passed"] is True, report["summary"]
    assert report["style"]["ok"] is True, report["style"]
    assert f"${f['safe_per_paycheck']:,.2f}" in msg
    assert "3,949.13" not in msg
    # Framed as progress, never as the goal being wrong.
    for word in ("wrong", "too big", "unrealistic", "can't", "cannot"):
        assert word not in msg.lower()
    assert "forward" in msg


def test_stored_suggested_amount_can_never_be_quoted(tables):
    f = engine.safe_to_save("MBR-0041", AS_OF, tables)

    def quotes_stored(system, user):
        return f"You can safely move $3,949.13 to your {f['goal_type']} this payday, because {f['reason']}."

    out = explain.coach_message(f, "Direct", call=quotes_stored)
    assert out["source"] == "template"
    assert out["validator"]["passed"] is False
    assert out["validator"]["blocked"][0]["value"] == 3949.13


def test_prompt_never_shows_stored_suggestion(tables):
    f = engine.safe_to_save("MBR-0041", AS_OF, tables)
    prompt = explain.build_user_prompt(f, "Direct")
    assert "3,949.13" not in prompt and "3949.13" not in prompt
    assert "reachable step" in prompt
    assert "never say the goal is wrong" in prompt


def test_debt_months_ours_vs_stored(tables):
    f = engine.safe_to_save("MBR-0026", AS_OF, tables)
    assert len(f["debts"]) == 2
    by_type = {d["debt_type"]: d for d in f["debts"]}
    personal = by_type["Personal Loan"]
    assert personal["months_remaining_stored"] == 33
    assert personal["months_remaining_ours"] == engine.months_to_payoff(10281.61, 0.1401, 227.21)
    assert personal["months_remaining_ours"] != personal["months_remaining_stored"]


def test_months_to_payoff_rules():
    assert engine.months_to_payoff(1000.0, 0.0, 100.0) == 10
    assert engine.months_to_payoff(1000.0, 0.12, 100.0) == 11
    assert engine.months_to_payoff(10000.0, 0.24, 100.0) is None  # minimum doesn't cover interest
    assert engine.months_to_payoff(None, 0.1, 100.0) is None
    assert engine.months_to_payoff(0.0, 0.1, 100.0) == 0


def test_stored_estimates_are_not_in_the_arithmetic(tables):
    """Dropping the stored columns must not change any engine number."""
    stripped = dict(tables)
    stripped["Goals"] = tables["Goals"].drop(columns=["Suggested_Monthly_Contribution"])
    stripped["Members"] = tables["Members"].drop(columns=["Monthly_Net_Income"])
    stripped["Debts"] = tables["Debts"].drop(columns=["Months_Remaining_Est", "Current_Balance", "APR"])
    a = engine.safe_to_save("MBR-0041", AS_OF, tables)
    b = engine.safe_to_save("MBR-0041", AS_OF, stripped)
    for key in ("monthly_income", "monthly_outflow", "surplus", "safe_monthly", "safe_per_paycheck", "reason"):
        assert a[key] == b[key]
    assert b["suggested_exceeds_income"] is False
    assert b["stored_suggested_monthly"] is None
