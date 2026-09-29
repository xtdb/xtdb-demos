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


def test_reviews_expose_both_kinds_of_model_error(conn):
    run = demo.create_run(conn)
    initial = demo.state(conn, run)
    assert initial['metrics']['reviewed'] == 4
    assert initial['metrics']['defects'] == 1
    assert initial['metrics']['agreement_pct'] == 100
    assert initial['metrics']['unreviewed'] == 8
    demo.review_part(conn, run, 6, False)
    after_fp = demo.state(conn, run)['metrics']
    assert after_fp['false_positive'] == 1
    assert after_fp['defects'] == 1
    assert after_fp['agreement_pct'] == 80
    demo.review_part(conn, run, 5, True)
    after_fn = demo.state(conn, run)['metrics']
    assert after_fn['false_negative'] == 1
    assert after_fn['defects'] == 2
    demo.complete_reviews(conn, run)
    final = demo.state(conn, run)
    assert final['metrics'] == {
        'total':12, 'reviewed':12, 'unreviewed':0, 'defects':3, 'acceptable':9,
        'true_positive':2, 'true_negative':7, 'false_positive':2, 'false_negative':1,
        'agreement_pct':75, 'defect_pct':25, 'model_rejected':4,
    }
    replay = demo.replay(conn, run, initial['reports'][0]['_id'])
    assert replay['metrics'] == initial['metrics']
    assert replay['parts'] == initial['parts']
    immutable = ['part_id','predicted_defective','defect_score','model_version','line_action']
    assert [[p[k] for k in immutable] for p in final['parts']] == [[p[k] for k in immutable] for p in initial['parts']]


def test_review_revisions_preserve_saved_report(conn):
    run = demo.create_run(conn)
    demo.complete_reviews(conn, run)
    report = demo.save_report(conn, run)
    before = demo.replay(conn, run, report)
    demo.review_part(conn, run, 5, False)
    current = demo.state(conn, run)
    assert current['metrics']['defects'] == 2
    assert current['metrics']['false_negative'] == 0
    assert current['metrics']['agreement_pct'] == 83.3
    assert demo.replay(conn, run, report) == before
    with conn.cursor() as cur:
        cur.execute('SELECT defective FROM review FOR ALL SYSTEM_TIME WHERE _id=%s',(run+'/5',))
        assert {row[0] for row in cur.fetchall()} == {True,False}


def test_scripted_completion_preserves_user_judgements(conn):
    run = demo.create_run(conn)
    demo.review_part(conn, run, 6, True)
    demo.complete_reviews(conn, run)
    part = next(p for p in demo.state(conn, run)['parts'] if p['number'] == 6)
    assert part['reviewed_defective'] is True
    assert part['review_source'] == 'Your inspection'
    before = demo.state(conn, run)['parts']
    demo.complete_reviews(conn, run)
    assert demo.state(conn, run)['parts'] == before


def test_batches_and_reports_are_isolated(conn):
    first, second = demo.create_run(conn), demo.create_run(conn)
    report = demo.state(conn, first)['reports'][0]['_id']
    demo.complete_reviews(conn, first)
    assert demo.state(conn, second)['metrics']['reviewed'] == 4
    with pytest.raises(demo.NotFound): demo.replay(conn, second, report)
    with pytest.raises(demo.NotFound): demo.review_part(conn, first, 999, True)
    with pytest.raises(demo.NotFound): demo.state(conn, 'missing')


def test_api_walkthrough(conn, monkeypatch):
    from fastapi.testclient import TestClient
    import app
    monkeypatch.setenv('XTDB_DSN',os.environ['XTDB_TEST_DSN'])
    with TestClient(app.app) as client:
        assert client.get('/').status_code == 200
        created = client.post('/api/runs').json()
        base = '/api/runs/'+created['run_id']
        assert client.post(base+'/parts/6/review',json={'defective':False}).json()['metrics']['false_positive'] == 1
        assert client.post(base+'/parts/5/review',json={'defective':True}).json()['metrics']['false_negative'] == 1
        assert client.post(base+'/complete').json()['metrics']['agreement_pct'] == 75
        saved = client.post(base+'/reports').json()['reports'][-1]['_id']
        client.post(base+'/parts/5/review',json={'defective':False})
        assert client.get(base+'/reports/'+saved).json()['metrics']['agreement_pct'] == 75
        assert client.get(base).json()['metrics']['false_negative'] == 0
        assert client.post(base+'/parts/5/review',json={'defective':'yes'}).status_code == 422
        assert client.get('/api/runs/missing').status_code == 404
