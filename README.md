# Penny — AI-Powered Savings Coach

Penny is a savings coach built into a credit union's banking app. Each payday, she looks at a member's real income and spending, calculates a specific dollar amount they can safely set aside, and explains why in plain language — then checks back in over time instead of delivering a single one-off verdict.

Built for the **AI@Carson x Gesa Credit Union Hackathon** (1st place).

## The Problem

Most people already know they should save more. Knowing isn't the hard part — doing it in the middle of an ordinary week, with a real paycheck and real bills, is where it falls apart. General financial education is valuable but impersonal, and no credit union can afford to give every member a human coach. Penny is an attempt to close that gap: personal, timely, judgment-free guidance, delivered automatically.

## What Penny Does

- **Detects a paycheck** and calculates a safe amount to save, based on the member's own trailing income, spending, and debt minimums
- **Explains the number** in one or two plain-language sentences — one amount, one action, one reason
- Lets the member **save it, adjust it, or decline** — with no guilt attached either way
- Shows **exactly how the number was calculated**, in full transparency
- **Coaches over time**: if a member misses a week, Penny doesn't scold — she offers a smaller step next time, and treats any partial saving as a real win
- Lets members **ask Penny questions** directly (e.g. "why this amount," "what if I can't save this week")
- Knows when to **step back and hand off to a real person** — for members whose spending and debt already exceed their income, or who have a seriously delinquent debt, Penny doesn't push a savings number at all

## Why It's Trustworthy

Credit unions are a heavily regulated industry, and an AI that hallucinates a dollar figure is a real, costly problem. Penny is built so that never happens:

- **The AI never does math.** A plain-code engine calculates every dollar amount — income averages, spending, debt minimums, the savings figure — using conventional, testable arithmetic.
- **The AI only writes the sentence.** Penny (the language model) is handed exactly four allowed numbers and asked to explain them in an encouraging tone. She is never given raw transaction data or asked to compute anything herself.
- **A validator checks every output.** Before any AI-written message reaches a member, it's checked against the engine's calculated values. If any dollar figure doesn't match exactly, the message is blocked and replaced with a safe, pre-written template built directly from the correct numbers. The member never sees a wrong figure, and never sees a broken experience either way.
- **This is tested, not assumed.** The engine's results were checked against an independent hand calculation to the cent, backed by an automated test suite, and validated further by running real AI outputs through the validator — including catching a real hallucination during development (the AI once wrote "69 percent" and "two-thirds of the way there," and neither reached a member).

## Architecture

```
Member data → Engine (pure Python) → Explainer (Claude) → Validator → Member
```

- **Engine** — pure Python/pandas. Computes trailing income and spending, subtracts debt minimums, holds back a safety cushion, and splits the result across the member's actual pay cycle. No AI involved.
- **Explainer** — an LLM (Claude Haiku) that receives only four allowlisted numbers and writes a short, encouraging, member-facing explanation.
- **Validator** — checks every dollar figure the Explainer writes against the engine's actual output. Any mismatch blocks the AI message and substitutes a safe template.
- **Backend** — FastAPI serving a normalized SQLite database (built from the hackathon's synthetic dataset), with an Excel-based fallback loader.
- **Frontend** — a single-page phone-frame interface simulating a "Coach" tab inside an existing banking app, alongside a presenter panel for demoing internals live.

## By the Numbers

- Engine calculations verified against an independent hand calculation, to the cent
- 96 automated tests passing
- Across all 100 synthetic members: roughly 38 receive a personalized savings recommendation; the remaining 62 are routed to a human counselor, each with a specific reason (spending and debt already exceed income, or a debt is 60+ days delinquent)

## Known Limitations

Penny is a hackathon prototype, and her current scope is intentional:

- **Irregular/gig income** is handled conservatively (using the lowest of the last three months rather than an average), which often results in a $0 recommendation for gig workers. A planned fix is saving a small percentage of each deposit instead.
- **Household context** (e.g. a single parent, or a member supporting family abroad) isn't modeled yet. A planned fix is letting members mark specific recurring costs as fixed essentials.
- **This is guidance, not financial advice.** Penny does not recommend specific investments, promise outcomes, or replace a human financial counselor — she's designed to know when to step aside for one.
- A real deployment would need substantially more testing and stronger guardrails, implemented and reviewed by an AI engineer, before ever reaching a real member.

## Piloting This

Penny is designed to live as a "Coach" tab inside a credit union's existing banking app — nothing new for a member to download, and built entirely from data a bank already collects (transactions, balances, pay cycle). A pilot would start with an opt-in group and track fees avoided, goal progress, and counselor referrals.

## Tech Stack

Python, FastAPI, SQLite/pandas, Claude (Anthropic), HTML/JS frontend.

## Team

- [Vlad Revenko](https://www.linkedin.com/in/vlad-revenko-93b046326/)
- [Salomon Soto-Serna](https://www.linkedin.com/in/salomon-soto-serna/)
- [Osman Sumareh](https://www.linkedin.com/in/osman-sumareh/)
- [Alexander Clark](https://www.linkedin.com/in/alexander-clark-pnw/)

## Running It Locally

This project currently runs locally only (no hosted deployment).

```bash
git clone https://github.com/V-Revenko/penny-savings-coach.git
cd penny-savings-coach

python -m venv .venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt

cp .env.example .env        # then add your own Claude API key
```

See `CLAUDE.md` for the full project spec and run instructions, and `docs/` for the entity-relationship diagram and challenge brief.