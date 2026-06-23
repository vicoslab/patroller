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
