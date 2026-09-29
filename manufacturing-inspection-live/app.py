import logging
import os
from pathlib import Path
from threading import Lock

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.string import StrDumper
from pydantic import BaseModel, Field

import demo

app = FastAPI(title="Production line: calibration investigation")
action_lock = Lock()


def connect():
    conn = psycopg.connect(os.environ.get("XTDB_DSN", "postgresql://xtdb@localhost:5458/xtdb"),
                           autocommit=True, connect_timeout=5)
    conn.adapters.register_dumper(str, StrDumper)
    return conn


@app.exception_handler(psycopg.Error)
async def database_error(request, exc):
    logging.exception("XTDB request failed", exc_info=exc)
    return JSONResponse(status_code=503, content={"detail": "Could not reach XTDB or complete the query. Retry after checking the app logs."})


@app.exception_handler(demo.Conflict)
async def conflict(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(demo.NotFound)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


class Tick(BaseModel):
    expected_sequence: int = Field(ge=0)


@app.post("/api/runs")
def create():
    with action_lock, connect() as conn:
        return demo.state(conn, demo.create_run(conn))


@app.get("/api/runs/{run_id}")
def state(run_id: str):
    with action_lock, connect() as conn:
        return demo.state(conn, run_id)


@app.post("/api/runs/{run_id}/tick")
def tick(run_id: str, body: Tick):
    with action_lock, connect() as conn:
        demo.tick(conn, run_id, body.expected_sequence)
        return demo.state(conn, run_id)


@app.get("/api/runs/{run_id}/parts/{part_number}/replay")
def replay(run_id: str, part_number: int):
    with action_lock, connect() as conn:
        return demo.replay(conn, run_id, part_number)


@app.post("/api/runs/{run_id}/{action}")
def action(run_id: str, action: str):
    operations = {"report": demo.report, "reassess": demo.reassess, "contain": demo.contain}
    if action not in operations:
        raise HTTPException(404, "Unknown action")
    with action_lock, connect() as conn:
        demo.run_record(conn, run_id)
        operations[action](conn, run_id)
        return demo.state(conn, run_id)


app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="ui")
