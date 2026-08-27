.PHONY: install migrate run test lint typecheck ingest ingest-catalogs labels

install:
	pip install -e ".[dev]"

migrate:
	alembic upgrade head

run:
	uvicorn praman.main:app --reload --app-dir api

test:
	pytest -q

lint:
	ruff check api tests scripts
	ruff format --check api tests scripts

typecheck:
	mypy api/praman

# Runs the messy raw/ inputs (scraped HTML, inconsistent-unit CSV, photos)
# through the live LLM extraction pipeline (CLAUDE.md Phase 2 acceptance).
ingest:
	python -m praman.ingest.pipeline raw api/praman/seed/raw --out /tmp/praman_raw_ingest.json

# Rebuilds the two committed seed catalogs from their master CSVs. Not run
# automatically — the committed catalog_*.json files are the source of
# truth for the demo; re-run only when the master CSVs change.
ingest-catalogs:
	python -m praman.ingest.pipeline catalog api/praman/seed/masters/grocery_master.csv --out api/praman/seed/catalog_grocery.json
	python -m praman.ingest.pipeline catalog api/praman/seed/masters/jewellery_master.csv --out api/praman/seed/catalog_jewellery.json

# Regenerates harness/labels.json (the 60 hand-labeled carts). Not run
# automatically — the committed labels.json is the frozen ground truth the
# Phase 9 harness measures against; re-running this after seeing accuracy
# numbers would defeat the point (CLAUDE.md §5).
labels:
	python scripts/gen_labels.py
