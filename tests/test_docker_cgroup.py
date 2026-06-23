from patroller.docker import cgroup_to_container, container_id_from_cgroup_path

CONTAINER = "ad3dcdbd6bdd43165df4be6794671d8f58190656ef65d1ad54144d9d64b6dc82"
OLD_CONTAINER = "bbd452ffa06906a1a8485dc9c887c7504668bd399e9528caa790ef2851d7215c"


def test_extracts_container_from_cgroup_v2_systemd_scope():
    assert container_id_from_cgroup_path(
        "/system.slice/docker-%s.scope" % CONTAINER
    ) == CONTAINER


def test_extracts_container_from_legacy_docker_cgroup_path():
    assert container_id_from_cgroup_path("/docker/%s" % OLD_CONTAINER) == OLD_CONTAINER


def test_extracts_container_from_nested_systemd_docker_path():
    assert container_id_from_cgroup_path(
        "/system.slice/docker.service/docker/%s" % CONTAINER
    ) == CONTAINER


def test_cgroup_to_container_checks_unified_and_legacy_controllers():
    assert cgroup_to_container([
        "12:pids:/docker/%s\n" % OLD_CONTAINER,
        "0::/docker/%s\n" % OLD_CONTAINER,
    ]) == OLD_CONTAINER


def test_cgroup_to_container_ignores_malformed_and_non_docker_lines():
    assert cgroup_to_container([
        "not-a-cgroup-line\n",
        "0::/user.slice/user-1000.slice/session-2.scope\n",
    ]) is None
