"""FastAPI routes. Run: uvicorn app.main:app --reload, then open http://localhost:8000"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import data, engine, explain, validate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

DEFAULT_MEMBER = "MBR-0026"
REPLAY_STARTS = ("2026-06-01", "2026-09-01")  # replay from June, or jump to end of data

app = FastAPI(title="GESA Paycheck Savings Coach", version="0.2")

# In-memory state for the prototype. Prompt 6 moves check-ins to the coach_checkin table.
_state: dict = {"tables": None, "raw": None, "report": None, "loaded_at": None}
_replay: dict[str, dict] = {}
_settings: dict[str, dict] = {}  # per member; survives a replay reset, cleared by a full demo reset
_checkin_seq = 0
_note_seq = 0

ROOM_MESSAGE = (
    "Right now there's not quite enough room to save safely, but you're very close. "
    "Let's focus on finding some extra room first."
)
AUTO_PAUSED_MESSAGE = "Paused: let's talk to someone about your situation first."
# The real-world window is 1 hour. In the demo, Change and Cancel simply work immediately.
AUTO_WINDOW_HOURS = 1


def _load(force: bool = False) -> dict:
    if force or _state["tables"] is None:
        raw = data.read_workbook()  # the quality gate always audits the untouched workbook
        _state["raw"] = raw
        _state["report"] = data.quality_report(raw)
        if force:
            data.load_tables.cache_clear()
        _state["tables"] = data.load_tables()  # data/gesa.db when built, else the workbook
        _state["loaded_at"] = datetime.now().isoformat(timespec="seconds")
        _replay.clear()
        _settings.clear()
        data.clear_checkins()  # replay state is in memory, so stored check-ins start fresh too
    return _state


def _rounded(value):
    """Round money to cents for display. The engine keeps full precision internally."""
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    return value


def _member_summary(tables, member_id: str) -> dict:
    m = tables["Members"]
    row = m[m["Member_ID"] == member_id]
    if row.empty:
        raise HTTPException(404, f"{member_id} not found")
    r = row.iloc[0]
    return {
        "member_id": member_id,
        "name": str(r.get("Synthetic_Name", member_id)),
        "pay_cycle": str(r["Pay_Cycle"]),
        "region": str(r["Region"]),
    }


# --------------------------------------------------------------------------- #
# Replay state (Tier 2)
# --------------------------------------------------------------------------- #
def _replay_state(member_id: str, start: str | None = None) -> dict:
    tables = _load()["tables"]
    if start is not None or member_id not in _replay:
        start = start or engine.REPLAY_START
        if start not in REPLAY_STARTS:
            raise HTTPException(400, f"start must be one of {REPLAY_STARTS}")
        # Position on the first payday on/after the start so the first period is a real pay period.
        as_of = engine.first_payday_on_or_after(member_id, start, tables) or start
        if start == "2026-09-01":
            as_of = start  # end of data: reference-number view, nothing to advance to
        data.clear_checkins(member_id)
        _replay[member_id] = {
            "member_id": member_id,
            "start": start,
            "as_of": as_of,
            "planned": None,        # set by Accept / Adjust / Not now
            "offer": None,          # None = engine's safe amount
            "confirmed_leaks": {},  # item_id -> monthly_amount, added to the next payday's offer
            "history": [],
            "notes": [],            # auto-contribute notifications (Settings feature)
            "unread": 0,
        }
    return _replay[member_id]


def _auto_enabled(member_id: str) -> bool:
    return bool(_settings.get(member_id, {}).get("auto_contribute", False))


def _add_note(st: dict, kind: str, payday_date: str, amount: float, goal: str, message: str, status: str) -> dict:
    global _note_seq
    _note_seq += 1
    note = {
        "id": _note_seq,
        "kind": kind,
        "payday_date": payday_date,
        "amount": round(amount, 2),
        "goal": goal,
        "message": message,
        "status": status,  # active | changed | canceled | info | done
        "window_hours": AUTO_WINDOW_HOURS,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    st["notes"].append(note)
    st["unread"] += 1
    return note


def _active_auto_note(st: dict) -> dict | None:
    return next((n for n in reversed(st["notes"]) if n["kind"] == "auto_contribute" and n["status"] == "active"), None)


def _confirmed_leak_monthly(st: dict) -> float:
    return sum(st["confirmed_leaks"].values())


def _public_state(st: dict) -> dict:
    tables = _load()["tables"]
    nxt = engine.next_payday(st["member_id"], st["as_of"], tables)
    return {
        "member_id": st["member_id"],
        "start": st["start"],
        "as_of": st["as_of"],
        "next_payday": nxt,
        "can_advance": nxt is not None,
        "planned_amount": st["planned"],
        "confirmed_leaks_monthly": round(_confirmed_leak_monthly(st), 2),
        "history": st["history"],
        "notes": st["notes"],
        "auto_note": _active_auto_note(st),
        "unread": st["unread"],
    }


def _offer_for(st: dict, facts: dict) -> float:
    if facts["surplus"] < 0:
        return 0.0  # no room yet: nothing to save, and freed-up money closes the gap first
    safe = facts["safe_per_paycheck"]
    base = safe if st["offer"] is None else min(st["offer"], safe)
    leak_per_paycheck = _confirmed_leak_monthly(st) / facts["paychecks_per_month"] if facts["paychecks_per_month"] else 0.0
    return min(base + leak_per_paycheck, facts["goal_remaining"])


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def index():
    # Always fetch the live file: the phone UI is edited often during the demo,
    # and a browser-cached copy silently running old JS is a hard bug to spot.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# Data / import
# --------------------------------------------------------------------------- #
@app.post("/api/import")
def rebuild():
    """Reload the workbook and rerun the quality gate. (Tier 1 import screen.)"""
    return quality(force=True)


@app.get("/api/quality")
def quality(force: bool = False):
    s = _load(force)
    t = s["tables"]
    return {
        "loaded_at": s["loaded_at"],
        "source": data.WORKBOOK_PATH.name,
        "engine_reads_from": "data/gesa.db" if data.DB_PATH.exists() else "workbook (data/gesa.db not built yet)",
        "gate_passed": data.gate_passed(s["report"]),
        "checks": s["report"],
        "counts": {name: int(len(frame)) for name, frame in t.items()},
        "assumptions": [
            f"Example starting balance of ${data.DEFAULT_OPENING_BALANCE:,.0f} per member (the workbook has no balances)."
        ],
        "framing": "Inconsistencies in the data are treated as signals that need attention, not errors. Only structural problems fail.",
    }


SIGNAL_LABELS = {
    "credit_band_vs_score": "Credit band vs. credit score",
    "account_age_vs_member_age": "Account age vs. member age",
    "stored_debt_months_vs_ours": "Stored debt payoff months vs. our calculation",
    "minimums_over_half_income": "Debt minimums over half of income",
}


@app.get("/api/portfolio")
def portfolio(as_of: str = Query("2026-09-01")):
    s = _load()
    result = engine.portfolio_check(as_of, s["raw"], s["tables"])
    return {
        **result,
        "handoff_by_reason": [{"reason": r, "count": c} for r, c in result["handoff_by_reason"].items()],
        "needs_attention": [
            {"key": k, "label": SIGNAL_LABELS[k], "count": c, "note": "A person should review these, not a failure."}
            for k, c in result["needs_attention"].items()
        ],
    }


@app.get("/api/members")
def members():
    t = _load()["tables"]
    m = t["Members"]
    goals = t["Goals"].set_index("Member_ID")
    out = []
    for _, r in m.iterrows():
        mid = str(r["Member_ID"])
        out.append(
            {
                "member_id": mid,
                "name": str(r.get("Synthetic_Name", mid)),
                "pay_cycle": str(r["Pay_Cycle"]),
                "goal_type": str(goals.loc[mid, "Goal_Type"]) if mid in goals.index else None,
            }
        )
    return {"default": DEFAULT_MEMBER, "members": out}


# --------------------------------------------------------------------------- #
# Recommendation
# --------------------------------------------------------------------------- #
@app.get("/api/recommendation")
def recommendation(
    member_id: str = Query(DEFAULT_MEMBER),
    as_of: str | None = Query(None, description="Defaults to the replay position for this member"),
    corrupt: bool = Query(False, description="Demo toggle: corrupt one number in the AI output"),
):
    s = _load()
    tables = s["tables"]
    member = _member_summary(tables, member_id)
    st = _replay_state(member_id)
    as_of = as_of or st["as_of"]

    failures = [c for c in s["report"] if c["status"] == "fail"]
    if failures:
        return {
            "member": member,
            "as_of": as_of,
            "gate_passed": False,
            "gate_failures": failures,
            "reason": "The data quality gate failed, so no recommendation can be made.",
        }

    try:
        facts = engine.safe_to_save(member_id, as_of, tables)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    facts["offer_amount"] = _offer_for(st, facts) if as_of == st["as_of"] else facts["safe_per_paycheck"]
    needed, handoff_reason = engine.handoff_needed(member_id, as_of, tables)
    # Right after a missed or partial payday, Penny's message should connect to what just happened.
    if as_of == st["as_of"] and st["history"] and st["history"][-1]["outcome"] in ("none", "partial"):
        last = st["history"][-1]
        facts["after_outcome"] = last["outcome"]
        if last["outcome"] == "partial":
            facts["saved_amount"] = last["saved_amount"]
            facts["planned_amount"] = last["planned_amount"]
    room, _room_reason = engine.room_needed(member_id, as_of, tables)
    if room:  # a small shortfall: soft message, no amount, found money is the next step
        text = ROOM_MESSAGE
        result = {
            "message": text, "source": "template", "model": None, "error": None, "ai_message": None,
            "validator": validate.validate_message(text, facts, handoff=True),
        }
    else:
        result = explain.coach_message(facts, facts["coaching_tone"], handoff_reason if needed else None, corrupt=corrupt)

    goal = tables["Goals"][tables["Goals"]["Member_ID"] == member_id].iloc[0]
    target_date = goal.get("Target_Date")

    window = engine.window_transactions(tables, member_id, as_of).sort_values("Transaction_Date", ascending=False)
    transactions = [
        {
            "id": str(r["Transaction_ID"]),
            "date": r["Transaction_Date"].date().isoformat(),
            "category": str(r["Category"]),
            "amount": round(float(r["Amount"]), 2),
            "possible_duplicate": bool(r["Possible_Duplicate_Flag"]),
        }
        for _, r in window.iterrows()
    ]

    comparisons = [
        {
            "item": "Monthly savings step",
            "ours": round(facts["safe_monthly"], 2),
            "ours_label": "safe per month",
            "stored": _rounded(facts["stored_suggested_monthly"]),
            "stored_label": "Stored estimate",
            "unit": "money",
            "signal": bool(facts["suggested_exceeds_income"]),
            "note": "Stored suggestion is above monthly income; Penny offers a smaller reachable step." if facts["suggested_exceeds_income"] else "",
        }
    ] + [
        {
            "item": f"{d['debt_type']}: months to pay off at the minimum",
            "ours": d["months_remaining_ours"],
            "ours_label": "with interest at the APR" if d["apr"] else "balance ÷ minimum",
            "stored": d["months_remaining_stored"],
            "stored_label": "Stored estimate",
            "unit": "months",
            "signal": d["months_remaining_ours"] != d["months_remaining_stored"],
            "note": "",
        }
        for d in facts["debts"]
    ]

    return {
        "member": {**member, "coaching_tone": facts["coaching_tone"]},
        "as_of": facts["as_of"],
        "replay": _public_state(st),
        "gate_passed": True,
        "handoff": {"needed": needed, "reason": handoff_reason},
        "room": {"needed": room, "message": ROOM_MESSAGE, "shortfall": round(max(0.0, -facts["surplus"]), 2)} if room else {"needed": False},
        "goal": {
            "type": facts["goal_type"],
            "target": round(facts["goal_target"], 2),
            "saved": round(facts["goal_saved"], 2),
            "remaining": round(facts["goal_remaining"], 2),
            "progress": round(facts["goal_saved"] / facts["goal_target"], 4) if facts["goal_target"] else 0,
            "target_date": target_date.date().isoformat() if hasattr(target_date, "date") else None,
        },
        "amount": round(facts["offer_amount"], 2),
        "safe_amount": round(facts["safe_per_paycheck"], 2),
        "confirmed_leaks_monthly": round(_confirmed_leak_monthly(st), 2),
        "smaller_step": bool(facts["suggested_exceeds_income"]),
        "reason": facts["reason"],
        "message": {
            "text": result["message"],
            "source": result["source"],
            "model": result["model"],
            "error": result["error"],
            "ai_message": result["ai_message"],
            "corrupted": corrupt,
        },
        "validator": result["validator"],
        "facts": _rounded(facts),
        "comparisons": comparisons,
        "assumptions": [f"Example starting balance of ${facts['opening_balance']:,.0f} (not in the data)."],
        "transactions": transactions,
    }


# --------------------------------------------------------------------------- #
# Adjust slider: "sooner by" comparison against the full safe amount
# --------------------------------------------------------------------------- #
@app.get("/api/paydays-sooner")
def paydays_sooner(member_id: str = Query(DEFAULT_MEMBER), amount: float = Query(..., ge=0)):
    tables = _load()["tables"]
    _member_summary(tables, member_id)
    st = _replay_state(member_id)
    facts = engine.safe_to_save(member_id, st["as_of"], tables)
    full = round(_offer_for(st, facts), 2)
    out = engine.paydays_sooner(facts["goal_remaining"], full, round(amount, 2))
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items()}


# --------------------------------------------------------------------------- #
# Goal path (chart)
# --------------------------------------------------------------------------- #
@app.get("/api/goal-path")
def goal_path(member_id: str = Query(DEFAULT_MEMBER)):
    tables = _load()["tables"]
    _member_summary(tables, member_id)
    st = _replay_state(member_id)
    extra = sum(c["saved_amount"] for c in st["history"])
    p = engine.goal_projection(member_id, st["as_of"], tables, extra_saved=extra)
    # "So far": the saved-to-date line, from the starting balance through each check-in.
    actual = []
    if st["history"]:
        running = p["saved"] - extra
        actual.append({"date": st["history"][0]["period_start"], "amount": round(running, 2)})
        for c in st["history"]:
            running += c["saved_amount"]
            actual.append({"date": c["payday_date"], "amount": round(running, 2)})
    return {
        **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in p.items() if k != "series"},
        "series": [{"date": s["date"], "amount": round(s["amount"], 2)} for s in p["series"]],
        "actual": actual,
        "checkin_saved": round(extra, 2),
    }


# --------------------------------------------------------------------------- #
# Ask Penny (chat)
# --------------------------------------------------------------------------- #
class AskRequest(BaseModel):
    member_id: str
    question: str
    corrupt: bool = False


@app.post("/api/ask")
def ask(req: AskRequest):
    tables = _load()["tables"]
    member = _member_summary(tables, req.member_id)
    st = _replay_state(req.member_id)
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "question must not be empty")

    facts = engine.safe_to_save(req.member_id, st["as_of"], tables)
    facts["offer_amount"] = _offer_for(st, facts)
    needed, handoff_reason = engine.handoff_needed(req.member_id, st["as_of"], tables)
    result = explain.ask_penny(
        facts, facts["coaching_tone"], question, handoff_reason if needed else None, corrupt=req.corrupt
    )
    return {
        "member": {**member, "coaching_tone": facts["coaching_tone"]},
        "question": question,
        "answer": result["message"],
        "source": result["source"],
        "model": result["model"],
        "error": result["error"],
        "ai_message": result["ai_message"],
        "category": result["category"],
        "show_counselor": result["show_counselor"],
        "validator": result["validator"],
    }


# --------------------------------------------------------------------------- #
# Decisions (Accept / Adjust / Not now)
# --------------------------------------------------------------------------- #
class Decision(BaseModel):
    member_id: str
    action: str  # accept | adjust | not_now
    amount: float | None = None


@app.post("/api/decision")
def decide(d: Decision):
    if d.action not in {"accept", "adjust", "not_now"}:
        raise HTTPException(400, "action must be accept, adjust or not_now")
    tables = _load()["tables"]
    st = _replay_state(d.member_id)
    facts = engine.safe_to_save(d.member_id, st["as_of"], tables)
    offer = round(_offer_for(st, facts), 2)
    if d.action == "accept":
        planned = offer
    elif d.action == "adjust":
        if d.amount is None or d.amount < 0:
            raise HTTPException(400, "adjust needs a non-negative amount")
        planned = round(min(float(d.amount), offer), 2)
    else:
        planned = 0.0
    st["planned"] = planned
    # Keep an automatic contribution's notification in step with what the member did.
    note = _active_auto_note(st)
    if note and d.action == "adjust":
        note.update(status="changed", amount=planned, message=f"Changed to ${planned:,.2f} toward your {note['goal']}.")
    elif note and d.action == "not_now":
        note.update(status="canceled", message="Canceled. Nothing was moved this payday.")
    return {
        "member_id": d.member_id,
        "as_of": st["as_of"],
        "action": d.action,
        "planned_amount": planned,
        "offer_amount": offer,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# Settings: auto-contribute
# --------------------------------------------------------------------------- #
class SettingsUpdate(BaseModel):
    member_id: str
    auto_contribute: bool


@app.get("/api/settings")
def get_settings(member_id: str = Query(DEFAULT_MEMBER)):
    _member_summary(_load()["tables"], member_id)
    return {"member_id": member_id, "auto_contribute": _auto_enabled(member_id), "window_hours": AUTO_WINDOW_HOURS}


@app.post("/api/settings")
def update_settings(req: SettingsUpdate):
    _member_summary(_load()["tables"], req.member_id)
    _settings.setdefault(req.member_id, {})["auto_contribute"] = req.auto_contribute
    return get_settings(member_id=req.member_id)


class AutoCancel(BaseModel):
    member_id: str
    note_id: int


@app.post("/api/auto/cancel")
def auto_cancel(req: AutoCancel):
    """Cancel an automatic contribution within its window: the plan for this payday is reversed."""
    st = _replay_state(req.member_id)
    note = next((n for n in st["notes"] if n["id"] == req.note_id), None)
    if note is None or note["kind"] != "auto_contribute":
        raise HTTPException(404, "no such automatic contribution")
    if note["status"] != "active":
        raise HTTPException(409, "this contribution can no longer be canceled")
    st["planned"] = 0.0  # nothing moves; the payday settles as a skipped one, not a miss
    note.update(status="canceled", message="Canceled. Nothing was moved this payday.")
    return {"note": note, "state": _public_state(st)}


# --------------------------------------------------------------------------- #
# Found money (Tier 3)
# --------------------------------------------------------------------------- #
@app.get("/api/leaks")
def leaks(member_id: str = Query(DEFAULT_MEMBER), corrupt: bool = Query(False)):
    tables = _load()["tables"]
    st = _replay_state(member_id)
    member = _member_summary(tables, member_id)
    needed, handoff_reason = engine.handoff_needed(member_id, st["as_of"], tables)
    room, _room_reason = engine.room_needed(member_id, st["as_of"], tables)
    facts = engine.leaks(member_id, st["as_of"], tables)
    tone = tables["Goals"][tables["Goals"]["Member_ID"] == member_id].iloc[0]["Coaching_Tone"]
    result = explain.leaks_message(facts, tone, corrupt=corrupt, handoff=needed or room)  # no goal or saving talk
    confirmed = st["confirmed_leaks"]
    items = [
        {
            **{k: v for k, v in item.items() if k != "transaction_ids"},
            "note": explain.leaks_item_note(item),
            "confirmed": item["id"] in confirmed,
            "transaction_ids": item["transaction_ids"],
        }
        for item in facts["items"]
    ]
    return {
        "member": member,
        "as_of": facts["as_of"],
        "window_start": facts["window_start"],
        "window_end": facts["window_end"],
        "handoff": needed,
        "room": room,
        "handoff_reason": handoff_reason,
        "total_monthly": round(facts["leaks_total_monthly"], 2),
        "fees_monthly": round(facts["fees_monthly"], 2),
        "duplicates_monthly": round(facts["duplicates_monthly"], 2),
        "confirmed_monthly": round(_confirmed_leak_monthly(st), 2),
        "items": items,
        "message": {
            "text": result["message"],
            "source": result["source"],
            "model": result["model"],
            "error": result["error"],
            "ai_message": result["ai_message"],
            "corrupted": corrupt,
        },
        "validator": result["validator"],
    }


class LeakConfirm(BaseModel):
    member_id: str
    item_id: str
    confirmed: bool


@app.post("/api/leaks/confirm")
def leaks_confirm(req: LeakConfirm):
    tables = _load()["tables"]
    st = _replay_state(req.member_id)
    facts = engine.leaks(req.member_id, st["as_of"], tables)
    item = next((i for i in facts["items"] if i["id"] == req.item_id), None)
    if item is None:
        raise HTTPException(404, f"leak item {req.item_id} not found")
    if req.confirmed:
        st["confirmed_leaks"][req.item_id] = item["monthly_amount"]
    else:
        st["confirmed_leaks"].pop(req.item_id, None)
    return {
        "item_id": req.item_id,
        "confirmed": req.item_id in st["confirmed_leaks"],
        "confirmed_monthly": round(_confirmed_leak_monthly(st), 2),
    }


# --------------------------------------------------------------------------- #
# Replay (Tier 2)
# --------------------------------------------------------------------------- #
class ReplayRequest(BaseModel):
    member_id: str
    start: str | None = None
    corrupt: bool = False


@app.get("/api/replay")
def replay(member_id: str = Query(DEFAULT_MEMBER)):
    return _public_state(_replay_state(member_id))


@app.post("/api/replay/reset")
def replay_reset(req: ReplayRequest):
    return _public_state(_replay_state(req.member_id, req.start or engine.REPLAY_START))


@app.post("/api/replay/read")
def replay_mark_read(req: ReplayRequest):
    st = _replay_state(req.member_id)
    st["unread"] = 0
    return _public_state(st)


@app.post("/api/replay/advance")
def replay_advance(req: ReplayRequest):
    """Move to the member's next payday: settle last period, recalc, and push a check-in."""
    global _checkin_seq
    tables = _load()["tables"]
    st = _replay_state(req.member_id)
    nxt = engine.next_payday(req.member_id, st["as_of"], tables)
    if nxt is None:
        raise HTTPException(409, "End of data: no later payday in the workbook.")

    facts_old = engine.safe_to_save(req.member_id, st["as_of"], tables)
    offer_old = _offer_for(st, facts_old)
    planned = st["planned"] if st["planned"] is not None else round(offer_old, 2)  # untouched = accepted

    flow = engine.period_net_cash_flow(req.member_id, st["as_of"], nxt, tables)
    saved = round(engine.actual_saved(planned, flow["net"]), 2)
    outcome = engine.checkin_outcome(planned, saved)

    facts_new = engine.safe_to_save(req.member_id, nxt, tables)
    needed, handoff_reason = engine.handoff_needed(req.member_id, nxt, tables)
    offer_new = round(engine.next_offer(facts_new["safe_per_paycheck"], planned, saved), 2)

    cf = explain.checkin_facts(facts_new, planned, saved, offer_new, outcome)
    msg = explain.checkin_message(cf, facts_new["coaching_tone"], corrupt=req.corrupt)

    # Found money the member committed to counts once savings actually landed this period.
    found_money = []
    if saved > 0:
        for item_id, monthly in st["confirmed_leaks"].items():
            kind, _, merchant = item_id.partition(":")
            found_money.append({"kind": kind, "merchant": merchant, "monthly_amount": round(monthly, 2)})

    _checkin_seq += 1
    record = {
        "checkin_id": _checkin_seq,
        "member_id": req.member_id,
        "goal_type": facts_new["goal_type"],
        "payday_date": nxt,
        "period_start": flow["period_start"],
        "period_end": flow["period_end"],
        "period_income": round(flow["income"], 2),
        "period_spending": round(flow["spending"], 2),
        "period_net": round(flow["net"], 2),
        "planned_amount": planned,
        "saved_amount": saved,
        "outcome": outcome,
        "next_offer": offer_new,
        "handoff": needed,
        "message": msg["message"],
        "source": msg["source"],
        "validator_passed": msg["validator"]["passed"],
        "validator": msg["validator"],
        "ai_message": msg["ai_message"],
        "found_money": found_money,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    st.update({"as_of": nxt, "planned": None, "offer": offer_new})
    st["history"].append(record)
    st["unread"] += 1
    data.write_checkin(req.member_id, nxt, planned, saved, msg["message"], msg["validator"]["passed"])

    # A new payday closes the previous automatic contribution's window.
    for n in st["notes"]:
        if n["status"] == "active":
            n["status"] = "done"

    # Auto-contribute: applied exactly as if the member had tapped Save, unless they're in hand-off.
    auto_note = None
    if _auto_enabled(req.member_id):
        if needed:
            auto_note = _add_note(st, "auto_paused", nxt, 0.0, facts_new["goal_type"], AUTO_PAUSED_MESSAGE, "info")
        else:
            facts_now = engine.safe_to_save(req.member_id, nxt, tables)
            amount = round(_offer_for(st, facts_now), 2)
            if amount > 0:
                st["planned"] = amount
                auto_note = _add_note(
                    st, "auto_contribute", nxt, amount, facts_now["goal_type"],
                    f"Penny moved ${amount:,.2f} toward your {facts_now['goal_type']}. "
                    f"Change the amount or cancel within {AUTO_WINDOW_HOURS} hour.",
                    "active",
                )
    return {"checkin": record, "state": _public_state(st), "auto_note": auto_note}


# --------------------------------------------------------------------------- #
# Goals screen: the goal, and the deposit history from the coach_checkin table
# --------------------------------------------------------------------------- #
@app.get("/api/goals")
def goals(member_id: str = Query(DEFAULT_MEMBER)):
    tables = _load()["tables"]
    _member_summary(tables, member_id)
    st = _replay_state(member_id)
    facts = engine.safe_to_save(member_id, st["as_of"], tables)

    if data.db_available():
        stored = data.read_checkins(member_id)
        source = "coach_checkin"
    else:  # no database built: fall back to this session's history
        stored = [
            {"payday_date": c["payday_date"], "planned_amount": c["planned_amount"], "saved_amount": c["saved_amount"]}
            for c in reversed(st["history"])
        ]
        source = "session"
    found_by_date = {c["payday_date"]: c.get("found_money", []) for c in st["history"]}

    deposits = [
        {
            "date": c["payday_date"],
            "amount": round(c["saved_amount"], 2),
            "planned": round(c["planned_amount"], 2),
            "outcome": engine.checkin_outcome(c["planned_amount"], c["saved_amount"]),
            "found_money": found_by_date.get(c["payday_date"], []),
        }
        for c in stored
    ]
    # An approved plan shows up right away as "pending". The real row (what actually
    # landed) replaces it at the next payday.
    if st["planned"]:
        pending_date = engine.next_payday(member_id, st["as_of"], tables) or st["as_of"]
        deposits.insert(
            0,
            {
                "date": pending_date,
                "amount": round(st["planned"], 2),
                "planned": round(st["planned"], 2),
                "outcome": "pending",
                "found_money": [],
            },
        )
    return {
        "goal": {
            "type": facts["goal_type"],
            "target": round(facts["goal_target"], 2),
            "saved": round(facts["goal_saved"], 2),
            "remaining": round(facts["goal_remaining"], 2),
            "progress": round(facts["goal_saved"] / facts["goal_target"], 4) if facts["goal_target"] else 0,
        },
        "deposits": deposits,
        "source": source,
    }
