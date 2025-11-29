import json
import random
from typing import Any, Dict

SYSTEM_MESSAGE = (
    "You are a monitoring assistant. You review log lines and decide whether the system is ok, "
    "in warning state, or in alert. Respond using strict JSON as instructed."
)


async def analyze_logs_with_llm(monitor_prompt: str, logs_text: str) -> Dict[str, Any]:
    """
    Placeholder LLM call.

    In production, replace this stub with a real LLM client (OpenAI, Anthropic, local model, etc.).
    Keep the JSON contract so downstream consumers continue to function.

    Logs are grouped by labels in the input text, e.g.:
    [FULL_LOGS]
    ...
    [ERRORS]
    ...
    """
    # Very small heuristic: if "error" appears, trigger alert; if "warn", set warn; otherwise ok.
    lowered = logs_text.lower()
    if "error" in lowered or "exception" in lowered:
        status = "alert"
    elif "warn" in lowered:
        status = "warn"
    else:
        status = random.choice(["ok", "ok", "warn"])  # bias toward ok

    response = {
        "status": status,
        "summary": f"Stub evaluation for prompt '{monitor_prompt[:30]}...'.",
        "details": "Replace analyze_logs_with_llm with a real LLM integration for production use.",
        "suggested_actions": [
            "Implement a real LLM provider.",
            "Tune prompts and thresholds based on production behavior.",
        ],
    }

    # Normally you would serialize / deserialize JSON to enforce the contract.
    json.dumps(response)
    return response
