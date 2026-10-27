from types import SimpleNamespace

from patroller.docker import DockerIdentityResolver


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
