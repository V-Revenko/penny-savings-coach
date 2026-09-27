"""Tier 2: replay mode, paydays, what was actually saved, and Penny's check-ins."""
import pytest

from app import engine, explain, validate


def test_first_and_next_payday_for_weekly_member(tables):
    assert engine.first_payday_on_or_after("MBR-0026", "2026-06-01", tables) == "2026-06-05"
    assert engine.next_payday("MBR-0026", "2026-06-01", tables) == "2026-06-05"
    assert engine.next_payday("MBR-0026", "2026-06-05", tables) == "2026-06-12"
    assert engine.next_payday("MBR-0026", "2026-08-28", tables) is None  # end of data


def test_period_net_cash_flow_matches_rows(tables):
    flow = engine.period_net_cash_flow("MBR-0026", "2026-06-05", "2026-06-12", tables)
    tx = tables["Transactions"]
    rows = tx[(tx.Member_ID == "MBR-0026") & (tx.Transaction_Date >= "2026-06-05") & (tx.Transaction_Date < "2026-06-12")]
    assert flow["net"] == pytest.approx(float(rows.Amount.sum()))
    assert flow["income"] > 0
    assert set(flow["transaction_ids"]) == set(rows.Transaction_ID)


@pytest.mark.parametrize(
    "planned, net, saved, outcome",
    [
        (150.0, 400.0, 150.0, "full"),
        (150.0, 80.0, 80.0, "partial"),
        (150.0, -20.0, 0.0, "none"),
        (0.0, 400.0, 0.0, "skipped"),
    ],
)
def test_actual_saved_and_outcome(planned, net, saved, outcome):
    got = engine.actual_saved(planned, net)
    assert got == saved
    assert engine.checkin_outcome(planned, got) == outcome


def test_next_offer_steps_down_after_a_miss():
    assert engine.next_offer(160.0, 150.0, 150.0) == 160.0   # full save: normal
    assert engine.next_offer(160.0, 150.0, 40.0) == 160.0    # partial: normal
    assert engine.next_offer(160.0, 150.0, 0.0) == 75.0      # none: half of planned
    assert engine.next_offer(60.0, 150.0, 0.0) == 60.0       # never above the safe amount
    assert engine.next_offer(160.0, 0.0, 0.0) == 160.0       # skipped: fresh start


@pytest.mark.parametrize("outcome, planned, saved", [("full", 153.18, 153.18), ("partial", 153.18, 61.4), ("none", 153.18, 0.0), ("skipped", 0.0, 0.0)])
def test_checkin_templates_validate(tables, outcome, planned, saved):
    f = engine.safe_to_save("MBR-0026", "2026-06-12", tables)
    offer = engine.next_offer(f["safe_per_paycheck"], planned, saved)
    cf = explain.checkin_facts(f, planned, saved, offer, outcome)
    msg = explain.template_checkin(cf)
    required = "saved_amount" if outcome in ("full", "partial") else "offer_amount"
    report = validate.validate_message(msg, cf, required=required)
    assert report["passed"] is True, (outcome, msg, report["summary"])
    assert report["style"]["ok"] is True, (outcome, msg, report["style"])
    if outcome == "partial":
        assert "153.18" not in msg  # never name what was missed
    if outcome == "none":
        assert "guilt" not in msg.lower() and "should" not in msg.lower()


def test_checkin_blocks_ai_that_invents_a_number(tables):
    f = engine.safe_to_save("MBR-0026", "2026-06-12", tables)
    cf = explain.checkin_facts(f, 153.18, 61.4, f["safe_per_paycheck"], "partial")

    def liar(system, user):
        return "You moved $61.40 of your $153.18 plan. You missed $91.78, but that's okay."

    out = explain.checkin_message(cf, "Encouraging", call=liar)
    assert out["source"] == "template"
    assert out["validator"]["blocked"][0]["value"] == 91.78


def test_checkin_requires_the_saved_amount(tables):
    f = engine.safe_to_save("MBR-0026", "2026-06-12", tables)
    cf = explain.checkin_facts(f, 153.18, 61.4, f["safe_per_paycheck"], "partial")

    def vague(system, user):
        return "Great job this week. Keep it going."

    out = explain.checkin_message(cf, "Encouraging", call=vague)
    assert out["source"] == "template"
    assert out["validator"]["missing_amount"] is True


def test_offer_amount_is_the_required_amount_when_present(tables):
    f = engine.safe_to_save("MBR-0026", "2026-06-12", tables)
    f["offer_amount"] = 75.0
    assert validate.required_amount_key(f) == "offer_amount"
    ok = validate.validate_message("You can safely move $75.00 to your goal.", f)
    assert ok["passed"] is True
    stale = validate.validate_message(f"You can safely move ${f['safe_per_paycheck']:,.2f} to your goal.", f)
    assert stale["missing_amount"] is True
    assert explain.template_message(f).startswith("You can safely move $75.00")
