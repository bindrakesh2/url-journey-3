#!/bin/bash

# Start Gunicorn with a single Uvicorn worker and a long timeout
gunicorn -w 1 -k uvicorn.workers.UvicornWorker main:app --bind "0.0.0.0:$PORT" --timeout 120
