
import logging
import os
import sys
from datetime import datetime, timezone
import json
import asyncio

from blinker import signal

import tornado.httpserver
from tornado.ioloop import IOLoop
import tornado.web

from collections import OrderedDict

logger = logging.getLogger("patroller")

from patroller.base import IdentityResolver, TestDeviceMonitor
from patroller.reservations import (Calendar, ReservationError, ProcessGuard,
                                   process_identity, reservation_identity, MAX_BODY)

UNKNOWN_USER = {"email": "N/A", "name": "Unknown"}
_UNOBSERVED_PROCESS = object()


class Node(object):

    class Claim(object):

        def __init__(self, user=None, clock=None):
            self._clock = clock or (lambda: datetime.now(timezone.utc))
            self._seen = {}
            self._users = OrderedDict()
            self._since = datetime.now()
            self._update = datetime.now()
            self._processes = OrderedDict()
            self._unclaimed = True
            if user is not None:
                self.add_user(user)

        @staticmethod
        def _user_key(user):
            identity = process_identity(user)
            if identity is not None:
                return identity
            return json.dumps(user, sort_keys=True)

        @property
        def user(self):
            for user in self._users.values():
                return user
            return None

        @property
        def users(self):
            return list(self._users.values())

        @property
        def age(self):
            return (datetime.now() - self._since).total_seconds()

        @property
        def updated(self):
            return (datetime.now() - self._update).total_seconds()

        @property
        def processes(self):
            return [pid for processes in self._processes.values() for pid in processes]

        @property
        def reservation(self):
            return self._unclaimed and not self.free

        @property
        def free(self):
            return len(self._users) == 0

        def has_user(self, user):
            return self._user_key(user) in self._users

        def add_user(self, user):
            key = self._user_key(user)
            if key not in self._users:
                self._users[key] = user
                self._processes[key] = []
            return key

        def prune(self):
            now = self._clock()
            stale = [pid for pid, seen in self._seen.items() if (now - seen).total_seconds() >= 5]
            for pid in stale:
                self._seen.pop(pid, None)
                for processes in self._processes.values():
                    if pid in processes:
                        processes.remove(pid)
            if stale and self.processes:
                for key in list(self._users):
                    if not self._processes[key]:
                        self._users.pop(key)
                        self._processes.pop(key)
            if stale and not self.processes:
                self._unclaimed = True
                self._update = datetime.now()

        def claim(self, user=None, pid=None):
            if pid is None:
                if len(self.processes) > 0:
                    self._unclaimed = True
                    self._seen.clear()
                    for key in self._processes:
                        self._processes[key] = []
                    self._update = datetime.now()
                return

            key = self.add_user(user)
            self._seen[pid] = self._clock()
            for other_key, processes in list(self._processes.items()):
                if other_key != key and pid in processes:
                    processes.remove(pid)
                    if not processes:
                        self._users.pop(other_key)
                        self._processes.pop(other_key)
            if pid not in self._processes[key]:
                self._unclaimed = False
                self._processes[key].append(pid)
                self._update = datetime.now()


    def __init__(self, *monitors, lease=5, resolver=IdentityResolver(), calendar=None,
                 enforcement="dry-run", clock=None, process_guard=None, test_mode=False):
        super().__init__()
        if enforcement not in ("off", "dry-run", "kill"):
            raise ValueError("PATROLLER_RESERVATION_ENFORCEMENT must be off, dry-run or kill")
        self.calendar = calendar
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        synthetic = test_mode or any(isinstance(m, TestDeviceMonitor) for m in monitors)
        safe_fixture = process_guard is not None and getattr(process_guard, "safe_for_synthetic", False)
        self.enforcement = "off" if synthetic and not safe_fixture else enforcement
        self._loop = IOLoop.current()
        self._guard = process_guard or ProcessGuard()
        self._lease = lease
        self._resolver = resolver
        self._devices = OrderedDict()
        self._pending = []
        signal("access").connect(self._handle_access_signal)
        for monitor in monitors:
            for device in monitor:
                self._devices[device.uuid] = device
                logger.info("Adding device %s", device.uuid)
        self._claims = {k: Node.Claim(clock=self._clock) for k, _ in self._devices.items()}
        self._cleanup()

    def __iter__(self):
        return iter(self._devices.values())

    def __getitem__(self, uuid):
        return self._devices[uuid]

    def _handle_access_signal(self, _, device, process=None):
        if device not in self._devices:
            return
        snapshot = self._observe_process(process)
        self._loop.add_callback(self._handle_access, device, process, snapshot)

    def _observe_process(self, process):
        if process is not None and self.calendar is not None and self.enforcement == "kill":
            try:
                return self._guard.inspect(process)
            except Exception:
                logger.warning("Unable to inspect observed process; skipping event")
        return None

    def _handle_access(self, device, process, snapshot=_UNOBSERVED_PROCESS):
        if not device in self._devices:
            return

        if process is None:
            self._claims[device].claim(pid=None)

        else:

            # Direct callers capture here; an explicit None from the monitor is
            # a failed observation and must never be replaced by a fresh one.
            if snapshot is _UNOBSERVED_PROCESS:
                snapshot = self._observe_process(process)
            validate = self.calendar is not None and self.enforcement == "kill"
            if validate and snapshot is None:
                logger.info("Skipping unresolved observed PID %s", process)
                return
            try:
                user = self._resolver(process)
            except Exception:
                logger.warning("Unable to resolve observed process; skipping enforcement")
                user = None
            if not isinstance(user, dict) or not user:
                user = UNKNOWN_USER

            # Resolution may itself race with exit/reuse. Do not attribute a
            # replacement process to this GPU or authorize its enforcement.
            if validate and self._observe_process(process) != snapshot:
                return
            if not self.claim(device, user, process):
                logger.warning("Trespassing process %d on device %s detected.", process, device)
            self._enforce(device, process, snapshot, user)

    def _cleanup(self):
        for uuid, r in self._claims.items():
            r.prune()
            if r.reservation and r.updated > self._lease:
                self._claims[uuid] = Node.Claim(clock=self._clock)
                logger.debug("Device %s is now free", uuid)

        if self._pending:
            self._process_pending()

        IOLoop.current().call_later(1, self._cleanup)


    def _process_pending(self):

        def process_pending(pending):
            if pending[0].cancelled():
                return False
            try:
                devices = self.reserve(pending[1], pending[2])

                if devices is not None:
                    pending[0].set_result(devices)
                    return False

            except Exception as ex:
                pending[0].set_exception(ex)
                return False

            return True

        self._pending[:] = [x for x in self._pending if process_pending(x)]

    def wait(self, user, requirements):
        future = asyncio.Future()
        self._pending.append((future, user, requirements))
        self._process_pending()

        return future

    def reserve(self, user, requirements):

        devices = []

        for group, count in requirements.items():
            filtered = [uuid for uuid, device in self._devices.items() if device.group == group]

            if len(filtered) < count:
                raise RuntimeError("Insufficient number of devices available")

            free = []
            for uuid in filtered:
                self._claims[uuid].prune()
                try:
                    active = self.calendar.active_next(uuid, self._clock())[0] if self.calendar else None
                except Exception:
                    raise ReservationError(503, "Calendar storage unavailable") from None
                owner = process_identity(user)
                if self._claims[uuid].free and (active is None or
                        owner is not None and reservation_identity(active) == owner):
                    free.append(uuid)

            if len(free) < count:
                return None

            devices.extend(free[:count])

        for uuid in devices:
            self.claim(uuid, user)

        return {self._devices[uuid] for uuid in devices}

    def claim(self, uuid, user, pid=None):
        if self._claims[uuid].has_user(user):
            self._claims[uuid].claim(user, pid)
            return True
        if self._claims[uuid].free:
            self._claims[uuid] = Node.Claim(user, clock=self._clock)
            if pid is not None:
                self._claims[uuid].claim(user, pid)
            logger.debug("Device %s claimed by user %s", uuid, user.get('email', 'N/A') if isinstance(user, dict) else 'N/A')
            return True
        if pid is not None:
            self._claims[uuid].claim(user, pid)
        return False

    def status(self, uuid):
        claim = self._claims[uuid]
        claim.prune()
        return dict(user=claim.user, users=claim.users, age=claim.age, processes=claim.processes)

    def reservation_info(self, uuid):
        info = dict(active_reservation=None, next_reservation=None,
                    reservation_state="disabled", reservation_enforcement=self.enforcement)
        if self.calendar is not None:
            try:
                active, upcoming = self.calendar.active_next(uuid, self._clock())
                info.update(active_reservation=active, next_reservation=upcoming, reservation_state="enabled")
            except Exception:
                info["reservation_state"] = "unavailable"
                logger.error("Calendar unavailable; reservation status cannot be determined")
        return info

    @staticmethod
    def _safe_snapshot(snapshot):
        if not snapshot or len(snapshot) != 2:
            return False
        uids = snapshot[1] if isinstance(snapshot[1], tuple) else (snapshot[1],)
        return bool(uids) and all(isinstance(uid, int) and uid > 0 for uid in uids)

    def _enforce(self, device, pid, observed, user):
        if self.calendar is None or self.enforcement == "off":
            return
        try:
            active, _ = self.calendar.active_next(device, self._clock())
            owner = process_identity(user)
            if active is None:
                return
            if owner is None:
                logger.info("Skipping reservation enforcement for unknown process %s", pid)
                return
            if owner == reservation_identity(active):
                return
            if self.enforcement == "dry-run":
                logger.info("Reservation dry-run: would kill PID %s on %s", pid, device)
                return
            if not self._safe_snapshot(observed):
                logger.info("Skipping reservation enforcement for unresolved/system PID %s", pid)
                return
            handle = self._guard.open(pid)
            try:
                if self._guard.inspect(pid) != observed:
                    return
                # The write lock serializes final calendar validation and signaling
                # against edits/cancellation from any service worker.
                with self.calendar.transaction() as db:
                    fresh_user = self._resolver(pid)
                    fresh_owner = process_identity(fresh_user)
                    if fresh_owner is None or fresh_owner != owner:
                        return
                    if self._guard.inspect(pid) != observed:
                        return
                    current, _ = self.calendar.active_next(device, self._clock(), db)
                    if current is None or current["id"] != active["id"] or reservation_identity(current) == fresh_owner:
                        return
                    self._guard.kill(pid, handle)
                    logger.warning("Reservation enforcement killed PID %s on %s", pid, device)
            finally:
                self._guard.close(handle)
        except Exception:
            # Errors must never become authorization to kill.
            logger.warning("Reservation enforcement could not safely validate PID %s; skipped", pid)

    def resolve(self, token):
        return self._resolver(token)

class APIHandler(tornado.web.RequestHandler):

    def initialize(self, node):
        self.node = node

    def set_default_headers(self):
        self.set_header("Content-Type", 'application/json')

    def write_error(self, status_code: int, **kwargs):
        self.finish(json.dumps({"error": self._reason, "code": status_code, "message": self._reason}))

class StatusHandler(APIHandler):

    def get(self):

        data = {}
        for device in self.node:
            data[device.uuid] = device.stats()
            data[device.uuid]["claim"] = self.node.status(device.uuid)
            data[device.uuid].update(self.node.reservation_info(device.uuid))

        self.write(json.dumps(data))

class DevicesHandler(APIHandler):

    def get(self):

        data = {}
        for device in self.node:
            data[device.uuid] = device.info()
            data[device.uuid]["group"] = device.group

        self.write(json.dumps(data))

class DeviceRequestHandler(APIHandler):

    def initialize(self, node, block = False):
        super().initialize(node)
        self._block = block
        self._future = None

    async def get(self):

        client_ip = self.request.remote_ip

        user = self.node.resolve(client_ip)

        if user is None:
            data = {"error": "Unable to determine user, aborting"}
            self.set_status(404)
            self.finish(json.dumps(data))
            return

        requirements = dict()
        for group in self.request.query_arguments:
            requirements[group] = int(self.get_query_argument(group, 0))

        try:

            if self._block:
                self._future = self.node.wait(user, requirements)
                try:
                    devices = await self._future
                except asyncio.CancelledError:
                    devices = []
                self._future = None
            else:
                devices = self.node.reserve(user, requirements)
                if devices is None:
                    data = {"error": "Currently unavailable"}
                    self.set_status(404)
                    self.finish(json.dumps(data))
                    return

            data = [dict(info=device.info(), uuid=device.uuid, group=device.group) for device in devices]

            self.finish(json.dumps(data))

        except ReservationError as e:
            self.set_status(e.status)
            self.finish(json.dumps({"error": str(e)}))
            return
        except RuntimeError as e:
            data = {"error": str(e)}
            self.set_status(404)
            self.finish(json.dumps(data))
            return

    def on_connection_close(self):
        if self._future is not None:
            self._future.cancel()


class ReservationAPIHandler(APIHandler):
    requires_write_token = False

    def prepare(self):
        if self.node.calendar is None:
            raise tornado.web.HTTPError(503, reason="Reservation calendar disabled")
        if self.request.method != "GET" or self.requires_write_token:
            self._require_write_token()

    def _require_write_token(self):
        expected = self.settings.get("reservation_token")
        if not expected:
            raise tornado.web.HTTPError(403, reason="Reservation writes disabled")
        supplied = self.request.headers.get("X-Patroller-Reservation-Token")
        if supplied is None:
            authorization = self.request.headers.get("Authorization", "")
            supplied = authorization[7:] if authorization.startswith("Bearer ") else None
        if not supplied:
            raise tornado.web.HTTPError(401, reason="Reservation credential required")
        import hmac
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise tornado.web.HTTPError(403, reason="Invalid reservation credential")

    def _body(self):
        if len(self.request.body) > MAX_BODY:
            raise ReservationError(400, "Reservation request body too large")
        try:
            value = json.loads(self.request.body)
        except (ValueError, UnicodeError):
            raise ReservationError(400, "Invalid JSON body") from None
        if not isinstance(value, dict):
            raise ReservationError(400, "Expected a JSON object")
        return value

    def _perform(self, operation, status=200):
        try:
            value = operation()
            self.set_status(status)
            # Tornado rejects top-level arrays passed directly to write().
            self.finish(json.dumps(value))
        except ReservationError as exc:
            self.set_status(exc.status)
            self.finish(json.dumps({"error": str(exc)}))
        except Exception:
            logger.error("Calendar operation unavailable")
            self.set_status(503)
            self.finish(json.dumps({"error": "Calendar storage unavailable"}))

class ReservationAccessHandler(ReservationAPIHandler):
    requires_write_token = True

    def get(self):
        self.finish(json.dumps({"writable": True}))


class ReservationsHandler(ReservationAPIHandler):
    def get(self):
        self._perform(self.node.calendar.list)


class ReserveHandler(ReservationAPIHandler):
    def post(self):
        self._perform(lambda: self.node.calendar.create(self._body()), 201)


class ReservationHandler(ReservationAPIHandler):
    def patch(self, reservation_id):
        self._perform(lambda: self.node.calendar.update(reservation_id, self._body()))


class UnreserveHandler(ReservationAPIHandler):
    def post(self):
        def cancel():
            value = self._body()
            if set(value) != {"id"} or not isinstance(value["id"], str) or not value["id"] or len(value["id"]) > 128:
                raise ReservationError(400, "Expected a reservation id")
            self.node.calendar.cancel(value["id"])
            return dict(id=value["id"], cancelled=True)
        self._perform(cancel)


class JSONErrorHandler(tornado.web.ErrorHandler):
    set_default_headers = APIHandler.set_default_headers
    write_error = APIHandler.write_error


def make_application(node, reservation_token=None):
    return tornado.web.Application([
        (r"/status", StatusHandler, dict(node=node)),
        (r"/devices", DevicesHandler, dict(node=node)),
        (r"/request", DeviceRequestHandler, dict(node=node, block=False)),
        (r"/wait", DeviceRequestHandler, dict(node=node, block=True)),
        (r"/reservation-access", ReservationAccessHandler, dict(node=node)),
        (r"/reservations", ReservationsHandler, dict(node=node)),
        (r"/reserve", ReserveHandler, dict(node=node)),
        (r"/reservations/([^/]+)", ReservationHandler, dict(node=node)),
        (r"/unreserve", UnreserveHandler, dict(node=node)),
    ], reservation_token=reservation_token, default_handler_class=JSONErrorHandler,
       default_handler_args=dict(status_code=404))


def run():
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    enforcement = os.environ.get("PATROLLER_RESERVATION_ENFORCEMENT", "dry-run")
    if enforcement not in ("off", "dry-run", "kill"):
        raise ValueError("PATROLLER_RESERVATION_ENFORCEMENT must be off, dry-run or kill")
    test_mode = "PATROLLER_TEST" in os.environ
    if test_mode:
        monitors = [TestDeviceMonitor(8)]
        resolver = IdentityResolver()
    else:
        from patroller.docker import DockerIdentityResolver
        from patroller.gpu import GPUMonitor
        user_labels = os.environ.get("PATROLLER_USER_LABELS")
        user_info_labels = os.environ.get("PATROLLER_USER_INFO_LABELS")
        resolver = DockerIdentityResolver(user_labels.split(",") if user_labels else None,
                                          user_info_labels.split(",") if user_info_labels else None)
        monitors = [GPUMonitor()]

    path = os.environ.get("PATROLLER_RESERVATIONS_DB")
    calendar = Calendar(path, [device.uuid for monitor in monitors for device in monitor]) if path else None
    node = Node(*monitors, resolver=resolver, lease=int(os.environ.get("PATROLLER_LEASE", "10")),
                calendar=calendar, enforcement=enforcement, test_mode=test_mode)
    app = make_application(node, os.environ.get("PATROLLER_RESERVATION_TOKEN"))
    server = app.listen(int(os.environ.get("PATROLLER_PORT", "80")), address=os.environ.get("PATROLLER_ADDRESS", ""))
    for monitor in monitors:
        monitor.start()
    try:
        IOLoop.current().start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        for monitor in monitors:
            monitor.stop()


main = run
