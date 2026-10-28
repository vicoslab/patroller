import json
import tempfile
import asyncio
from types import SimpleNamespace
from tornado.httputil import HTTPHeaders, HTTPServerRequest
from datetime import datetime, timezone
from unittest.mock import patch

import tornado.testing

from patroller import Node, make_application
from patroller.base import Device, DeviceMonitor, IdentityResolver
from patroller.reservations import Calendar
from test_reservations import record


class Monitor(DeviceMonitor):
    def __iter__(self):
        return iter([Device("GPU-a", "gpu"), Device("GPU-b", "gpu")])


class Resolver(IdentityResolver):
    def __call__(self, value):
        return {"email": "bob@example.org"}


class APIScenarios:
    # Tornado 6.0.4 expects this unittest hook, removed in Python 3.11.
    def runTest(self):
        pass

    def get_app(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cal = Calendar(self.tmp.name + "/bookings.sqlite", ["GPU-a", "GPU-b"])
        self.node = Node(Monitor(), resolver=Resolver(), calendar=self.cal,
                         clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
        return make_application(self.node, reservation_token="secret")

    def call(self, path, method="GET", body=None, headers=None):
        response = self.fetch(path, method=method, body=json.dumps(body) if body is not None else None,
                              headers=headers or {})
        return response.code, json.loads(response.body)

    def test_recurring_crud_actual_status_gaps_and_rollback(self):
        auth = {"Authorization": "Bearer secret"}
        rule = dict(frequency="daily", until="2030-01-03", timezone="UTC")
        payload = record(["GPU-a", "GPU-b"], recurrence=rule)
        assert self.call("/reserve", "POST", payload)[0] == 401
        code, series = self.call("/reserve", "POST", payload, auth)
        assert code == 201 and len(series["occurrences"]) == 3
        assert self.call("/reservations") == (200, [series])
        self.node._clock = lambda: datetime(2030, 1, 2, 0, 30, tzinfo=timezone.utc)
        status = self.call("/status")[1]["GPU-a"]
        assert status["active_reservation"]["id"] == series["id"]
        assert status["active_reservation"]["start_at"] == "2030-01-02T00:00:00Z"
        assert status["next_reservation"]["start_at"] == "2030-01-03T00:00:00Z"
        assert "occurrences" not in status["active_reservation"]
        assert "occurrences" not in status["next_reservation"]
        self.node._clock = lambda: datetime(2030, 1, 2, 1, tzinfo=timezone.utc)
        status = self.call("/status")[1]["GPU-a"]
        assert status["active_reservation"] is None
        assert status["next_reservation"]["start_at"] == "2030-01-03T00:00:00Z"
        assert self.call("/request?gpu=2")[0] == 200
        gap = record(start_at="2030-01-02T02:00:00Z", end_at="2030-01-02T03:00:00Z")
        code, gap_booking = self.call("/reserve", "POST", gap, auth)
        assert code == 201
        edit = dict(payload, start_at="2030-01-01T02:00:00Z", end_at="2030-01-01T03:00:00Z")
        assert self.call("/reservations/" + series["id"], "PATCH", edit, auth)[0] == 409
        assert series in self.call("/reservations")[1]
        edit = dict(payload, note="whole series", recurrence=dict(rule, until="2030-01-04"))
        code, updated = self.call("/reservations/" + series["id"], "PATCH", edit, auth)
        assert code == 200 and updated["id"] == series["id"] and len(updated["occurrences"]) == 4
        assert self.call("/reserve", "POST", dict(payload, occurrences=[]), auth)[0] == 400
        assert self.call("/reserve", "POST", dict(payload, recurrence=dict(rule, until=True)), auth)[0] == 400
        assert self.call("/unreserve", "POST", {"id": series["id"]}, auth) == (200, {"id": series["id"], "cancelled": True})
        assert self.call("/reservations") == (200, [gap_booking])
        assert self.call("/status")[1]["GPU-b"]["next_reservation"] is None

    def test_invalid_timezone_returns_400_without_writing(self):
        auth = {"Authorization": "Bearer secret"}
        for zone in ("x" * 256, "A/" + "x" * 256, "UTC/../UTC"):
            payload = record(recurrence=dict(frequency="daily", until="2030-01-03", timezone=zone))
            code, error = self.call("/reserve", "POST", payload, auth)
            assert code == 400 and "error" in error
            assert self.cal.list() == []

    def test_corrupt_series_blocks_http_availability_and_protection(self):
        import sqlite3
        series = self.cal.create(record(recurrence=dict(
            frequency="daily", until="2030-01-03", timezone="UTC")))
        series["occurrences"] = []
        with sqlite3.connect(self.cal.path) as db:
            db.execute("UPDATE reservations SET record=?", (json.dumps(series),))
        self.node._clock = lambda: datetime(2030, 1, 2, 0, 30, tzinfo=timezone.utc)
        assert self.call("/reservations")[0] == 503
        assert self.call("/request?gpu=2")[0] == 503
        code, status = self.call("/status")
        assert code == 200
        assert status["GPU-a"]["reservation_state"] == "unavailable"
        assert status["GPU-b"]["reservation_state"] == "unavailable"
        payload = record(start_at="2030-01-02T00:30:00Z", end_at="2030-01-02T01:30:00Z")
        assert self.call("/reserve", "POST", payload, {"Authorization": "Bearer secret"})[0] == 503

    def test_crud_auth_status_and_legacy_filter(self):
        assert self.call("/reservations") == (200, [])
        assert self.call("/reserve", "POST", record())[0] == 401
        auth = {"Authorization": "Bearer secret"}
        code, first = self.call("/reserve", "POST", record(), auth)
        assert code == 201
        assert self.call("/reserve", "POST", record(), auth)[0] == 409
        code, status = self.call("/status")
        assert code == 200
        assert status["GPU-a"]["active_reservation"] == first
        assert status["GPU-a"]["next_reservation"] is None
        assert "claim" in status["GPU-a"]
        code, allocated = self.call("/request?gpu=1")
        assert code == 200 and allocated[0]["uuid"] == "GPU-b"
        code, edited = self.call("/reservations/" + first["id"], "PATCH", record(note="new"), auth)
        assert code == 200 and edited["note"] == "new"
        assert self.call("/unreserve", "POST", {"id": first["id"]}, auth) == (200, {"id": first["id"], "cancelled": True})
        assert self.call("/reservations") == (200, [])

    def test_alternate_header_basic_auth_and_safe_errors(self):
        headers = {"Authorization": "Basic abc", "X-Patroller-Reservation-Token": "secret"}
        assert self.call("/reserve", "POST", record(), headers)[0] == 201
        assert self.call("/reserve", "POST", record(), {"Authorization": "Bearer wrong"})[0] == 403
        raw = self.fetch("/reserve", method="POST", body="{", headers=headers)
        assert raw.code == 400 and "error" in json.loads(raw.body)
        assert self.call("/reserve", "POST", [1], headers)[0] == 400
        assert self.call("/reserve", "POST", record(note="x" * 20000), headers)[0] == 400
        with patch.object(self.cal, "list", side_effect=RuntimeError("secret failure")):
            code, error = self.call("/reservations")
        assert code == 503 and "error" in error and "secret" not in str(error)

    def test_disabled_and_unconfigured_token(self):
        self.node.calendar = None
        for path, method, body in [("/reservations", "GET", None), ("/reserve", "POST", record()),
                                   ("/reservations/id", "PATCH", record()), ("/unreserve", "POST", {"id": "x"})]:
            assert self.call(path, method, body)[0] == 503
        assert self.call("/status")[0] == 200
        self.node.calendar = self.cal
        self._app.settings["reservation_token"] = None
        assert self.call("/reserve", "POST", record(), {"Authorization": "Bearer secret"})[0] == 403


    def test_edit_conflict_rollback_and_json_not_found(self):
        auth = {"Authorization": "Bearer secret"}
        _, first = self.call("/reserve", "POST", record(), auth)
        self.call("/reserve", "POST", record(start_at="2030-01-01T01:00:00Z", end_at="2030-01-01T02:00:00Z"), auth)
        assert self.call("/reservations/" + first["id"], "PATCH", record(end_at="2030-01-01T01:30:00Z"), auth)[0] == 409
        assert self.call("/reservations")[1][0] == first
        assert self.call("/reservations/missing", "PATCH", record(), auth)[0] == 404
        assert self.call("/unreserve", "POST", {"id": "missing"}, auth)[0] == 404
        for path in ("/missing", "/reservations/missing"):
            code, error = self.call(path)
            assert code in (404, 405) and "error" in error

    def test_calendar_failure_preserves_monitoring_and_blocks_legacy(self):
        self.cal.path = self.tmp.name + "/missing/db.sqlite"
        assert self.call("/reservations")[0] == 503
        assert self.call("/request?gpu=1")[0] == 503
        code, status = self.call("/status")
        assert code == 200
        assert status["GPU-a"]["reservation_state"] == "unavailable"



    def test_upcoming_status_and_blocking_wait_release(self):
        auth = {"Authorization": "Bearer secret"}
        _, active = self.call("/reserve", "POST", record(), auth)
        _, upcoming = self.call("/reserve", "POST", record(start_at=active["end_at"], end_at="2030-01-01T02:00:00Z"), auth)
        status = self.call("/status")[1]["GPU-a"]
        assert status["active_reservation"] == active and status["next_reservation"] == upcoming
        def cancel():
            self.cal.cancel(active["id"])
            self.node._process_pending()
        self.io_loop.call_later(0.01, cancel)
        code, allocated = self.call("/wait?gpu=2")
        assert code == 200 and {item["uuid"] for item in allocated} == {"GPU-a", "GPU-b"}



class TestLoopbackAPI(APIScenarios, tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        try:
            super().setUp()
        except PermissionError as exc:
            tornado.testing.AsyncTestCase.tearDown(self)
            self.skipTest("Sandbox forbids AF_INET loopback sockets: %s" % exc)


class TestInProcessAPI(APIScenarios, tornado.testing.AsyncTestCase):
    """Execute real routing/prepare/handlers over an in-memory HTTP connection."""
    def setUp(self):
        super().setUp()
        self._app = self.get_app()

    def fetch(self, path, method="GET", body=None, headers=None):
        async def execute():
            class Connection:
                context = SimpleNamespace(remote_ip="127.0.0.1", protocol="http")
                def __init__(self):
                    self.body = b""
                def set_close_callback(self, callback):
                    pass
                def write_headers(self, start_line, headers, chunk):
                    self.code = start_line.code
                    self.body += chunk
                    future = asyncio.get_running_loop().create_future()
                    future.set_result(None)
                    return future
                def finish(self):
                    pass
            connection = Connection()
            request = HTTPServerRequest(method=method, uri=path, headers=HTTPHeaders(headers or {}),
                                        body=body.encode() if isinstance(body, str) else body,
                                        connection=connection)
            delegate = self._app.find_handler(request)
            handler = delegate.handler_class(self._app, request, **delegate.handler_kwargs)
            await handler._execute([], *delegate.path_args, **delegate.path_kwargs)
            return SimpleNamespace(code=connection.code, body=connection.body)
        return self.io_loop.run_sync(execute)
