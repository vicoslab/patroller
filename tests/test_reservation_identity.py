from types import SimpleNamespace

from patroller.docker import DockerIdentityResolver
from patroller import Node
from test_reservation_http import Monitor


def fake_docker(labels):
    resolver = object.__new__(DockerIdentityResolver)
    resolver._labels = ["email"]
    resolver.user_info_lables = ["ccc-user.name", "name", "email"]
    resolver._docker_errors = SimpleNamespace(NotFound=LookupError)
    container = SimpleNamespace(labels=labels)
    resolver._docker = SimpleNamespace(containers=SimpleNamespace(get=lambda _: container))
    return resolver, container


def test_original_name_only_labels_preserved_in_status():
    labels = {"ccc-user.name": "toolbox-nanoparticles", "email": "N/A"}
    node = Node(Monitor(), resolver=lambda _: labels)
    node._handle_access("GPU-a", 101)
    assert node.status("GPU-a")["user"] == labels


def test_docker_keeps_fresh_email_less_metadata():
    labels = {"ccc-user.name": "toolbox-nanoparticles", "email": "N/A"}
    resolver, container = fake_docker(labels)
    assert resolver._extract_identity("container") == labels
    container.labels = {"ccc-user.name": "new-container"}
    assert resolver._extract_identity("container") == container.labels
    container.labels = {}
    assert resolver._extract_identity("container") is None


def test_process_identity_must_not_fall_back_to_old_labels():
    # Bypass constructor: never connect to Docker/socket.
    resolver = object.__new__(DockerIdentityResolver)
    resolver._cache = {"container": {"email": "old@example.org"}}
    resolver._labels = ["email"]
    resolver.user_info_lables = None
    resolver._docker_errors = SimpleNamespace(NotFound=LookupError)
    container = SimpleNamespace(labels={"email": "new@example.org"})
    resolver._docker = SimpleNamespace(containers=SimpleNamespace(get=lambda _: container))
    assert resolver._extract_identity("container") == {"email": "new@example.org"}
    container.labels = {}
    assert resolver._extract_identity("container") is None
