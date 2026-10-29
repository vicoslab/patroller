"""Typed trusted container identities; every process and Docker object is a fixture."""
import json
import sqlite3
from datetime import timedelta

import pytest

from patroller import Node
from patroller import reservations as identities
from patroller.reservations import Calendar, ReservationError
from test_reservations import NOW, record
from test_reservation_enforcement import fixture
from test_reservation_http import APIScenarios, TestInProcessAPI as InProcessAPI
from test_reservation_identity import fake_docker

NAME = 'toolbox-nanoparticles'
USER = {'email': 'N/A', 'ccc-user.name': NAME}


def payload(**changes):
    return record(owner_type='name', owner_email='', owner_name=NAME, **changes)


@pytest.mark.parametrize('value', [None, 1, True, [], {}, '', ' ', 'Unknown', ' unknown USER ', 'N/A', 'None', 'NULL', 'x' * 201, 'a\x00b', '\tname', 'a\x1fb', 'bad\x7f'])
def test_invalid_identity_names(value):
    assert identities.identity_name(value) is None


@pytest.mark.parametrize('user,expected', [
    (USER, ('name', NAME)),
    ({'email': ' Alice@Example.org ', 'ccc-user.name': NAME}, ('email', 'alice@example.org')),
    ({'ccc-user.name': '  Case Sensitive  ', 'name': 'other'}, ('name', 'Case Sensitive')),
    ({'ccc-user.name': 'Unknown', 'name': 'Fallback'}, ('name', 'Fallback')),
    ({'email': 'N/A', 'name': 'Fallback'}, ('name', 'Fallback')),
    ({'email': 'N/A', 'ccc-user.name': 'Unknown'}, None),
    (None, None), ({}, None), ('name', None),
])
def test_process_identity_priority(user, expected):
    assert identities.process_identity(user) == expected


@pytest.mark.parametrize('changes', [
    {'owner_type': 'email'}, {'owner_type': None}, {'owner_type': 'other'},
    {'owner_type': []}, {'owner_type': {}},
    {'owner_email': 'alice@example.org'}, {'owner_email': ' '}, {'owner_email': None},
    {'owner_name': ''}, {'owner_name': 'Unknown'}, {'owner_name': 'n/a'},
    {'owner_name': None}, {'owner_name': 3}, {'owner_name': 'x' * 201},
    {'owner_name': '\tname'}, {'owner_name': 'bad\x7f'},
])
def test_invalid_name_booking_rejected(tmp_path, changes):
    cal = Calendar(tmp_path / 'calendar', ['GPU-a'])
    value = dict(payload(), **changes)
    with pytest.raises(ReservationError) as error:
        cal.create(value)
    assert error.value.status == 400
    assert cal.list() == []


def test_explicit_identity_required_and_no_implicit_upgrade(tmp_path):
    cal = Calendar(tmp_path / 'calendar', ['GPU-a'])
    value = payload()
    value.pop('owner_type')
    with pytest.raises(ReservationError) as error:
        cal.create(value)
    assert error.value.status == 400
    value = payload()
    value.pop('owner_name')
    with pytest.raises(ReservationError):
        cal.create(value)
    assert identities.reservation_identity(record(owner_name=NAME)) == ('email', 'alice@example.org')
    assert identities.reservation_identity(payload()) == ('name', NAME)
    for bad in (None, {}, dict(payload(), owner_type='email'), dict(payload(), owner_email='a@example.org')):
        assert identities.reservation_identity(bad) is None


def test_name_crud_recurrence_restart_and_legacy_bytes(tmp_path):
    cal = Calendar(tmp_path / 'calendar', ['GPU-a', 'GPU-b'])
    legacy = cal.create(record(['GPU-b']))
    with sqlite3.connect(cal.path) as db:
        before = db.execute('SELECT record FROM reservations WHERE id=?', (legacy['id'],)).fetchone()[0]
    value = payload(recurrence=dict(frequency='daily', until='2030-01-03', timezone='UTC'))
    value['owner_name'] = '  ' + NAME + '  '
    created = cal.create(value)
    assert created['owner_email'] == '' and created['owner_name'] == NAME and created['owner_type'] == 'name'
    cal = Calendar(cal.path, ['GPU-a', 'GPU-b'])
    assert created in cal.list()
    active, upcoming = cal.active_next('GPU-a', NOW + timedelta(days=1, minutes=1))
    assert active['owner_name'] == upcoming['owner_name'] == NAME
    assert active['owner_type'] == upcoming['owner_type'] == 'name'
    assert cal.update(created['id'], payload(note='edited'))['owner_type'] == 'name'
    with sqlite3.connect(cal.path) as db:
        assert db.execute('SELECT record FROM reservations WHERE id=?', (legacy['id'],)).fetchone()[0] == before
    cal.cancel(created['id'])
    assert cal.list() == [legacy]


@pytest.mark.parametrize('changes', [
    {'owner_type': 'email'}, {'owner_type': None}, {'owner_type': 'bogus'},
    {'owner_email': 'alice@example.org'}, {'owner_email': ' '},
    {'owner_name': ' ' + NAME}, {'owner_name': 'Unknown'}, {'owner_name': ''},
])
def test_corrupt_name_record_fails_closed(tmp_path, changes):
    cal = Calendar(tmp_path / 'calendar', ['GPU-a'])
    created = cal.create(payload())
    with sqlite3.connect(cal.path) as db:
        db.execute('UPDATE reservations SET record=?', (json.dumps(dict(created, **changes)),))
    for operation in (cal.list, lambda: cal.active_next('GPU-a', NOW), lambda: cal.create(payload())):
        with pytest.raises(ReservationError) as error:
            operation()
        assert error.value.status == 503


@pytest.mark.parametrize('user,allowed', [
    (USER, True), ({'name': NAME}, True), ({'ccc-user.name': NAME.upper()}, False),
    ({'ccc-user.name': 'other'}, False),
    ({'email': 'alice@example.org', 'ccc-user.name': NAME}, False),
    ({'email': 'bob@example.org', 'name': NAME}, False),
    ({'email': 'N/A', 'name': 'Unknown'}, None), (None, None),
])
def test_name_enforcement_and_allocation(tmp_path, user, allowed):
    node, cal, booking, resolver, guard, _ = fixture(tmp_path)
    cal.update(booking['id'], payload())
    resolver.user = user
    assert (node.reserve(user, {'gpu': 2}) is not None) == (allowed is True)
    node._claims = {key: Node.Claim(clock=node._clock) for key in node._claims}
    node._handle_access('GPU-a', 101)
    assert bool(guard.sent) == (allowed is False)


def test_metadata_name_cannot_bypass_email_booking(tmp_path):
    node, cal, booking, resolver, guard, _ = fixture(tmp_path)
    cal.update(booking['id'], record(owner_name=NAME))
    resolver.user = USER
    assert node.reserve(USER, {'gpu': 2}) is None
    node._handle_access('GPU-a', 101)
    assert guard.sent == [(101, 42)]


@pytest.mark.parametrize('fresh', [USER, {'ccc-user.name': 'third'}, {'email': 'bob@example.org', 'ccc-user.name': 'other'}, None, {}, {'name': 'Unknown'}])
def test_fresh_identity_change_skips_signal(tmp_path, fresh):
    node, cal, booking, resolver, guard, _ = fixture(tmp_path)
    cal.update(booking['id'], payload())
    resolver.user = {'ccc-user.name': 'other'}
    calls = []
    def change():
        calls.append(1)
        if len(calls) == 2:
            resolver.user = fresh
    resolver.hook = change
    node._handle_access('GPU-a', 101)
    assert len(calls) == 2 and not guard.sent and guard.closed == [42]


def test_final_booking_identity_is_rechecked_under_lock(tmp_path):
    node, cal, booking, resolver, guard, _ = fixture(tmp_path)
    cal.update(booking['id'], payload())
    resolver.user = {'ccc-user.name': 'other'}
    original = guard.open
    def edit(pid):
        cal.update(booking['id'], dict(payload(), owner_name='other'))
        return original(pid)
    guard.open = edit
    node._handle_access('GPU-a', 101)
    assert not guard.sent and guard.closed == [42]


def test_claim_keys_use_typed_identity_and_stable_unknown_metadata():
    claim = Node.Claim()
    for pid, user in enumerate([USER, {'email': 'N/A', 'ccc-user.name': 'other'},
                                {'email': 'N/A', 'extra': 'a'}, {'extra': 'b', 'email': 'N/A'},
                                {'email': ' Alice@Example.org '}, {'email': 'alice@example.org'}], 101):
        claim.claim(user, pid)
    assert len(claim.users) == 5
    assert claim.has_user({'name': NAME})
    assert claim.has_user({'extra': 'a', 'email': 'N/A'})
    assert claim._user_key({'name': 'alice@example.org'}) != claim._user_key({'email': 'alice@example.org'})


@pytest.mark.parametrize('metadata', [{'ccc-user.name': 'Unknown'}, {'email': 'N/A'}, {'extra': 'label'}])
def test_display_keeps_even_unresolvable_metadata(tmp_path, metadata):
    node, _, _, resolver, guard, _ = fixture(tmp_path)
    resolver.user = metadata
    node._handle_access('GPU-a', 101)
    assert node.status('GPU-a')['user'] == metadata
    assert not guard.sent


def test_docker_fresh_name_without_email_and_notfound():
    resolver, container = fake_docker({'ccc-user.name': NAME})
    resolver._cache = {'container': {'email': 'stale@example.org'}}
    assert resolver._extract_identity('container') == {'ccc-user.name': NAME}
    def missing(_):
        raise LookupError()
    resolver._docker.containers.get = missing
    assert resolver._extract_identity('container') is None


def test_docker_retains_container_names_without_extra_label_configuration():
    resolver, container = fake_docker({'ccc-user.name': NAME, 'name': 'fallback'})
    resolver.user_info_lables = None
    assert resolver._extract_identity('container') == container.labels


def test_stored_email_identity_must_be_canonical(tmp_path):
    cal = Calendar(tmp_path / 'calendar', ['GPU-a'])
    created = cal.create(record())
    with sqlite3.connect(cal.path) as db:
        db.execute('UPDATE reservations SET record=?', (json.dumps(dict(created, owner_email=' Alice@Example.org ')),))
    with pytest.raises(ReservationError) as error:
        cal.list()
    assert error.value.status == 503


def test_name_http_crud_restart_allocation_and_corruption():
    import tornado.testing

    class NameAPI(tornado.testing.AsyncTestCase):
        get_app = APIScenarios.get_app
        call = APIScenarios.call
        fetch = InProcessAPI.fetch

        def setUp(self):
            super().setUp()
            self._app = self.get_app()

        def runTest(self):
            auth = {'Authorization': 'Bearer secret'}
            value = payload(recurrence=dict(frequency='daily', until='2030-01-03', timezone='UTC'))
            assert self.call('/reserve', 'POST', dict(value, owner_type='email'), auth)[0] == 400
            code, created = self.call('/reserve', 'POST', value, auth)
            assert code == 201 and created['owner_email'] == '' and created['owner_type'] == 'name'
            self.node.calendar = self.cal = Calendar(self.cal.path, ['GPU-a', 'GPU-b'])
            assert self.call('/reservations') == (200, [created])
            self.node._resolver = lambda _: USER
            self.node._handle_access('GPU-a', 101)
            status = self.call('/status')[1]['GPU-a']
            assert status['claim']['user'] == USER
            assert status['active_reservation']['owner_type'] == status['next_reservation']['owner_type'] == 'name'
            self.node._claims = {key: Node.Claim(clock=self.node._clock) for key in self.node._claims}
            assert self.call('/request?gpu=2')[0] == 200
            self.node._claims = {key: Node.Claim(clock=self.node._clock) for key in self.node._claims}
            self.node._resolver = lambda _: {'email': 'bob@example.org', 'ccc-user.name': NAME}
            assert self.call('/request?gpu=2')[0] == 404
            assert self.call('/reservations/' + created['id'], 'PATCH', payload(note='edited'), auth)[0] == 200
            assert self.call('/unreserve', 'POST', {'id': created['id']}, auth)[0] == 200
            assert self.call('/reservations') == (200, [])
            _, created = self.call('/reserve', 'POST', payload(), auth)
            with sqlite3.connect(self.cal.path) as db:
                db.execute('UPDATE reservations SET record=?', (json.dumps(dict(created, owner_email='mixed@example.org')),))
            assert self.call('/reservations')[0] == 503
            assert self.call('/request?gpu=2')[0] == 503
            assert self.call('/status')[1]['GPU-a']['reservation_state'] == 'unavailable'

    result = NameAPI().run()
    assert result.wasSuccessful(), (result.errors, result.failures)


@pytest.mark.parametrize('snapshots', [[None], [(123, 0)], [(123, (1000, 0, 1000, 1000))],
                                       [(123, 1000), (124, 1000)], [(123, 1000)] * 3 + [(124, 1000)]])
def test_name_enforcement_preserves_process_safety(tmp_path, snapshots):
    node, cal, booking, resolver, guard, _ = fixture(tmp_path)
    cal.update(booking['id'], payload())
    resolver.user = {'ccc-user.name': 'other'}
    guard.snapshots = snapshots
    node._handle_access('GPU-a', 101)
    assert not guard.sent


def test_name_recurring_enforcement_gap_wait_and_cancellation(tmp_path):
    node, cal, booking, resolver, guard, now = fixture(tmp_path)
    cal.update(booking['id'], payload(recurrence=dict(frequency='daily', until='2030-01-03', timezone='UTC')))
    resolver.user = {'ccc-user.name': 'other'}
    now[0] = NOW + timedelta(days=1)
    node._handle_access('GPU-a', 101)
    node._handle_access('GPU-a', 101)
    assert len(guard.sent) == 2
    node._claims = {key: Node.Claim(clock=node._clock) for key in node._claims}
    pending = node.wait(resolver.user, {'gpu': 2})
    assert not pending.done()
    now[0] += timedelta(hours=1)
    node._process_pending()
    assert pending.done() and len(pending.result()) == 2
    node._handle_access('GPU-a', 101)
    assert len(guard.sent) == 2
    now[0] = NOW + timedelta(days=2)
    resolver.user = USER
    node._handle_access('GPU-a', 102)
    assert len(guard.sent) == 2
    cal.cancel(booking['id'])
    resolver.user = {'ccc-user.name': 'other'}
    node._handle_access('GPU-a', 101)
    assert len(guard.sent) == 2


def test_docker_label_race_never_resurrects_old_identity(tmp_path):
    node, cal, booking, _, guard, _ = fixture(tmp_path)
    cal.update(booking['id'], payload())
    resolver, container = fake_docker({'ccc-user.name': 'other'})
    calls = []
    def resolve(_):
        calls.append(1)
        if len(calls) == 2:
            container.labels = {}
        return resolver._extract_identity('container')
    node._resolver = resolve
    node._handle_access('GPU-a', 101)
    assert len(calls) == 2 and not guard.sent and guard.closed == [42]


@pytest.mark.parametrize('unresolved', [None, {}, '', 'unknown', []])
def test_only_missing_empty_or_nondict_resolver_payload_uses_unknown(tmp_path, unresolved):
    node, _, _, resolver, guard, _ = fixture(tmp_path)
    resolver.user = unresolved
    node._handle_access('GPU-a', 101)
    from patroller import UNKNOWN_USER
    assert node.status('GPU-a')['user'] == UNKNOWN_USER
    assert not guard.sent
