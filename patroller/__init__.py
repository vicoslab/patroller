
import logging
import io
import os
import sys
from datetime import datetime
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

loop = IOLoop.instance()

class Node(object):

    class Claim(object):

        def __init__(self, user=None):
            self._user = user
            self._since = datetime.now()
            self._update = datetime.now()
            self._processes = []
            self._unclaimed = True

        @property
        def user(self):
            return self._user

        @property
        def age(self):
            return (datetime.now() - self._since).total_seconds()

        @property
        def updated(self):
            return (datetime.now() - self._update).total_seconds()

        @property
        def processes(self):
            return list(self._processes)

        @property
        def reservation(self):
            return self._unclaimed and not self.free

        @property
        def free(self):
            return self._user is None

        def claim(self, pid=None):
            if pid is None:
                if len(self._processes) > 0:
                    self._processes = []
                    self._update = datetime.now()
            elif pid not in self._processes:
                self._unclaimed = False
                self._processes.append(pid)
                self._update = datetime.now()


    def __init__(self, *monitors, lease=5, resolver=IdentityResolver()):
        super().__init__()
        self._lease = lease
        self._resolver = resolver
        self._devices = OrderedDict()
        self._pending = []
        signal("access").connect(self._handle_access_signal)
        for monitor in monitors:
            for device in monitor:
                self._devices[device.uuid] = device
                logger.info("Adding device %s", device.uuid)
        self._claims = {k: Node.Claim() for k, _ in self._devices.items()}
        self._cleanup()

    def __iter__(self):
        return iter(self._devices.values())

    def __getitem__(self, uuid):
        return self._devices[uuid]

    def _handle_access_signal(self, _, device, process=None):
        loop.add_callback(self._handle_access, device, process)

    def _handle_access(self, device, process):
        if not device in self._devices:
            return

        if process is None:
            self._claims[device].claim()

        else:

            user = self._resolver(process)

            if user is None:
                logger.warning("Unable to identify user for process %d", process)
            else:
                if not self.claim(device, user, process):
                    logger.warning("Trespassing process %d on device %s detected.", process, device)

    def _cleanup(self):
        change = False
        for uuid, r in self._claims.items():
            if r.reservation and r.updated > self._lease:
                self._claims[uuid] = Node.Claim()
                logger.debug("Device %s is now free", uuid)
                change = True

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
        if user == self._claims[uuid].user:
            self._claims[uuid].claim(pid)
            return True
        if self._claims[uuid].free:
            self._claims[uuid] = Node.Claim(user)
            if pid is not None:
                self._claims[uuid].claim(pid)
            logger.debug("Device %s claimed by user %s", uuid, user['email'])
            return True
        return False

    def status(self, uuid):
        claim = self._claims[uuid]
        return dict(user=claim.user, age=claim.age, processes=claim.processes)

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

