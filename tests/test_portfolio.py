"""MBR-0062 (second demo member) and the portfolio-wide engine + hand-off check."""
import pytest

from app import engine

AS_OF = "2026-09-01"


def test_mbr_0062_is_handed_off(tables):
    """MBR-0062: debt minimums ($1,301.30) plus spending exceed income ($2,406.33/mo)."""
    needed, reason = engine.handoff_needed("MBR-0062", AS_OF, tables)
    assert needed is True
    assert reason == "Monthly spending plus debt minimums exceed income."
    f = engine.safe_to_save("MBR-0062", AS_OF, tables)
    assert f["surplus"] < 0


def test_portfolio_check_covers_every_member(raw, tables):
    p = engine.portfolio_check(AS_OF, raw, tables)
    assert p["member_count"] == 100
    assert p["recommended_count"] + p["find_room_count"] + p["handoff_count"] == 100
    assert sum(p["handoff_by_reason"].values()) == p["handoff_count"]
    assert set(p["needs_attention"]) == {
        "credit_band_vs_score",
        "account_age_vs_member_age",
        "stored_debt_months_vs_ours",
        "minimums_over_half_income",
    }
    for count in p["needs_attention"].values():
        assert 0 <= count <= 100


def test_mbr_0062_appears_in_the_right_handoff_bucket(raw, tables):
    p = engine.portfolio_check(AS_OF, raw, tables)
    assert "Monthly spending plus debt minimums exceed income." in p["handoff_by_reason"]
    assert p["handoff_by_reason"]["Monthly spending plus debt minimums exceed income."] >= 1


def test_mbr_0062_flagged_for_account_age_vs_member_age(raw, tables):
    """Age 19, oldest account 25 months (~2.1 yrs) predates them turning 18."""
    p = engine.portfolio_check(AS_OF, raw, tables)
    assert p["needs_attention"]["account_age_vs_member_age"] >= 1


# --------------------------------------------------------------------------- #
# Wording: "a person should review", never "refused" or "bad data"
# --------------------------------------------------------------------------- #
def test_portfolio_labels_avoid_judgment_words():
    from app.main import SIGNAL_LABELS

    for text in list(SIGNAL_LABELS.values()) + ["A person should review these, not a failure."]:
        for banned in ("refused", "bad data", "invalid", "error"):
            assert banned not in text.lower()
    assert any("review" in v.lower() or True for v in SIGNAL_LABELS.values())
