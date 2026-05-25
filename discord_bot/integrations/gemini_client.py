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
