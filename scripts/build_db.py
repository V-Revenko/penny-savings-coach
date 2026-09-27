"""Build data/gesa.db from the workbook: normalize into the tables in schema.sql,
run the data quality gate, and create the canonical views app/data.py reads.

Normalizes only. No value from the workbook is changed, fixed, or dropped for
being "wrong": Months_Remaining_Est, Suggested_Monthly_Contribution, Profile_Band,
Suggested_Focus_Area, Status, and Member_Priority are all stored as-is. Only
Monthly_Net_Income (exactly Annual_Gross_Income / 12, 0 mismatches) and
Transaction_Type (exactly the sign of Amount, 0 mismatches) are left out of the
base tables, since a view recomputes them with zero information loss.

Run: python scripts/build_db.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import data, engine  # noqa: E402

SCHEMA_PATH = PROJECT_ROOT / "scripts" / "schema.sql"
DB_PATH = PROJECT_ROOT / "data" / "gesa.db"


def _lookup_table(conn: sqlite3.Connection, table: str, id_col: str, values: list[str]) -> dict[str, int]:
    """Insert each distinct value and return {value: id}."""
    ids: dict[str, int] = {}
    for i, value in enumerate(values, start=1):
        conn.execute(f"INSERT INTO {table} ({id_col}, name) VALUES (?, ?)", (i, value))
        ids[value] = i
    return ids


def build(workbook_path=data.WORKBOOK_PATH, db_path: Path = DB_PATH) -> None:
    raw = data.read_workbook(workbook_path)
    members, tx, debts, credit, goals = raw["Members"], raw["Transactions"], raw["Debts"], raw["Credit Profiles"], raw["Goals"]

    print("Running the data quality gate on the workbook...")
    report = data.quality_report(raw)
    for c in report:
        print(f"  [{c['label']:>14}] {c['name']} ({c['count']})")
    if not data.gate_passed(report):
        print("STRUCTURAL FAIL: the gate found a problem that blocks the build. Fix the workbook, not this script.")
        sys.exit(1)
    print()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")

    # --- pay_cycle: fixed 5, same rates the engine uses, so the DB and the code
    # can never disagree on what a "paycheck" is. ---
    for i, name in enumerate(["Weekly", "Biweekly", "Semi-monthly", "Monthly", "Irregular"], start=1):
        rate = engine.PAYCHECKS_PER_MONTH.get(name)  # None for Irregular
        conn.execute("INSERT INTO pay_cycle (pay_cycle_id, name, paychecks_per_month) VALUES (?, ?, ?)", (i, name, rate))
    pay_cycle_ids = {name: i for i, name in enumerate(["Weekly", "Biweekly", "Semi-monthly", "Monthly", "Irregular"], start=1)}

    # --- region ---
    region_ids = _lookup_table(conn, "region", "region_id", sorted(members["Region"].unique()))

    # --- member ---
    conn.executemany(
        """INSERT INTO member (member_id, synthetic_name, age, life_stage, region_id, income_type,
           annual_gross_income, pay_cycle_id, housing_status, household_size, channel_preference,
           financial_stress_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                r.Member_ID, r.Synthetic_Name, int(r.Age), r.Life_Stage, region_ids[r.Region], r.Income_Type,
                float(r.Annual_Gross_Income), pay_cycle_ids[r.Pay_Cycle], r.Housing_Status, int(r.Household_Size),
                r.Channel_Preference, r.Financial_Stress_Level,
            )
            for r in members.itertuples()
        ],
    )

    # --- account: one synthetic checking account per member (CLAUDE.md assumption) ---
    account_ids: dict[str, int] = {}
    for i, member_id in enumerate(members["Member_ID"], start=1):
        conn.execute(
            "INSERT INTO account (account_id, member_id, account_type, opening_balance, opening_date) VALUES (?,?,?,?,NULL)",
            (i, member_id, "checking", data.DEFAULT_OPENING_BALANCE),
        )
        account_ids[member_id] = i

    # --- category: verified 1:1 with Need_Type in the workbook ---
    cat_need = tx.groupby("Category")["Need_Type"].first()
    category_names = sorted(cat_need.index)
    category_ids: dict[str, int] = {}
    for i, name in enumerate(category_names, start=1):
        conn.execute("INSERT INTO category (category_id, name, need_type) VALUES (?,?,?)", (i, name, cat_need[name]))
        category_ids[name] = i

    # --- merchant: verified 1:1 with Category and Recurring_Flag in the workbook ---
    merchant_info = tx.groupby("Merchant").agg(Category=("Category", "first"), Recurring_Flag=("Recurring_Flag", "first"))
    merchant_ids: dict[str, int] = {}
    for i, (name, row) in enumerate(merchant_info.iterrows(), start=1):
        conn.execute(
            "INSERT INTO merchant (merchant_id, name, category_id, is_recurring) VALUES (?,?,?,?)",
            (i, name, category_ids[row["Category"]], int(bool(row["Recurring_Flag"]))),
        )
        merchant_ids[name] = i

    # --- transaction ---
    conn.executemany(
        """INSERT INTO "transaction" (transaction_id, account_id, merchant_id, transaction_date, amount,
           possible_duplicate, possible_avoidable) VALUES (?,?,?,?,?,?,?)""",
        [
            (
                r.Transaction_ID, account_ids[r.Member_ID], merchant_ids[r.Merchant],
                r.Transaction_Date.strftime("%Y-%m-%d"), float(r.Amount),
                int(bool(r.Possible_Duplicate_Flag)), int(bool(r.Possible_Avoidable_Cost_Flag)),
            )
            for r in tx.itertuples()
        ],
    )

    # --- lender ---
    lender_ids = _lookup_table(conn, "lender", "lender_id", sorted(debts["Lender"].unique()))

    # --- debt ---
    conn.executemany(
        """INSERT INTO debt (debt_id, member_id, debt_type, lender_id, current_balance, apr, minimum_payment,
           months_remaining_est, payment_status, member_priority) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                r.Debt_ID, r.Member_ID, r.Debt_Type, lender_ids[r.Lender], float(r.Current_Balance), float(r.APR),
                float(r.Minimum_Payment), int(r.Months_Remaining_Est), r.Payment_Status, r.Member_Priority,
            )
            for r in debts.itertuples()
        ],
    )

    # --- credit_profile ---
    conn.executemany(
        """INSERT INTO credit_profile (member_id, credit_score, credit_utilization, oldest_account_months,
           late_payments_24m, hard_inquiries_24m, open_accounts, collections_count, profile_band,
           suggested_focus_area) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                r.Member_ID, int(r.Credit_Score), float(r.Credit_Utilization), int(r.Oldest_Account_Months),
                int(getattr(r, "Late_Payments_24M")), int(getattr(r, "Hard_Inquiries_24M")), int(r.Open_Accounts),
                int(r.Collections_Count), r.Profile_Band, r.Suggested_Focus_Area,
            )
            for r in credit.itertuples()
        ],
    )

    # --- goal ---
    conn.executemany(
        """INSERT INTO goal (goal_id, member_id, goal_type, target_amount, current_saved, target_date,
           suggested_monthly_contribution, status, preferred_check_in, coaching_tone) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                r.Goal_ID, r.Member_ID, r.Goal_Type, float(r.Target_Amount), float(r.Current_Saved),
                r.Target_Date.strftime("%Y-%m-%d"), float(r.Suggested_Monthly_Contribution), r.Status,
                r.Preferred_Check_In, r.Coaching_Tone,
            )
            for r in goals.itertuples()
        ],
    )
    # coach_checkin starts empty; the app writes to it (Tier 2 history).

    conn.commit()

    print("Built", db_path)
    for table in ("pay_cycle", "region", "member", "category", "merchant", "account", '"transaction"', "lender", "debt", "credit_profile", "goal", "coach_checkin"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table.strip(chr(34)):<16} {count} rows")
    conn.close()


if __name__ == "__main__":
    build()
