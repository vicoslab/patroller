"""Small per-node SQLite calendar and Linux process guard; no job scheduling."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
import signal
import sqlite3
import uuid

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8; installed by the conditional dependency.
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
MAX_BODY = 16384
MAX_NOTE = 4096
EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                   r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                   r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
MAX_RECURRENCE_DAYS = 366
MAX_OCCURRENCES = 367
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ReservationError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def email(value):
    """Normalize trusted-label identity exactly as calendar owner identity."""
    if not isinstance(value, str):
        return None
    value = value.strip().casefold()
    if len(value) > 254 or not EMAIL.fullmatch(value):
        return None
    local = value.split("@", 1)[0]
    return value if len(local) <= 64 and not local.startswith(".") and not local.endswith(".") and ".." not in local else None


def parse_time(value):
    if not isinstance(value, str) or not ISO_TIME.fullmatch(value):
        raise ReservationError(400, "Timestamps require ISO8601 date/time and an explicit offset")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, OverflowError):
        raise ReservationError(400, "Invalid timestamp") from None


def timestamp(value):
    delta = value.astimezone(UTC) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def utc_string(value):
    return value.isoformat().replace("+00:00", "Z")


def _local_start(local, zone):
    """Resolve a wall clock only if it identifies exactly one real instant."""
    candidates = set()
    for fold in (0, 1):
        instant = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == local:
            candidates.add(instant)
    if len(candidates) != 1:
        raise ReservationError(400, "Recurrence start is nonexistent or ambiguous in its timezone")
    return candidates.pop()


def _recurrence(rule, start, end):
    """Validate and materialize a bounded series before entering the write path."""
    fields = {"frequency", "until", "timezone"}
    if not isinstance(rule, dict):
        raise ReservationError(400, "recurrence must be an object or null")
    frequency = rule.get("frequency")
    if not isinstance(frequency, str) or frequency not in {"daily", "weekly", "biweekly", "monthly"}:
        raise ReservationError(400, "Invalid recurrence frequency")
    weekly = frequency in {"weekly", "biweekly"}
    if set(rule) != fields | ({"weekdays"} if weekly else set()):
        raise ReservationError(400, "Expected frequency, until, timezone and weekdays only for weekly/biweekly")
    zone_name = rule["timezone"]
    if (not isinstance(zone_name, str) or not 1 <= len(zone_name) <= 255 or
            any(not re.fullmatch(r"[A-Za-z0-9_+-]+", part) for part in zone_name.split("/"))):
        raise ReservationError(400, "Recurrence timezone must be an IANA name")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ReservationError(400, "Invalid recurrence timezone") from None
    until = rule["until"]
    if not isinstance(until, str) or not ISO_DATE.fullmatch(until):
        raise ReservationError(400, "Recurrence until requires YYYY-MM-DD")
    try:
        until_date = date.fromisoformat(until)
        local = start.astimezone(zone).replace(tzinfo=None)
        day_count = (until_date - local.date()).days
        if not 0 <= day_count <= MAX_RECURRENCE_DAYS:
            raise ReservationError(400, "Recurrence until must be from the initial local date through 366 days later")
        normalized = dict(frequency=frequency, until=until, timezone=zone_name)
        if weekly:
            weekdays = rule["weekdays"]
            if (not isinstance(weekdays, list) or not weekdays or
                    any(type(day) is not int or not 0 <= day <= 6 for day in weekdays) or
                    len(set(weekdays)) != len(weekdays) or local.weekday() not in weekdays):
                raise ReservationError(400, "weekdays must be unique integers 0..6 including the initial weekday")
            normalized["weekdays"] = sorted(weekdays)
        duration = end - start
        windows = []
        previous_end = None
        # At most 367 dates; biweekly weeks are anchored to the initial Monday.
        for offset in range(day_count + 1):
            day = local.date() + timedelta(days=offset)
            if weekly and day.weekday() not in weekdays:
                continue
            if frequency == "biweekly" and ((offset + local.weekday()) // 7) % 2:
                continue
            if frequency == "monthly" and day.day != local.day:
                continue
            window_start = _local_start(datetime.combine(day, local.time()), zone)
            window_end = window_start + duration  # Elapsed duration, always in UTC.
            if previous_end is not None and window_start < previous_end:
                raise ReservationError(400, "Recurring reservation overlaps itself")
            windows.append(dict(start_at=utc_string(window_start), end_at=utc_string(window_end)))
            previous_end = window_end
        if len(windows) > MAX_OCCURRENCES:
            raise ReservationError(400, "Too many recurrence occurrences")
        return normalized, windows
    except (ValueError, OverflowError):
        raise ReservationError(400, "Invalid recurrence date or timestamp range") from None


def _windows(record):
    """One-offs remain unchanged in storage and participate as a single window."""
    return record["occurrences"] if "occurrences" in record else (record,)


def _stored_object(pairs):
    """Duplicate JSON keys are ambiguous stored data, not a last-key-wins record."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate stored key")
        result[key] = value
    return result


def _intervals(record):
    return [(timestamp(parse_time(window["start_at"])), timestamp(parse_time(window["end_at"])))
            for window in _windows(record)]


def _overlap(left, right):
    """Linear comparison of two sorted, internally disjoint interval lists."""
    i, j = 0, 0
    while i < len(left) and j < len(right):
        a_start, a_end = left[i]
        b_start, b_end = right[j]
        if a_start < b_end and b_start < a_end:
            return True
        if a_end <= b_end:
            i += 1
        else:
            j += 1
    return False


class Calendar:
    def __init__(self, path, gpu_uuids):
        if not path or path == ":memory:":
            raise ValueError("Calendar requires a local SQLite file path")
        self.path = os.fspath(path)
        self.gpu_uuids = frozenset(gpu_uuids)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS reservations "
                       "(id TEXT PRIMARY KEY, start INTEGER NOT NULL, end INTEGER NOT NULL, record TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS reservation_window ON reservations(start, end)")

    @contextmanager
    def transaction(self):
        # Independent connections plus BEGIN IMMEDIATE serialize validation and edits
        # across threads/processes. Enforcement holds this same lock through signaling.
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db is not None:
                db.rollback()
            raise ReservationError(503, "Calendar storage unavailable") from None
        finally:
            if db is not None:
                db.close()

    def _validate(self, value):
        fields = {"gpu_uuids", "owner_email", "start_at", "end_at", "note"}
        if not isinstance(value, dict) or not fields <= set(value) or set(value) - fields - {"recurrence", "owner_name"}:
            raise ReservationError(400, "Expected gpu_uuids, owner_email, start_at, end_at, note and optional recurrence or owner_name")
        gpus = value["gpu_uuids"]
        if (not isinstance(gpus, list) or not gpus or
                any(not isinstance(gpu, str) or gpu not in self.gpu_uuids for gpu in gpus) or
                len(set(gpus)) != len(gpus)):
            raise ReservationError(400, "GPU UUIDs must be a nonempty list of unique known GPUs")
        owner = email(value["owner_email"])
        if owner is None:
            raise ReservationError(400, "Invalid owner email")
        note = value["note"]
        if not isinstance(note, str) or len(note) > MAX_NOTE:
            raise ReservationError(400, "Note must be a string of at most 4096 characters")
        start, end = parse_time(value["start_at"]), parse_time(value["end_at"])
        if end <= start:
            raise ReservationError(400, "end_at must be after start_at")
        record = dict(gpu_uuids=gpus, owner_email=owner, start_at=utc_string(start),
                      end_at=utc_string(end), note=note)
        if "owner_name" in value:
            name = value["owner_name"]
            if (not isinstance(name, str) or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                raise ReservationError(400, "owner_name must be a string without ASCII control characters")
            name = name.strip()
            if len(name) > 200:
                raise ReservationError(400, "owner_name must be at most 200 characters")
            if name:
                record["owner_name"] = name
        if value.get("recurrence") is not None:
            rule, windows = _recurrence(value["recurrence"], start, end)
            record.update(recurrence=rule, occurrences=windows)
            end = parse_time(windows[-1]["end_at"])
        return record, timestamp(start), timestamp(end)

    def _write(self, value, reservation_id=None):
        record, start, end = self._validate(value)
        intervals = _intervals(record)
        gpus = set(record["gpu_uuids"])
        with self.transaction() as db:
            if reservation_id is not None and not db.execute(
                    "SELECT 1 FROM reservations WHERE id = ?", (reservation_id,)).fetchone():
                raise ReservationError(404, "Reservation not found")
            for row in db.execute("SELECT id, start, end, record FROM reservations"):
                other, other_intervals = self._load(row)
                if other["id"] == reservation_id:
                    continue
                if (gpus.intersection(other["gpu_uuids"]) and
                        _overlap(intervals, other_intervals)):
                    raise ReservationError(409, "Reservation overlaps an existing booking")
            record["id"] = reservation_id or str(uuid.uuid4())
            db.execute("INSERT OR REPLACE INTO reservations VALUES (?, ?, ?, ?)",
                       (record["id"], start, end, json.dumps(record)))
        return record

    def create(self, value):
        return self._write(value)

    def update(self, reservation_id, value):
        return self._write(value, reservation_id)

    def cancel(self, reservation_id):
        with self.transaction() as db:
            if db.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,)).rowcount == 0:
                raise ReservationError(404, "Reservation not found")

    def _load(self, row):
        """Validate once per row/operation, without changing legacy JSON bytes.

        Rebuilding a series once (at most 367 dates) detects missing, moved and
        truncated windows as well as malformed schemas. SQL envelopes are checked
        too; callers scan rows before filtering so a bad index value cannot hide
        protection. This deliberately favors fail-closed behavior at lab scale.
        """
        try:
            sql_id, sql_start, sql_end, raw = row
            record = json.loads(raw, object_pairs_hook=_stored_object)
            fields = {"id", "gpu_uuids", "owner_email", "start_at", "end_at", "note"}
            if not isinstance(record, dict):
                raise ValueError("Invalid stored record")
            series = "recurrence" in record
            if set(record) != fields | ({"recurrence", "occurrences"} if series else set()) | ({"owner_name"} if "owner_name" in record else set()):
                raise ValueError("Invalid stored fields")
            if (not isinstance(record["id"], str) or not record["id"].strip() or
                    record["id"] != sql_id):
                raise ValueError("Invalid stored ID")
            if series and (not isinstance(record["recurrence"], dict) or
                           not isinstance(record["occurrences"], list) or
                           not 1 <= len(record["occurrences"]) <= MAX_OCCURRENCES):
                raise ValueError("Invalid stored series")
            payload = {key: value for key, value in record.items() if key not in {"id", "occurrences"}}
            validated, start, end = self._validate(payload)
            if "owner_name" in record and record["owner_name"] != validated.get("owner_name"):
                raise ValueError("Invalid stored owner name")
            if series and (record["recurrence"] != validated["recurrence"] or
                           record["occurrences"] != validated["occurrences"]):
                raise ValueError("Inconsistent stored series")
            if (type(sql_start) is not int or type(sql_end) is not int or
                    (sql_start, sql_end) != (start, end)):
                raise ValueError("Inconsistent stored envelope")
            return record, _intervals(record)
        except (ReservationError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ReservationError(503, "Calendar storage unavailable") from None

    def list(self):
        with self.transaction() as db:
            return [self._load(row)[0] for row in db.execute(
                "SELECT id, start, end, record FROM reservations ORDER BY start, id")]

    def active_next(self, gpu, now, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.active_next(gpu, now, connection)
        current = timestamp(now)
        active, upcoming = None, None
        next_start = None
        rows = db.execute("SELECT id, start, end, record FROM reservations ORDER BY start, id")
        for row in rows:
            record, intervals = self._load(row)
            if gpu not in record["gpu_uuids"]:
                continue
            for window, (start, end) in zip(_windows(record), intervals):
                if end <= current:
                    continue
                # Status/enforcement need only the actual occurrence, never the array.
                occurrence = {key: value for key, value in record.items() if key != "occurrences"}
                occurrence.update(start_at=window["start_at"], end_at=window["end_at"])
                if start <= current:
                    active = occurrence
                else:
                    if next_start is None or start < next_start:
                        upcoming, next_start = occurrence, start
                    break
        return active, upcoming


class ProcessGuard:
    """Only used on observed PIDs in explicit kill mode, never by synthetic run()."""
    def inspect(self, pid):
        if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
            return None
        try:
            with open("/proc/%d/stat" % pid) as fp:
                # comm can contain spaces and parentheses; fields after final ')' start at field 3.
                fields = fp.read().rsplit(")", 1)[1].split()
            with open("/proc/%d/status" % pid) as fp:
                uids = next(line.split()[1:] for line in fp if line.startswith("Uid:"))
            return int(fields[19]), tuple(map(int, uids))
        except (OSError, ValueError, IndexError, StopIteration):
            return None

    def open(self, pid):
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                return os.pidfd_open(pid, 0)
            except OSError as exc:
                # Only lack of kernel support permits the checked PID fallback.
                import errno
                if exc.errno not in (errno.ENOSYS, errno.EINVAL):
                    raise
        return None

    def close(self, handle):
        if handle is not None:
            os.close(handle)

    def kill(self, pid, handle):
        if handle is not None:
            signal.pidfd_send_signal(handle, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
