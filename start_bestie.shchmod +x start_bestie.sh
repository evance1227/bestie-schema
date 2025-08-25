#!/bin/bash
# Start Bestie stack with pm2

# Navigate to project folder
cd ~/bestie-backend

# API
pm2 start "source .venv/bin/activate && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env" --name bestie-api

# Worker
pm2 start "source .venv/bin/activate && rq worker bestie_queue" --name bestie-worker

# Ngrok
pm2 start "ngrok http --domain=dingo-enough-stinkbug.ngrok.app 8000" --name bestie-ngrok

# Save so pm2 remembers
pm2 save


