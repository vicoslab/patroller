
import uuid

class Device(object):

    def __init__(self, uuid, group, **kwargs):
        self._uuid = uuid
        self._group = group

    @property
    def updated(self):
        raise NotImplementedError()

    @property
    def uuid(self):
        return self._uuid

    @property
    def group(self):
        return self._group

    def update(self, **kwargs):
        pass

    def stats(self):
        return dict()

    def info(self):
        return dict()


class DeviceMonitor(object):

    def start(self):
        pass

    def stop(self):
        pass

    def __iter__(self):
        return iter([])

class TestDeviceMonitor(DeviceMonitor):

    def __init__(self, count):
        self._devices = []
        for _ in range(count):
            self._devices.append(Device("TEST-" + str(uuid.uuid4()), "test"))

    def __iter__(self):
        return iter(self._devices)

    def start(self):
        pass

    def stop(self):
        pass

class IdentityResolver(object):

    def __call__(self, token):
        if isinstance(token, str):
            return self.identify_client(token)
        else:
            return self.identify_process(token)

    def identify_process(self, pid):
        return "unknown"

    def identify_client(self, address):
        return "unknown"
