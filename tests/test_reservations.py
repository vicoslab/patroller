from datetime import datetime, timedelta

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


def test_add_update_and_remove_reservation_for_specific_email(tmp_path):
    node = Node(StaticMonitor(), resolver=IdentityResolver(), reservations_file=str(tmp_path / "reservations.json"))
    expires = datetime.now() + timedelta(minutes=5)

    reservation = node.add_reservation("GPU-0", "alice@example.org", expires)
    assert reservation["device"] == "GPU-0"
    assert reservation["user"] == {"email": "alice@example.org"}
    assert node.reserve({"email": "bob@example.org"}, {"gpu": 1}) is None

    updated = node.update_reservation(
        "GPU-0",
        email="carol@example.org",
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    assert updated["user"] == {"email": "carol@example.org"}

    node.remove_reservation("GPU-0")
    assert node.status("GPU-0")["reservation"] is None
    assert node.reserve({"email": "bob@example.org"}, {"gpu": 1}) is not None


def test_expired_reservation_is_removed_from_status_and_frees_device(tmp_path):
    node = Node(StaticMonitor(), resolver=IdentityResolver(), reservations_file=str(tmp_path / "reservations.json"))
    node.add_reservation("GPU-0", "alice@example.org", datetime.now() + timedelta(seconds=1))

    node._claims["GPU-0"]._reservation["expires_at"] = datetime.now() - timedelta(seconds=1)
    node._cleanup()

    assert node.status("GPU-0")["reservation"] is None
    assert node.reserve({"email": "bob@example.org"}, {"gpu": 1}) is not None


def test_reserved_user_claims_device_and_other_user_is_recorded_but_not_removed(tmp_path):
    alice = {"email": "alice@example.org"}
    bob = {"email": "bob@example.org"}
    node = Node(StaticMonitor(), resolver=ProcessResolver({101: alice, 202: bob}), reservations_file=str(tmp_path / "reservations.json"))
    node.add_reservation("GPU-0", "alice@example.org", datetime.now() + timedelta(minutes=5))

    assert node.claim("GPU-0", bob, 202) is False
    status = node.status("GPU-0")
    assert status["reservation"]["user"] == alice
    assert status["users"] == [bob]
    assert status["processes"] == [202]

    assert node.claim("GPU-0", alice, 101) is True
    status = node.status("GPU-0")
    assert status["reservation"] is None
    assert status["users"] == [bob, alice]
    assert status["processes"] == [202, 101]


def test_reservations_are_loaded_from_disk_after_restart(tmp_path):
    reservations_file = tmp_path / "reservations.json"
    expires_at = datetime.now() + timedelta(minutes=5)
    node = Node(StaticMonitor(), resolver=IdentityResolver(), reservations_file=str(reservations_file))
    node.add_reservation("GPU-0", "alice@example.org", expires_at)

    restarted = Node(StaticMonitor(), resolver=IdentityResolver(), reservations_file=str(reservations_file))

    status = restarted.status("GPU-0")
    assert status["reservation"]["user"] == {"email": "alice@example.org"}
    assert restarted.reserve({"email": "bob@example.org"}, {"gpu": 1}) is None
