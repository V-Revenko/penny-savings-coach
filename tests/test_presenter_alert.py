"""The validator's fallback is invisible on the phone and visible only in the presenter panel."""
from pathlib import Path

import pytest

from app import explain, main

STATIC = Path(main.STATIC_DIR) / "index.html"
MEMBER = "MBR-0026"


@pytest.fixture(scope="module")
def html():
    return STATIC.read_text(encoding="utf-8")


def test_phone_never_shows_offline_template_or_blocked_badges(html):
    assert "Offline template" not in html
    # The only template-styled badge left is the opt-in (i) panel's own summary line.
    assert html.count('class="badge tmpl"') == 1
    assert 'class="badge tmpl">Pre-checked message' in html
    assert html.count("VALIDATED_BADGE") >= 5  # coach card, found money, check-in, chat all use the one badge


def test_badge_says_verified_against_your_account_everywhere(html):
    assert "Checked by our validator" not in html
    assert "Verified against your account" in html
    # one badge constant feeds the coach card, found money, check-in card, and chat answers
    assert html.count("VALIDATED_BADGE") >= 5
    assert html.count("Verified against your account") == 1
    # the (i) icon still sits next to it on the recommendation card
    assert "${srcBadge}${howBtn}%%HOWPANEL%%" in html


def test_presenter_panel_has_the_blocked_line(html):
    aside = html[html.index('<aside class="presenter">') : html.index("</aside>")]
    assert 'id="presenterAlert"' in aside
    assert "Showing the template instead." in html
    assert "expected ${money(expected)}" in html


def test_a_blocked_message_carries_what_the_presenter_line_needs(monkeypatch):
    def fake_ai(system, user):
        return "You can safely move $153.18 to your Home Down Payment, because your bills and debt payments are covered."

    monkeypatch.setattr(explain, "call_model", fake_ai)
    main._load(force=True)
    main._replay_state(MEMBER, "2026-06-01")
    out = main.recommendation(member_id=MEMBER, as_of=None, corrupt=True)
    assert out["message"]["source"] == "template"          # the phone shows the template...
    assert out["message"]["ai_message"]                     # ...and the presenter can see what the AI wrote
    assert out["validator"]["blocked"][0]["text"] == "$163.18"
    assert out["amount"] == pytest.approx(153.18)           # "expected $153.18"
    assert out["message"]["text"].startswith("You can safely move $153.18")
    main._load(force=True)
