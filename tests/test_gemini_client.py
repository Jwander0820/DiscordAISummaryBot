import importlib
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


class FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeModels:
    def __init__(self):
        self.generate_content = AsyncMock(return_value=types.SimpleNamespace(text="ok"))


class FakeAio:
    def __init__(self):
        self.models = FakeModels()


class FakeClient:
    instances = []

    def __init__(self, *, api_key):
        self.api_key = api_key
        self.aio = FakeAio()
        FakeClient.instances.append(self)


class GeminiClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_modules = {
            "google": sys.modules.get("google"),
            "google.genai": sys.modules.get("google.genai"),
            "google.genai.types": sys.modules.get("google.genai.types"),
            "discord_bot.integrations.gemini_client": sys.modules.get("discord_bot.integrations.gemini_client"),
        }
        FakeClient.instances = []

        google_stub = types.ModuleType("google")
        genai_stub = types.ModuleType("google.genai")
        types_stub = types.ModuleType("google.genai.types")
        genai_stub.Client = FakeClient
        types_stub.GenerateContentConfig = FakeGenerateContentConfig
        genai_stub.types = types_stub
        google_stub.genai = genai_stub

        sys.modules["google"] = google_stub
        sys.modules["google.genai"] = genai_stub
        sys.modules["google.genai.types"] = types_stub
        sys.modules.pop("discord_bot.integrations.gemini_client", None)

    def tearDown(self):
        for name, module in self.original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_create_models_uses_google_genai_client(self):
        with patch.dict(os.environ, {"GOOGLE_GENAI_API_KEY": "test-key"}, clear=False):
            module = importlib.import_module("discord_bot.integrations.gemini_client")

        self.assertEqual(FakeClient.instances[0].api_key, "test-key")
        self.assertEqual(module.gemini_model.model_name, "gemini-3-flash-preview")
        self.assertEqual(module.role_model.model_name, "gemini-2.5-flash-lite")

    async def test_adapter_normalizes_legacy_contents_and_generation_config(self):
        with patch.dict(os.environ, {"GOOGLE_GENAI_API_KEY": "test-key"}, clear=False):
            module = importlib.import_module("discord_bot.integrations.gemini_client")

        response = await module.gemini_model.generate_content_async(
            contents=[
                {"role": "model", "parts": ["system prompt"]},
                {"role": "user", "parts": ["user prompt"]},
            ],
            generation_config={"temperature": 0.7},
        )

        self.assertEqual(response.text, "ok")
        call_kwargs = FakeClient.instances[0].aio.models.generate_content.await_args.kwargs
        self.assertEqual(call_kwargs["model"], "gemini-3-flash-preview")
        self.assertIn("System:\nsystem prompt", call_kwargs["contents"])
        self.assertIn("User:\nuser prompt", call_kwargs["contents"])
        self.assertEqual(call_kwargs["config"].kwargs, {"temperature": 0.7})

    def test_missing_api_key_disables_models(self):
        with patch.dict(os.environ, {}, clear=True):
            module = importlib.import_module("discord_bot.integrations.gemini_client")

        self.assertIsNone(module.gemini_model)
        self.assertIsNone(module.role_model)


if __name__ == "__main__":
    unittest.main()
