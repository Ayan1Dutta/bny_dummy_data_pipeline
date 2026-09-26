from __future__ import annotations

import pytest

STAGES = {
    "unit": "Unit tests: individual transformation and validation rules",
    "data_model": "Data model tests: grain, referential integrity, reconciliation",
    "pipeline": "Pipeline tests: end-to-end runs, idempotency, incremental loads, gates",
    "api": "API tests: status codes, contract, latency",
    "business": "Business rule tests: settlement rate, gap, SLA, exceptions",
    "security": "Security tests: injection, PII, secrets, production configuration",
}

STAGE_BY_FILE = {
    "test_transformations.py": "unit",
    "test_data_model.py": "data_model",
    "test_pipeline_production.py": "pipeline",
    "test_bluegreen_config.py": "pipeline",
    "test_api_contract.py": "api",
    "test_performance.py": "api",
    "test_business_rules.py": "business",
    "test_security.py": "security",
    "test_production_config.py": "security",
    "test_security_gate.py": "security",
}

STAGE_BY_TEST_NAME = {
    "idempotent": "pipeline",
    "incremental": "pipeline",
    "reopens_a_closed_day": "pipeline",
}


def pytest_configure(config: pytest.Config) -> None:
    for name, description in STAGES.items():
        config.addinivalue_line("markers", f"{name}: {description}")


def stage_for(item: pytest.Item) -> str:
    for fragment, stage in STAGE_BY_TEST_NAME.items():
        if fragment in item.name:
            return stage
    return STAGE_BY_FILE.get(item.path.name, "unit")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(getattr(pytest.mark, stage_for(item)))


@pytest.fixture(autouse=True)
def _fresh_rate_limit_window():
    from src.api.security import reset_rate_limits

    reset_rate_limits()
    yield
