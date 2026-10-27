"""Hard boundaries: tests cannot contact production or signal unowned processes."""
import ipaddress
import os
import signal
import socket
import subprocess
import sys

import pytest


@pytest.fixture(autouse=True)
def isolated_test_boundaries(monkeypatch):
    children = {}
    handles = set()
    original_popen, original_kill = subprocess.Popen, os.kill
    original_connect, original_connect_ex, original_bind = socket.socket.connect, socket.socket.connect_ex, socket.socket.bind

    def own_child(args, *positional, **kwargs):
        assert args == [sys.executable, "-c", "import time; time.sleep(30)"], "Only the explicitly owned smoke child may be spawned"
        child = original_popen(args, *positional, **kwargs)
        children[child.pid] = child
        return child

    def kill(pid, sig):
        assert pid in children and children[pid].poll() is None, "Cannot signal an unowned/reaped PID"
        return original_kill(pid, sig)

    def local_address(sock, address):
        assert sock.family in (socket.AF_INET, socket.AF_INET6), "No Docker/Unix socket access"
        assert ipaddress.ip_address(address[0]).is_loopback, "Only literal loopback networking is allowed"

    def connect(sock, address):
        local_address(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        local_address(sock, address)
        return original_connect_ex(sock, address)

    def bind(sock, address):
        local_address(sock, address)
        return original_bind(sock, address)

    monkeypatch.setattr(subprocess, "Popen", own_child)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "bind", bind)
    if hasattr(os, "pidfd_open"):
        original_open = os.pidfd_open
        def pidfd_open(pid, flags=0):
            assert pid in children and children[pid].poll() is None
            handle = original_open(pid, flags)
            handles.add(handle)
            return handle
        monkeypatch.setattr(os, "pidfd_open", pidfd_open)
    if hasattr(signal, "pidfd_send_signal"):
        original_send = signal.pidfd_send_signal
        def pidfd_send_signal(fd, *args, **kwargs):
            assert fd in handles, "Cannot signal an unowned pidfd"
            return original_send(fd, *args, **kwargs)
        monkeypatch.setattr(signal, "pidfd_send_signal", pidfd_send_signal)
    yield
    for child in children.values():
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=3)
