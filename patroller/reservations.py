"""Small per-node SQLite calendar and Linux process guard; no job scheduling."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import re
import signal
import sqlite3
import uuid

UTC = timezone.utc
MAX_BODY = 16384
MAX_NOTE = 4096
EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                   r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                   r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z")
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
        if not isinstance(value, dict) or set(value) != fields:
            raise ReservationError(400, "Expected gpu_uuids, owner_email, start_at, end_at and note")
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
        return dict(gpu_uuids=gpus, owner_email=owner, start_at=utc_string(start),
                    end_at=utc_string(end), note=note), timestamp(start), timestamp(end)

    def _write(self, value, reservation_id=None):
        record, start, end = self._validate(value)
        with self.transaction() as db:
            if reservation_id is not None and not db.execute(
                    "SELECT 1 FROM reservations WHERE id = ?", (reservation_id,)).fetchone():
                raise ReservationError(404, "Reservation not found")
            rows = db.execute("SELECT record FROM reservations WHERE start < ? AND end > ? AND id != ?",
                              (end, start, reservation_id or "")).fetchall()
            if any(set(json.loads(row[0])["gpu_uuids"]) & set(record["gpu_uuids"]) for row in rows):
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

    def list(self):
        with self.transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT record FROM reservations ORDER BY start, id")]

    def active_next(self, gpu, now, db=None):
        if db is None:
            with self.transaction() as connection:
                return self.active_next(gpu, now, connection)
        current = timestamp(now)
        active, upcoming = None, None
        rows = db.execute("SELECT start, record FROM reservations WHERE end > ? ORDER BY start, id", (current,))
        for start, raw in rows:
            record = json.loads(raw)
            if gpu not in record["gpu_uuids"]:
                continue
            if start <= current:
                active = record
            elif upcoming is None:
                upcoming = record
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
