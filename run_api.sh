#!/bin/bash
set -e
# Start the Takeoff Python FastAPI backend (port 8001)
# The Next.js frontend proxies to this via TAKEOFF_API_URL
# 0.0.0.0 is intentional: allows access from host when running in Docker or VM
command -v uvicorn >/dev/null 2>&1 || { echo "Error: uvicorn not found. Run: pip install -r takeoff/requirements.txt"; exit 1; }
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Error: ANTHROPIC_API_KEY is not set. Export it or add it to a .env file."
  exit 1
fi
uvicorn takeoff.api:app --host 0.0.0.0 --port 8001
