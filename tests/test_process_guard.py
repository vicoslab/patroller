import errno
from io import StringIO
import os
import signal
from unittest.mock import patch

import pytest

from patroller.reservations import ProcessGuard


def test_proc_stat_comm_parsing_and_all_uids():
    stat = "123 (a tricky ) process) " + " ".join(["S"] + ["0"] * 18 + ["456"])
    def fake_open(path):
        return StringIO(stat if path.endswith("/stat") else "Uid:\t1001\t1002\t1003\t1004\n")
    with patch("builtins.open", side_effect=fake_open):
        assert ProcessGuard().inspect(123) == (456, (1001, 1002, 1003, 1004))
    assert ProcessGuard().inspect(1) is None
    assert ProcessGuard().inspect(os.getpid()) is None


def test_pidfd_preferred_and_only_kernel_unsupported_falls_back(monkeypatch):
    guard = ProcessGuard()
    sent, closed = [], []
    monkeypatch.setattr(os, "pidfd_open", lambda pid, flags: 42, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: sent.append(args), raising=False)
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    assert guard.open(123) == 42
    guard.kill(123, 42)
    guard.close(42)
    assert sent == [(42, signal.SIGKILL)] and closed == [42]
    def unsupported(*args):
        raise OSError(errno.ENOSYS, "unsupported")
    monkeypatch.setattr(os, "pidfd_open", unsupported)
    assert guard.open(123) is None
    def denied(*args):
        raise PermissionError(errno.EPERM, "denied")
    monkeypatch.setattr(os, "pidfd_open", denied)
    with pytest.raises(PermissionError):
        guard.open(123)
