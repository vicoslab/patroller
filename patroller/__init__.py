
import logging
import io
import os
import sys
from datetime import datetime, timedelta
import json
import asyncio

from blinker import signal

import tornado.httpserver
from tornado.ioloop import IOLoop
import tornado.web

from cachetools import TTLCache, cached
from collections import OrderedDict

logger = logging.getLogger("patroller")

from patroller.base import IdentityResolver, TestDeviceMonitor

UNKNOWN_USER = {"email": "N/A", "name": "Unknown"}

loop = IOLoop.instance()

def parse_expiration(value):
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise RuntimeError("Expiration must be an ISO-8601 string")
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1]
    if "+" in value:
        value = value.split("+", 1)[0]
    elif len(value) > 10 and "-" in value[10:]:
        value = value.rsplit("-", 1)[0]
    for pattern in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    raise RuntimeError("Expiration must be an ISO-8601 string")

def user_from_email(email):
    if not isinstance(email, str) or not email.strip():
        raise RuntimeError("Reservation email is required")
    return {"email": email.strip()}

class Node(object):

    class Claim(object):

        def __init__(self, user=None, reservation=None):
            self._users = OrderedDict()
            self._since = datetime.now()
            self._update = datetime.now()
            self._processes = OrderedDict()
            self._unclaimed = True
            self._reservation = reservation
            if user is not None:
                self.add_user(user)

        @staticmethod
        def _user_key(user):
            if isinstance(user, dict) and "email" in user:
                return user["email"]
            return json.dumps(user, sort_keys=True)

        @property
        def user(self):
            for user in self._users.values():
                return user
            return self.reserved_user

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
            return self._reservation

        @property
        def reserved_user(self):
            if self._reservation is None:
                return None
            return self._reservation["user"]

        @property
        def reserved(self):
            return self._reservation is not None

        @property
        def pending_claim(self):
            return self._unclaimed and len(self._users) > 0 and not self.reserved

        @property
        def free(self):
            return len(self._users) == 0 and not self.reserved

        def has_user(self, user):
            return self._user_key(user) in self._users

        def add_user(self, user):
            key = self._user_key(user)
            if key not in self._users:
                self._users[key] = user
                self._processes[key] = []
            return key

        def add_reservation(self, email, expires_at):
            self._reservation = {"user": user_from_email(email), "expires_at": expires_at}
            self._update = datetime.now()

        def clear_reservation(self):
            self._reservation = None
            self._update = datetime.now()

        def reservation_expired(self):
            return self.reserved and datetime.now() >= self._reservation["expires_at"]

        def reservation_info(self):
            if not self.reserved:
                return None
            return {
                "user": self._reservation["user"],
                "expires_at": self._reservation["expires_at"].isoformat(),
            }

        def claim(self, user=None, pid=None):
            if pid is None:
                if len(self.processes) > 0:
                    self._unclaimed = True
                    for key in self._processes:
                        self._processes[key] = []
                    self._update = datetime.now()
                return

            key = self.add_user(user)
            if pid not in self._processes[key]:
                self._unclaimed = False
                self._processes[key].append(pid)
                self._update = datetime.now()


    def __init__(self, *monitors, lease=5, resolver=IdentityResolver(), reservations_file=None):
        super().__init__()
        self._lease = lease
        self._resolver = resolver
        self._devices = OrderedDict()
        self._pending = []
        self._reservations_file = reservations_file
        if self._reservations_file is None:
            self._reservations_file = os.environ.get("PATROLLER_RESERVATIONS_FILE", "/var/lib/patroller/reservations.json")
        signal("access").connect(self._handle_access_signal)
        for monitor in monitors:
            for device in monitor:
                self._devices[device.uuid] = device
                logger.info("Adding device %s", device.uuid)
        self._claims = {k: Node.Claim() for k, _ in self._devices.items()}
        self._load_reservations()
        self._cleanup()

    def __iter__(self):
        return iter(self._devices.values())

    def __getitem__(self, uuid):
        return self._devices[uuid]

    def _load_reservations(self):
        if self._reservations_file is None or not os.path.exists(self._reservations_file):
            return
        try:
            with open(self._reservations_file) as fp:
                data = json.load(fp)
        except (IOError, ValueError) as e:
            logger.warning("Unable to load reservations from %s: %s", self._reservations_file, e)
            return
        changed = False
        for item in data.get("reservations", []):
            uuid = item.get("device")
            try:
                expires_at = parse_expiration(item.get("expires_at"))
                if uuid not in self._claims or expires_at <= datetime.now():
                    changed = True
                    continue
                self._claims[uuid].add_reservation(item.get("email"), expires_at)
            except RuntimeError as e:
                logger.warning("Skipping invalid stored reservation for %s: %s", uuid, e)
                changed = True
        if changed:
            self._save_reservations()

    def _save_reservations(self):
        if self._reservations_file is None:
            return
        directory = os.path.dirname(self._reservations_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        data = {"reservations": [
            {
                "device": reservation["device"],
                "email": reservation["user"]["email"],
                "expires_at": reservation["expires_at"],
            }
            for reservation in self.reservations()
        ]}
        tmp_path = self._reservations_file + ".tmp"
        with open(tmp_path, "w") as fp:
            json.dump(data, fp, indent=2, sort_keys=True)
        os.replace(tmp_path, self._reservations_file)

    def _handle_access_signal(self, _, device, process=None):
        loop.add_callback(self._handle_access, device, process)

    def _handle_access(self, device, process):
        if not device in self._devices:
            return

        if process is None:
            self._claims[device].claim(pid=None)

        else:

            user = self._resolver(process)

            if user is None:
                user = UNKNOWN_USER

            if not self.claim(device, user, process):
                logger.warning("Trespassing process %d on device %s detected.", process, device)

    def _cleanup(self):
        change = False
        for uuid, r in self._claims.items():
            if r.reservation_expired():
                r.clear_reservation()
                logger.debug("Reservation for device %s expired", uuid)
                change = True
            if r.pending_claim and r.updated > self._lease:
                self._claims[uuid] = Node.Claim()
                logger.debug("Device %s is now free", uuid)
                change = True

        if change:
            self._save_reservations()

        if change and self._pending:
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

            free = [uuid for uuid in filtered if self._claims[uuid].free]

            if len(free) < count:
                return None

            devices.extend(free[:count])

        for uuid in devices:
            self.claim(uuid, user)

        return {self._devices[uuid] for uuid in devices}

    def claim(self, uuid, user, pid=None):
        claim = self._claims[uuid]
        if claim.has_user(user):
            claim.claim(user, pid)
            return True
        if claim.reserved:
            if claim.reserved_user == user:
                claim.clear_reservation()
                self._save_reservations()
                claim.claim(user, pid)
                logger.debug("Device %s claimed by reserved user %s", uuid, user['email'])
                return True
            if pid is not None:
                claim.claim(user, pid)
                self._handle_reserved_device_conflict(uuid, user, pid, claim.reserved_user)
            return False
        if claim.free:
            self._claims[uuid] = Node.Claim(user)
            if pid is not None:
                self._claims[uuid].claim(user, pid)
            logger.debug("Device %s claimed by user %s", uuid, user['email'])
            return True
        if pid is not None:
            claim.claim(user, pid)
        return False

    def _handle_reserved_device_conflict(self, uuid, user, pid, reserved_user):
        logger.warning(
            "Process %d owned by %s is using reserved device %s assigned to %s; automatic removal is not enabled",
            pid, user.get("email"), uuid, reserved_user.get("email"),
        )

    def add_reservation(self, uuid, email, expires_at):
        if uuid not in self._devices:
            raise RuntimeError("Unknown device")
        expires_at = parse_expiration(expires_at)
        if expires_at <= datetime.now():
            raise RuntimeError("Reservation expiration must be in the future")
        claim = self._claims[uuid]
        if not claim.free and not claim.reserved:
            raise RuntimeError("Device is already in use")
        claim.add_reservation(email, expires_at)
        self._save_reservations()
        return self.reservation(uuid)

    def update_reservation(self, uuid, email=None, expires_at=None):
        if uuid not in self._devices or not self._claims[uuid].reserved:
            raise RuntimeError("Reservation not found")
        current = self._claims[uuid].reservation
        email = current["user"]["email"] if email is None else email
        expires_at = current["expires_at"] if expires_at is None else parse_expiration(expires_at)
        if expires_at <= datetime.now():
            raise RuntimeError("Reservation expiration must be in the future")
        self._claims[uuid].add_reservation(email, expires_at)
        self._save_reservations()
        return self.reservation(uuid)

    def remove_reservation(self, uuid):
        if uuid not in self._devices or not self._claims[uuid].reserved:
            raise RuntimeError("Reservation not found")
        self._claims[uuid].clear_reservation()
        self._save_reservations()
        self._process_pending()

    def reservation(self, uuid):
        info = self._claims[uuid].reservation_info()
        if info is not None:
            info["device"] = uuid
        return info

    def reservations(self):
        return [r for r in (self.reservation(uuid) for uuid in self._devices) if r is not None]

    def status(self, uuid):
        claim = self._claims[uuid]
        return dict(user=claim.user, users=claim.users, age=claim.age, processes=claim.processes, reservation=claim.reservation_info())

    def resolve(self, token):
        return self._resolver(token)

class APIHandler(tornado.web.RequestHandler):

    def initialize(self, node):
        self.node = node

    def set_default_headers(self):
        self.set_header("Content-Type", 'application/json')

    def write_error(self, status_code: int, **kwargs):
        self.finish(json.dumps({"code": status_code, "message": self._reason}))

class StatusHandler(APIHandler):

    def get(self):

        data = {}
        for device in self.node:
            data[device.uuid] = device.stats()
            data[device.uuid]["claim"] = self.node.status(device.uuid)

        self.write(json.dumps(data))

class DevicesHandler(APIHandler):

    def get(self):

        data = {}
        for device in self.node:
            data[device.uuid] = device.info()
            data[device.uuid]["group"] = device.group

        self.write(json.dumps(data))

class ReservationsHandler(APIHandler):

    def get(self):
        self.write(json.dumps(self.node.reservations()))

    def post(self):
        try:
            data = json.loads(self.request.body.decode("utf-8") or "{}")
            expires_at = data.get("expires_at")
            if expires_at is None and "ttl" in data:
                expires_at = datetime.now() + timedelta(seconds=int(data["ttl"]))
            reservation = self.node.add_reservation(data.get("device"), data.get("email"), expires_at)
            self.set_status(201)
            self.finish(json.dumps(reservation))
        except (RuntimeError, ValueError, TypeError) as e:
            self.set_status(400)
            self.finish(json.dumps({"error": str(e)}))

class ReservationHandler(APIHandler):

    def patch(self, uuid):
        try:
            data = json.loads(self.request.body.decode("utf-8") or "{}")
            reservation = self.node.update_reservation(uuid, data.get("email"), data.get("expires_at"))
            self.finish(json.dumps(reservation))
        except (RuntimeError, ValueError, TypeError) as e:
            self.set_status(400)
            self.finish(json.dumps({"error": str(e)}))

    def delete(self, uuid):
        try:
            self.node.remove_reservation(uuid)
            self.set_status(204)
            self.finish()
        except RuntimeError as e:
            self.set_status(404)
            self.finish(json.dumps({"error": str(e)}))

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

        except RuntimeError as e:
            data = {"error": str(e)}
            self.set_status(404)
            self.finish(json.dumps(data))
            return

    def on_connection_close(self):
        if self._future is not None:
            self._future.cancel()


def run():

    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler(sys.stdout))

    if "PATROLLER_TEST" in os.environ:
        logger.setLevel(logging.DEBUG)
        monitors = [TestDeviceMonitor(8)]
        resolver = IdentityResolver()
    else:
        from patroller.docker import DockerIdentityResolver
        from patroller.gpu import GPUMonitor

        user_labels = None if not "PATROLLER_USER_LABELS" in os.environ else os.environ["PATROLLER_USER_LABELS"].split(",")
        user_info_labels = None if not "PATROLLER_USER_INFO_LABELS" in os.environ else os.environ["PATROLLER_USER_INFO_LABELS"].split(",")

        resolver = DockerIdentityResolver(user_labels, user_info_labels)
        monitors = [GPUMonitor()]

    lease_time = 10 if not "PATROLLER_LEASE" in os.environ else int(os.environ["PATROLLER_LEASE"])

    node = Node(*monitors, resolver=resolver, lease=lease_time)

    app = tornado.web.Application([
        (r"/status", StatusHandler, dict(node=node)),
        (r"/devices", DevicesHandler, dict(node=node)),
        (r"/request", DeviceRequestHandler, dict(node=node, block=False)),
        (r"/wait", DeviceRequestHandler, dict(node=node, block=True)),
        (r"/reservations", ReservationsHandler, dict(node=node)),
        (r"/reservations/([^/]+)", ReservationHandler, dict(node=node)),
    ])

    for monitor in monitors:
        monitor.start()
    try:
        app.listen(80)
        IOLoop.current().start()
    except KeyboardInterrupt:
        pass
    for monitor in monitors:
        monitor.stop()
