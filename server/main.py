"""
server/main.py

FastAPI server exposing a /refresh endpoint that triggers the daily data pipeline:
  1. python3 analyze_indices.py
  2. python3 main.py run
"""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Stock Screener API",
    description="Exposes endpoints to trigger the daily data refresh pipeline.",
    version="1.0.0",
)

# CORS — allow all origins so the frontend can call this freely.
# Restrict `allow_origins` to your frontend URL in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# State tracking (in-memory, sufficient for a single-worker server)
# ---------------------------------------------------------------------------
refresh_status: dict[str, Any] = {
    "running": False,
    "last_run": None,
    "last_status": None,  # "success" | "error"
    "last_error": None,
    "steps": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run_step(script_args: list[str]) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        script_args,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _do_refresh() -> None:
    """Run the full refresh pipeline in the background."""
    refresh_status["running"] = True
    refresh_status["last_run"] = datetime.now().isoformat()
    refresh_status["last_status"] = None
    refresh_status["last_error"] = None
    refresh_status["steps"] = []

    steps = [
        {
            "name": "analyze_indices",
            "cmd": [PYTHON, str(PROJECT_ROOT / "analyze_indices.py")],
        },
        {
            "name": "main_run",
            "cmd": [PYTHON, str(PROJECT_ROOT / "main.py"), "run"],
        },
    ]

    try:
        for step in steps:
            rc, stdout, stderr = _run_step(step["cmd"])
            step_result = {
                "name": step["name"],
                "returncode": rc,
                "stdout": stdout[-3000:] if stdout else "",   # keep last 3k chars
                "stderr": stderr[-3000:] if stderr else "",
                "success": rc == 0,
            }
            refresh_status["steps"].append(step_result)

            if rc != 0:
                refresh_status["last_status"] = "error"
                refresh_status["last_error"] = (
                    f"Step '{step['name']}' failed with exit code {rc}.\n"
                    f"stderr: {stderr[-1000:]}"
                )
                return

        refresh_status["last_status"] = "success"

    except Exception as exc:
        refresh_status["last_status"] = "error"
        refresh_status["last_error"] = str(exc)

    finally:
        refresh_status["running"] = False


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
class RefreshResponse(BaseModel):
    message: str
    started_at: str


class StatusResponse(BaseModel):
    running: bool
    last_run: str | None
    last_status: str | None
    last_error: str | None
    steps: list[dict]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", tags=["Health"])
def root():
    """Health check."""
    return {"status": "ok", "service": "stock-screener-api"}


@app.post("/refresh", response_model=RefreshResponse, tags=["Pipeline"])
def trigger_refresh(background_tasks: BackgroundTasks):
    """
    Trigger the daily data refresh pipeline:
      1. python3 analyze_indices.py
      2. python3 main.py run

    The pipeline runs in the background. Poll /refresh/status to track progress.
    """
    if refresh_status["running"]:
        raise HTTPException(
            status_code=409,
            detail="A refresh is already running. Check /refresh/status for progress.",
        )

    started_at = datetime.now().isoformat()
    background_tasks.add_task(_do_refresh)

    return RefreshResponse(
        message="Refresh pipeline started in the background.",
        started_at=started_at,
    )


@app.get("/refresh/status", response_model=StatusResponse, tags=["Pipeline"])
def get_refresh_status():
    """
    Get the current status of the refresh pipeline.
    """
    return StatusResponse(**refresh_status)
