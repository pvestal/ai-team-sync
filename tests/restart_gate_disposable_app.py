"""A DISPOSABLE service for the #3580 acceptance tests -- never production.

Runs under a transient `systemd-run --user` unit so `ats safe-restart` restarts
a real systemd service, and mounts the anime-studio in-flight middleware loaded
from a file path (INFLIGHT_PY), so the service half of the gate under test is
the real module, not a copy.

    python restart_gate_disposable_app.py <port>

Env: INFLIGHT_PY (path to packages/core/inflight.py), ATS_DRAIN_TOKEN,
JOBS_FILE (JSON object of active-job counts; fault injection writes it).
"""

from __future__ import annotations

import asyncio
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


inflight = _load_inflight()
app = FastAPI()


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
    path = os.environ.get("JOBS_FILE", "")
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


inflight.tracker.set_active_jobs_provider(_jobs)
app.add_middleware(inflight.InflightMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
