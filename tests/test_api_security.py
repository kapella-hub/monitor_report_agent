import importlib
import sys
from pathlib import Path

from fastapi import HTTPException

# Ensure app package importable
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def reload_app(monkeypatch):
    """Reload app modules with API token enabled and in-memory storage."""

    monkeypatch.setenv("API_TOKEN", "secret-token")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("LLM_PROVIDER", "stub")

    import app.config as config
    importlib.reload(config)
    import app.storage as storage_module
    importlib.reload(storage_module)
    import app.service as service
    importlib.reload(service)
    import app.main as main
    importlib.reload(main)

    return main, storage_module


def test_api_token_guard(monkeypatch):
    main, storage_module = reload_app(monkeypatch)

    try:
        main.require_api_key(None)
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("Expected HTTPException for missing API token")

    # Correct token should pass without raising
    assert main.require_api_key("secret-token") is None

    storage_module.storage.close()


def test_default_target_seeded(monkeypatch):
    main, storage_module = reload_app(monkeypatch)

    # Manually seed default target using the refreshed storage
    main._ensure_default_target()
    targets = storage_module.storage.list_targets()
    assert targets
    assert targets[0]["name"] == "local"

    storage_module.storage.close()
