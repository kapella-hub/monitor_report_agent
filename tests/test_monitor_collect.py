import asyncio
import importlib
import sys
from pathlib import Path


# Ensure app package importable
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def reload_app(monkeypatch):
    """Reload app modules with in-memory storage and no scheduler."""

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
    import app.schemas as schemas
    importlib.reload(schemas)

    return main, storage_module, service, schemas


def test_collect_monitor_inputs(monkeypatch):
    main, storage_module, service, schemas = reload_app(monkeypatch)

    # Seed default target
    main._ensure_default_target()
    target = storage_module.storage.list_targets()[0]

    monitor = schemas.PromptMonitor(
        name="collect-only-monitor",
        target_id=target["id"],
        interval_seconds=60,
        prompt="test",
        inputs=[
            schemas.MonitorInput(label="ECHO", mode="command", command="echo hello"),
        ],
    )

    storage_module.storage.create_monitor(monitor.model_dump())

    logs_text, success, total, _ = asyncio.run(
        service.collect_monitor_inputs(monitor.model_dump())
    )

    assert success == 1
    assert total == 1
    assert "hello" in logs_text

    storage_module.storage.close()


def test_collect_monitor_inputs_with_log_source(monkeypatch, tmp_path):
    main, storage_module, service, schemas = reload_app(monkeypatch)

    main._ensure_default_target()
    target = storage_module.storage.list_targets()[0]

    log_file = tmp_path / "example.log"
    log_file.write_text("source logs line\n")

    log_source = schemas.LogSource(
        target_id=target["id"],
        name="file-source",
        mode="file",
        config={"path": str(log_file)},
    )
    storage_module.storage.create_log_source(log_source.model_dump())

    monitor = schemas.PromptMonitor(
        name="collect-with-log-source",
        target_id=target["id"],
        interval_seconds=60,
        prompt="test",
        inputs=[
            schemas.MonitorInput(label="ECHO", mode="command", command="echo cmd"),
        ],
        log_source_id=log_source.id,
    )

    storage_module.storage.create_monitor(monitor.model_dump())

    logs_text, success, total, source = asyncio.run(
        service.collect_monitor_inputs(monitor.model_dump())
    )

    assert success == 2
    assert total == 2
    assert "[ECHO]" in logs_text
    assert "[file-source]" in logs_text
    assert "cmd" in logs_text
    assert "source logs line" in logs_text
    assert source["id"] == log_source.id

    storage_module.storage.close()
