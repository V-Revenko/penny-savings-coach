"""Goal path chart: colors and line styles come from one table, so legend swatches match the chart."""
import re
from pathlib import Path

import pytest

from app import main

STATIC = Path(main.STATIC_DIR) / "index.html"
MEMBER = "MBR-0026"


@pytest.fixture(scope="module")
def html():
    return STATIC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chart_src(html):
    return html[html.index("function goalPathCard()") : html.index("function feeTip(merchant)")]


def _path_table(html):
    block = html[html.index("const PATH = {") : html.index("const pathStroke")]
    return {
        name: {"color": color, "width": float(width), "dash": dash}
        for name, color, width, dash in re.findall(r"(\w+): \{ color: '(#[0-9A-Fa-f]{6})', width: ([\d.]+), dash: '([^']*)' \}", block)
    }


def test_actual_is_brand_blue_solid_and_target_marker_is_amber(html):
    path = _path_table(html)
    brand = re.search(r"--brand: (#[0-9A-Fa-f]{6})", html).group(1)
    attention = re.search(r"--attention: (#[0-9A-Fa-f]{6})", html).group(1)
    assert path["actual"]["color"].lower() == brand.lower()
    assert path["actual"]["dash"] == ""  # solid
    assert path["target"]["color"].lower() == attention.lower()


def test_projected_line_is_dashed_and_visually_distinct(html):
    path = _path_table(html)
    assert path["projected"]["dash"] != ""
    assert path["projected"]["dash"] != path["actual"]["dash"]
    colors = {v["color"].lower() for v in path.values()}
    assert len(colors) == 3  # three elements, three distinct colors: no overlap


def test_chart_and_legend_both_use_the_same_style_table(chart_src):
    for key in ("actual", "projected", "target"):
        uses = len(re.findall(rf"pathStroke\(PATH\.{key}\)", chart_src))
        assert uses >= 2, f"{key} should be used by the chart and by its legend swatch"
    # no color literals drawn for the three data elements outside the PATH table
    assert not re.findall(r"stroke=\"#(?:4356FF|8C97FF|B7791F)", chart_src, re.I)


def test_legend_lists_only_what_is_drawn_with_the_requested_labels(chart_src):
    assert "So far:" in chart_src
    assert "Projected at safe pace" in chart_src
    assert "Your target date:" in chart_src
    assert "Safe pace:" not in chart_src  # the redundant third row is gone
    # each row appears only when its element is drawn
    assert re.search(r"\$\{projected \? `<p>\$\{swProj\}Projected at safe pace</p>`", chart_src)
    assert re.search(r"\$\{targetInView \? `<p>\$\{swTarget\}", chart_src)


def test_legend_labels_name_the_line_style_not_a_color(chart_src):
    legend = chart_src[chart_src.index('class="path-legend"') :]
    for color_word in ("blue", "amber", "green", "orange", "purple"):
        assert color_word not in legend.lower().split("</div>")[0]


def test_api_returns_a_distinct_so_far_series_after_checkins():
    main._load(force=True)
    main._replay_state(MEMBER, "2026-06-01")
    assert main.goal_path(member_id=MEMBER)["actual"] == []
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # Jun 12: nothing saved
    main.replay_advance(main.ReplayRequest(member_id=MEMBER))  # Jun 19: $17.40
    out = main.goal_path(member_id=MEMBER)
    dates = [a["date"] for a in out["actual"]]
    amounts = [a["amount"] for a in out["actual"]]
    assert dates == ["2026-06-05", "2026-06-12", "2026-06-19"]
    assert amounts == [3610.50, 3610.50, 3627.90]
    assert out["series"][0]["date"] == "2026-06-19"  # the projection starts at today
    assert out["series"][0]["amount"] == pytest.approx(3627.90)
    main._load(force=True)
