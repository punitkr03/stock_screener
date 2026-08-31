# Stock Screener — API Server

A lightweight FastAPI server that exposes a `/refresh` endpoint to trigger the daily data pipeline.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check |
| `POST` | `/refresh` | Start the refresh pipeline in the background |
| `GET` | `/refresh/status` | Poll the current pipeline status |

Interactive docs available at `http://localhost:8000/docs`.

## Pipeline steps (in order)

1. `python3 main.py analyze-indices` (RRG sector/index metrics)
2. `python3 main.py run` (End-to-end stock pipeline):
   - Ingestion (`download --recent`)
   - Screening (`scan`)
   - Breakout confirmation (`breakout`)
   - Fundamental & solvency analytics (`compute-metrics` via `analytics/processor.py`)
   - Export (`export` → JSON watchlists & MongoDB sync)

## Setup & run

```bash
# From the repo root
pip install -r server/requirements.txt

# Start the server (development)
uvicorn server.main:app --reload --port 8000

# Or run directly from the server/ directory
cd server
uvicorn main:app --reload --port 8000
```

## Calling from the frontend

```js
// Trigger refresh
const res = await fetch("http://localhost:8000/refresh", { method: "POST" });
const data = await res.json();
// { message: "...", started_at: "..." }

// Poll status
const status = await fetch("http://localhost:8000/refresh/status").then(r => r.json());
// { running: true/false, last_status: "success"|"error"|null, ... }
```

## Notes

- A `409 Conflict` is returned if a refresh is already in progress.
- CORS is open to all origins (`*`). Restrict `allow_origins` in production.
