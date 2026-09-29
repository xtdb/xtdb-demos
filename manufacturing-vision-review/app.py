import logging
import os
from pathlib import Path
from threading import Lock

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.string import StrDumper
from pydantic import BaseModel, StrictBool

import demo

app = FastAPI(title='Vision inspection: production quality and model accuracy')
action_lock = Lock()


def connect():
    conn = psycopg.connect(os.environ.get('XTDB_DSN', 'postgresql://xtdb@localhost:5460/xtdb'),
                           autocommit=True, connect_timeout=5)
    conn.adapters.register_dumper(str, StrDumper)
    return conn


@app.exception_handler(psycopg.Error)
async def database_error(request, exc):
    logging.exception('XTDB request failed', exc_info=exc)
    return JSONResponse(status_code=503, content={'detail':'Could not complete the database query. Check the app logs and retry.'})


@app.exception_handler(demo.NotFound)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={'detail':str(exc)})


class Review(BaseModel):
    defective: StrictBool


@app.post('/api/runs')
def create():
    with action_lock, connect() as conn:
        return demo.state(conn, demo.create_run(conn))


@app.get('/api/runs/{run_id}')
def state(run_id: str):
    with action_lock, connect() as conn:
        return demo.state(conn, run_id)


@app.post('/api/runs/{run_id}/parts/{number}/review')
def review(run_id: str, number: int, body: Review):
    with action_lock, connect() as conn:
        demo.review_part(conn, run_id, number, body.defective)
        return demo.state(conn, run_id)


@app.post('/api/runs/{run_id}/complete')
def complete(run_id: str):
    with action_lock, connect() as conn:
        demo.complete_reviews(conn, run_id)
        return demo.state(conn, run_id)


@app.post('/api/runs/{run_id}/reports')
def save_report(run_id: str):
    with action_lock, connect() as conn:
        demo.save_report(conn, run_id)
        return demo.state(conn, run_id)


@app.get('/api/runs/{run_id}/reports/{report_id}')
def replay(run_id: str, report_id: str):
    with action_lock, connect() as conn:
        return demo.replay(conn, run_id, report_id)


app.mount('/', StaticFiles(directory=Path(__file__).with_name('static'), html=True), name='ui')
