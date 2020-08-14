
import subprocess
import io
from threading import Thread, Lock, RLock
import xml.etree.ElementTree as ET
from copy import copy
from datetime import datetime
import json

from blinker import signal

from patroller.base import Device, DeviceMonitor

SMI_BINARY = "nvidia-smi"

class GPU(Device):

    ATTRIBUTES = ["name", "brand", "number"]

    STATS = ["power", "gtemp", "mtemp", "sm", "mem", "enc", "dec", "mclk", "pclk"]

    def __init__(self, uuid, **kwargs):
        super().__init__(uuid, "gpu")
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

    def info(self):
        return copy(self._attributes)

def parse_int(s):
    if s == "-":
        return None
    else:
        return int(s)

class ProcessLineReader(Thread):

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

class DMon(ProcessLineReader):

    def __init__(self, monitor):
        super().__init__([SMI_BINARY, "dmon"])
        self._monitor = monitor

    def line(self, line):
        if line.startswith("#"):
            return
        tokens = line.split()
        if len(tokens) < 10:
            return
        try:
            device_id = int(tokens[0])
            device_uuid = self._node.find(device_id)
            self._monitor.find(device_id).update(power=parse_int(tokens[1]), gtemp=parse_int(tokens[2]),
                mtemp=parse_int(tokens[3]), sm=parse_int(tokens[4]), mem=parse_int(tokens[5]), enc=parse_int(tokens[6]),
                dec=parse_int(tokens[7]), mclk=parse_int(tokens[8]), pclk=parse_int(tokens[9]))
        except ValueError as e:
            print(e)

class PMon(ProcessLineReader):

    def __init__(self, monitor):
        super().__init__([SMI_BINARY, "pmon"])
        self._monitor = monitor
        self._claim_signal = signal("claim")

    def line(self, line):
        if line.startswith("#"):
            return
        tokens = line.split()
        if len(tokens) < 8:
            return

        try:
            device_id = int(tokens[0])
            gpu = self._manager.find(device_id)

            if tokens[1] == "-":
                self._claim_signal.send(device=gpu.uuid)
                return

            pid = int(tokens[1])

            self._claim_signal.send(process=pid, device=gpu.uuid)

        except ValueError as e:
            print(e)

class GPUMonitor(DeviceMonitor):

    def __init__(self):
        response = subprocess.check_output([SMI_BINARY, "-q", "-x"])
        doc = ET.parse(io.StringIO(response.decode("utf-8")))
        self._gpus = dict()
        for gpu in doc.getroot().iter("gpu"):
            name = gpu.find("product_name").text
            brand = gpu.find("product_brand").text
            uuid = gpu.find("uuid").text
            number = int(gpu.find("minor_number").text)
            self._gpus[number] = GPU(uuid, name=name, brand=brand, number=number)

        self._dmon = DMon(self)
        self._pmon = PMon(self)

    def start(self):
        self._dmon.start()
        self._pmon.start()

    def stop(self):
        self._dmon.stop()
        self._pmon.stop()
        self._dmon.join()
        self._pmon.join()

    def __iter__(self):
        return iter(self._gpus.values())

    def find(self, number):
        return self._gpus[number]

