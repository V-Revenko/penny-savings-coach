"""Tier 3: 'Penny found $X a month' from fees and flagged duplicate subscriptions."""
import pytest

from app import engine, explain, validate

AS_OF = "2026-09-01"


def test_mbr_0026_reference_numbers(tables):
    """CLAUDE.md: about $49.95/mo fees and $23.32/mo duplicate subscriptions, June to August."""
    f = engine.leaks("MBR-0026", AS_OF, tables)
    assert f["window_start"] == "2026-06-01" and f["window_end"] == "2026-08-31"
    assert f["fees_monthly"] == pytest.approx(49.95, abs=0.01)
    assert f["duplicates_monthly"] == pytest.approx(23.32, abs=0.01)
    assert f["leaks_total_monthly"] == pytest.approx(73.27, abs=0.01)


def test_items_are_grouped_by_merchant_with_source_rows(tables):
    f = engine.leaks("MBR-0026", AS_OF, tables)
    fee_items = [i for i in f["items"] if i["kind"] == "fee"]
    dup_items = [i for i in f["items"] if i["kind"] == "duplicate"]
    assert {i["merchant"] for i in fee_items} == {"Late Payment Fee", "Account Service Fee"}
    assert {i["merchant"] for i in dup_items} == {"NewsDaily Digital", "FitNow Monthly"}
    for item in f["items"]:
        assert item["transaction_ids"]
        assert all(t.startswith("TXN-") for t in item["transaction_ids"])
        assert item["monthly_amount"] > 0
    news = next(i for i in dup_items if i["merchant"] == "NewsDaily Digital")
    assert news["count"] == 2
    assert news["monthly_amount"] == pytest.approx((39.99 + 15.99) / 3, abs=0.01)


def test_no_leaks_for_a_member_with_none(tables):
    # MBR-0081 has debts, not necessarily fees/duplicates; test a member unlikely to have either.
    for member_id in tables["Members"]["Member_ID"]:
        f = engine.leaks(member_id, AS_OF, tables)
        if not f["items"]:
            assert f["leaks_total_monthly"] == 0.0
            assert f["fees_monthly"] == 0.0 and f["duplicates_monthly"] == 0.0
            return
    pytest.fail("expected at least one member with no leak items")


# --------------------------------------------------------------------------- #
# Wording: judgment-free
# --------------------------------------------------------------------------- #
def test_item_wording_is_judgment_free(tables):
    f = engine.leaks("MBR-0026", AS_OF, tables)
    for item in f["items"]:
        note = explain.leaks_item_note(item)
        for banned in ("waste", "wasting", "mistake", "shouldn't", "bad", "blew"):
            assert banned not in note.lower(), (item["merchant"], note)
    dup = next(i for i in f["items"] if i["kind"] == "duplicate" and i["merchant"] == "NewsDaily Digital")
    assert "might overlap" in explain.leaks_item_note(dup)


def test_fee_description_is_short_and_not_repetitive():
    fee = {"kind": "fee", "merchant": "Late Payment Fee", "count": 2}
    assert explain.leaks_item_note(fee) == "Charged 2 times in the last 3 months."
    assert explain.leaks_item_note({**fee, "count": 1}) == "Charged 1 time in the last 3 months."
    assert "Late Payment Fee" not in explain.leaks_item_note(fee)  # the merchant is already the item's title


def test_fee_items_use_a_question_mark_button_and_the_tightened_tips():
    from pathlib import Path

    html = (Path(main_static()) / "index.html").read_text(encoding="utf-8")
    assert "Avoid this fee" not in html
    assert 'aria-label="Show a tip for avoiding this fee"' in html
    assert ">?</button>" in html and "leak-q" in html
    assert "min-height: 46px" in html[html.index(".leak-q") : html.index(".leak-q") + 200]  # same tap target size
    assert "skip: 'Dismiss'" in html                                                            # Dismiss unchanged
    for tip in (
        "A low-balance alert can help you avoid this.",
        "A payment reminder a few days ahead can help you avoid this.",
        "Some accounts waive this with a minimum balance or direct deposit. Worth asking about.",
    ):
        assert tip in html
    assert "Noted" in html


def main_static():
    from app import main

    return main.STATIC_DIR


def test_template_headline_validates(tables):
    f = engine.leaks("MBR-0026", AS_OF, tables)
    msg = explain.template_leaks(f)
    report = validate.validate_message(msg, f, required="leaks_total_monthly")
    assert report["passed"] is True, report["summary"]
    assert report["style"]["ok"] is True, report["style"]
    for banned in ("waste", "wasting", "mistake"):
        assert banned not in msg.lower()


# --------------------------------------------------------------------------- #
# Validator: every amount from the engine
# --------------------------------------------------------------------------- #
def test_leaks_message_blocks_invented_total(tables):
    f = engine.leaks("MBR-0026", AS_OF, tables)

    def liar(system, user):
        return "Penny found $500.00 a month. Take a look below."

    out = explain.leaks_message(f, "Encouraging", call=liar)
    assert out["source"] == "template"
    assert out["validator"]["blocked"][0]["value"] == 500.0


def test_leaks_message_accepts_real_total(tables):
    f = engine.leaks("MBR-0026", AS_OF, tables)
    total = f"${round(f['leaks_total_monthly'], 2):,.2f}"

    def honest(system, user):
        return f"Penny found {total} a month. Take a look below, no pressure to change anything."

    out = explain.leaks_message(f, "Encouraging", call=honest)
    assert out["source"] == "ai"
    assert out["validator"]["passed"] is True


def test_leaks_message_requires_the_total(tables):
    f = engine.leaks("MBR-0026", AS_OF, tables)

    def vague(system, user):
        return "Penny spotted a few things. Take a look below."

    out = explain.leaks_message(f, "Encouraging", call=vague)
    assert out["source"] == "template"
    assert out["validator"]["missing_amount"] is True


# --------------------------------------------------------------------------- #
# API: confirming an item adds it to the next payday's offer
# --------------------------------------------------------------------------- #
def test_confirming_a_leak_item_raises_the_next_offer():
    from app import main

    main._load(force=True)
    member_id = "MBR-0026"
    main._replay_state(member_id, "2026-06-01")
    before = main.recommendation(member_id=member_id, as_of=None, corrupt=False)
    facts = engine.leaks(member_id, before["as_of"], main._state["tables"])
    item = facts["items"][0]

    result = main.leaks_confirm(main.LeakConfirm(member_id=member_id, item_id=item["id"], confirmed=True))
    assert result["confirmed"] is True
    assert result["confirmed_monthly"] == pytest.approx(item["monthly_amount"], abs=0.01)

    after = main.recommendation(member_id=member_id, as_of=None, corrupt=False)
    assert after["confirmed_leaks_monthly"] == pytest.approx(item["monthly_amount"], abs=0.01)
    assert after["amount"] > before["amount"]
    assert validate.validate_message(after["message"]["text"], {**after["facts"], "offer_amount": after["amount"]}, handoff=after["handoff"]["needed"])["passed"] or after["message"]["source"] == "template"

    undo = main.leaks_confirm(main.LeakConfirm(member_id=member_id, item_id=item["id"], confirmed=False))
    assert undo["confirmed"] is False
    assert undo["confirmed_monthly"] == 0.0


# --------------------------------------------------------------------------- #
# Hand-off framing: "ways to free up money", no savings push (MBR-0062)
# --------------------------------------------------------------------------- #
def test_mbr_0062_handoff_leaks_have_items_to_show(tables):
    needed, reason = engine.handoff_needed("MBR-0062", AS_OF, tables)
    assert needed is True
    f = engine.leaks("MBR-0062", AS_OF, tables)
    assert f["items"], "MBR-0062 needs at least one leak item to demo the hand-off card"
    assert f["leaks_total_monthly"] > 0


def test_handoff_template_never_mentions_goals_or_saving(tables):
    f = engine.leaks("MBR-0062", AS_OF, tables)
    msg = explain.template_leaks(f, handoff=True)
    report = validate.validate_message(msg, f, required="leaks_total_monthly")
    assert report["passed"] is True, report["summary"]
    for banned in ("goal", "save", "saving", "safely move"):
        assert banned not in msg.lower(), msg
    assert "free up" in msg.lower()


def test_handoff_prompt_forbids_goal_and_saving_language(tables):
    f = engine.leaks("MBR-0062", AS_OF, tables)
    prompt = explain.build_leaks_prompt(f, "Direct", handoff=True)
    assert "do not mention goals, saving" in prompt.lower()
    non_handoff_prompt = explain.build_leaks_prompt(f, "Direct", handoff=False)
    assert "do not mention goals" not in non_handoff_prompt.lower()


def test_handoff_leaks_message_uses_the_handoff_prompt(tables):
    f = engine.leaks("MBR-0062", AS_OF, tables)
    total = f"${round(f['leaks_total_monthly'], 2):,.2f}"

    def honest(system, user):
        return f"Penny found {total} a month you might be able to free up. Take a look below."

    out = explain.leaks_message(f, "Direct", call=honest, handoff=True)
    assert out["source"] == "ai"
    assert out["validator"]["passed"] is True
    assert "goal" not in out["message"].lower()


def test_api_leaks_flags_handoff_and_frames_without_confirm(monkeypatch):
    from app import main

    main._load(force=True)
    main._replay_state("MBR-0062", "2026-09-01")
    result = main.leaks(member_id="MBR-0062", corrupt=False)
    assert result["handoff"] is True
    assert result["handoff_reason"]
    assert result["total_monthly"] > 0
    assert len(result["items"]) > 0
    assert "goal" not in result["message"]["text"].lower()
