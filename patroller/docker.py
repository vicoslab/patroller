
from email.utils import parseaddr

import docker
from cachetools import TTLCache, cached

from patroller.base import IdentityResolver

def pid_to_container(pid):
    container = None
    with open("/proc/%d/cgroup" % pid) as fp:
        for line in fp:
            line = line.strip()
            _, subsys, name = line.split(':', 2)
            if subsys == "cpuset" and name.startswith("/docker/"):
                container = name[8:]
    return container

class DockerIdentityResolver(IdentityResolver):

    IDENTIFIER_LABELS = ["vicos.user.email", "user.email", "email", "maintainer"]

    def __init__(self):
        super().__init__()
        self._cache = {}
        self._docker = docker.from_env()

    def identify_process(self, pid):

        container = pid_to_container(pid)

        if container is None:
            return None, None

        return self._extract_identity(container), container

    def _extract_identity(self, container):

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
            return None

        if container in self._cache:
            return self._cache[container]

    def identify_client(self, address):
        container = self._find_container(address)

        if container is None:
            return None, None

        return self._extract_identity(container), container

    @cached(TTLCache(100, 5))
    def _find_container(self, address):
        for container in self._docker.containers.list():
            networks = container.attrs['NetworkSettings'].get('Networks', {})
            for _, network in networks.items():
                if network["IPAddress"] == address:
                    return container.id
        return None
