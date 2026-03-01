#!/bin/bash
set -e
# Start the Takeoff Python FastAPI backend (port 8001)
# The Next.js frontend proxies to this via TAKEOFF_API_URL
# 0.0.0.0 is intentional: allows access from host when running in Docker or VM
uvicorn takeoff.api:app --host 0.0.0.0 --port 8001
