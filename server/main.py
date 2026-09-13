"""
server/main.py

FastAPI server exposing a /refresh endpoint that triggers the complete end-to-end pipeline:
  1. analyze-indices (RRG & Sector momentum)
  2. run (download -> scan -> breakout -> compute-metrics -> export)
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
import logging
import time
_log = logging.getLogger("server.refresh")


def _run_step(script_args: list[str]) -> tuple[int, str, str]:
    """
    Run a subprocess command, stream its output line-by-line to the server log,
    and return (returncode, stdout, stderr).

    PYTHONPATH is forwarded so all project-root imports resolve correctly
    regardless of the working directory the server was launched from.
    """
    import os as _os
    env = _os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (
        _os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else ""
    )

    proc = subprocess.Popen(
        script_args,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,   # line-buffered
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    # Stream stdout and stderr to logs in real time
    import threading

    def _drain(pipe, buf, log_fn):
        for line in pipe:
            line = line.rstrip("\n")
            buf.append(line)
            log_fn("%s", line)

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_lines, _log.info), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_lines, _log.warning), daemon=True)
    t_out.start()
    t_err.start()

    proc.wait()
    t_out.join()
    t_err.join()

    return proc.returncode, "\n".join(stdout_lines), "\n".join(stderr_lines)


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
            "cmd": [PYTHON, str(PROJECT_ROOT / "main.py"), "analyze-indices"],
        },
        {
            "name": "main_run",
            "cmd": [PYTHON, str(PROJECT_ROOT / "main.py"), "run"],
        },
    ]

    try:
        for step in steps:
            _log.info("Starting pipeline step: %s", step["name"])
            t0 = time.time()
            rc, stdout, stderr = _run_step(step["cmd"])
            elapsed = round(time.time() - t0, 1)

            step_result = {
                "name": step["name"],
                "returncode": rc,
                "elapsed_seconds": elapsed,
                "stdout": stdout[-3000:] if stdout else "",   # keep last 3k chars
                "stderr": stderr[-3000:] if stderr else "",
                "success": rc == 0,
            }
            refresh_status["steps"].append(step_result)
            _log.info(
                "Step '%s' finished in %.1fs with exit code %d.",
                step["name"], elapsed, rc,
            )

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
        _log.exception("Unhandled error in refresh pipeline: %s", exc)

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
# Crude Oil State Tracking
# ---------------------------------------------------------------------------
crude_status: dict[str, Any] = {
    "running": False,
    "last_run": None,
    "last_status": None,
    "last_error": None,
}


def _do_crude_refresh(init: bool = False, days: int = 30) -> None:
    """Run crude oil strategy refresh or init in the background."""
    crude_status["running"] = True
    crude_status["last_run"] = datetime.now().isoformat()
    crude_status["last_status"] = None
    crude_status["last_error"] = None

    try:
        from crude_oil import init_crude_oil_data, update_crude_oil_data, update_crude_oil_pcr
        if init:
            init_crude_oil_data(days=days)
        else:
            update_crude_oil_pcr()
            update_crude_oil_data()
        crude_status["last_status"] = "success"
    except Exception as exc:
        crude_status["last_status"] = "error"
        crude_status["last_error"] = str(exc)
    finally:
        crude_status["running"] = False


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
      1. python3 main.py analyze-indices
      2. python3 main.py run (download -> scan -> breakout -> compute-metrics -> export)

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


# ---------------------------------------------------------------------------
# Crude Oil Routes
# ---------------------------------------------------------------------------
@app.get("/crude-oil/status", tags=["Crude Oil"])
def get_crude_status_endpoint(limit: int = 10, pcr_limit: int = 50):
    """
    Return the latest strategy state, signals, PCR, PCR history, and breakout status for Crude Oil Mini from DB.
    By default returns the last 10 candles and last 50 PCR history entries (sorted descending, newest first).
    """
    try:
        from crude_oil import get_crude_oil_status
        return get_crude_oil_status(limit=limit, pcr_limit=pcr_limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load crude oil status: {exc}")



@app.post("/crude-oil/refresh", tags=["Crude Oil"])
def trigger_crude_refresh(background_tasks: BackgroundTasks):
    """
    Trigger an incremental refresh for Crude Oil Mini (fetches latest 5m candles, recomputes HA, UT Bot, Breakouts, and live PCR).
    """
    if crude_status["running"]:
        raise HTTPException(
            status_code=409,
            detail="A crude oil refresh is already running.",
        )

    started_at = datetime.now().isoformat()
    background_tasks.add_task(_do_crude_refresh, init=False)

    return {
        "message": "Crude oil refresh started in background.",
        "started_at": started_at,
    }


@app.post("/crude-oil/init", tags=["Crude Oil"])
def trigger_crude_init(background_tasks: BackgroundTasks, days: int = 30):
    """
    Initialize historical 5-minute candles (default 30 days / 1 month) for Crude Oil Mini.
    """
    if crude_status["running"]:
        raise HTTPException(
            status_code=409,
            detail="A crude oil operation is already running.",
        )

    started_at = datetime.now().isoformat()
    background_tasks.add_task(_do_crude_refresh, init=True, days=days)

    return {
        "message": f"Crude oil initialization for {days} days started in background.",
        "started_at": started_at,
    }


# ---------------------------------------------------------------------------
# FCM Token registration & Signal endpoints
# ---------------------------------------------------------------------------

class FCMTokenRequest(BaseModel):
    token: str
    label: str | None = None   # optional hint: "web", "ios", "android"


class FCMTokenResponse(BaseModel):
    token: str
    registered: bool           # True = newly inserted, False = already existed / reactivated
    message: str


class TestNotificationRequest(BaseModel):
    token: str                          # FCM token to push to
    signal: str = "STRONG_BUY"         # signal type to simulate
    pcr: float | None = None
    avg_pcr_3: float | None = None
    delta_pct: float | None = None


@app.post("/crude-oil/fcm-token", response_model=FCMTokenResponse, tags=["FCM Tokens"])
def register_fcm_token_endpoint(req: FCMTokenRequest):
    """
    Register a device FCM token for crude oil signal push notifications.
    If the token already exists and was previously deregistered, it is reactivated.
    """
    try:
        from crude_oil.db import register_fcm_token, init_db
        init_db()
        inserted = register_fcm_token(req.token, label=req.label)
        return FCMTokenResponse(
            token=req.token,
            registered=inserted,
            message="Token registered." if inserted else "Token already registered (reactivated).",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to register token: {exc}")


@app.delete("/crude-oil/fcm-token", tags=["FCM Tokens"])
def deregister_fcm_token_endpoint(req: FCMTokenRequest):
    """
    Deregister (soft-delete) a FCM token so it no longer receives push notifications.
    """
    try:
        from crude_oil.db import deregister_fcm_token
        found = deregister_fcm_token(req.token)
        if not found:
            raise HTTPException(status_code=404, detail="Token not found.")
        return {"message": "Token deregistered.", "token": req.token}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to deregister token: {exc}")


@app.get("/crude-oil/signal", tags=["FCM Tokens"])
def get_current_signal_endpoint():
    """
    Return the current PCR signal state.
    Fields: signal, pcr, avg_pcr_3, pcr_delta_pct, updated_at.
    """
    try:
        from crude_oil.db import get_signal_state, init_db
        init_db()
        state = get_signal_state()
        return state or {
            "signal": "NONE",
            "pcr": None,
            "avg_pcr_3": None,
            "pcr_delta_pct": None,
            "updated_at": None,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load signal state: {exc}")


@app.post("/crude-oil/notifications/test", tags=["FCM Tokens"])
def test_notification_endpoint(req: TestNotificationRequest):
    """
    Send a test FCM push notification to a **single** token.

    Does NOT register or store the token — purely for verifying that Firebase
    credentials are valid and the device token is reachable.

    Returns 502 with error details if the FCM push fails.
    """
    try:
        from crude_oil.signals import ALL_SIGNALS
        from crude_oil.notifications import send_pcr_signal_notification

        if req.signal not in ALL_SIGNALS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid signal '{req.signal}'. Must be one of: {list(ALL_SIGNALS)}",
            )

        result = send_pcr_signal_notification(
            signal=req.signal,
            tokens=[req.token],
            current_pcr=req.pcr,
            avg_pcr_3=req.avg_pcr_3,
            delta_pct=req.delta_pct,
        )

        if result.get("sent", 0) == 0:
            raise HTTPException(
                status_code=502,
                detail={"message": "FCM push failed or Firebase not configured.", "result": result},
            )

        return {"message": "Test notification sent successfully.", "result": result}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Notification test error: {exc}")
