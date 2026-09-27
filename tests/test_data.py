import pandas as pd

from app import data


def test_canonical_columns_present(tables):
    for name, cols in data.CANONICAL_COLUMNS.items():
        assert name in tables
        for col in cols:
            assert col in tables[name].columns, f"{name}.{col} missing"


def test_canonical_types(tables):
    tx = tables["Transactions"]
    assert pd.api.types.is_datetime64_any_dtype(tx["Transaction_Date"])
    assert pd.api.types.is_numeric_dtype(tx["Amount"])
    assert pd.api.types.is_bool_dtype(tx["Possible_Duplicate_Flag"])


def test_accounts_synthesized_with_default_balance(tables):
    accounts = tables["Accounts"]
    assert len(accounts) == len(tables["Members"])
    assert (accounts["Opening_Balance"] == data.DEFAULT_OPENING_BALANCE).all()


def test_quality_gate_passes_on_workbook(raw):
    report = data.quality_report(raw)
    names = {c["name"] for c in report}
    assert "Required columns present" in names
    assert "No duplicate Transaction_IDs" in names
    failures = [c for c in report if c["status"] == "fail"]
    assert not failures, failures
    assert data.gate_passed(report)


def test_quality_gate_flags_known_signals_as_needs_attention(raw):
    """CLAUDE.md: 4 goals have a suggested contribution above 100% of income."""
    report = {c["name"]: c for c in data.quality_report(raw)}
    over = report["Suggested contribution above monthly income"]
    assert over["status"] == "warn"
    assert over["label"] == "needs attention"
    assert over["count"] == 4
    assert report["Debt months remaining: stored estimate"]["status"] == "warn"
    # Signals are never called errors or bad data.
    for c in report.values():
        text = (c["name"] + " " + c["detail"]).lower()
        for word in ("bad data", "invalid", "error", "unreliable", "wrong"):
            assert word not in text, (c["name"], word)


def test_quality_gate_fails_on_duplicate_transaction_id(raw):
    broken = dict(raw)
    tx = raw["Transactions"].copy()
    tx.loc[1, "Transaction_ID"] = tx.loc[0, "Transaction_ID"]
    broken["Transactions"] = tx
    report = {c["name"]: c for c in data.quality_report(broken)}
    assert report["No duplicate Transaction_IDs"]["status"] == "fail"
    assert report["No duplicate Transaction_IDs"]["count"] == 1
    assert not data.gate_passed(list(report.values()))
