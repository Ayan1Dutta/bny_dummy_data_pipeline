from __future__ import annotations

import json

from scripts import security_gate

FAKE_AWS_KEY = "AKIA" + "Q7XH2LMN4PRT6VWZ"
FAKE_DSN = "postgresql://settlement_admin:" + "Wint3rIsC0ming" + "@db.internal:5432/settlement"


def test_secret_scan_catches_a_cloud_access_key():
    hits = security_gate.scan_lines("infra/deploy.env", [f"AWS_ACCESS_KEY_ID={FAKE_AWS_KEY}"])
    assert "aws_access_key_id" in {h["rule"] for h in hits}


def test_secret_scan_catches_a_database_password_in_a_connection_string():
    hits = security_gate.scan_lines("src/settings.py", [f'WAREHOUSE_DSN = "{FAKE_DSN}"'])
    assert "password_in_connection_string" in {h["rule"] for h in hits}


def test_secret_scan_catches_a_hardcoded_password_literal():
    hits = security_gate.scan_lines("src/db.py", ['db_password = "' + "Pr0d-S3cret-99" + '"'])
    assert "credential_assignment" in {h["rule"] for h in hits}


def test_secret_scan_catches_a_private_key():
    hits = security_gate.scan_lines("deploy/key.pem", ["-----BEGIN " + "RSA PRIVATE KEY-----"])
    assert "private_key_block" in {h["rule"] for h in hits}


def test_secret_scan_never_prints_the_full_secret():
    hits = security_gate.scan_lines("infra/deploy.env", [f"AWS_ACCESS_KEY_ID={FAKE_AWS_KEY}"])
    assert all(FAKE_AWS_KEY not in h["evidence"] for h in hits)


def test_secret_scan_ignores_environment_lookups_and_placeholders():
    lines = [
        'api_key: str = os.getenv("API_KEY", "dev-local-key-change-me")',
        "API_KEY=replace-me-from-secrets-manager",
        "API_KEY: ${API_KEY:-dev-local-key-change-me}",
        'prefix = f"arn:aws:secretsmanager:{region}:{account}:secret:settlement/{env}"',
    ]
    assert security_gate.scan_lines("src/config.py", lines) == []


def test_sast_blocks_a_finding_that_was_never_triaged(tmp_path):
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"findings": []}), encoding="utf-8")
    result = security_gate.sast(triage)
    assert result["blocking"]
    assert any("not triaged" in f for f in result["blocking_findings"])


def test_sast_blocks_a_new_finding_above_the_triaged_count(tmp_path):
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"findings": [
        {"file": "src/ingestion/bronze.py", "rule": "B608", "max_count": 1, "justification": "x"},
        {"file": "src/transform/silver.py", "rule": "B608", "max_count": 99, "justification": "x"},
        {"file": "src/transform/gold.py", "rule": "B608", "max_count": 99, "justification": "x"},
    ]}), encoding="utf-8")
    result = security_gate.sast(triage)
    assert result["blocking"]
    assert any("bronze.py" in f and "new finding" in f for f in result["blocking_findings"])


def test_sast_passes_with_the_reviewed_triage():
    result = security_gate.sast(security_gate.ROOT / "security" / "sast_triage.json")
    assert not result["blocking"], result["blocking_findings"]
    assert result["counts_by_severity"].get("HIGH", 0) == 0
