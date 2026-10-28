"""Bounded series contract tests; all calendars and PIDs are isolated fixtures."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from patroller.reservations import Calendar, ReservationError, parse_time, timestamp
from test_reservations import calendar, record


def repeated(frequency="daily", until="2030-01-03", zone="UTC", **rule):
    return dict(frequency=frequency, until=until, timezone=zone, **rule)


def starts(booking):
    return [window["start_at"] for window in booking["occurrences"]]


def assert_error(cal, payload, status=400):
    before = cal.list()
    with pytest.raises(ReservationError) as exc:
        cal.create(payload)
    assert exc.value.status == status
    assert cal.list() == before


@pytest.mark.parametrize("rule,expected", [
    (repeated(), ["2030-01-01", "2030-01-02", "2030-01-03"]),
    (repeated("weekly", "2030-01-14", weekdays=[4, 1]),
     ["2030-01-01", "2030-01-04", "2030-01-08", "2030-01-11"]),
    (repeated("biweekly", "2030-01-21", weekdays=[0, 1, 6]),
     ["2030-01-01", "2030-01-06", "2030-01-14", "2030-01-15", "2030-01-20"]),
    (repeated("monthly", "2030-04-01"), ["2030-01-01", "2030-02-01", "2030-03-01", "2030-04-01"]),
])
def test_all_frequencies_inclusive_dates_and_week_anchor(tmp_path, rule, expected):
    booking = calendar(tmp_path).create(record(recurrence=rule))
    assert starts(booking) == [day + "T00:00:00Z" for day in expected]
    assert booking["start_at"] == "2030-01-01T00:00:00Z"
    assert booking["end_at"] == "2030-01-01T01:00:00Z"
    assert booking["recurrence"] == dict(rule, **({"weekdays": sorted(rule["weekdays"])} if "weekdays" in rule else {}))


def test_monthly_skips_invalid_months_and_stops_on_until(tmp_path):
    booking = calendar(tmp_path).create(record(
        start_at="2032-01-31T10:00:00Z", end_at="2032-01-31T12:00:00Z",
        recurrence=repeated("monthly", "2032-07-30")))
    assert starts(booking) == ["2032-%02d-31T10:00:00Z" % month for month in (1, 3, 5)]


def test_until_uses_local_date_and_allows_maximum_367_windows(tmp_path):
    cal = calendar(tmp_path)
    booking = cal.create(record(start_at="2030-01-01T23:00:00Z", end_at="2030-01-01T23:15:00Z",
                                recurrence=repeated(until="2031-01-03", zone="Europe/Ljubljana")))
    assert len(booking["occurrences"]) == 367
    assert starts(booking)[-1] == "2031-01-02T23:00:00Z"
    assert_error(cal, record(recurrence=repeated(until="2031-01-03")))
    one = cal.create(record(["GPU-b"], recurrence=repeated(until="2030-01-01")))
    assert len(one["occurrences"]) == 1


@pytest.mark.parametrize("rule", [
    True, False, [], "daily", 1, {},
    repeated(frequency="yearly"), repeated(frequency=[]),
    repeated(until=None), repeated(until=True), repeated(until=20300103),
    repeated(until="2030-1-3"), repeated(until="2030-02-30"),
    repeated(until="2029-12-31"), repeated(until="2031-01-03"),
    repeated(until="2030-01-03T00:00:00Z"), repeated(zone=None),
    repeated(zone=""), repeated(zone="No/Such_Zone"), repeated(zone=[]),
    repeated(zone="/etc/passwd"), repeated(zone="../UTC"),
    repeated(extra="no"), repeated(weekdays=[1]),
    repeated("monthly", weekdays=[1]), repeated("weekly"),
    repeated("weekly", weekdays=[]), repeated("weekly", weekdays=[0]),
    repeated("weekly", weekdays=[1, 1]), repeated("weekly", weekdays=[True]),
    repeated("weekly", weekdays=[1, False]), repeated("weekly", weekdays=[1.0]),
    repeated("weekly", weekdays=[1, 7]), repeated("weekly", weekdays=[-1, 1]),
    repeated("weekly", weekdays="1"), repeated("biweekly", weekdays=None),
])
def test_rule_validation_is_atomic(tmp_path, rule):
    assert_error(calendar(tmp_path), record(recurrence=rule))


@pytest.mark.parametrize("changes", [{"until": "2030-01-03"}, {"timezone": "UTC"}, {"frequency": "daily"}])
def test_required_rule_fields(tmp_path, changes):
    assert_error(calendar(tmp_path), record(recurrence=changes))


@pytest.mark.parametrize("start,end,until,expected", [
    ("2030-03-30T09:00:00+01:00", "2030-03-30T11:00:00+01:00", "2030-04-01",
     ["2030-03-30T08:00:00Z", "2030-03-31T07:00:00Z", "2030-04-01T07:00:00Z"]),
    ("2030-10-26T09:00:00+02:00", "2030-10-26T11:00:00+02:00", "2030-10-28",
     ["2030-10-26T07:00:00Z", "2030-10-27T08:00:00Z", "2030-10-28T08:00:00Z"]),
    ("2030-10-26T01:30:00+02:00", "2030-10-26T03:30:00+02:00", "2030-10-27",
     ["2030-10-25T23:30:00Z", "2030-10-26T23:30:00Z"]),
])
def test_dst_preserves_local_start_and_elapsed_duration(tmp_path, start, end, until, expected):
    booking = calendar(tmp_path).create(record(start_at=start, end_at=end,
        recurrence=repeated(until=until, zone="Europe/Ljubljana")))
    assert starts(booking) == expected
    assert all(parse_time(w["end_at"]) - parse_time(w["start_at"]) == timedelta(hours=2)
               for w in booking["occurrences"])


@pytest.mark.parametrize("start,end,until", [
    ("2030-03-30T02:30:00+01:00", "2030-03-30T03:30:00+01:00", "2030-03-31"),
    ("2030-10-26T02:30:00+02:00", "2030-10-26T03:30:00+02:00", "2030-10-27"),
    ("2030-10-27T02:30:00+02:00", "2030-10-27T03:30:00+02:00", "2030-10-27"),
    ("2030-10-27T02:30:00+01:00", "2030-10-27T03:30:00+01:00", "2030-10-27"),
])
def test_invalid_or_ambiguous_generated_and_initial_starts_rejected(tmp_path, start, end, until):
    assert_error(calendar(tmp_path), record(start_at=start, end_at=end,
        recurrence=repeated(until=until, zone="Europe/Ljubljana")))


def test_duration_can_cross_spring_transition_and_end_on_repeated_hour(tmp_path):
    cal = calendar(tmp_path)
    spring = cal.create(record(start_at="2030-03-30T01:30:00+01:00", end_at="2030-03-30T03:30:00+01:00",
                               recurrence=repeated(until="2030-03-31", zone="Europe/Ljubljana")))
    assert spring["occurrences"][-1] == dict(start_at="2030-03-31T00:30:00Z", end_at="2030-03-31T02:30:00Z")
    fall = cal.create(record(start_at="2030-10-26T01:30:00+02:00", end_at="2030-10-26T03:30:00+02:00",
                             recurrence=repeated(until="2030-10-27", zone="Europe/Ljubljana")))
    assert fall["occurrences"][-1] == dict(start_at="2030-10-26T23:30:00Z", end_at="2030-10-27T01:30:00Z")


@pytest.mark.parametrize("end,rule,start", [
    ("2030-01-02T00:00:01Z", repeated(), "2030-01-01T00:00:00Z"),
    ("2030-01-05T00:00:01Z", repeated("weekly", "2030-01-08", weekdays=[1, 5]), "2030-01-01T00:00:00Z"),
    # Nominal 24 hours overlaps the 23-hour spring-forward interval.
    ("2030-03-31T08:00:00Z", repeated(until="2030-03-31", zone="Europe/Ljubljana"), "2030-03-30T08:00:00Z"),
])
def test_self_overlap_is_bad_request(tmp_path, end, rule, start):
    assert_error(calendar(tmp_path), record(start_at=start, end_at=end, recurrence=rule))


def test_touching_occurrences_and_microseconds(tmp_path):
    cal = calendar(tmp_path)
    booking = cal.create(record(start_at="2030-01-01T00:00:00.000001Z", end_at="2030-01-02T00:00:00.000001Z",
                                recurrence=repeated()))
    assert booking["occurrences"][0]["end_at"] == starts(booking)[1]
    active, upcoming = cal.active_next("GPU-a", parse_time(starts(booking)[1]))
    assert active["start_at"] == starts(booking)[1]
    assert upcoming["start_at"] == starts(booking)[2]


def test_actual_gaps_and_later_conflicts_both_directions(tmp_path):
    cal = calendar(tmp_path)
    series = cal.create(record(recurrence=repeated("weekly", "2030-01-15", weekdays=[1])))
    gap = cal.create(record(start_at="2030-01-02T00:00:00Z", end_at="2030-01-08T00:00:00Z"))
    assert_error(cal, record(start_at="2030-01-15T00:30:00Z", end_at="2030-01-15T02:00:00Z"), 409)
    # New series begins clear but collides on its second occurrence.
    assert_error(cal, record(["GPU-a", "GPU-b"], start_at="2030-01-14T00:30:00Z", end_at="2030-01-14T01:30:00Z",
                             recurrence=repeated(until="2030-01-15")), 409)
    assert cal.list() == [series, gap]
    cal.cancel(series["id"])
    assert_error(cal, record(start_at="2030-01-01T00:00:00Z", end_at="2030-01-01T01:00:00Z",
                             recurrence=repeated()), 409)


def test_series_vs_series_conflicts_and_atomic_whole_edit(tmp_path):
    cal = calendar(tmp_path)
    first = cal.create(record(["GPU-a", "GPU-b"], recurrence=repeated()))
    second_payload = record(start_at="2030-01-01T02:00:00Z", end_at="2030-01-01T03:00:00Z", recurrence=repeated())
    second = cal.create(second_payload)
    bad = record(start_at="2030-01-03T00:30:00Z", end_at="2030-01-03T01:30:00Z", recurrence=repeated(until="2030-01-04"))
    with pytest.raises(ReservationError) as exc:
        cal.update(second["id"], bad)
    assert exc.value.status == 409
    assert cal.list() == [first, second]
    changed = cal.update(first["id"], record(["GPU-b"], note="all occurrences", recurrence=repeated(until="2030-01-04")))
    assert changed["id"] == first["id"] and len(changed["occurrences"]) == 4
    assert cal.active_next("GPU-a", parse_time("2030-01-03T00:30:00Z"))[0] is None
    assert cal.active_next("GPU-b", parse_time("2030-01-04T00:30:00Z"))[0]["note"] == "all occurrences"


def test_active_next_chooses_actual_later_window_across_envelope_order(tmp_path):
    cal = calendar(tmp_path)
    series = cal.create(record(recurrence=repeated("weekly", "2030-01-15", weekdays=[1])))
    gap = cal.create(record(start_at="2030-01-02T00:00:00Z", end_at="2030-01-02T01:00:00Z"))
    assert cal.active_next("GPU-a", parse_time("2030-01-01T01:00:00Z")) == (None, gap)
    active, upcoming = cal.active_next("GPU-a", parse_time("2030-01-08T00:30:00Z"))
    expected = {key: value for key, value in series.items() if key != "occurrences"}
    assert active == dict(expected, **series["occurrences"][1])
    assert upcoming == dict(expected, **series["occurrences"][2])
    assert cal.active_next("GPU-a", parse_time("2030-01-15T01:00:00Z")) == (None, None)
    assert cal.active_next("GPU-b", parse_time("2030-01-08T00:30:00Z")) == (None, None)


def test_existing_db_shape_persistence_envelope_and_whole_cancellation(tmp_path):
    path = tmp_path / "old.sqlite"
    old = dict(record(), id="old-oneoff", owner_email="alice@example.org")
    raw = json.dumps(old, separators=(",", ":"))
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE reservations (id TEXT PRIMARY KEY, start INTEGER NOT NULL, end INTEGER NOT NULL, record TEXT NOT NULL)")
        db.execute("INSERT INTO reservations VALUES (?, ?, ?, ?)", (old["id"], timestamp(parse_time(old["start_at"])), timestamp(parse_time(old["end_at"])), raw))
    cal = Calendar(path, ["GPU-a", "GPU-b"])
    assert cal.list() == [old]
    assert cal.active_next("GPU-a", parse_time(old["start_at"])) == (old, None)
    series = cal.create(record(["GPU-b"], recurrence=repeated()))
    restarted = Calendar(path, ["GPU-a", "GPU-b"])
    assert sorted(restarted.list(), key=lambda r: r["id"]) == sorted([old, series], key=lambda r: r["id"])
    with sqlite3.connect(str(path)) as db:
        assert db.execute("SELECT record FROM reservations WHERE id = ?", (old["id"],)).fetchone()[0] == raw
        assert db.execute("SELECT start, end FROM reservations WHERE id = ?", (series["id"],)).fetchone() == (
            timestamp(parse_time(series["occurrences"][0]["start_at"])), timestamp(parse_time(series["occurrences"][-1]["end_at"])))
    restarted.cancel(series["id"])
    assert restarted.active_next("GPU-b", parse_time("2030-01-02T00:30:00Z")) == (None, None)
    assert restarted.list() == [old]


def test_null_rule_old_shape_readonly_windows_and_series_to_oneoff(tmp_path):
    cal = calendar(tmp_path)
    oneoff = cal.create(record(recurrence=None))
    assert set(oneoff) == set(record()) | {"id"}
    assert_error(cal, record(occurrences=[]))
    series = cal.update(oneoff["id"], record(recurrence=repeated()))
    assert_error(cal, record(recurrence=repeated(), occurrences=series["occurrences"]))
    restored = cal.update(series["id"], record(recurrence=None))
    assert restored == oneoff
    assert cal.active_next("GPU-a", parse_time("2030-01-02T00:30:00Z")) == (None, None)


def test_concurrent_series_multi_gpu_writes_are_atomic(tmp_path):
    cal = calendar(tmp_path)
    other = Calendar(cal.path, ["GPU-a", "GPU-b"])
    def create(store):
        try:
            return store.create(record(["GPU-a", "GPU-b"], recurrence=repeated()))
        except ReservationError as exc:
            return exc.status
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(create, [cal, other]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 409 in results
    assert len(cal.list()) == 1


# Corruption is injected only into isolated SQLite fixtures, never through the API.
CORRUPTIONS = [
    "empty", "missing", "malformed-json", "json-list", "json-null", "windows-type",
    "window-type", "window-extra", "window-missing", "truncated", "moved", "reverse",
    "first", "last", "template-start", "template-end", "invalid-rule", "null-rule",
    "extra", "missing-note", "bad-owner", "bad-gpus", "bad-note", "bad-start",
    "bad-end", "empty-id", "numeric-id", "wrong-id", "sql-start", "sql-end",
    "sql-start-type", "sql-end-type", "sql-excludes-conflict", "sql-excludes-active",
]


def corrupt_row(cal, booking, corruption):
    value = json.loads(json.dumps(booking))
    sql_start = timestamp(parse_time(booking["start_at"]))
    sql_end = timestamp(parse_time(booking["occurrences"][-1]["end_at"]))
    if corruption == "empty":
        value["occurrences"] = []
    elif corruption == "missing":
        del value["occurrences"]
    elif corruption == "windows-type":
        value["occurrences"] = {}
    elif corruption == "window-type":
        value["occurrences"][1] = None
    elif corruption == "window-extra":
        value["occurrences"][1]["extra"] = True
    elif corruption == "window-missing":
        del value["occurrences"][1]["end_at"]
    elif corruption == "truncated":
        value["occurrences"] = value["occurrences"][:1]
    elif corruption == "moved":
        value["occurrences"][1] = dict(start_at="2030-01-02T02:00:00Z", end_at="2030-01-02T03:00:00Z")
    elif corruption == "reverse":
        value["occurrences"].reverse()
    elif corruption in ("first", "last"):
        value["occurrences"][0 if corruption == "first" else -1]["end_at"] = "2030-01-01T00:00:00Z"
    elif corruption == "template-start":
        value["start_at"] = "2030-01-01T00:01:00Z"
    elif corruption == "template-end":
        value["end_at"] = "2030-01-01T02:00:00Z"
    elif corruption == "invalid-rule":
        value["recurrence"]["until"] = "2030-01-04"
    elif corruption == "null-rule":
        value["recurrence"] = None
    elif corruption == "extra":
        value["extra"] = True
    elif corruption == "missing-note":
        del value["note"]
    elif corruption.startswith("bad-"):
        key, replacement = {
            "bad-owner": ("owner_email", []), "bad-gpus": ("gpu_uuids", [None]),
            "bad-note": ("note", 4), "bad-start": ("start_at", []),
            "bad-end": ("end_at", "2030-01-01T00:00:00Z"),
        }[corruption]
        value[key] = replacement
    elif corruption in ("empty-id", "numeric-id", "wrong-id"):
        value["id"] = {"empty-id": "", "numeric-id": 7, "wrong-id": "other-old-id"}[corruption]
    elif corruption in ("sql-start", "sql-end"):
        if corruption == "sql-start":
            sql_start += 1
        else:
            sql_end -= 1
    elif corruption == "sql-start-type":
        sql_start = "invalid"
    elif corruption == "sql-end-type":
        sql_end = "invalid"
    elif corruption == "sql-excludes-conflict":
        sql_start = timestamp(parse_time("2031-01-01T00:00:00Z"))
        sql_end = sql_start + 1
    elif corruption == "sql-excludes-active":
        sql_end = timestamp(parse_time("2029-01-01T00:00:00Z"))
        sql_start = sql_end - 1
    raw = {"malformed-json": "{", "json-list": "[]", "json-null": "null"}.get(corruption, json.dumps(value))
    with sqlite3.connect(cal.path) as db:
        db.execute("UPDATE reservations SET start=?, end=?, record=? WHERE id=?",
                   (sql_start, sql_end, raw, booking["id"]))
    return raw


@pytest.mark.parametrize("operation", ["list", "conflict", "active_next"])
@pytest.mark.parametrize("corruption", CORRUPTIONS)
def test_corrupt_stored_series_fails_closed(tmp_path, operation, corruption):
    cal = calendar(tmp_path)
    booking = cal.create(record(recurrence=repeated()))
    raw = corrupt_row(cal, booking, corruption)
    with pytest.raises(ReservationError) as exc:
        if operation == "list":
            cal.list()
        elif operation == "conflict":
            cal.create(record(start_at="2030-01-02T00:30:00Z", end_at="2030-01-02T01:30:00Z"))
        else:
            cal.active_next("GPU-a", parse_time("2030-01-02T00:30:00Z"))
    assert exc.value.status == 503
    assert str(exc.value) == "Calendar storage unavailable"
    with sqlite3.connect(cal.path) as db:
        assert db.execute("SELECT record FROM reservations").fetchall() == [(raw,)]


@pytest.mark.parametrize("zone", ["x" * 256, "A/" + "x" * 256, "UTC/../UTC", "UTC//UTC", "UTC/./UTC", "UTC\x00"])
def test_invalid_timezone_paths_are_bad_requests(tmp_path, zone):
    assert_error(calendar(tmp_path), record(recurrence=repeated(zone=zone)))


@pytest.mark.parametrize("operation", ["list", "conflict", "active_next"])
def test_stored_series_materialized_once_per_operation(tmp_path, monkeypatch, operation):
    import patroller.reservations as reservations
    cal = calendar(tmp_path)
    cal.create(record(recurrence=repeated(until="2031-01-02")))
    original = reservations._recurrence
    calls = []
    def counted(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(reservations, "_recurrence", counted)
    if operation == "list":
        cal.list()
    elif operation == "conflict":
        cal.create(record(["GPU-b"]))
    else:
        cal.active_next("GPU-a", parse_time("2030-07-01T00:30:00Z"))
    assert len(calls) == 1


@pytest.mark.parametrize("corruption", ["extra", "missing-note", "bad-gpus", "empty-id", "wrong-id", "orphan-windows", "duplicate-key"])
@pytest.mark.parametrize("operation", ["list", "conflict", "active_next"])
def test_corrupt_legacy_oneoff_fails_closed(tmp_path, corruption, operation):
    cal = calendar(tmp_path)
    booking = cal.create(record())
    value = dict(booking)
    if corruption == "extra":
        value["unknown"] = True
    elif corruption == "missing-note":
        del value["note"]
    elif corruption == "bad-gpus":
        value["gpu_uuids"] = "GPU-a"
    elif corruption == "empty-id":
        value["id"] = ""
    elif corruption == "wrong-id":
        value["id"] = "different-legacy-id"
    elif corruption == "orphan-windows":
        value["occurrences"] = []
    raw = json.dumps(value)
    if corruption == "duplicate-key":
        raw = raw[:-1] + ', "note": "duplicate"}'
    with sqlite3.connect(cal.path) as db:
        db.execute("UPDATE reservations SET record=?", (raw,))
    with pytest.raises(ReservationError) as exc:
        if operation == "list":
            cal.list()
        elif operation == "conflict":
            cal.create(record())
        else:
            cal.active_next("GPU-a", parse_time(booking["start_at"]))
    assert exc.value.status == 503


def test_timezone_paths_rejected_before_lookup_and_lookup_oserror_is_400(tmp_path, monkeypatch):
    import errno
    import patroller.reservations as reservations
    calls = []
    def unavailable(name):
        calls.append(name)
        raise OSError(errno.ENAMETOOLONG, "File name too long")
    monkeypatch.setattr(reservations, "ZoneInfo", unavailable)
    cal = calendar(tmp_path)
    for zone in ("x" * 256, "/UTC", "UTC/../UTC", "UTC//UTC", "UTC/./UTC", "UTC\x00"):
        assert_error(cal, record(recurrence=repeated(zone=zone)))
    assert calls == []
    assert_error(cal, record(recurrence=repeated(zone="UTC")))
    assert calls == ["UTC"]
