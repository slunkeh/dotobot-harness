"""Chart specs: the shared vocabulary behind show_chart and the chart node."""

from __future__ import annotations

import pytest

from agent.charts import KINDS, chart_summary, normalize_chart, validate_chart
from providers.base import Message, ToolSpec
from providers.echo import EchoProvider

_LINE = {
    "kind": "line",
    "labels": ["Mon", "Tue", "Wed"],
    "series": [{"name": "visits", "values": [3, 5, 4]}],
}


# -- validate_chart --------------------------------------------------------
def test_validate_accepts_a_line_chart():
    assert validate_chart(_LINE) is None


@pytest.mark.parametrize("kind", ["line", "area", "bar", "column", "pie", "donut", "sparkline"])
def test_validate_accepts_every_category_kind(kind):
    assert validate_chart({**_LINE, "kind": kind}) is None


def test_validate_rejects_an_unknown_kind():
    err = validate_chart({**_LINE, "kind": "radar"})
    assert err.startswith("error: unknown chart kind")
    assert "column" in err  # the error lists what to use instead


def test_validate_needs_series():
    assert validate_chart({"kind": "line"}).startswith("error:")
    assert validate_chart({"kind": "line", "series": []}).startswith("error:")


def test_validate_rejects_non_numeric_values():
    err = validate_chart({"kind": "line", "series": [{"values": [1, "high"]}]})
    assert "must all be numbers" in err


def test_validate_accepts_numeric_strings():
    assert validate_chart({"kind": "line", "series": [{"values": ["1", 2.5]}]}) is None


def test_validate_requires_labels_to_line_up():
    err = validate_chart({**_LINE, "labels": ["Mon", "Tue"]})
    assert "must line up" in err


def test_validate_scatter_wants_pairs():
    assert validate_chart({"kind": "scatter", "series": [{"values": [[1, 2], [3, 4]]}]}) is None
    err = validate_chart({"kind": "scatter", "series": [{"values": [1, 2]}]})
    assert "[x, y] number pairs" in err


def test_validate_pie_takes_one_series_and_no_negatives():
    two = {"kind": "pie", "series": [{"values": [1]}, {"values": [2]}]}
    assert "exactly one series" in validate_chart(two)
    assert "negative" in validate_chart({"kind": "pie", "series": [{"values": [1, -2]}]})


def test_validate_checks_colors_and_bounds():
    assert "hex" in validate_chart({**_LINE, "series": [{"values": [1], "color": "blue"}]})
    assert validate_chart({"kind": "line", "series": [{"values": [1]}], "y_min": 5, "y_max": 1})
    assert validate_chart({**_LINE, "y_min": 0, "y_max": 10}) is None


def test_validate_caps_series_and_points():
    many = {"kind": "line", "series": [{"values": [1]} for _ in range(9)]}
    assert "more than 8 series" in validate_chart(many)
    long = {"kind": "line", "series": [{"values": list(range(501))}]}
    assert "more than 500 points" in validate_chart(long)


def test_validate_rejects_non_finite_values():
    assert validate_chart({"kind": "line", "series": [{"values": [float("nan")]}]})
    assert validate_chart({"kind": "line", "series": [{"values": [float("inf")]}]})


# -- normalize_chart -------------------------------------------------------
def test_normalize_coerces_numbers_and_drops_extras():
    out = normalize_chart(
        {
            "kind": "Column",
            "labels": ["a", "b"],
            "series": [{"name": " sales ", "values": ["1", 2], "color": "#4C8DFF"}],
            "stacked": 1,
            "height": "120",
            "nonsense": "dropped",
        }
    )
    assert out["kind"] == "column"
    assert out["series"] == [{"values": [1.0, 2.0], "name": "sales", "color": "#4C8DFF"}]
    assert out["stacked"] is True
    assert out["height"] == 120.0
    assert "nonsense" not in out


def test_normalize_keeps_scatter_pairs():
    out = normalize_chart({"kind": "scatter", "series": [{"values": [["1", 2]]}]})
    assert out["series"][0]["values"] == [[1.0, 2.0]]


def test_chart_summary_reads_as_a_sentence():
    assert chart_summary(normalize_chart(_LINE)) == "line chart, 1 series, 3 points (visits)"


# -- keyless echo demo -----------------------------------------------------
def _echo_tools():
    return [ToolSpec(name="show_chart", description="", parameters={})]


@pytest.mark.parametrize("kind", KINDS)
def test_echo_demo_draws_every_kind(kind):
    out = EchoProvider().complete(
        [Message(role="user", content=f"show a {kind} chart")], tools=_echo_tools()
    )
    call = out.tool_calls[0]
    assert call.name == "show_chart"
    assert call.arguments["kind"] == kind
    # the demo must be a spec the tool accepts, or the tour shows an error
    assert validate_chart(call.arguments) is None


def test_echo_demo_defaults_to_a_line():
    out = EchoProvider().complete([Message(role="user", content="show chart")], tools=_echo_tools())
    assert out.tool_calls[0].arguments["kind"] == "line"
