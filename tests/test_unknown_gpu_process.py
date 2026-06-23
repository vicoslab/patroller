from patroller import Node, UNKNOWN_USER
from patroller.base import IdentityResolver, TestDeviceMonitor


class UnresolvedProcessResolver(IdentityResolver):
    def identify_process(self, pid):
        return None


def test_unresolved_gpu_process_is_reported_as_unknown_user():
    monitor = TestDeviceMonitor(1)
    device = next(iter(monitor)).uuid
    node = Node(monitor, resolver=UnresolvedProcessResolver())

    node._handle_access(device, 1234)

    status = node.status(device)
    assert status["user"] == UNKNOWN_USER
    assert status["processes"] == [1234]


def test_multiple_unresolved_gpu_processes_are_grouped_without_warning_spam(caplog):
    monitor = TestDeviceMonitor(1)
    device = next(iter(monitor)).uuid
    node = Node(monitor, resolver=UnresolvedProcessResolver())

    node._handle_access(device, 1234)
    node._handle_access(device, 5678)

    status = node.status(device)
    assert status["user"] == UNKNOWN_USER
    assert status["processes"] == [1234, 5678]
    assert "Unable to identify user" not in caplog.text
