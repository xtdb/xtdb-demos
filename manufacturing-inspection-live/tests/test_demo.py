import os

import psycopg
import pytest
from psycopg.types.string import StrDumper

import demo


@pytest.fixture
def conn():
    dsn = os.environ.get('XTDB_TEST_DSN')
    if not dsn:
        pytest.fail('Set XTDB_TEST_DSN to the separate test playground')
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.adapters.register_dumper(str, StrDumper)
        yield connection


def test_investigation_preserves_original_and_records_new_holds(conn):
    run = demo.create_run(conn)
    original = demo.optional_rows(conn, 'inspection_decision', run)
    assert len(original) == 18
    demo.tick(conn, run, 18)
    demo.report(conn, run)
    reported = demo.state(conn, run)
    assert len(reported['candidates']) == 12
    assert all(p['group'] == 'candidate' for p in reported['candidates'])
    demo.reassess(conn, run)
    assessed = demo.state(conn, run)
    assert {key: assessed['counts'][key] for key in ['review','now_passes','unchanged']} == {'review':5,'now_passes':1,'unchanged':6}
    assert assessed['control']['assessment'] == 'pass'
    assert float(assessed['control']['width_mm']) == 10.05
    replay = demo.replay(conn, run, 7)
    assert float(replay['original']['width_mm']) == 10.05
    assert float(replay['current']['width_mm']) == 10.251
    assert replay['original']['assessment'] == 'pass'
    assert replay['current']['assessment'] == 'fail'
    demo.contain(conn, run)
    contained = demo.state(conn, run)
    assert contained['counts']['quality_holds'] == 5
    assert all(p['disposition'] == 'quality_hold' for p in contained['candidates'] if p['group'] == 'review')
    assert all(p['disposition'] == 'held' for p in contained['candidates'] if p['group'] == 'now_passes')
    after = {d['_id']:d for d in demo.optional_rows(conn, 'inspection_decision', run)}
    assert all(after[d['_id']] == d for d in original)
    demo.tick(conn, run, 19)
    assert demo.state(conn, run)['counts']['inspected'] == 20
    assert demo.replay(conn, run, 7)['original'] == replay['original']


def test_actions_and_ticks_are_idempotent_and_ordered(conn):
    run = demo.create_run(conn)
    with pytest.raises(demo.Conflict): demo.reassess(conn, run)
    with pytest.raises(demo.Conflict): demo.contain(conn, run)
    demo.tick(conn, run, 18)
    demo.tick(conn, run, 18)
    assert demo.state(conn, run)['counts']['inspected'] == 19
    demo.report(conn, run)
    demo.report(conn, run)
    assert len(demo.optional_rows(conn, 'incident', run)) == 1
    with pytest.raises(demo.Conflict): demo.contain(conn, run)
    demo.reassess(conn, run)
    demo.reassess(conn, run)
    demo.contain(conn, run)
    demo.contain(conn, run)
    assert len(demo.optional_rows(conn, 'stock_hold', run)) == 5


def test_shifts_are_isolated(conn):
    first, second = demo.create_run(conn), demo.create_run(conn)
    demo.report(conn, first)
    demo.reassess(conn, first)
    demo.contain(conn, first)
    untouched = demo.state(conn, second)
    assert untouched['incident'] is None
    assert untouched['counts']['quality_holds'] == 0
    assert demo.replay(conn, second, 7)['current']['assessment'] == 'pass'
    with pytest.raises(demo.NotFound): demo.run_record(conn, 'nonexistent')
    with pytest.raises(demo.NotFound): demo.replay(conn, first, 999)


def test_replay_uses_original_product_spec_too(conn):
    run = demo.create_run(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute('INSERT INTO product_spec (_id, _valid_from, min_mm, max_mm) VALUES (%s, %s, 9.80, 10.00)', (run+'/product',demo.START))
    replay = demo.replay(conn, run, 7)
    assert replay['original']['assessment'] == 'pass'
    assert replay['current']['assessment'] == 'fail'
    assert float(replay['original']['max_mm']) == 10.2


def test_missing_calibration_never_releases_a_part(conn):
    run = demo.create_run(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute('DELETE FROM calibration FOR ALL VALID_TIME WHERE _id = %s', (run+'/camera',))
    demo.tick(conn, run, 18)
    latest = demo.state(conn, run)['recent'][-1]
    assert latest['assessment'] == 'unknown'
    assert latest['decision']['released'] is False
    assert latest['disposition'] == 'held'


def test_recovers_inspection_without_decision(conn):
    run = demo.create_run(conn)
    with conn.transaction(), conn.cursor() as cur:
        demo.write_inspection(cur, run, 18)
        cur.execute('UPDATE demo_run SET next_sequence = 19 WHERE _id = %s',(run,))
    demo.report(conn, run)
    decisions = demo.optional_rows(conn, 'inspection_decision', run)
    assert len(decisions) == 19


def test_api_walkthrough_and_validation(conn, monkeypatch):
    from fastapi.testclient import TestClient
    import app
    monkeypatch.setenv('XTDB_DSN', os.environ['XTDB_TEST_DSN'])
    with TestClient(app.app) as client:
        assert client.get('/').status_code == 200
        created = client.post('/api/runs')
        assert created.status_code == 200
        run = created.json()['run_id']
        base = f'/api/runs/{run}'
        assert client.post(base+'/contain').status_code == 409
        assert client.post(base+'/tick', json={'expected_sequence':-1}).status_code == 422
        assert client.post(base+'/tick', json={'expected_sequence':18}).json()['counts']['inspected'] == 19
        assert client.post(base+'/report').json()['incident']['stage'] == 'reported'
        assert client.post(base+'/reassess').json()['counts']['review'] == 5
        assert client.post(base+'/contain').json()['counts']['quality_holds'] == 5
        assert client.get(base+'/parts/7/replay').json()['original']['assessment'] == 'pass'
        assert client.get(base).json()['incident']['stage'] == 'contained'
        assert client.get('/api/runs/does-not-exist').status_code == 404
        assert client.post(base+'/unknown').status_code == 404
