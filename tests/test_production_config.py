from __future__ import annotations

import pytest

from src.config import Settings

STRONG_KEY = "k" * 32
STRONG_SALT = "s" * 24


@pytest.mark.parametrize("env", ["test", "prod"])
def test_protected_environments_refuse_placeholder_secrets(env):
    with pytest.raises(ValueError, match="Refusing to start"):
        Settings(env=env, api_key="dev-local-key-change-me", pii_salt="dev-salt-change-me")


def test_production_refuses_missing_secrets():
    with pytest.raises(ValueError, match="API_KEY"):
        Settings(env="prod", api_key="", pii_salt=STRONG_SALT)
    with pytest.raises(ValueError, match="PII_SALT"):
        Settings(env="prod", api_key=STRONG_KEY, pii_salt="")


def test_production_refuses_short_secrets():
    with pytest.raises(ValueError):
        Settings(env="prod", api_key="short", pii_salt=STRONG_SALT)


def test_production_refuses_disabled_authentication():
    with pytest.raises(ValueError, match="API_KEYS_ENABLED"):
        Settings(env="prod", api_key=STRONG_KEY, pii_salt=STRONG_SALT, api_keys_enabled=False)


def test_production_starts_with_real_secrets():
    assert Settings(env="prod", api_key=STRONG_KEY, pii_salt=STRONG_SALT).env == "prod"


def test_local_development_still_works_with_placeholders():
    assert Settings(env="dev", api_key="dev-local-key-change-me", pii_salt="dev-salt-change-me").env == "dev"


def test_refusal_message_never_echoes_the_secret_value():
    secret = "not-long-enough-secret"
    with pytest.raises(ValueError) as exc:
        Settings(env="prod", api_key=secret, pii_salt=STRONG_SALT)
    assert secret not in str(exc.value)
