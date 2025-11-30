import json
import sys
from pathlib import Path

# Ensure app package is importable when running from repository root
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import llm_client, service


def test_truncate_output_limits_lines_and_chars():
    text = "\n".join(str(i) for i in range(100))
    window = {"max_lines": 10, "max_chars": 15}
    truncated = service._truncate_output(text, window)
    # Should keep the tail of the log and respect the char cap
    assert truncated.endswith("99")
    assert len(truncated.splitlines()) <= 10
    assert len(truncated) <= window["max_chars"]


def test_map_llm_status_normalizes_values():
    assert service._map_llm_status("healthy") == "ok"
    assert service._map_llm_status("WARNING") == "warn"
    assert service._map_llm_status("critical") == "alert"
    # Unknown status should fall back to error
    assert service._map_llm_status("unexpected") == "error"


def test_merge_provider_metadata_overrides_defaults():
    defaults = {"model": "gpt-default", "region": "us-east-1", "keep": "x"}
    merged = service._merge_provider_metadata({"model": "custom", "region": None}, defaults)
    assert merged["model"] == "custom"
    # None values are dropped rather than overriding defaults
    assert "region" not in merged
    assert merged["keep"] == "x"


def test_parse_llm_json_invalid_wraps_warning():
    raw = "not json"
    parsed = llm_client._parse_llm_json(raw)
    assert parsed["status"] == "CRITICAL"
    assert parsed["report"] == raw
    assert "recommendations" in parsed


def test_truncate_storage_keeps_tail():
    text = "A" * 10 + "B" * 10
    truncated = service._truncate_storage(text, 8)
    assert truncated == "B" * 8

