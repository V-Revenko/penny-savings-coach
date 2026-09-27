"""Data loader: workbook -> canonical tables, plus the data quality gate.

``load_tables`` reads the normalized SQLite database (``data/gesa.db``, built by
scripts/build_db.py) through its canonical views when that file exists, and
falls back to reading the Excel workbook directly when it doesn't. Either way
nothing downstream changes, because the engine only ever sees the canonical
columns below.

The quality gate and ``read_workbook`` always read the workbook directly: the
gate audits the untouched source, independent of whether the DB has been built.

Framing: the organizer said to assume the dataset is correct and that
inconsistencies tell us something. The gate treats them as signals that
"need attention", never as errors. Only structural problems (missing columns,
broken Member_ID links, duplicate IDs, unparseable values) are fails.
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKBOOK_PATH = PROJECT_ROOT / "data" / "AI_Hackathon_Synthetic_Financial_Wellness_Dataset.xlsx"
DB_PATH = PROJECT_ROOT / "data" / "gesa.db"

# Workbook headers live on row 2 (0-based header=1).
HEADER_ROW = 1

# Example starting balance: the workbook has no balances. Labeled as an assumption in the UI.
DEFAULT_OPENING_BALANCE = 500.0

# Transactions cover this range (dates only, no time of day).
DATA_START = pd.Timestamp("2026-01-01")
DATA_END = pd.Timestamp("2026-08-31")

# Canonical schema: the handoff contract with the data teammate. The engine
# reads ONLY these columns for arithmetic.
CANONICAL_COLUMNS: dict[str, list[str]] = {
    "Members": ["Member_ID", "Pay_Cycle", "Region"],
    "Transactions": [
        "Transaction_ID",
        "Member_ID",
        "Transaction_Date",
        "Amount",
        "Category",
        "Possible_Duplicate_Flag",
    ],
    "Debts": ["Member_ID", "Minimum_Payment", "Payment_Status"],
    "Goals": ["Member_ID", "Goal_Type", "Target_Amount", "Current_Saved", "Coaching_Tone"],
    "Accounts": ["Member_ID", "Opening_Balance"],
}

# Nice-to-have columns. Never required, never used in arithmetic. The stored
# estimates are kept so the UI can show "Our calculation" vs "Stored estimate".
OPTIONAL_COLUMNS: dict[str, list[str]] = {
    "Members": ["Synthetic_Name", "Monthly_Net_Income"],
    "Goals": ["Target_Date", "Suggested_Monthly_Contribution"],
    "Debts": ["Debt_ID", "Debt_Type", "Current_Balance", "APR", "Months_Remaining_Est"],
    # Merchant is display-only (Tier 3 "found money" card); never used in arithmetic.
    "Transactions": ["Merchant"],
}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def read_workbook(path: Path | str = WORKBOOK_PATH) -> dict[str, pd.DataFrame]:
    """Read the raw workbook sheets (all columns) with headers on row 2."""
    xls = pd.ExcelFile(path)
    sheets = ["Members", "Transactions", "Debts", "Credit Profiles", "Goals"]
    return {name: pd.read_excel(xls, name, header=HEADER_ROW) for name in sheets}


def to_canonical(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Project raw sheets onto the canonical schema and synthesize Accounts."""
    tables: dict[str, pd.DataFrame] = {}
    for name, required in CANONICAL_COLUMNS.items():
        if name == "Accounts":
            continue
        source = raw[name]
        keep = [c for c in required + OPTIONAL_COLUMNS.get(name, []) if c in source.columns]
        tables[name] = source[keep].copy()

    members = tables["Members"]
    tables["Accounts"] = pd.DataFrame(
        {
            "Member_ID": members["Member_ID"].to_numpy(),
            "Opening_Balance": DEFAULT_OPENING_BALANCE,
        }
    )

    tx = tables["Transactions"]
    tx["Transaction_Date"] = pd.to_datetime(tx["Transaction_Date"])
    tx["Amount"] = pd.to_numeric(tx["Amount"])
    tx["Possible_Duplicate_Flag"] = tx["Possible_Duplicate_Flag"].astype(bool)
    return tables


DB_VIEWS: dict[str, str] = {
    "Members": "v_members",
    "Transactions": "v_transactions",
    "Debts": "v_debts",
    "Goals": "v_goals",
    "Accounts": "v_accounts",
}


def read_tables_from_db(db_path: Path | str = DB_PATH) -> dict[str, pd.DataFrame]:
    """Canonical tables read from the normalized database's views."""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {name: pd.read_sql_query(f"SELECT * FROM {view}", conn) for name, view in DB_VIEWS.items()}
    finally:
        conn.close()

    tx = tables["Transactions"]
    tx["Transaction_Date"] = pd.to_datetime(tx["Transaction_Date"])
    tx["Amount"] = pd.to_numeric(tx["Amount"])
    tx["Possible_Duplicate_Flag"] = tx["Possible_Duplicate_Flag"].astype(bool)
    goals = tables["Goals"]
    if "Target_Date" in goals.columns:
        goals["Target_Date"] = pd.to_datetime(goals["Target_Date"])
    return tables


@lru_cache(maxsize=1)
def load_tables(path: Path | str = WORKBOOK_PATH, db_path: Path | str = DB_PATH) -> dict[str, pd.DataFrame]:
    """Canonical tables, cached. Reads data/gesa.db when it exists, else the
    workbook directly. Callers must not mutate the frames."""
    if Path(db_path).exists():
        return read_tables_from_db(db_path)
    return to_canonical(read_workbook(path))


# --------------------------------------------------------------------------- #
# Data quality gate
# --------------------------------------------------------------------------- #
STATUS_LABELS = {"pass": "pass", "warn": "needs attention", "fail": "fail"}


def _check(name: str, status: str, count: int, detail: str = "") -> dict:
    assert status in STATUS_LABELS
    return {"name": name, "status": status, "label": STATUS_LABELS[status], "count": int(count), "detail": detail}


def quality_report(raw: dict[str, pd.DataFrame]) -> list[dict]:
    """Run every quality check on the raw workbook frames.

    Returns a list of ``{name, status, label, count, detail}`` where status is
    pass / warn ("needs attention") / fail. Only structural fails block the engine.
    """
    checks: list[dict] = []
    tables = to_canonical(raw)

    # 1. Required columns present.
    missing = [
        f"{t}.{c}"
        for t, cols in CANONICAL_COLUMNS.items()
        for c in cols
        if c not in tables[t].columns
    ]
    checks.append(
        _check("Required columns present", "fail" if missing else "pass", len(missing), ", ".join(missing))
    )

    tx = tables["Transactions"]

    # 2. Types.
    bad_dates = int(pd.to_datetime(tx["Transaction_Date"], errors="coerce").isna().sum())
    checks.append(_check("Transaction dates parse", "fail" if bad_dates else "pass", bad_dates))
    bad_amounts = int(pd.to_numeric(tx["Amount"], errors="coerce").isna().sum())
    checks.append(_check("Amounts numeric", "fail" if bad_amounts else "pass", bad_amounts))
    flag_ok = tx["Possible_Duplicate_Flag"].isin([True, False]).all()
    checks.append(_check("Duplicate flags boolean", "pass" if flag_ok else "fail", 0 if flag_ok else 1))

    # 3. Keys (structural).
    dup_ids = int(tx["Transaction_ID"].duplicated().sum())
    checks.append(_check("No duplicate Transaction_IDs", "fail" if dup_ids else "pass", dup_ids))
    member_ids = set(tables["Members"]["Member_ID"])
    for name in ("Transactions", "Debts", "Goals"):
        orphans = int((~tables[name]["Member_ID"].isin(member_ids)).sum())
        checks.append(_check(f"{name} Member_IDs exist in Members", "fail" if orphans else "pass", orphans))

    # 4. Sign convention: income positive, spending negative (uses raw Need_Type).
    raw_tx = raw["Transactions"]
    if "Need_Type" in raw_tx.columns:
        is_income = raw_tx["Need_Type"].eq("Income")
        wrong_sign = int(((is_income & (raw_tx["Amount"] <= 0)) | (~is_income & (raw_tx["Amount"] >= 0))).sum())
        checks.append(_check("Sign convention (income +, spending -)", "fail" if wrong_sign else "pass", wrong_sign))

    # 5. Dates inside expected range.
    out_of_range = int(((tx["Transaction_Date"] < DATA_START) | (tx["Transaction_Date"] > DATA_END)).sum())
    checks.append(_check("Dates within 2026-01-01..2026-08-31", "fail" if out_of_range else "pass", out_of_range))

    # 6. Signals: the data tells us something. Flag as "needs attention", never fix silently.
    goals, members = raw["Goals"], raw["Members"]
    if "Suggested_Monthly_Contribution" in goals.columns and "Monthly_Net_Income" in members.columns:
        merged = goals.merge(members[["Member_ID", "Monthly_Net_Income"]], on="Member_ID", how="left")
        over = int((merged["Suggested_Monthly_Contribution"] > merged["Monthly_Net_Income"]).sum())
        checks.append(
            _check(
                "Suggested contribution above monthly income",
                "warn" if over else "pass",
                over,
                "Penny offers a smaller, reachable step from our safe amount instead of the stored suggestion.",
            )
        )
    if "Months_Remaining_Est" in raw["Debts"].columns:
        checks.append(
            _check(
                "Debt months remaining: stored estimate",
                "warn",
                len(raw["Debts"]),
                "Differs from our payoff calculation. Both are shown side by side; only ours is used.",
            )
        )
    credit = raw.get("Credit Profiles")
    if credit is not None and {"Profile_Band", "Credit_Score"}.issubset(credit.columns):
        # Bands should partition the score range. Count rows whose score falls
        # inside another band's observed [min, max] range.
        ranges = credit.groupby("Profile_Band")["Credit_Score"].agg(["min", "max"])
        inconsistent = 0
        for band, score in zip(credit["Profile_Band"], credit["Credit_Score"]):
            others = ranges.drop(index=band)
            if ((others["min"] <= score) & (score <= others["max"])).any():
                inconsistent += 1
        checks.append(
            _check(
                "Profile band vs credit score",
                "warn" if inconsistent else "pass",
                inconsistent,
                "Band overlaps another band's score range. Shown as a signal; not used in any calculation.",
            )
        )

    return checks


# --------------------------------------------------------------------------- #
# coach_checkin: written by the app (Tier 2 history), read by the Goals screen.
# Only used when data/gesa.db exists; callers fall back to in-memory history.
# --------------------------------------------------------------------------- #
def db_available(db_path: Path | str | None = None) -> bool:
    return Path(db_path or DB_PATH).exists()


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def clear_checkins(member_id: str | None = None, db_path: Path | str | None = None) -> None:
    """Delete stored check-ins for one member, or all (a demo reset)."""
    if not db_available(db_path):
        return
    conn = _connect(db_path)
    try:
        if member_id is None:
            conn.execute("DELETE FROM coach_checkin")
        else:
            conn.execute("DELETE FROM coach_checkin WHERE member_id = ?", (member_id,))
        conn.commit()
    finally:
        conn.close()


def write_checkin(
    member_id: str,
    payday_date: str,
    planned_amount: float,
    saved_amount: float,
    message: str,
    validator_passed: bool,
    db_path: Path | str | None = None,
) -> None:
    if not db_available(db_path):
        return
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT goal_id FROM goal WHERE member_id = ?", (member_id,)).fetchone()
        conn.execute(
            """INSERT INTO coach_checkin (member_id, goal_id, payday_date, planned_amount, saved_amount, message, validator_passed)
               VALUES (?,?,?,?,?,?,?)""",
            (member_id, row[0] if row else None, payday_date, float(planned_amount), float(saved_amount), message, int(bool(validator_passed))),
        )
        conn.commit()
    finally:
        conn.close()


def read_checkins(member_id: str, db_path: Path | str | None = None) -> list[dict]:
    """Stored check-ins for a member, newest first."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT checkin_id, payday_date, planned_amount, saved_amount, message, validator_passed
               FROM coach_checkin WHERE member_id = ? ORDER BY payday_date DESC, checkin_id DESC""",
            (member_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"checkin_id": r[0], "payday_date": r[1], "planned_amount": r[2], "saved_amount": r[3], "message": r[4], "validator_passed": bool(r[5])}
        for r in rows
    ]


def gate_passed(report: list[dict]) -> bool:
    return not any(c["status"] == "fail" for c in report)
