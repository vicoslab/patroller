
import subprocess
import logging
import io
from threading import Thread, Lock
import xml.etree.ElementTree as ET
from copy import copy
from datetime import datetime
import json
from email.utils import parseaddr

from flask import Flask, Response
import docker

SMI_BINARY = "nvidia-smi"

logger = logging.getLogger("gpu")

def parse_int(s):
    if s == "-":
        return None
    else:
        return int(s)

def pid_to_container(pid):
    container = None
    with open("/proc/%d/cgroup" % pid) as fp:
        for line in fp:
            line = line.strip()
            _, subsys, name = line.split(':', 2)
            if subsys == "cpuset" and name.startswith("/docker/"):
                container = name[8:]
    return container

class GPU(object):

    ATTRIBUTES = ["name", "brand", "number"]

    STATS = ["power", "gtemp", "mtemp", "sm", "mem", "enc", "dec", "mclk", "pclk"]

    def __init__(self, uuid, **kwargs):
        self._uuid = uuid
        self._stats = {n: None for n in GPU.STATS}
        self._attributes = {k: v for k, v in kwargs.items() if k in GPU.ATTRIBUTES}
        self._updated = None
        self._lock = Lock()

    def __getattr__(self, name):
        if name in GPU.ATTRIBUTES:
            return self._attributes[name]
        elif name in GPU.STATS:
            with self._lock:
                return self._stats[name]
        else:
            return super().__getattribute__(name)

    @property
    def updated(self):
        return self._updated

    @property
    def uuid(self):
        return self._uuid

    @property
    def user(self):
        return self._claim_user

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if not k in GPU.STATS:
                    raise RuntimeError("Illegal attribute")
                self._stats[k] = v
            self._updated = datetime.now()

    def stats(self):
        with self._lock:
            data = copy(self._stats)
            data["updated"] = self._updated.isoformat()
            return data

class Node(object):

    def __init__(self):
        response = subprocess.check_output([SMI_BINARY, "-q", "-x"])
        doc = ET.parse(io.StringIO(response.decode("utf-8")))
        self._gpus = dict()
        for gpu in doc.getroot().iter("gpu"):
            name = gpu.find("product_name").text
            brand = gpu.find("product_brand").text
            uuid = gpu.find("uuid").text
            number = int(gpu.find("minor_number").text)
            self._gpus[uuid] = GPU(uuid, name=name, brand=brand, number=number)

    def __iter__(self):
        return iter(self._gpus.values())

    def __getitem__(self, uuid):
        if isinstance(uuid, str):
            return self._gpus[uuid]
        uuid = self.resolve(uuid)
        return self._gpus[uuid]

    def resolve(self, number):
        for uuid, gpu in self._gpus.items():
            if gpu.number == number:
                return uuid
        return None

class Monitor(Thread):

    def __init__(self, process):
        super().__init__()
        self._process = process
        self._run = True

    def stop(self):
        self._run = False

    def line(self, line):
        pass

    def run(self):
        smi = subprocess.Popen(self._process, stdout=subprocess.PIPE)

        while smi and self._run:
            line = smi.stdout.readline().decode("utf-8").strip()
            self.line(line)

class DMon(Monitor):

    def __init__(self, node):
        super().__init__([SMI_BINARY, "dmon"])
        self._node = node

    def line(self, line):
        if line.startswith("#"):
            return
        tokens = line.split()
        if len(tokens) < 10:
            return
        try:
            device_id = int(tokens[0])
            self._node[device_id].update(power=parse_int(tokens[1]), gtemp=parse_int(tokens[2]),
                mtemp=parse_int(tokens[3]), sm=parse_int(tokens[4]), mem=parse_int(tokens[5]), enc=parse_int(tokens[6]),
                dec=parse_int(tokens[7]), mclk=parse_int(tokens[8]), pclk=parse_int(tokens[9]))
        except ValueError as e:
            print(e)

class PMon(Monitor):

    def __init__(self, node, identity_manager, reservation_manager):
        super().__init__([SMI_BINARY, "pmon"])
        self._node = node
        self._identifier = identity_manager
        self._reservations = reservation_manager

    def line(self, line):
        if line.startswith("#"):
            return
        tokens = line.split()
        if len(tokens) < 8:
            return

        try:
            device_id = int(tokens[0])

            if tokens[1] == "-":
                self._node[device_id].claim(None)
                return

            device_uuid = self._node.resolve(device_id)

            pid = int(tokens[1])

            user, container = self._identifier(pid)

            if container is None:
                logger.warning("Unable to identify container for process %d", pid)
            elif user is None:
                logger.warning("Unable to identify user for container %s", container)
            else:
                if not self._reservations.claim(device_uuid, user, pid):
                    logger.warning("Tresspassing process on device %d (%s): %d", device_id, device_uuid, pid)

        except ValueError as e:
            print(e)

class DockerIdentityManager(object):

    IDENTIFIER_LABELS = ["vicos.user.email", "user.email", "email", "maintainer"]

    def __init__(self):
        super().__init__()
        self._cache = {}
        self._docker = docker.from_env()

    def __call__(self, pid):

        container = pid_to_container(pid)

        if container is None:
            return None, container

        try:

            ct = self._docker.containers.get(container)
            labels = ct.labels

            for name in DockerIdentityManager.IDENTIFIER_LABELS:
                if name in labels:
                    name, address = parseaddr(labels[name])
                    if address:
                        self._cache[container] = address
                    break

        except docker.errors.NotFound:
            return None, None

        if container in self._cache:
            return self._cache[container], container

class Reservation(object):

    def __init__(self, user=None):
        self._user = user
        self._since = datetime.now()
        self._update = datetime.now()
        self._processes = []
  
    @property
    def user(self):
        return self._user

    @property
    def age(self):
        return (datetime.now()-self._since).total_seconds()

    @property
    def updated(self):
        return (datetime.now()-self._update).total_seconds()

    @property
    def claims(self):
        return list(self._processes)

    @property
    def free(self):
        return self._user is None

    @property
    def empty(self):
        return len(self._processes) == 0

    def claim(self, pid):
        if pid not in self._processes:
            self._processes.append(pid)
        self._update = datetime.now()

class ReservationManager(object):

    def __init__(self, node, lease=10):
        super().__init__()
        self._node = node
        self._lease = lease
        self._reservations = {device.uuid : Reservation() for device in node}
        self._lock = Lock()

    def cleanup(self):
        with self._lock:
            for uuid, r in self._reservations.items():
                if r.empty and r.updated > self._lease:
                    self._reservations[uuid] = Reservation()

    def reserve(self, user, count):
        self.cleanup()
        with self._lock:
            free = [i for i, r in enumerate(self._reservations) if r.free]

            if len(free) < count:
                return False
            
            reserve = free[:count]
            for r in reserve:
                self._reservations[r] = Reservation(user)

            return reserve

    def claim(self, uuid, user, pid=None):
        with self._lock:
            if user == self._reservations[uuid].user:
                self._reservations[uuid].claim(pid)
                return True
            if user is None or self._reservations[uuid].free:
                self._reservations[uuid] = Reservation(user)
                if pid is not None:
                    self._reservations[uuid].claim(pid)
                return True
            return False

def main():

    node = Node()

    identifier = DockerIdentityManager()
    reservations = ReservationManager(node)

    app = Flask("gpumonitor")
    dmon = DMon(node)
    pmon = PMon(node, identifier, reservations)

    @app.route("/status")
    def stats():

        data = {}
        for gpu in node:
            data[gpu.uuid] = gpu.stats()

        return Response(json.dumps(data), mimetype="application/json")


    @app.route("/devices")
    def devices():

        data = {}
        for gpu in node:
            data[gpu.uuid] = gpu.stats()

        return Response(json.dumps(data), mimetype="application/json")


    @app.route("/request")
    def request():
        count = request.args.get("count", 1)

    dmon.start()
    pmon.start()
    try:
        app.run(host="0.0.0.0", port=6868)
    except KeyboardInterrupt:
        pass
    dmon.stop()
    pmon.stop()
    dmon.join()
    pmon.join()



if __name__ == "__main__":

    main()