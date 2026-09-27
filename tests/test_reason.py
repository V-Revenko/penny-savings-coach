from datetime import date

from app import engine

AS_OF = date(2026, 9, 1)


def test_reason_is_from_fixed_list(tables):
    for member_id in tables["Members"]["Member_ID"]:
        f = engine.safe_to_save(member_id, AS_OF, tables)
        assert f["reason"] is None or f["reason"] in engine.REASONS
        if f["surplus"] <= 0:
            assert f["reason"] is None
        else:
            assert f["reason"] is not None


def test_mbr_0026_reason_is_lower_spending(tables):
    """August 2026 spending ($2,138) is well below June/July (~$3,387); pay varies >10%; goal 69% funded."""
    f = engine.safe_to_save("MBR-0026", AS_OF, tables)
    assert f["reason"] == engine.REASON_SPENDING_LOWER


def test_pick_reason_rules():
    steady = [4000.0, 4000.0, 4000.0]
    flat = [2000.0, 2000.0, 2000.0]
    assert engine.pick_reason(steady, flat, 800.0, 1000.0, 500.0, "Weekly") == engine.REASON_CLOSE_TO_GOAL
    assert engine.pick_reason(steady, [2500.0, 2500.0, 1500.0], 100.0, 1000.0, 500.0, "Weekly") == engine.REASON_SPENDING_LOWER
    assert engine.pick_reason(steady, flat, 100.0, 1000.0, 500.0, "Weekly") == engine.REASON_PAY_STEADY
    assert engine.pick_reason([3000.0, 5000.0, 4000.0], flat, 100.0, 1000.0, 500.0, "Weekly") == engine.REASON_BILLS_COVERED
    assert engine.pick_reason(steady, flat, 100.0, 1000.0, 500.0, "Irregular") == engine.REASON_BILLS_COVERED
    assert engine.pick_reason(steady, flat, 100.0, 1000.0, -5.0, "Weekly") is None
