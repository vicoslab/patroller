"""Owner names are optional display metadata, never an identity."""
import json
import sqlite3
from datetime import timedelta

import pytest

from patroller.reservations import Calendar, ReservationError
from test_reservations import NOW, record
from test_reservation_enforcement import fixture


def calendar(tmp_path):
    return Calendar(str(tmp_path / 'calendar.sqlite'), ['GPU-a', 'GPU-b'])


@pytest.mark.parametrize('name', [None, 1, True, [], {}, 'x' * 201, 'a\x00b', 'a\x1fb', 'a\x7fb', '\tAlice'])
def test_invalid_name_rejected(tmp_path, name):
    with pytest.raises(ReservationError) as error:
        calendar(tmp_path).create(record(owner_name=name))
    assert error.value.status == 400


def test_name_lifecycle_and_recurring_occurrences(tmp_path):
    cal = calendar(tmp_path)
    payload = record(owner_name='  Alice Example  ', recurrence={
        'frequency': 'daily', 'until': '2030-01-03', 'timezone': 'UTC'})
    created = cal.create(payload)
    assert created['owner_name'] == 'Alice Example'
    assert Calendar(cal.path, ['GPU-a', 'GPU-b']).list() == [created]
    active, upcoming = cal.active_next('GPU-a', NOW + timedelta(days=1, minutes=1))
    assert active['owner_name'] == upcoming['owner_name'] == 'Alice Example'
    payload['owner_name'] = 'x' * 200
    assert cal.update(created['id'], payload)['owner_name'] == 'x' * 200
    payload['owner_name'] = '  '
    assert 'owner_name' not in cal.update(created['id'], payload)
    payload.pop('owner_name')
    assert 'owner_name' not in cal.update(created['id'], payload)


def test_old_row_bytes_unchanged_after_read_and_restart(tmp_path):
    cal = calendar(tmp_path)
    created = cal.create(record())
    with sqlite3.connect(cal.path) as db:
        before = db.execute('SELECT record FROM reservations').fetchone()[0]
    assert 'owner_name' not in Calendar(cal.path, ['GPU-a', 'GPU-b']).list()[0]
    cal.active_next('GPU-a', NOW)
    with sqlite3.connect(cal.path) as db:
        assert db.execute('SELECT record FROM reservations').fetchone()[0] == before
    assert 'owner_name' not in created


@pytest.mark.parametrize('name', [None, '', ' Alice ', 'x' * 201, 'bad\x7f'])
def test_corrupt_stored_name_fails_closed(tmp_path, name):
    cal = calendar(tmp_path)
    cal.create(record())
    with sqlite3.connect(cal.path) as db:
        raw = json.loads(db.execute('SELECT record FROM reservations').fetchone()[0])
        raw['owner_name'] = name
        db.execute('UPDATE reservations SET record = ?', (json.dumps(raw),))
    for operation in (cal.list, lambda: cal.active_next('GPU-a', NOW)):
        with pytest.raises(ReservationError) as error:
            operation()
        assert error.value.status == 503


def test_name_does_not_grant_process_exemption(tmp_path):
    node, cal, booking, resolver, guard, _ = fixture(tmp_path)
    cal.update(booking['id'], record(owner_name='bob@example.org'))
    node._handle_access('GPU-a', 101)
    assert guard.sent == [(101, 42)]
    resolver.user = {'email': 'alice@example.org'}
    node._handle_access('GPU-a', 102)
    assert guard.sent == [(101, 42)]


def test_http_owner_name_round_trip_and_errors(tmp_path):
    # Use the existing in-memory HTTP harness: real routing without a socket.
    import tornado.testing
    from test_reservation_http import APIScenarios, TestInProcessAPI

    class OwnerAPI(tornado.testing.AsyncTestCase):
        get_app = APIScenarios.get_app
        call = APIScenarios.call
        fetch = TestInProcessAPI.fetch

        def setUp(self):
            super().setUp()
            self._app = self.get_app()

        def runTest(self):
            auth = {'Authorization': 'Bearer secret'}
            assert self.call('/reserve', 'POST', record(owner_name=None), auth)[0] == 400
            code, created = self.call('/reserve', 'POST', record(owner_name=' Alice '), auth)
            assert code == 201 and created['owner_name'] == 'Alice'
            assert self.call('/reservations')[1][0]['owner_name'] == 'Alice'
            assert self.call('/status')[1]['GPU-a']['active_reservation']['owner_name'] == 'Alice'
            code, updated = self.call('/reservations/' + created['id'], 'PATCH', record(), auth)
            assert code == 200 and 'owner_name' not in updated
            with sqlite3.connect(self.cal.path) as db:
                raw = json.loads(db.execute('SELECT record FROM reservations').fetchone()[0])
                raw['owner_name'] = 'bad\x7f'
                db.execute('UPDATE reservations SET record = ?', (json.dumps(raw),))
            assert self.call('/reservations')[0] == 503

    result = OwnerAPI().run()
    assert result.wasSuccessful(), (result.errors, result.failures)
