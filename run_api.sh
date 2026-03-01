#!/bin/bash
# Start the Takeoff Python FastAPI backend (port 8001)
# The Next.js frontend proxies to this via TAKEOFF_API_URL
uvicorn takeoff.api:app --host 0.0.0.0 --port 8001 --reload
