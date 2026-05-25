from datetime import datetime, timezone, timedelta

_TZ_ACST = timezone(timedelta(hours=9, minutes=30))


def _make_fake_db(rows):
    class _FakeDB:
        def execute_query(self, sql, params=None, fetch=False):
            return rows
    return _FakeDB()


def test_mobile_sync_auth(client):
    resp = client.get('/schedule/api/mobile-sync')
    assert resp.status_code == 401
    assert 'error' in resp.json


def test_mobile_sync_success(client, schedule_auth_headers, monkeypatch):
    with client.session_transaction() as sess:
        sess['schedule_authenticated'] = True

    import admin.blueprints.schedule.api_routes as _api
    _api._mobile_sync_cache_invalidate()

    row = {
        "id": "evt1",
        "start_time": datetime(2026, 5, 10, 10, 0, tzinfo=_TZ_ACST),
        "end_time": datetime(2026, 5, 10, 11, 0, tzinfo=_TZ_ACST),
        "client_name": "Test Client",
        "phone": "+61400000001",
        "duration": "1 hour",
        "type": "incall",
        "experience": "GFE",
        "preferences": [],
        "deposit_status": "not_required",
        "deposit_amount": 0,
        "deposit_reference": None,
        "status": "confirmed",
        "special_requests": None,
        "organise_other_escort": False,
        "notes": "",
        "price_total": None,
        "remaining_amount": None,
        "outcall_address": None,
    }
    monkeypatch.setattr(
        'admin.blueprints.schedule.api_routes.get_shared_db',
        lambda _url: _make_fake_db([row]),
    )
    resp = client.get('/schedule/api/mobile-sync', headers=schedule_auth_headers)
    assert resp.status_code == 200
    data = resp.json
    assert 'bookings' in data
    assert isinstance(data['bookings'], list)
    assert data['bookings'][0]['event_id'] == 'evt1'
    assert data['bookings'][0]['summary'] == 'Test Client — GFE'
    assert data['bookings'][0]['start'] == '2026-05-10T10:00:00+09:30'
    assert data['bookings'][0]['end'] == '2026-05-10T11:00:00+09:30'
    assert data['bookings'][0]['description'] == ''
    assert data['bookings'][0]['color_id'] == '10'


def test_mobile_sync_date_filter(client, schedule_auth_headers, monkeypatch):
    with client.session_transaction() as sess:
        sess['schedule_authenticated'] = True

    import admin.blueprints.schedule.api_routes as _api
    _api._mobile_sync_cache_invalidate()

    monkeypatch.setattr(
        'admin.blueprints.schedule.api_routes.get_shared_db',
        lambda _url: _make_fake_db([]),
    )
    resp = client.get('/schedule/api/mobile-sync?start_date=2026-05-10&end_date=2026-05-12', headers=schedule_auth_headers)
    assert resp.status_code == 200
    assert 'bookings' in resp.json
    assert resp.json['bookings'] == []


def test_mobile_sync_invalid_date(client, schedule_auth_headers):
    with client.session_transaction() as sess:
        sess['schedule_authenticated'] = True
    resp = client.get('/schedule/api/mobile-sync?start_date=bad-date', headers=schedule_auth_headers)
    assert resp.status_code == 400
    assert 'error' in resp.json
