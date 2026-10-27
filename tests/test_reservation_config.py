from types import SimpleNamespace

import pytest
import tornado.web
from tornado.ioloop import IOLoop

import patroller
from patroller.base import TestDeviceMonitor as SyntheticMonitor
from patroller.reservations import Calendar, ProcessGuard


@pytest.mark.parametrize("mode", [None, "off", "dry-run", "kill"])
def test_synthetic_run_configuration_never_enforces(tmp_path, monkeypatch, mode):
    for key in list(patroller.os.environ):
        if key.startswith("PATROLLER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("PATROLLER_TEST", "1")
    monkeypatch.setenv("PATROLLER_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("PATROLLER_PORT", "18888")
    monkeypatch.setenv("PATROLLER_RESERVATIONS_DB", str(tmp_path / "calendar.sqlite"))
    monkeypatch.setenv("PATROLLER_RESERVATION_TOKEN", "a-service-secret")
    if mode:
        monkeypatch.setenv("PATROLLER_RESERVATION_ENFORCEMENT", mode)
    captured = {}
    original = patroller.make_application
    def application(node, token):
        captured.update(node=node, token=token)
        return original(node, token)
    def listen(app, port, address):
        captured.update(port=port, address=address)
        return SimpleNamespace(stop=lambda: None)
    monkeypatch.setattr(patroller, "make_application", application)
    monkeypatch.setattr(tornado.web.Application, "listen", listen)
    monkeypatch.setattr(IOLoop.current(), "start", lambda: None)
    monkeypatch.setattr(ProcessGuard, "inspect", lambda *args: pytest.fail("Synthetic run inspected a real process"))
    patroller.run()
    node = captured["node"]
    assert node.enforcement == "off"
    assert captured["port"] == 18888 and captured["address"] == "127.0.0.1"
    assert captured["token"] == "a-service-secret"
    assert node.calendar.list() == []
    node._handle_access(next(iter(node)).uuid, 123)


def test_invalid_enforcement_rejected_before_monitor_creation(monkeypatch):
    monkeypatch.setenv("PATROLLER_RESERVATION_ENFORCEMENT", "KILL")
    with pytest.raises(ValueError):
        patroller.run()


def test_synthetic_explicit_default_guard_is_still_disabled(tmp_path):
    cal = Calendar(str(tmp_path / "calendar.sqlite"), [])
    node = patroller.Node(SyntheticMonitor(1), calendar=cal, enforcement="kill", process_guard=ProcessGuard())
    assert node.enforcement == "off"


def test_synthetic_gpu_ids_survive_restart():
    assert [device.uuid for device in SyntheticMonitor(2)] == [device.uuid for device in SyntheticMonitor(2)]
