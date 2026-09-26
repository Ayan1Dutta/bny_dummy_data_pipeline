.PHONY: install data pipeline full-refresh api ui test test-gate lint security security-gate drill incident-drill bg-status bg-down evidence docker jenkins-up jenkins-down

install:
	pip install -r requirements-dev.txt

data:
	python scripts/generate_sample_data.py

pipeline:
	python -m src.pipeline.run_pipeline

full-refresh:
	python -m src.pipeline.run_pipeline --full-refresh

api:
	uvicorn src.api.main:app --reload --port 8000

ui:
	python -m http.server 8080 --directory frontend

test:
	pytest --cov=src --cov-report=term-missing

test-gate:
	python -m scripts.run_test_gate

lint:
	ruff check src tests scripts deploy incident

security:
	bandit -r src -ll
	pip-audit -r requirements.txt

security-gate:
	python -m scripts.security_gate

drill:
	python -m scripts.run_drill

incident-drill:
	python -m scripts.run_drill --inject A

bg-status:
	python -m deploy.bluegreen status

bg-down:
	python -m deploy.bluegreen down

evidence:
	python -m scripts.collect_evidence

docker:
	docker compose up --build

jenkins-up:
	docker compose -f ci/jenkins/docker-compose.yml up -d --build

jenkins-down:
	docker compose -f ci/jenkins/docker-compose.yml down
