-- Normalized schema for data/gesa.db, per CLAUDE.md "Normalized database".
-- Small value sets use CHECK constraints (verified against the workbook's actual
-- distinct values) instead of extra lookup tables, per CLAUDE.md.
-- Columns the analysis called "wrong or inconsistent" (months_remaining_est,
-- suggested_monthly_contribution, profile_band, suggested_focus_area) are kept
-- as-is on their tables: normalizing never deletes a value, it only removes
-- what's truly redundant (a column recomputable with zero information loss,
-- e.g. monthly income from annual income, exposed by a view instead).

PRAGMA foreign_keys = ON;

CREATE TABLE pay_cycle (
    pay_cycle_id        INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    paychecks_per_month REAL                    -- NULL for Irregular
);

CREATE TABLE region (
    region_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE
);

CREATE TABLE member (
    member_id               TEXT PRIMARY KEY,
    synthetic_name          TEXT NOT NULL,
    age                     INTEGER NOT NULL,
    life_stage              TEXT NOT NULL CHECK (life_stage IN
                             ('Early Career','Established','Family Builder','Pre-Retirement','Retired')),
    region_id               INTEGER NOT NULL REFERENCES region(region_id),
    income_type             TEXT NOT NULL,
    annual_gross_income     REAL NOT NULL,
    pay_cycle_id            INTEGER NOT NULL REFERENCES pay_cycle(pay_cycle_id),
    housing_status          TEXT NOT NULL CHECK (housing_status IN
                             ('Living with family','Mortgage','Own outright','Rent')),
    household_size          INTEGER NOT NULL,
    channel_preference      TEXT NOT NULL,
    financial_stress_level  TEXT NOT NULL
);

CREATE TABLE category (
    category_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    need_type   TEXT NOT NULL CHECK (need_type IN ('Essential','Discretionary','Income'))
);

CREATE TABLE merchant (
    merchant_id  INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    category_id  INTEGER NOT NULL REFERENCES category(category_id),
    is_recurring INTEGER NOT NULL CHECK (is_recurring IN (0, 1))
);

CREATE TABLE account (
    account_id      INTEGER PRIMARY KEY,
    member_id       TEXT NOT NULL REFERENCES member(member_id),
    account_type    TEXT NOT NULL CHECK (account_type IN ('checking', 'savings')),
    opening_balance REAL NOT NULL,               -- synthetic assumption, labeled in the UI
    opening_date    TEXT                         -- not in the workbook; left NULL, never invented
);

CREATE TABLE "transaction" (
    transaction_id      TEXT PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES account(account_id),
    merchant_id         INTEGER NOT NULL REFERENCES merchant(merchant_id),
    transaction_date    TEXT NOT NULL,
    amount               REAL NOT NULL,
    possible_duplicate  INTEGER NOT NULL CHECK (possible_duplicate IN (0, 1)),
    possible_avoidable  INTEGER NOT NULL CHECK (possible_avoidable IN (0, 1))
);

CREATE TABLE lender (
    lender_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE
);

CREATE TABLE debt (
    debt_id               TEXT PRIMARY KEY,
    member_id              TEXT NOT NULL REFERENCES member(member_id),
    debt_type               TEXT NOT NULL CHECK (debt_type IN
                             ('Auto Loan','Credit Card','Medical Debt','Personal Loan','Student Loan')),
    lender_id               INTEGER NOT NULL REFERENCES lender(lender_id),
    current_balance         REAL NOT NULL,
    apr                     REAL NOT NULL,
    minimum_payment         REAL NOT NULL,
    months_remaining_est    INTEGER NOT NULL,    -- kept as-is; unreliable, engine computes its own
    payment_status          TEXT NOT NULL CHECK (payment_status IN
                             ('30 days late','60+ days late','Current')),
    member_priority         TEXT
);

CREATE TABLE credit_profile (
    member_id               TEXT PRIMARY KEY REFERENCES member(member_id),
    credit_score             INTEGER NOT NULL,
    credit_utilization       REAL NOT NULL,
    oldest_account_months    INTEGER NOT NULL,
    late_payments_24m        INTEGER NOT NULL,
    hard_inquiries_24m       INTEGER NOT NULL,
    open_accounts            INTEGER NOT NULL,
    collections_count        INTEGER NOT NULL,
    profile_band             TEXT,               -- kept as-is; may not match credit_score, not used
    suggested_focus_area     TEXT                -- kept as-is; not used
);

CREATE TABLE goal (
    goal_id                          TEXT PRIMARY KEY,
    member_id                        TEXT NOT NULL REFERENCES member(member_id),
    goal_type                        TEXT NOT NULL CHECK (goal_type IN
                                      ('Debt Reduction','Education','Emergency Fund','Home Down Payment',
                                       'Retirement Boost','Vacation','Vehicle Purchase')),
    target_amount                    REAL NOT NULL,
    current_saved                    REAL NOT NULL,
    target_date                      TEXT,
    suggested_monthly_contribution   REAL NOT NULL,  -- kept as-is; unreliable, engine computes its own
    status                           TEXT,
    preferred_check_in               TEXT NOT NULL,
    coaching_tone                    TEXT NOT NULL CHECK (coaching_tone IN
                                      ('Direct','Educational','Encouraging'))
);

-- Written by the app at runtime (Tier 2 history), not loaded from the workbook.
CREATE TABLE coach_checkin (
    checkin_id        INTEGER PRIMARY KEY,
    member_id         TEXT NOT NULL REFERENCES member(member_id),
    goal_id           TEXT REFERENCES goal(goal_id),
    payday_date       TEXT NOT NULL,
    planned_amount    REAL NOT NULL,
    saved_amount      REAL NOT NULL,
    message           TEXT,
    validator_passed  INTEGER CHECK (validator_passed IN (0, 1))
);

-- ===================== Views = the canonical schema ========================
-- app/data.py reads only these. Base tables above can change freely as long
-- as these views keep outputting exactly these columns, spelled this way.

CREATE VIEW v_members AS
SELECT
    m.member_id                              AS Member_ID,
    pc.name                                  AS Pay_Cycle,
    r.name                                   AS Region,
    m.synthetic_name                         AS Synthetic_Name,
    ROUND(m.annual_gross_income / 12.0, 2)   AS Monthly_Net_Income
FROM member m
JOIN pay_cycle pc ON pc.pay_cycle_id = m.pay_cycle_id
JOIN region r ON r.region_id = m.region_id;

CREATE VIEW v_transactions AS
SELECT
    t.transaction_id      AS Transaction_ID,
    a.member_id            AS Member_ID,
    t.transaction_date     AS Transaction_Date,
    t.amount                AS Amount,
    c.name                   AS Category,
    t.possible_duplicate    AS Possible_Duplicate_Flag,
    mch.name                 AS Merchant
FROM "transaction" t
JOIN account a ON a.account_id = t.account_id
JOIN merchant mch ON mch.merchant_id = t.merchant_id
JOIN category c ON c.category_id = mch.category_id;

CREATE VIEW v_debts AS
SELECT
    d.debt_id              AS Debt_ID,
    d.member_id              AS Member_ID,
    d.debt_type                AS Debt_Type,
    d.current_balance          AS Current_Balance,
    d.apr                       AS APR,
    d.minimum_payment           AS Minimum_Payment,
    d.months_remaining_est      AS Months_Remaining_Est,
    d.payment_status            AS Payment_Status
FROM debt d;

CREATE VIEW v_goals AS
SELECT
    g.member_id                        AS Member_ID,
    g.goal_type                         AS Goal_Type,
    g.target_amount                     AS Target_Amount,
    g.current_saved                     AS Current_Saved,
    g.target_date                       AS Target_Date,
    g.suggested_monthly_contribution    AS Suggested_Monthly_Contribution,
    g.coaching_tone                     AS Coaching_Tone
FROM goal g;

CREATE VIEW v_accounts AS
SELECT
    a.member_id        AS Member_ID,
    a.opening_balance  AS Opening_Balance
FROM account a;
