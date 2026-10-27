"""Local calendar tests: no monitor, Docker, network or GPU access."""
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest

from patroller.reservations import Calendar, ReservationError

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def record(gpus=None, **changes):
    value = dict(gpu_uuids=gpus or ["GPU-a"], owner_email=" Alice@Example.org ",
                 start_at="2030-01-01T00:00:00Z", end_at="2030-01-01T01:00:00Z", note="hello")
    value.update(changes)
    return value


def calendar(tmp_path):
    return Calendar(str(tmp_path / "calendar.sqlite"), ["GPU-a", "GPU-b"])


def test_crud_persistence_atomic_edits_and_boundaries(tmp_path):
    cal = calendar(tmp_path)
    first = cal.create(record(["GPU-a", "GPU-b"]))
    assert first["owner_email"] == "alice@example.org"
    assert cal.list() == [first]
    assert Calendar(cal.path, ["GPU-a", "GPU-b"]).list() == [first]
    second = cal.create(record(start_at=first["end_at"], end_at="2030-01-01T02:00:00Z"))
    with pytest.raises(ReservationError) as err:
        cal.update(first["id"], record(end_at="2030-01-01T01:30:00Z"))
    assert err.value.status == 409
    assert cal.list() == [first, second]
    assert cal.active_next("GPU-a", NOW) == (first, second)
    assert cal.active_next("GPU-a", NOW + timedelta(hours=1)) == (second, None)
    updated = cal.update(first["id"], record(["GPU-b"], note="edited"))
    assert updated["id"] == first["id"]
    cal.cancel(first["id"])
    assert cal.list() == [second]
    with pytest.raises(ReservationError) as err:
        cal.cancel(first["id"])
    assert err.value.status == 404


@pytest.mark.parametrize("changes", [
    {"gpu_uuids": []}, {"gpu_uuids": ["GPU-a", "GPU-a"]}, {"gpu_uuids": ["missing"]},
    {"gpu_uuids": "GPU-a"}, {"owner_email": "unknown"}, {"owner_email": None},
    {"start_at": "2030-01-01T00:00:00"}, {"start_at": "2030-01-01"},
    {"start_at": "not a date"}, {"end_at": "2030-01-01T00:00:00Z"},
    {"end_at": "2029-01-01T00:00:00Z"}, {"note": "x" * 4097}, {"note": None},
    {"id": "caller-id"}, {"start_at": 123}, {"gpu_uuids": [None]},
])
def test_validation(tmp_path, changes):
    with pytest.raises(ReservationError) as err:
        calendar(tmp_path).create(record(**changes))
    assert err.value.status == 400


def test_timezone_equivalence_and_microseconds(tmp_path):
    cal = calendar(tmp_path)
    first = cal.create(record(start_at="2030-01-01T01:00:00.000001+01:00"))
    assert first["start_at"] == "2030-01-01T00:00:00.000001Z"
    assert cal.active_next("GPU-a", NOW)[0] is None
    assert cal.active_next("GPU-a", NOW + timedelta(microseconds=1))[0] == first
    with pytest.raises(ReservationError) as err:
        cal.create(record(start_at="2029-12-31T19:00:00-05:00"))
    assert err.value.status == 409


def test_concurrent_multi_gpu_writes_are_atomic(tmp_path):
    cal = calendar(tmp_path)
    other = Calendar(cal.path, ["GPU-a", "GPU-b"])
    def create(store):
        try:
            return store.create(record(["GPU-a", "GPU-b"]))
        except ReservationError as exc:
            return exc.status
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(create, [cal, other]))
    assert sum(isinstance(x, dict) for x in results) == 1
    assert 409 in results
    assert len(cal.list()) == 1


@pytest.mark.parametrize("changes", [
    {"start_at": "2030-01-01T00:00:00+01:60"},
    {"owner_email": "alice,bob@example.org"}, {"owner_email": ".alice@example.org"},
    {"owner_email": "alice..bob@example.org"}, {"owner_email": "alice@-example.org"},
])
def test_rejects_malformed_offsets_and_emails(tmp_path, changes):
    with pytest.raises(ReservationError) as err:
        calendar(tmp_path).create(record(**changes))
    assert err.value.status == 400


def test_storage_error_is_service_unavailable(tmp_path):
    cal = calendar(tmp_path)
    cal.path = str(tmp_path / "missing" / "db")
    with pytest.raises(ReservationError) as err:
        cal.list()
    assert err.value.status == 503


def test_disjoint_gpus_overlap_and_edit_excludes_self(tmp_path):
    cal = calendar(tmp_path)
    first = cal.create(record())
    second = cal.create(record(["GPU-b"]))
    assert cal.update(first["id"], record(note="same window"))["note"] == "same window"
    with pytest.raises(ReservationError) as err:
        cal.update(first["id"], record(["GPU-a", "GPU-b"]))
    assert err.value.status == 409
    assert cal.list()[1]["gpu_uuids"] in (["GPU-a"], ["GPU-b"])
    assert len(cal.list()) == 2
