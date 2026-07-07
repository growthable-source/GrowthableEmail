# GrowthableEmail — GHL ↔ Resend pipeline

FastAPI + Supabase service that pulls audiences from GoHighLevel, renders React Email
templates, dispatches via Resend, writes events back to GHL, and keeps a canonical
suppression list. Spec: docs/spec.md. Runbook: see bottom of this file.

## Dev setup
    uv sync
    (cd emails && npm install)
    docker start growthable-test-pg || docker run -d --name growthable-test-pg \
      -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16
    uv run pytest
