#!/bin/bash
# Simple starter for local use
export FLASK_DEBUG=0
export PORT=${PORT:-5000}
# Uncomment and set these for production:
# export ADMIN_PASSWORD="change-me"
# export SECRET_KEY="change-me-to-something-long-and-random"
python3 app.py
