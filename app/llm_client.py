import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Protocol

from .config import settings

logger = logging.getLogger(__name__)

SUPPORTED_LLM_PROVIDERS = {"openai", "amazon_q", "stub", "dummy", "mock"}


class LLMClient(Protocol):
    async def analyze_logs(
        self, monitor_prompt: str, logs_text: str, *, provider_metadata: dict | None = None
    ) -> dict:
        """
        Analyze logs according to the monitor_prompt and grouped logs_text.

        Returns a dict shaped like:
        {
            "status": "HEALTHY" | "WARNING" | "CRITICAL",
            "summary": "short summary",
            "report": "full human-readable report",
            "recommendations": ["list", "of", "actions"],
        }
        """


@dataclass
class OpenAIClient:
    api_key: str
    model: str
    max_chars: int

    async def analyze_logs(
        self,
        monitor_prompt: str,
        logs_text: str,
        *,
        provider_metadata: dict | None = None,
    ) -> dict:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("openai package is required for OpenAI provider") from exc

        client = AsyncOpenAI(api_key=self.api_key)
        model_override = (provider_metadata or {}).get("model") if isinstance(provider_metadata, dict) else None
        truncated_logs = logs_text[-self.max_chars :] if self.max_chars and len(logs_text) > self.max_chars else logs_text
        system_message = (
            "You are an AI log monitoring assistant. You receive: 1) A monitoring prompt with detailed instructions. "
            "2) Aggregated logs grouped by labels like [FULL_LOGS], [GRID_HEALTH], [BUDGET], [ERRORS]."
            "Use the monitoring instructions and labels to classify system health."
        )
        user_message = (
            "MONITORING PROMPT:\n"
            f"{monitor_prompt}\n\n"
            "LOGS TO ANALYZE:\n```text\n"
            f"{truncated_logs}\n```\n\n"
            "Follow the monitoring instructions above. Respond ONLY in JSON with the following shape:\n"
            "{\n"
            '"status": "HEALTHY" | "WARNING" | "CRITICAL",\n'
            '"summary": "short overall summary (1-3 sentences)",\n'
            '"report": "full multi-section human-readable report",\n'
            '"recommendations": ["bullet", "points", "of", "actions"]\n'
            "}"
        )

        response = await client.responses.create(
            model=model_override or self.model,
            input=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message},
            ],
        )

        content = response.output[0].content[0].text if hasattr(response, "output") else response.choices[0].message.content
        return _parse_llm_json(content)


@dataclass
class AmazonQClient:
    app_id: str
    region: str
    max_chars: int

    async def analyze_logs(
        self,
        monitor_prompt: str,
        logs_text: str,
        *,
        provider_metadata: dict | None = None,
    ) -> dict:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("boto3 is required for Amazon Q provider") from exc

        meta = provider_metadata or {}
        app_id = meta.get("application_id") or meta.get("app_id") or self.app_id
        region = meta.get("region") or self.region
        truncated_logs = logs_text[-self.max_chars :] if self.max_chars and len(logs_text) > self.max_chars else logs_text
        prompt = (
            "You are an AI log monitoring assistant. Review the monitoring prompt and the labeled log blocks. "
            "Respond with JSON containing status, summary, report, and recommendations.\n\n"
            f"MONITORING PROMPT:\n{monitor_prompt}\n\n"
            "LOGS TO ANALYZE:\n"
            f"{truncated_logs}"
        )

        def _call_q() -> dict:
            client = boto3.client("qbusiness", region_name=region)
            result = client.chat_sync(
                applicationId=app_id,
                userMessage=prompt,
            )
            return result

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _call_q)
        message = _extract_q_content(result)
        return _parse_llm_json(message)


@dataclass
class StubLLMClient:
    """Deterministic offline client useful for development and tests."""

    max_chars: int

    async def analyze_logs(
        self, monitor_prompt: str, logs_text: str, *, provider_metadata: dict | None = None
    ) -> dict:
        truncated_logs = logs_text[-self.max_chars :] if self.max_chars and len(logs_text) > self.max_chars else logs_text
        lowered = truncated_logs.lower()
        if any(token in lowered for token in ("critical", "traceback", "exception")):
            status = "CRITICAL"
            summary = "Critical signals detected in logs"
        elif "warn" in lowered or "warning" in lowered:
            status = "WARNING"
            summary = "Warnings detected in logs"
        else:
            status = "HEALTHY"
            summary = "Logs look healthy"

        return {
            "status": status,
            "summary": summary,
            "report": f"Prompt: {monitor_prompt[:200]}...\n\nLogs (truncated):\n{truncated_logs[:5000]}",
            "recommendations": ["Replace stub provider with a real LLM for production analysis."],
        }


def _extract_q_content(result: Dict[str, Any]) -> str:
    outputs = result.get("output", []) if isinstance(result, dict) else []
    if outputs:
        text = outputs[0].get("text", {})
        if text:
            return text.get("content", "")
    return ""


def _parse_llm_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("LLM response not valid JSON; wrapping in error payload")
        return {
            "status": "CRITICAL",
            "summary": "LLM response parsing failed",
            "report": raw,
            "recommendations": ["Review LLM output format"]
        }


_llm_clients: dict[str, LLMClient] = {}


def supported_llm_providers() -> list[str]:
    return sorted(SUPPORTED_LLM_PROVIDERS)


def get_llm_client(provider: str | None = None) -> LLMClient:
    """Return a cached client for the requested provider (defaults to settings)."""

    provider = (provider or settings.llm_provider or "openai").lower()
    if provider in _llm_clients:
        return _llm_clients[provider]

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY must be set for OpenAI provider")
        _llm_clients[provider] = OpenAIClient(
            api_key=settings.openai_api_key,
            model=settings.openai_model or "gpt-4.1-mini",
            max_chars=settings.llm_max_chars,
        )
    elif provider == "amazon_q":
        if not settings.qbusiness_app_id or not settings.aws_region:
            raise RuntimeError("QBUSINESS_APP_ID and AWS_REGION are required for Amazon Q provider")
        _llm_clients[provider] = AmazonQClient(
            app_id=settings.qbusiness_app_id,
            region=settings.aws_region,
            max_chars=settings.llm_max_chars,
        )
    elif provider in {"stub", "dummy", "mock"}:
        _llm_clients[provider] = StubLLMClient(max_chars=settings.llm_max_chars)
    else:
        raise RuntimeError(f"Unsupported LLM provider: {provider}")

    return _llm_clients[provider]


# When adding new providers, implement LLMClient and extend get_llm_client above.


def validate_llm_provider_config(provider: str | None = None) -> dict:
    """Return readiness details for the configured provider without making API calls."""

    normalized = (provider or settings.llm_provider or "openai").lower()
    if normalized not in SUPPORTED_LLM_PROVIDERS:
        return {
            "provider": normalized,
            "ready": False,
            "message": f"Unsupported LLM provider: {normalized}",
            "supported": supported_llm_providers(),
        }

    if normalized == "openai":
        if not settings.openai_api_key:
            return {
                "provider": normalized,
                "ready": False,
                "message": "Missing OPENAI_API_KEY",
                "supported": supported_llm_providers(),
            }
        return {
            "provider": normalized,
            "ready": True,
            "message": "OpenAI configured",
            "supported": supported_llm_providers(),
        }

    if normalized == "amazon_q":
        if not settings.qbusiness_app_id or not settings.aws_region:
            return {
                "provider": normalized,
                "ready": False,
                "message": "Missing QBUSINESS_APP_ID or AWS_REGION",
                "supported": supported_llm_providers(),
            }
        return {
            "provider": normalized,
            "ready": True,
            "message": "Amazon Q configured",
            "supported": supported_llm_providers(),
        }

    # Stub-like providers never require credentials
    return {
        "provider": normalized,
        "ready": True,
        "message": "Stub provider ready",
        "supported": supported_llm_providers(),
    }
