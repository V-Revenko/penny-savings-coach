# GESA Hackathon: Paycheck Savings Coach (Direction 4)

Prototype for the GESA Credit Union "AI-Powered Financial Wellness" hackathon.
**Synthetic data only. No real member data. No real money movement. No login screen.**

## The single insight (everything serves this sentence)
> "This payday, you can safely move **$X** to **[your goal]**, because **[one specific reason]**."

Direction 4 from the brief: find what the member can realistically set aside, schedule it around their actual pay cycle, and coach them over time in a tone that keeps them engaged, especially after they fall off track.

**Do not build a personal finance platform.** Anything not listed in Tier 1 to 4 below is out of scope. If a feature isn't in this file, ask before building it.

## Reference files
- `docs/challenge_brief.pdf` is the original hackathon brief, for background only. **If it conflicts with this file, this file wins.** Scope decisions here are final.
- The data analysis behind this file (reference numbers, data problems) is already done and verified. Don't redo it from scratch; write tests that confirm it.
- The API key lives in `.env` (see `.env.example`). Load it with python-dotenv. Never print, log, or commit it.

## Stack
- Python 3.11+, pandas, openpyxl, sqlite3 (standard library), FastAPI, uvicorn, pytest.
- Frontend: ONE `static/index.html` with vanilla JS and CSS, styled as a phone frame. No build step, no React.
- AI: Anthropic Python SDK, key from env var `ANTHROPIC_API_KEY`. Provider isolated in `app/explain.py` so it can be swapped.
- Run: `uvicorn app.main:app --reload`, then open http://localhost:8000

## Folder layout
```
data/AI_Hackathon_Synthetic_Financial_Wellness_Dataset.xlsx
data/gesa.db       # normalized SQLite, built by the script below
scripts/build_db.py  # xlsx -> normalized tables + quality gate + views
scripts/schema.sql   # CREATE TABLE / CREATE VIEW statements
app/data.py        # reads the views only
app/engine.py      # ALL arithmetic lives here
app/explain.py     # LLM writes the message from a facts object
app/validate.py    # checks LLM output against facts
app/main.py        # FastAPI routes
static/index.html  # the phone UI
tests/             # pytest
```

## Data
Workbook sheets: Members, Transactions, Debts, Credit Profiles, Goals (plus README). **Headers are on row 2**: `pd.read_excel(path, sheet, header=1)`.
- Transactions: 2026-01-01 to 2026-08-31, dates only (no time of day). Expenses negative, income positive. `Need_Type` is Essential / Discretionary / Income.
- Members: `Pay_Cycle` (Weekly, Biweekly, Semi-monthly, Monthly, Irregular), `Income_Type`, `Financial_Stress_Level`.
- Goals (one per member): `Goal_Type`, `Target_Amount`, `Current_Saved`, `Target_Date`, `Suggested_Monthly_Contribution`, `Status`, `Preferred_Check_In`, `Coaching_Tone`.
- Debts: `Minimum_Payment`, `Payment_Status`.

### Known data signals (handle in code, mention in demo)
The organizer said to assume the dataset is correct and that inconsistencies tell us something. Treat them as **signals, not errors**: in code and on screen say "needs attention", never "bad data", "invalid", "unreliable" or "error". Only structural problems (missing columns, broken Member_ID links, duplicate IDs, unparseable values) are fails.
- Transactions contain **no debt payments**. Always subtract Debt minimums before computing surplus.
- No account balances exist in the workbook. The normalized database adds `account.opening_balance` (default 500, labeled "Example starting balance" in the UI). Until the database is ready, the loader uses 500 directly.
- `Suggested_Monthly_Contribution` is above 100% of monthly income for 4 goals (MBR-0001, MBR-0003, MBR-0041, MBR-0080). Never quote it and never use it in arithmetic. Penny offers a smaller, reachable step from our engine's safe amount, framed as progress, not as the goal being wrong. The panel shows "Our calculation" (safe per month) vs "Stored estimate". MBR-0041 is the only one of the four with a positive surplus, so use it to demo this.
- `Months_Remaining_Est` in Debts differs from our payoff calculation (balance, APR, minimum payment). Never use it in arithmetic; show both as "Our calculation" vs "Stored estimate" in the panel without calling the stored value wrong.
- Flags (`Possible_Duplicate_Flag`, `Possible_Avoidable_Cost_Flag`) are only *possible*. The member confirms or dismisses.

## Normalized database (GESA cares about this)
GESA emphasized that a clean, normalized database is essential. The provided workbook is flat and has redundant and derived columns, so we normalize it to 3NF in SQLite (`data/gesa.db`), built by `scripts/build_db.py` from the xlsx. Enable foreign keys (`PRAGMA foreign_keys = ON`).

### Problems in the flat data that normalization fixes (verified)
- **Transitive dependency:** Merchant determines Category (all 37 merchants map to exactly one category), and Category determines Need_Type (1:1). Recurring_Flag also depends only on Merchant. Storing these on every transaction repeats them 10,000+ times.
- **Derivable columns:** Transaction_Type always matches the sign of Amount (0 mismatches). Monthly_Net_Income is exactly Annual_Gross_Income / 12 (so it is not really "net"; flag this).
- **Stored estimates that differ from our calculation:** Months_Remaining_Est, Suggested_Monthly_Contribution, Profile_Band, Suggested_Focus_Area. Compute in code; keep the stored values only for side-by-side display ("Our calculation" vs "Stored estimate").
- **Missing entity:** no accounts or balances. Add an `account` table.

### Tables
| Table | Columns (PK first, FK marked) |
|---|---|
| pay_cycle | pay_cycle_id, name, paychecks_per_month (NULL for Irregular) |
| region | region_id, name |
| member | member_id, synthetic_name, age, life_stage, region_id FK, income_type, annual_gross_income, pay_cycle_id FK, housing_status, household_size, channel_preference, financial_stress_level |
| category | category_id, name, need_type |
| merchant | merchant_id, name, category_id FK, is_recurring |
| account | account_id, member_id FK, account_type (checking / savings), opening_balance, opening_date |
| transaction | transaction_id, account_id FK, merchant_id FK, transaction_date, amount, possible_duplicate, possible_avoidable |
| lender | lender_id, name |
| debt | debt_id, member_id FK, debt_type, lender_id FK, current_balance, apr, minimum_payment, payment_status |
| credit_profile | member_id PK/FK, credit_score, credit_utilization, oldest_account_months, late_payments_24m, hard_inquiries_24m, open_accounts, collections_count |
| goal | goal_id, member_id FK, goal_type, target_amount, current_saved, target_date, preferred_check_in, coaching_tone |
| coach_checkin | checkin_id, member_id FK, goal_id FK, payday_date, planned_amount, saved_amount, message, validator_passed | 

- `coach_checkin` is written by the app (Tier 2 history), not loaded from the workbook.
- Small value sets (life_stage, housing_status, debt_type, goal_type, coaching_tone, payment_status) use CHECK constraints rather than extra lookup tables, to avoid over-engineering.
- `account.opening_balance`: synthetic. Rule to agree as a team; default is 500 for every checking account, labeled as an assumption in the UI.

### Views = the canonical schema
The engine never queries base tables. SQL views rebuild the canonical columns (e.g. `v_transactions` joins transaction, account, merchant, category to output Transaction_ID, Member_ID, Transaction_Date, Amount, Category, Possible_Duplicate_Flag). The teammate can change base tables freely as long as the views keep outputting the contract below.

### Show it
Put an ER diagram on one slide (generate a Mermaid `erDiagram` from the schema). Say in one sentence what was removed and why.

## Canonical schema (handoff contract with the data teammate)
The base tables can be restructured freely, but the views (read by `app/data.py`) must always output these columns, spelled exactly like this. The engine reads ONLY these.

| Table | Required columns |
|---|---|
| Members | Member_ID, Pay_Cycle, Region |
| Transactions | Transaction_ID, Member_ID, Transaction_Date, Amount, Category, Possible_Duplicate_Flag |
| Debts | Member_ID, Minimum_Payment, Payment_Status |
| Goals | Member_ID, Goal_Type, Target_Amount, Current_Saved, Coaching_Tone |
| Accounts | Member_ID, Opening_Balance |

Nice to have (UI works without them): Synthetic_Name (member picker), Target_Date ("on pace for" date).

- `Member_ID` links all four tables and must match exactly.
- Calculated values (surplus, safe-to-save, balances) are computed in code, never stored in the clean tables.
- Category may come from a join (transaction -> merchant -> category); it does not have to be stored on each transaction.
- Opening_Balance is the only invented value: default 500 per member unless the team agrees on another rule. Label it as an assumption in the UI.

## Data quality gate (app/data.py)
Runs when `scripts/build_db.py` loads the workbook into the normalized database. **The engine only runs on data that passed the gate.**

On import, run checks and return a report:
- Required columns present (canonical schema above).
- Types: dates parse, amounts numeric, flags boolean.
- No duplicate `Transaction_ID`s. Every `Member_ID` in Transactions, Debts, Goals exists in Members.
- Sign convention: income positive, spending negative.
- Dates inside the expected range.
- Known signals labeled "needs attention", never "error" or "bad data", and never silently fixed: goals whose suggested contribution is above monthly income, `Months_Remaining_Est` differing from our calculation, `Profile_Band` overlapping another band's `Credit_Score` range.

Each check reports pass / needs attention (warn) / fail with a count. Only structural fails block the engine, and the UI says why.

## Engine rules (app/engine.py)
All functions are pure and take an `as_of` date so the replay (Tier 2) can run them at any point in time.

**safe_to_save(member_id, as_of)**
1. Window = the 3 full calendar months before `as_of`.
2. `monthly_income` = mean monthly income in the window. **If Pay_Cycle is Irregular, use the lowest month instead of the mean** (conservative for gig income).
3. `monthly_outflow` = mean monthly spending in the window (absolute value).
4. `debt_minimums` = sum of `Minimum_Payment` for the member.
5. `surplus = monthly_income - monthly_outflow - debt_minimums`
6. `safe_monthly = max(0, surplus * (1 - BUFFER))`, with `BUFFER = 0.25`.
7. Paychecks per month: Weekly 52/12, Biweekly 26/12, Semi-monthly 2, Monthly 1, Irregular = income transactions in window / 3.
8. `safe_per_paycheck = safe_monthly / paychecks_per_month`, capped at goal remaining (`Target_Amount - Current_Saved`).
9. Round money to cents only at output. Return a facts dict that includes every intermediate number plus the list of source `Transaction_ID`s.

**handoff_needed(member_id, as_of)** returns True with a reason when: surplus < 0 by more than a small shortfall, OR total debt minimums > monthly income, OR any debt is "60+ days late". When True, the UI shows a counselor hand-off instead of a savings amount.

**Middle tier: room_needed(member_id, as_of)**. A *small* negative surplus (a shortfall within $150 a month, or under 5% of monthly income) with no 60+ day late debt is not a hand-off. The member sees a softer message ("Right now there's not quite enough room to save safely, but you're very close. Let's focus on finding some extra room first."), no savings amount, and the Found Money items (fees, duplicate subscriptions) as the next action. Freed-up money never creates a savings offer while the surplus is negative. A small shortfall plus a 60+ day late debt is still a full hand-off, and its reason is the late debt. Thresholds are `SMALL_SHORTFALL_DOLLARS` and `SMALL_SHORTFALL_PCT` in `app/engine.py`. At 2026-09-01 this moves MBR-0022, MBR-0029, MBR-0036 and MBR-0039 out of hand-off; MBR-0062 (about -$1,200/month) is unchanged.

**leaks(member_id, as_of)** (Tier 3): monthly average of Fees category plus flagged duplicate subscriptions in the window, with the transaction rows.

### Reference numbers (verified in analysis; tests must reproduce these)
With `as_of = 2026-09-01` (window June to August 2026):

| Member | Pay cycle | Income/mo | Outflow/mo | Debt mins | Surplus | Safe/mo | Safe/paycheck | Goal remaining |
|---|---|---|---|---|---|---|---|---|
| MBR-0026 | Weekly | 4424.33 | 2970.46 | 489.54 | 964.34 | 723.25 | 166.90 | 1640.13 |
| MBR-0081 | Biweekly | 3077.01 | 1977.36 | 1675.66 | -576.01 | 0 | 0 (handoff) | 9578.78 |

MBR-0026 leaks, June to August: fees about $49.95/mo, duplicate subscriptions about $23.32/mo.
If the code disagrees with these, investigate before changing the test.

## AI rules (app/explain.py)
- Input: the facts dict plus the member's `Coaching_Tone`. Output: a short message (2 to 3 sentences) a stressed person can read in ten seconds.
- The model **never calculates**. The prompt must say: use only the numbers provided, copied exactly; do not compute new numbers.
- Never shame. Recognize partial wins. No investment or product recommendations. No promised outcomes.
- If the API call fails, fall back to a templated message built from the facts, and show a small "offline template" label.

## Validator (app/validate.py)
- Extract every dollar amount and percentage from the AI message.
- Each one must equal a value in the facts dict (tolerance $0.01). Anything else means **block** the message and use the template.
- Return a report (what was checked, pass/fail) that the UI can show in a "How this was calculated" panel.
- Include a demo toggle that corrupts one number in the AI output, to show the validator catching it live.

## UI and branding

### Brand colors (GESA)
The organizer said framing the tool for GESA is fine. Get exact colors from GESA's own files rather than guessing:
1. Download `https://www.gesa.com/wp-content/uploads/2022/05/blue-logo.svg` and read its `fill` hex values. The main blue is the primary brand color.
2. Confirmed from the site: body text is `#191919`.
3. Put every color in CSS variables at the top of `static/index.html`:
   `--brand` (logo blue), `--brand-dark` (darker shade for pressed states), `--text` (#191919), `--muted` (gray for secondary text), `--bg` (white or very light gray), `--success` (calm green for wins), `--attention` (soft amber for "needs attention"). Never use red for the member's money situation; red reads as failure and shame.
4. Report the hex codes you used so they can be put on the slides.

Do not use GESA's logo image, name as the app title, or any mascot artwork unless the user confirms permission. Use the text "Savings Coach" with a small "Built for a Gesa Credit Union pilot" line.

### The coach: Penny
- The coach is named **Penny** and speaks in the member's `Coaching_Tone`.
- Show a small label under Penny's name: "Penny is an AI assistant."
- Avatar: a simple circle with the letter P in `--brand`. **Do not draw an owl or any mascot.** Leave an `<img>` slot so an official image can be swapped in later.
- In hand-off messages Penny says clearly that a real person will help, and shows the counselor contact.

### Layout and style rules
- Phone frame about 390px wide, centered on the laptop screen, so it reads like a mobile app in the demo. Outside the frame (right side), a narrow "Presenter panel" holds the demo controls (member picker, Advance to next payday, corrupt-a-number toggle) so they aren't part of the member's app.
- The recommendation is the hero: the dollar amount large (about 40px, bold, `--brand`), Penny's message directly under it, then Accept / Adjust / Not now. Accept is the only filled button.
- One goal progress bar showing saved vs target. No other charts on the main screen (the brief: "the bar is not a chart").
- "How this was calculated" is opened from a small (i) icon next to the "Verified against your account" badge on the recommendation card, and expands inside that card (not as a separate card further down). It is closed by default. Inside: validator report table, engine numbers (including surplus and buffer), source transaction IDs. Red is allowed inside this panel for the blocked number, since it is for the auditor, but never on member-facing screens.
- Plain, warm copy everywhere. No banking jargon on member screens. Buttons use verbs ("Save $166.90", not "Submit").
- System font stack, generous spacing, rounded corners (12px), large tap targets (at least 44px tall), readable contrast.
- Notifications: bell icon with a badge in the app header, plus a toast that slides in when a check-in arrives.
- Label the $500 opening balance as "Example starting balance" wherever it appears.

## Build tiers (finish each before starting the next)

**Tier 1: must run live**
- Import screen: rebuild the database from the workbook and show the data quality report (pass / warn / fail per check).
- Member picker dropdown (default MBR-0026). No login.
- Engine plus pytest tests matching the reference table.
- Explainer plus validator.
- One recommendation screen: the insight sentence, the goal progress bar, Accept / Adjust / Not now, and an expandable "How this was calculated" panel (opened from the (i) icon) listing the numbers and source transactions.

**Tier 2: the coach checks in (this is what makes it Direction 4)**
- Replay mode: start at 2026-06-01. An "Advance to next payday" button moves `as_of` to the member's next income transaction and:
  1. recalculates the safe amount,
  2. computes what was actually saved last period = `min(planned, max(0, net cash flow for that period))`, so partial and missed weeks come from the real data,
  3. pushes an in-app notification (bell icon plus toast) with the coach message.
- Coach message rules: full save means celebrate briefly; partial save means name the amount saved as a win; zero means no guilt, offer a smaller amount for next payday.
- Show a small history of paydays: planned vs saved.

**Tier 3: found money (borrowed from Direction 1)**
- "We found $X/month" card from `leaks()`. Each item has Confirm / Not a duplicate. Confirmed items can be added to the savings amount.

**Tier 4: only if time remains**
- Hand-off screen for MBR-0081 (counselor contact, no savings push).
- Irregular-income demo: MBR-0055 (Irregular pay cycle, debts current). Under the required lowest-month rule the engine returns **$0** for this member: July 2026 has a single $1,391.34 deposit, so the lowest month is far below the ~$3,539 mean and the surplus goes negative. Show this honestly as both a conservative protection (never plan savings on income that may not arrive) and a limitation (a member with a good year gets no recommendation). Only MBR-0054 has a positive lowest-month surplus among the 13 Irregular members, and it is tiny.
  - **Proposed fix for later (do not build now):** for Irregular pay cycles, save a percentage of each deposit as it arrives instead of a fixed amount per payday, so a lean month automatically saves less rather than blocking savings entirely.

**Stubs (static, no logic)**
- Enrollment screen with defaults on: overdraft protection off, duplicate detector on, round-up on (show one small number).

**Cut:** investment accounts, external account linking, category alerts, time-of-day rules, login, real texting.

## Responsible AI (for the demo and the code)
- Guidance, not advice. Every move is opt-in and pausable.
- Serve the member's own goal, never GESA products.
- Hand off to a human when `handoff_needed` is True.
- Irregular income: conservative lowest-month estimate plus the 25% buffer.
- Open questions for GESA: compliance treatment of any savings lock, overdraft defaults, whether flags are reliable in production data.

## Demo script (5 minutes)
1. Meet MBR-0026. 2. The one recommendation and why. 3. Advance paydays, including a partial-win check-in. 4. Toggle the corrupted number, validator blocks it. 5. MBR-0081 hand-off. 6. Honest limitations.
Stop building with 45 minutes left and rehearse.
