"""All PIDs are fictional except the one child explicitly spawned in smoke test."""
import os
import signal
import subprocess
import sys
from datetime import timedelta

import pytest

from patroller import Node
from patroller.base import TestDeviceMonitor as SyntheticMonitor
from patroller.reservations import Calendar, ProcessGuard
from test_reservation_http import Monitor
from test_reservations import NOW, record


class FixtureGuard:
    def __init__(self):
        self.snapshots = [(123, 1000)] * 20
        self.sent = []
        self.opened = []
        self.closed = []

    def inspect(self, pid):
        return self.snapshots.pop(0)

    def open(self, pid):
        self.opened.append(pid)
        return 42

    def close(self, handle):
        self.closed.append(handle)

    def kill(self, pid, handle):
        self.sent.append((pid, handle))


class Resolver:
    def __init__(self):
        self.user = {"email": "bob@example.org"}
        self.hook = None

    def __call__(self, pid):
        if self.hook:
            self.hook()
        return self.user


def fixture(tmp_path, mode="kill", monitor=None, guard=None):
    cal = Calendar(str(tmp_path / "cal.sqlite"), ["GPU-a", "GPU-b"])
    booking = cal.create(record())
    resolver = Resolver()
    guard = guard or FixtureGuard()
    now = [NOW]
    node = Node(monitor or Monitor(), resolver=resolver, calendar=cal, enforcement=mode,
                clock=lambda: now[0], process_guard=guard)
    return node, cal, booking, resolver, guard, now


def test_every_observation_enforces_even_existing_claim_and_owner_exempt(tmp_path):
    node, cal, booking, resolver, guard, now = fixture(tmp_path)
    node._handle_access("GPU-a", 101)
    node._handle_access("GPU-a", 101)
    assert guard.sent == [(101, 42), (101, 42)]
    resolver.user = {"email": " ALICE@example.org "}
    node._handle_access("GPU-a", 102)
    assert len(guard.sent) == 2
    assert guard.closed == [42, 42]
    cal.cancel(booking["id"])
    node._handle_access("GPU-a", 101)
    assert len(guard.sent) == 2


@pytest.mark.parametrize("mode", ["off", "dry-run"])
def test_safe_modes_never_signal(tmp_path, mode):
    node, _, _, _, guard, _ = fixture(tmp_path, mode)
    node._handle_access("GPU-a", 101)
    assert not guard.sent


@pytest.mark.parametrize("user", [None, "unknown", {}, {"email": "N/A"}])
def test_unknown_owner_skipped(tmp_path, user):
    node, _, _, resolver, guard, _ = fixture(tmp_path)
    resolver.user = user
    node._handle_access("GPU-a", 101)
    assert not guard.sent


@pytest.mark.parametrize("snapshots", [[None], [(123, 0), (123, 0)], [(123, 1000), (124, 1000)],
                                       [(123, 1000), (123, 1001)], [(123, 1000), None]])
def test_unresolved_system_stale_or_reused_process_skipped(tmp_path, snapshots):
    node, _, _, _, guard, _ = fixture(tmp_path)
    guard.snapshots = snapshots
    node._handle_access("GPU-a", 101)
    assert not guard.sent


def test_final_owner_and_calendar_revalidation(tmp_path):
    node, cal, booking, resolver, guard, now = fixture(tmp_path)
    calls = [0]
    def expire():
        calls[0] += 1
        if calls[0] >= 2:
            now[0] = NOW + timedelta(hours=1)
    resolver.hook = expire
    node._handle_access("GPU-a", 101)
    assert not guard.sent
    now[0] = NOW
    def owner_change():
        resolver.user = {"email": "alice@example.org"}
    resolver.hook = owner_change
    node._handle_access("GPU-a", 102)
    assert not guard.sent


def test_cancel_between_observation_and_enforcement(tmp_path):
    node, cal, booking, resolver, guard, _ = fixture(tmp_path)
    resolver.hook = lambda: (setattr(resolver, "hook", None), cal.cancel(booking["id"]))
    node._handle_access("GPU-a", 101)
    assert not guard.sent


def test_calendar_error_never_signals_or_allocates(tmp_path, monkeypatch):
    node, cal, _, _, guard, _ = fixture(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("broken calendar")
    monkeypatch.setattr(cal, "active_next", fail)
    node._handle_access("GPU-a", 101)
    assert not guard.sent
    with pytest.raises(Exception):
        node.reserve({"email": "carol@example.org"}, {"gpu": 1})
    assert node.reservation_info("GPU-a")["reservation_state"] == "unavailable"


def test_synthetic_mode_never_uses_default_process_guard(tmp_path, monkeypatch):
    cal = Calendar(str(tmp_path / "cal.sqlite"), ["GPU-a"])
    monkeypatch.setattr(ProcessGuard, "inspect", lambda *a: pytest.fail("real process inspection"))
    node = Node(SyntheticMonitor(1), calendar=cal, enforcement="kill")
    node._handle_access(next(iter(node)).uuid, 101)
    assert node.enforcement == "off"


def test_inactive_default_and_legacy_wait_filtering(tmp_path):
    node, cal, booking, _, guard, now = fixture(tmp_path)
    pending = node.wait({"email": "bob@example.org"}, {"gpu": 2})
    assert not pending.done()
    cal.cancel(booking["id"])
    node._process_pending()
    assert pending.done()
    assert len(pending.result()) == 2
    node._handle_access("GPU-a", 101)
    assert not guard.sent
    with pytest.raises(ValueError):
        Node(Monitor(), enforcement="typo")


def test_process_status_removes_stale_observations(tmp_path):
    node, _, _, _, _, now = fixture(tmp_path, "off")
    node._handle_access("GPU-a", 101)
    now[0] += timedelta(seconds=30)
    assert node.status("GPU-a")["processes"] == []


def test_real_kill_smoke_only_owned_child(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        class OwnedGuard(ProcessGuard):
            def inspect(self, pid):
                assert pid == child.pid
                # Safe injected identity: ONLY this tracked, unreaped child. The
                # sandbox's /proc does not expose subprocess namespace PIDs.
                return (id(child), 1000) if child.poll() is None else None
            def open(self, pid):
                assert pid == child.pid
                return super().open(pid)
            def kill(self, pid, handle):
                assert pid == child.pid and child.poll() is None
                return super().kill(pid, handle)
        node, _, _, _, _, _ = fixture(tmp_path, guard=OwnedGuard())
        node._handle_access("GPU-a", child.pid)
        assert child.wait(timeout=3) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=3)


def test_workload_running_before_window_is_enforced_at_start(tmp_path):
    node, cal, booking, resolver, guard, now = fixture(tmp_path)
    now[0] = NOW - timedelta(seconds=1)
    node._handle_access("GPU-a", 101)
    assert not guard.sent
    now[0] = NOW
    node._handle_access("GPU-a", 101)
    assert guard.sent == [(101, 42)]
    now[0] += timedelta(hours=1)
    node._handle_access("GPU-a", 101)
    assert len(guard.sent) == 1


def test_recurring_enforcement_uses_later_windows_and_stops_in_gaps(tmp_path):
    node, cal, booking, resolver, guard, now = fixture(tmp_path)
    cal.update(booking["id"], record(recurrence=dict(frequency="daily", until="2030-01-03", timezone="UTC")))
    now[0] = NOW + timedelta(days=1)
    node._handle_access("GPU-a", 101)
    assert guard.sent == [(101, 42)]
    now[0] += timedelta(hours=1)
    node._handle_access("GPU-a", 101)
    assert len(guard.sent) == 1
    node._claims = {key: Node.Claim(clock=node._clock) for key in node._claims}
    assert len(node.reserve({"email": "bob@example.org"}, {"gpu": 2})) == 2
    now[0] = NOW + timedelta(days=2)
    node._handle_access("GPU-a", 101)
    assert len(guard.sent) == 2
    resolver.user = {"email": "alice@example.org"}
    node._handle_access("GPU-a", 101)
    assert len(guard.sent) == 2
    cal.cancel(booking["id"])
    resolver.user = {"email": "bob@example.org"}
    node._handle_access("GPU-a", 101)
    assert len(guard.sent) == 2


def test_recurring_queued_event_revalidates_current_window(tmp_path, monkeypatch):
    guard = ObservedGuard()
    node, cal, booking, _, _, now = fixture(tmp_path, guard=guard)
    cal.update(booking["id"], record(recurrence=dict(frequency="daily", until="2030-01-03", timezone="UTC")))
    now[0] = NOW + timedelta(days=1)
    queued = queue_access(node, monkeypatch)
    now[0] += timedelta(hours=1)
    drain_access(queued)
    assert not guard.sent


def test_final_owner_change_or_resolution_error_skips(tmp_path):
    node, _, _, resolver, guard, _ = fixture(tmp_path)
    calls = [0]
    def change():
        calls[0] += 1
        if calls[0] == 2:
            resolver.user = {"email": "alice@example.org"}
    resolver.hook = change
    node._handle_access("GPU-a", 101)
    assert calls[0] == 2 and not guard.sent
    resolver.user = {"email": "bob@example.org"}
    def fail():
        raise OSError("Resolver unavailable")
    resolver.hook = fail
    node._handle_access("GPU-a", 101)
    assert not guard.sent


def test_owner_legacy_allocation_and_future_bookings(tmp_path):
    node, _, _, _, _, now = fixture(tmp_path, "off")
    assert len(node.reserve({"email": " ALICE@EXAMPLE.ORG "}, {"gpu": 2})) == 2
    node._claims = {key: Node.Claim(clock=node._clock) for key in node._claims}
    now[0] = NOW - timedelta(seconds=1)
    assert len(node.reserve({"email": "bob@example.org"}, {"gpu": 2})) == 2


def test_final_process_change_skips_and_closes_handle(tmp_path):
    node, _, _, _, guard, _ = fixture(tmp_path)
    guard.snapshots = [(123, 1000), (123, 1000), (123, 1000), (124, 1000)]
    node._handle_access("GPU-a", 101)
    assert not guard.sent and guard.closed == [42]


def test_cancel_serializes_with_final_signal(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    node, cal, booking, _, guard, _ = fixture(tmp_path)
    started, cancelled = Event(), Event()
    original_kill = guard.kill
    with ThreadPoolExecutor(1) as pool:
        def cancel():
            started.set()
            cal.cancel(booking["id"])
            cancelled.set()
        def kill(pid, handle):
            pool.submit(cancel)
            assert started.wait(1)
            assert not cancelled.wait(0.05)
            original_kill(pid, handle)
        guard.kill = kill
        node._handle_access("GPU-a", 101)
    assert cancelled.is_set() and guard.sent == [(101, 42)]
    assert cal.list() == []


class ObservedGuard(FixtureGuard):
    """Mutable fictional process identity across a delayed monitor callback."""
    def __init__(self):
        super().__init__()
        self.current = (100, 1000)
        self.inspections = []

    def inspect(self, pid):
        self.inspections.append(self.current)
        if isinstance(self.current, Exception):
            raise self.current
        return self.current


def queue_access(node, monkeypatch, device="GPU-a", process=101):
    queued = []
    monkeypatch.setattr(node._loop, "add_callback", lambda callback, *args: queued.append((callback, args)))
    node._handle_access_signal(None, device, process)
    return queued


def drain_access(queued):
    for callback, args in queued:
        callback(*args)


def test_delayed_monitor_pid_reuse_does_not_signal_or_change_claim(tmp_path, monkeypatch):
    guard = ObservedGuard()
    node, _, _, resolver, _, _ = fixture(tmp_path, guard=guard)
    alice = {"email": "alice@example.org"}
    node.claim("GPU-a", alice, 202)
    queued = queue_access(node, monkeypatch)
    guard.current = (200, 1000)
    resolver.user = {"email": "carol@example.org"}
    drain_access(queued)
    assert not guard.sent and not guard.opened
    assert guard.inspections[0] == (100, 1000)
    status = node.status("GPU-a")
    assert status["user"] == alice
    assert status["users"] == [alice]
    assert status["processes"] == [202]


def test_delayed_monitor_unchanged_process_still_signals(tmp_path, monkeypatch):
    guard = ObservedGuard()
    node, _, _, _, _, _ = fixture(tmp_path, guard=guard)
    queued = queue_access(node, monkeypatch)
    assert guard.inspections == [(100, 1000)]
    assert not guard.sent
    drain_access(queued)
    assert guard.sent == [(101, 42)]
    assert guard.closed == [42]
    assert node.status("GPU-a")["processes"] == [101]


@pytest.mark.parametrize("unresolved", [None, OSError("process vanished")])
def test_delayed_unresolved_snapshot_never_recaptured(tmp_path, monkeypatch, unresolved):
    guard = ObservedGuard()
    guard.current = unresolved
    node, _, _, _, _, _ = fixture(tmp_path, guard=guard)
    queued = queue_access(node, monkeypatch)
    guard.current = (200, 1000)
    drain_access(queued)
    assert not guard.sent and not guard.opened
    assert guard.inspections == [unresolved]
    assert node.status("GPU-a")["processes"] == []


def test_direct_handler_revalidates_identity_before_claim(tmp_path):
    guard = ObservedGuard()
    node, _, _, resolver, _, _ = fixture(tmp_path, guard=guard)
    resolver.hook = lambda: setattr(guard, "current", (200, 1000))
    node._handle_access("GPU-a", 101)
    assert not guard.sent
    assert node.status("GPU-a")["processes"] == []


@pytest.mark.parametrize("mode,synthetic,device,process", [
    ("kill", False, "unrecognized-GPU", 101),
    ("kill", False, "GPU-a", None),
    ("off", False, "GPU-a", 101),
    ("dry-run", False, "GPU-a", 101),
    ("kill", True, "TEST-0", 101),
])
def test_monitor_callback_skips_unneeded_inspection(tmp_path, monkeypatch, mode, synthetic, device, process):
    monitor = SyntheticMonitor(1) if synthetic else Monitor()
    node, _, _, _, guard, _ = fixture(tmp_path, mode, monitor=monitor)
    monkeypatch.setattr(guard, "inspect", lambda pid: pytest.fail("unexpected process inspection"))
    drain_access(queue_access(node, monkeypatch, device, process))
    assert not guard.sent


def test_calendar_disabled_monitor_never_inspects(tmp_path, monkeypatch):
    node, _, _, _, guard, _ = fixture(tmp_path)
    node.calendar = None
    monkeypatch.setattr(guard, "inspect", lambda pid: pytest.fail("unexpected process inspection"))
    drain_access(queue_access(node, monkeypatch))
    assert not guard.sent
