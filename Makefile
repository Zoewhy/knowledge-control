.PHONY: dev api worker web test lint smoke seed seed-business migrate backfill

dev:
	docker compose up --build

api:
	uvicorn app.main:app --app-dir backend --reload --host 0.0.0.0 --port 8000

worker:
	celery -A app.worker.celery_app worker --loglevel=INFO

web:
	cd frontend && npm run dev

test:
	pytest -q

lint:
	ruff check backend

seed:
	python -m app.scripts.seed

seed-business:
	PYTHONPATH=backend python -m app.scripts.production_seed --scale standard

smoke:
	python backend/scripts/smoke.py

migrate:
	alembic upgrade head

backfill:
	PYTHONPATH=backend python -m app.scripts.backfill_legacy
