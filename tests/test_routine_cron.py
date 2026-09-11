"""Every accepted recurring schedule must match the times it describes."""

from datetime import UTC, datetime

import pytest

from agent.messaging import pending
from harness.paths import HarnessPaths
from harness.routines import (
    RoutineError,
    add_routine,
    cron_match,
    fire_due,
    list_routines,
    parse_schedule,
    update_routine,
)


def test_social_schedule_fires_once_at_each_listed_hour(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    cron = "0 9,11,13,15,17,19 * * *"
    row = add_routine(
        paths, "atlas", prompt="Check opportunities", when=cron, timezone="Etc/GMT", enabled=True
    )
    # The inspector can retain an old friendly time alongside its new cron.
    update_routine(
        paths, "atlas", row["id"], triggers=[{"type": "schedule", "time": "08:00", "cron": cron}]
    )
    for hour in range(24):
        now = datetime(2026, 9, 6, hour, 0, tzinfo=UTC)
        fired = fire_due(paths, ["atlas"], now=now)
        assert len(fired) == (1 if hour in {9, 11, 13, 15, 17, 19} else 0)
        assert fire_due(paths, ["atlas"], now=now) == []
    assert len(pending(paths, "atlas")) == 6
    assert {entry["kind"] for entry in list_routines(paths, "atlas")[0]["history"]} == {"schedule"}


@pytest.mark.parametrize(
    ("cron", "matching", "other"),
    [
        ("0 8 * * 1,4", datetime(2026, 9, 7, 8), datetime(2026, 9, 8, 8)),
        ("0 9 * * 1,4", datetime(2026, 9, 10, 9), datetime(2026, 9, 11, 9)),
        ("0 10 1,15 * *", datetime(2026, 9, 15, 10), datetime(2026, 9, 16, 10)),
        ("30 9 1,15 * *", datetime(2026, 9, 1, 9, 30), datetime(2026, 9, 2, 9, 30)),
        ("5,10-12 8-10 1-20 8-10 1-5", datetime(2026, 9, 8, 9, 11), datetime(2026, 7, 8, 9, 11)),
        ("0 8 * * 5-7", datetime(2026, 9, 6, 8), datetime(2026, 9, 7, 8)),
    ],
)
def test_lists_and_ranges_match_across_cron_fields(cron, matching, other):
    assert parse_schedule(cron) == cron
    assert cron_match(cron, matching)
    assert not cron_match(cron, other)


def test_cron_day_rules_match_either_restricted_day_and_both_sunday_numbers():
    # When both day fields are restricted, either matching day is due.
    assert cron_match("0 8 15 * 1", datetime(2026, 9, 7, 8))
    assert cron_match("0 8 15 * 1", datetime(2026, 9, 15, 8))
    assert not cron_match("0 8 15 * 1", datetime(2026, 9, 8, 8))
    assert not cron_match("0 8 * * 1", datetime(2026, 9, 8, 8))
    assert not cron_match("0 8 15 * *", datetime(2026, 9, 8, 8))
    sunday = datetime(2026, 9, 6, 8)
    assert cron_match("0 8 * * 0", sunday)
    assert cron_match("0 8 * * 7", sunday)


@pytest.mark.parametrize(
    "cron",
    [
        "60 8 * * *",
        "0 24 * * *",
        "0 8 0 * *",
        "0 8 32 * *",
        "0 8 * 0 *",
        "0 8 * 13 *",
        "0 8 * * 8",
        "0 9,,11 * * *",
        "0 9, * * *",
        "0 ,9 * * *",
        "0 12-9 * * *",
        "0 9- * * *",
        "0 9--11 * * *",
        "0 *,9 * * *",
        "*/5 8 * * *",
        "0 8 * * MON",
        "@daily",
        "0 8 * *",
        "0 8 * * * *",
    ],
)
def test_invalid_cron_is_rejected_on_create_and_both_update_paths(tmp_path, cron):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    assert not cron_match(cron, datetime(2026, 9, 6, 8))
    with pytest.raises(RoutineError):
        add_routine(paths, "atlas", prompt="check", when=cron, enabled=True)
    assert list_routines(paths, "atlas") == []
    good = add_routine(paths, "atlas", prompt="check", when="8am", enabled=True)
    for fields in ({"cron": cron}, {"triggers": [{"type": "schedule", "cron": cron}]}):
        with pytest.raises(RoutineError):
            update_routine(paths, "atlas", good["id"], **fields)
        assert list_routines(paths, "atlas")[0]["cron"] == "0 8 * * *"
