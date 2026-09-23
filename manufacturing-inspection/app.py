import logging
import os
from pathlib import Path
from threading import Lock

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.string import StrDumper

import demo

app = FastAPI(title="Manufacturing inspection with XTDB")
write_lock = Lock()


def connect():
    conn = psycopg.connect(os.environ.get("XTDB_DSN", "postgresql://xtdb@localhost:5456/xtdb"),
                           autocommit=True, connect_timeout=5)
    conn.adapters.register_dumper(str, StrDumper)
    return conn


@app.exception_handler(psycopg.Error)
async def database_error(request, exc):
    logging.exception("XTDB request failed", exc_info=exc)
    return JSONResponse(status_code=503, content={
        "detail": "XTDB is unavailable or could not complete the query. Check the application logs."})


@app.exception_handler(demo.StageConflict)
async def stage_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(demo.RunNotFound)
async def missing_run(request, exc):
    return JSONResponse(status_code=404, content={"detail": "This batch was not found. Run a new batch."})


@app.post("/api/runs")
def create_run():
    with write_lock, connect() as conn:
        return demo.state(conn, demo.create_run(conn))


@app.get("/api/runs/{run_id}")
def get_state(run_id: str):
    with write_lock, connect() as conn:
        return demo.state(conn, run_id)


@app.post("/api/runs/{run_id}/{action}")
def action(run_id: str, action: str):
    operations = {"release": demo.release_batch, "correct": demo.correct_calibration}
    if action not in operations:
        raise HTTPException(404, "Unknown action")
    with write_lock, connect() as conn:
        operations[action](conn, run_id)
        return demo.state(conn, run_id)


app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="ui")
