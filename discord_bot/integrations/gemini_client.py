from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("discord_digest_bot")

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - depends on deployed optional package
    genai = None
    genai_types = None


GEMINI_API_KEY = os.environ.get("GOOGLE_GENAI_API_KEY")

GEMINI_BUSY_MESSAGE = "SERN 目前忙線中，觀測世界線的人太多了。請稍後再試一次。"
GEMINI_RATE_LIMIT_MESSAGE = "SERN 收到的請求太多了，請稍後再試一次。"
GEMINI_TIMEOUT_MESSAGE = "SERN 的回應超時了，請稍後再試一次。"
GEMINI_ERROR_MESSAGE = "SERN 暫時無法連上 AI 服務，請稍後再試一次。"


def _error_status_code(exc: Exception) -> Optional[int]:
    """Best-effort extraction across google-genai error versions."""
    for value in (
        getattr(exc, "code", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def gemini_user_message(exc: Exception) -> Optional[str]:
    """Return a safe Discord message when an exception came from Gemini."""
    status_code = _error_status_code(exc)
    error_text = str(exc).upper()
    error_module = exc.__class__.__module__
    is_gemini_error = error_module.startswith("google.genai") or status_code is not None

    if not is_gemini_error:
        return None
    if status_code == 503 or "UNAVAILABLE" in error_text or "HIGH DEMAND" in error_text:
        return GEMINI_BUSY_MESSAGE
    if status_code == 429 or "RESOURCE_EXHAUSTED" in error_text:
        return GEMINI_RATE_LIMIT_MESSAGE
    if status_code == 504 or "DEADLINE_EXCEEDED" in error_text or "TIMEOUT" in error_text:
        return GEMINI_TIMEOUT_MESSAGE
    return GEMINI_ERROR_MESSAGE


class GeminiAsyncModel:
    """Small adapter that preserves the project's previous async model interface."""

    def __init__(self, client: Any, model_name: str) -> None:
        self.client = client
        self.model_name = model_name

    async def generate_content_async(self, contents: Any, generation_config: Optional[Any] = None) -> Any:
        kwargs = {
            "model": self.model_name,
            "contents": _normalize_contents(contents),
        }
        config = _normalize_generation_config(generation_config)
        if config is not None:
            kwargs["config"] = config
        return await self.client.aio.models.generate_content(**kwargs)


def _normalize_contents(contents: Any) -> Any:
    """Convert the legacy google-generativeai chat shape into a simple GenAI contents string."""
    if isinstance(contents, str):
        return contents
    if not isinstance(contents, list):
        return contents

    blocks = []
    for item in contents:
        if not isinstance(item, dict):
            blocks.append(str(item))
            continue

        role = str(item.get("role") or "user").strip().lower()
        label = "System" if role in {"model", "system"} else "User"
        parts = item.get("parts") or []
        if not isinstance(parts, list):
            parts = [parts]

        texts = []
        for part in parts:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and "text" in part:
                texts.append(str(part["text"]))
            else:
                texts.append(str(part))
        if texts:
            blocks.append(f"{label}:\n" + "\n".join(texts))

    return "\n\n".join(blocks)


def _normalize_generation_config(generation_config: Optional[Any]) -> Optional[Any]:
    if generation_config is None:
        return None

    temperature = None
    if isinstance(generation_config, dict):
        temperature = generation_config.get("temperature")
    else:
        temperature = getattr(generation_config, "temperature", None)

    if temperature is None:
        return generation_config

    if genai_types is not None and hasattr(genai_types, "GenerateContentConfig"):
        return genai_types.GenerateContentConfig(temperature=temperature)
    return {"temperature": temperature}


def _create_models() -> tuple[Optional[GeminiAsyncModel], Optional[GeminiAsyncModel]]:
    if not GEMINI_API_KEY:
        logger.warning("WARNING: GOOGLE_GENAI_API_KEY environment variable not set. Summarization will fail.")
        return None, None

    if genai is None:
        logger.error("google-genai package is not installed. Summarization might fail.")
        return None, None

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        summary_model = GeminiAsyncModel(client, "gemini-3-flash-preview")
        logger.info("Initialized Gemini Model: %s", summary_model.model_name)

        cloud_role_model = GeminiAsyncModel(client, "gemini-2.5-flash-lite")
        logger.info("Initialized Gemma Model: %s", cloud_role_model.model_name)
        return summary_model, cloud_role_model
    except Exception as exc:
        logger.error("Error initializing Gemini model: %s. Summarization might fail.", exc)
        return None, None


gemini_model, role_model = _create_models()
