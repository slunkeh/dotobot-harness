"""Chart specs: the one shape a chart takes wherever it is drawn.

A chart is described as plain JSON so a bot can emit one from any tool and
every client draws it natively. The same spec is used in two places:

    show_chart        a `chart` card in the conversation (payload["chart"])
    show_block        a `{"type": "chart", ...}` node inside a block view

so this module owns the vocabulary and the validation for both. Shape:

    {
      "kind": "line",                       # see KINDS
      "labels": ["Mon", "Tue", "Wed"],      # category axis (optional)
      "series": [
        {"name": "visits", "values": [12, 30, 21], "color": "#4C8DFF"},
      ],
      "x_label": "day", "y_label": "visits",
      "stacked": false,                     # bar/column/area
      "y_min": 0, "y_max": 100,             # axis clamps (optional)
      "legend": true,                       # default: on for 2+ series
      "height": 180                         # points; clients clamp
    }

Values are numbers for the category kinds; `scatter` takes `[x, y]` pairs
instead. `pie`/`donut` take exactly one series, one slice per label.
Everything else is layout the client is free to interpret.
"""

from __future__ import annotations

import math
import re
from typing import Any

#: chart kinds. bar is horizontal (categories down the side), column is
#: vertical (categories along the bottom) — the two names the model reaches
#: for when a user says "horizontal bar chart" / "vertical bar chart".
KINDS = (
    "line",
    "area",
    "bar",
    "column",
    "pie",
    "donut",
    "scatter",
    "sparkline",
)

#: kinds that plot one value per category label
_CATEGORY_KINDS = ("line", "area", "bar", "column", "sparkline")
#: kinds that take exactly one series, one slice per label
_SLICE_KINDS = ("pie", "donut")

MAX_SERIES = 8
MAX_POINTS = 500

#: six-digit hex only — the clients parse exactly this form
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _number(value: Any) -> float | None:
    """Coerce a JSON scalar to a finite float, or None when it is not one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return num if math.isfinite(num) else None


def validate_chart(spec: Any) -> str | None:
    """Return an error string for a bad chart spec, or None when it is valid.

    Errors are worded for the model so it can correct the spec and retry —
    same contract as `agent.blocks.validate_view`.
    """
    if not isinstance(spec, dict):
        return "error: chart must be an object"
    kind = str(spec.get("kind") or "").strip().lower()
    if kind not in KINDS:
        return f"error: unknown chart kind {kind!r}; use one of: {', '.join(KINDS)}"
    series = spec.get("series")
    if not isinstance(series, list) or not series:
        return "error: chart needs a non-empty 'series' list"
    if len(series) > MAX_SERIES:
        return f"error: chart has more than {MAX_SERIES} series"
    if kind in _SLICE_KINDS and len(series) != 1:
        return f"error: a {kind} chart takes exactly one series (one slice per label)"
    labels = spec.get("labels")
    if labels is not None and not isinstance(labels, list):
        return "error: chart 'labels' must be a list of strings"
    for index, entry in enumerate(series):
        if not isinstance(entry, dict):
            return f"error: series {index} must be an object with 'values'"
        values = entry.get("values")
        if not isinstance(values, list) or not values:
            return f"error: series {index} needs a non-empty 'values' list"
        if len(values) > MAX_POINTS:
            return f"error: series {index} has more than {MAX_POINTS} points"
        color = entry.get("color")
        if color is not None and not _HEX.match(str(color)):
            return f"error: series {index} color must be a 6-digit hex string like '#4C8DFF'"
        if kind == "scatter":
            for point in values:
                if (
                    not isinstance(point, (list, tuple))
                    or len(point) != 2
                    or _number(point[0]) is None
                    or _number(point[1]) is None
                ):
                    return f"error: scatter series {index} takes [x, y] number pairs"
        else:
            if any(_number(v) is None for v in values):
                return f"error: series {index} values must all be numbers"
            if kind in _SLICE_KINDS and any((_number(v) or 0.0) < 0 for v in values):
                return f"error: a {kind} chart cannot plot negative values"
            if isinstance(labels, list) and labels and len(labels) != len(values):
                return (
                    f"error: series {index} has {len(values)} values but there are "
                    f"{len(labels)} labels; they must line up"
                )
    for bound in ("y_min", "y_max"):
        if spec.get(bound) is not None and _number(spec.get(bound)) is None:
            return f"error: chart {bound!r} must be a number"
    y_min, y_max = _number(spec.get("y_min")), _number(spec.get("y_max"))
    if y_min is not None and y_max is not None and y_min >= y_max:
        return "error: chart 'y_min' must be below 'y_max'"
    return None


def normalize_chart(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a clean copy of a validated spec: numbers as floats, no extras.

    Callers validate first; this only tidies (models happily send "12" for a
    value or "Line" for a kind) so every client decodes the same shapes.
    """
    kind = str(spec.get("kind") or "").strip().lower()
    out: dict[str, Any] = {"kind": kind}
    labels = spec.get("labels")
    if isinstance(labels, list) and labels:
        out["labels"] = ["" if x is None else str(x) for x in labels]
    clean_series: list[dict[str, Any]] = []
    for entry in spec.get("series") or []:
        values = entry.get("values") or []
        if kind == "scatter":
            points: Any = [[_number(p[0]) or 0.0, _number(p[1]) or 0.0] for p in values]
        else:
            points = [_number(v) or 0.0 for v in values]
        row: dict[str, Any] = {"values": points}
        name = str(entry.get("name") or "").strip()
        if name:
            row["name"] = name
        color = entry.get("color")
        if color is not None:
            row["color"] = str(color)
        clean_series.append(row)
    out["series"] = clean_series
    for key in ("title", "x_label", "y_label"):
        text = str(spec.get(key) or "").strip()
        if text:
            out[key] = text
    if spec.get("stacked") is not None:
        out["stacked"] = bool(spec.get("stacked"))
    if spec.get("legend") is not None:
        out["legend"] = bool(spec.get("legend"))
    for key in ("y_min", "y_max", "height"):
        num = _number(spec.get(key))
        if num is not None:
            out[key] = num
    return out


def chart_summary(spec: dict[str, Any]) -> str:
    """One-line description of a chart, for tool results and text fallbacks."""
    kind = str(spec.get("kind") or "chart")
    series = spec.get("series") or []
    points = sum(len(s.get("values") or []) for s in series)
    names = [str(s.get("name") or "") for s in series if str(s.get("name") or "")]
    tail = f" ({', '.join(names)})" if names else ""
    return f"{kind} chart, {len(series)} series, {points} points{tail}"
