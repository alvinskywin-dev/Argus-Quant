#!/bin/sh
# Container healthcheck — hits the FastAPI /health endpoint.
PORT="${DASHBOARD_PORT:-8010}"
curl --fail --silent --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null
