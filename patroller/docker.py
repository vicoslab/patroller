
from email.utils import parseaddr
import re

from cachetools import TTLCache, cached

from patroller.base import IdentityResolver

CONTAINER_ID_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])")
DOCKER_SCOPE_RE = re.compile(r"docker-([0-9a-f]{64})\.scope")


def container_id_from_cgroup_path(path):
    """Extract a Docker container id from a cgroup path.

    Docker's cgroup path depends on the host init system, Docker cgroup driver,
    and cgroup version.  Older Ubuntu hosts commonly expose paths like
    ``/docker/<id>`` while newer systemd/cgroup-v2 hosts expose paths like
    ``/system.slice/docker-<id>.scope``.  Docker can also be nested under
    systemd slices or other cgroup parents, so scan the whole path instead of
    relying on a specific controller such as cpuset.
    """

    match = DOCKER_SCOPE_RE.search(path)
    if match is not None:
        return match.group(1)

    match = CONTAINER_ID_RE.search(path)
    if match is not None:
        return match.group(1)

    return None


def pid_to_container(pid):
    with open("/proc/%d/cgroup" % pid) as fp:
        return cgroup_to_container(fp)


def cgroup_to_container(lines):
    for line in lines:
        line = line.strip()
        try:
            _, _, name = line.split(':', 2)
        except ValueError:
            continue

        container = container_id_from_cgroup_path(name)
        if container is not None:
            return container

    return None


class DockerIdentityResolver(IdentityResolver):

    def __init__(self, user_labels=None, user_info_lables=None):
        super().__init__()
        import docker

        self._cache = {}
        self._docker_errors = docker.errors
        self._docker = docker.from_env()
        if user_labels is None:
            self._labels = ["user.email", "email", "maintainer"]
        else:
            self._labels = user_labels

        self.user_info_lables = user_info_lables

    def identify_process(self, pid):

        container = pid_to_container(pid)

        if container is None:
            return None

        return self._extract_identity(container)

    def _extract_identity(self, container):

        try:

            ct = self._docker.containers.get(container)
            labels = ct.labels

            user_id = dict()

            for name in self._labels:
                if name in labels:
                    name, address = parseaddr(labels[name])
                    if address:
                        user_id = dict(email=address)
                    break
            
            if self.user_info_lables is not None:                
                user_id.update({name: labels[name] for name in self.user_info_lables if name in labels})                

            if len(user_id) > 0:
                self._cache[container] = user_id

        except self._docker_errors.NotFound:
            return None

        if container in self._cache:
            return self._cache[container]

    def identify_client(self, address):
        container = self._find_container(address)

        if container is None:
            return None

        return self._extract_identity(container)

    @cached(TTLCache(100, 5))
    def _find_container(self, address):
        for container in self._docker.containers.list():
            networks = container.attrs['NetworkSettings'].get('Networks', {})
            for _, network in networks.items():
                if network["IPAddress"] == address:
                    return container.id
        return None
