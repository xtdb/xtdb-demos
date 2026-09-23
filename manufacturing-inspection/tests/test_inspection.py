import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.string import StrDumper

import app
import demo


@pytest.fixture
def dsn():
    return f"postgresql://xtdb@localhost:{os.environ.get('TEST_PORT', '5457')}/test_{uuid4().hex}"


def connect(dsn):
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    conn.adapters.register_dumper(str, StrDumper)
    return conn


@pytest.fixture
def conn(dsn):
    with connect(dsn) as conn:
        yield conn


def test_correction_changes_only_its_effective_interval_and_replays_every_original_row(conn):
    run = demo.create_run(conn)
    demo.release_batch(conn, run)
    before = demo.state(conn, run)
    assert len(before['current']) == 24
    assert len(before['releases']) == 19
    assert before['original'] == before['current']
    demo.correct_calibration(conn, run)
    after = demo.state(conn, run)
    assert after['original'] == before['current']
    assert after['releases'] == before['releases']
    assert after['release_basis'] == before['release_basis']
    assert len(after['review_ids']) == 5
    assert sum(p['assessment'] == 'pass' for p in after['current']) == 15
    for old, new in zip(before['current'], after['current']):
        assert old['pixel_width'] == new['pixel_width']
        inside = demo.CORRECTION_START <= new['inspected_at'] < demo.CORRECTION_END
        assert new['mm_per_pixel'] == (0.051 if inside else 0.050)
        if not inside:
            assert old == new
    part = after['current'][6]
    assert part['pixel_width'] == 201
    assert part['width_mm'] == pytest.approx(10.251)
    assert part['assessment'] == 'fail'
    assert after['original'][6]['width_mm'] == pytest.approx(10.050)
    assert [p['mm_per_pixel'] for p in after['timeline']] == [0.050, 0.051, 0.050]


def test_tolerance_boundaries_are_inclusive(conn):
    run = demo.create_run(conn)
    assessed = demo.state(conn, run)['current']
    for part in assessed:
        expected = 'pass' if 196 <= part['pixel_width'] <= 204 else 'fail'
        assert part['assessment'] == expected


def test_saved_basis_also_preserves_the_original_product_specification(conn):
    run = demo.create_run(conn)
    demo.release_batch(conn, run)
    before = demo.state(conn, run)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO product_spec (_id, _valid_from, min_mm, max_mm)
                       VALUES (%s, %s, 9.80, 10.00)""", (run + '/product', demo.START))
    after = demo.state(conn, run)
    assert after['original'] == before['original']
    assert after['current'][6]['assessment'] == 'fail'
    assert after['original'][6]['assessment'] == 'pass'


@pytest.mark.parametrize('table', ['calibration', 'product_spec'])
def test_missing_applicable_history_never_silently_drops_or_releases_parts(conn, table):
    run = demo.create_run(conn)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table} FOR ALL VALID_TIME")
    state = demo.state(conn, run)
    assert len(state['current']) == 24
    assert {p['assessment'] for p in state['current']} == {'unknown'}
    demo.release_batch(conn, run)
    assert demo.state(conn, run)['releases'] == []


def test_new_batch_does_not_change_previous_batch_or_reuse_its_correction(conn):
    first = demo.create_run(conn)
    demo.release_batch(conn, first)
    demo.correct_calibration(conn, first)
    previous = demo.state(conn, first)
    second = demo.create_run(conn)
    fresh = demo.state(conn, second)
    assert fresh['stage'] == 'inspected'
    assert fresh['releases'] == []
    assert {p['mm_per_pixel'] for p in fresh['current']} == {0.05}
    assert demo.state(conn, first)['current'] == previous['current']


def test_http_workflow_survives_reload_and_rejects_out_of_order_or_duplicate_actions(dsn, monkeypatch):
    monkeypatch.setattr(app, 'connect', lambda: connect(dsn))
    with TestClient(app.app) as client:
        assert client.get('/api/runs/missing').status_code == 404
        assert client.get('/').status_code == 200
        response = client.post('/api/runs')
        assert response.status_code == 200, response.text
        run = response.json()['run_id']
        assert client.post(f'/api/runs/{run}/correct').status_code == 409
        assert client.post(f'/api/runs/{run}/release').status_code == 200
        assert client.post(f'/api/runs/{run}/release').status_code == 409
        corrected = client.post(f'/api/runs/{run}/correct')
        assert corrected.status_code == 200, corrected.text
        assert len(corrected.json()['review_ids']) == 5
        assert client.post(f'/api/runs/{run}/correct').status_code == 409
    with TestClient(app.app) as client:
        restored = client.get(f'/api/runs/{run}')
        assert restored.json()['original'] == corrected.json()['original']
        assert restored.json()['releases'] == corrected.json()['releases']
