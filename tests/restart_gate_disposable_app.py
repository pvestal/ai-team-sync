"""A DISPOSABLE service for the #3580 acceptance tests -- never production.

Runs under a transient `systemd-run --user` unit so `ats safe-restart` restarts
a real systemd service, and mounts the anime-studio in-flight middleware loaded
from a file path (INFLIGHT_PY), so the service half of the gate under test is
the real module, not a copy.

    python restart_gate_disposable_app.py <port>
    python -m uvicorn restart_gate_disposable_app:app --workers 2 ...   (multi-worker)

Env (all fault-injection knobs):
  INFLIGHT_PY      path to anime-studio packages/core/inflight.py
  ATS_DRAIN_TOKEN  the drain token
  JOBS_FILE        JSON written by the test; the active-jobs provider returns it
  MIDDLEWARE_FLAG  if set, the middleware is mounted only when this file exists
                   (models the FIRST load the bootstrap path exists for)
  SLOW_STOP_S      seconds the lifespan shutdown sleeps (a slow `systemctl restart`)
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import sys

import uvicorn
from fastapi import FastAPI


def _load_inflight():
    spec = importlib.util.spec_from_file_location("as_inflight_under_test",
                                                  os.environ["INFLIGHT_PY"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.asynccontextmanager
async def lifespan(app):
    yield
    await asyncio.sleep(float(os.environ.get("SLOW_STOP_S", "0") or 0))


app = FastAPI(lifespan=lifespan)


@app.post("/work")
async def work(seconds: float = 1.0):
    await asyncio.sleep(seconds)
    return {"slept": seconds, "pid": os.getpid()}


@app.post("/write")
async def write():
    return {"written": True, "pid": os.getpid()}


@app.get("/health")
async def health():
    return {"ok": True, "pid": os.getpid()}


async def _jobs() -> dict:
    with open(os.environ["JOBS_FILE"]) as f:
        return json.load(f)


_flag = os.environ.get("MIDDLEWARE_FLAG", "")
if not _flag or os.path.exists(_flag):
    inflight = _load_inflight()
    inflight.tracker.set_active_jobs_provider(_jobs)
    app.add_middleware(inflight.InflightMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
