from datetime import datetime, timedelta, timezone

from patroller import Node
from patroller.base import Device, DeviceMonitor, IdentityResolver


class StaticMonitor(DeviceMonitor):
    def __init__(self):
        self.device = Device("GPU-0", "gpu")

    def __iter__(self):
        return iter([self.device])


class ProcessResolver(IdentityResolver):
    def __init__(self, users):
        self.users = users

    def identify_process(self, pid):
        return self.users[pid]


def test_records_multiple_users_on_same_gpu_without_replacing_original_claim():
    alice = {"email": "alice@example.org"}
    bob = {"email": "bob@example.org"}
    node = Node(StaticMonitor(), resolver=ProcessResolver({101: alice, 202: bob}))

    assert node.claim("GPU-0", alice, 101) is True
    assert node.claim("GPU-0", bob, 202) is False

    status = node.status("GPU-0")
    assert status["user"] == alice
    assert status["users"] == [alice, bob]
    assert status["processes"] == [101, 202]


def test_shared_gpu_is_not_available_for_new_reservations():
    alice = {"email": "alice@example.org"}
    bob = {"email": "bob@example.org"}
    carol = {"email": "carol@example.org"}
    node = Node(StaticMonitor(), resolver=ProcessResolver({}))

    assert node.reserve(alice, {"gpu": 1}) is not None
    assert node.claim("GPU-0", bob, 202) is False

    assert node.reserve(carol, {"gpu": 1}) is None


def test_same_pid_owner_transfer_removes_empty_old_owner_after_repeated_observations():
    alice = {"email": "alice@example.org"}
    bob = {"email": "bob@example.org"}
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    resolver = ProcessResolver({101: alice})
    node = Node(StaticMonitor(), resolver=resolver, clock=lambda: now[0])
    node._handle_access("GPU-0", 101)
    resolver.users[101] = bob
    for _ in range(8):
        now[0] += timedelta(seconds=1)
        node._handle_access("GPU-0", 101)
        status = node.status("GPU-0")  # Prune while Bob remains observed.
        assert status["user"] == bob
        assert status["users"] == [bob]
        assert status["processes"] == [101]
    assert list(node._claims["GPU-0"]._processes) == [("email", "bob@example.org")]


def test_owner_transfer_preserves_old_owners_other_pids_and_legacy_lease():
    alice = {"email": "alice@example.org"}
    bob = {"email": "bob@example.org"}
    lease = {"email": "lease@example.org"}
    node = Node(StaticMonitor(), resolver=ProcessResolver({}))
    assert node.reserve(lease, {"gpu": 1}) is not None
    node.claim("GPU-0", alice, 101)
    node.claim("GPU-0", alice, 202)
    node.claim("GPU-0", bob, 101)
    status = node.status("GPU-0")
    assert status["user"] == lease
    assert status["users"] == [lease, alice, bob]
    assert status["processes"] == [202, 101]
    node.claim("GPU-0", bob, 202)
    status = node.status("GPU-0")
    assert status["users"] == [lease, bob]
    assert status["processes"] == [101, 202]
