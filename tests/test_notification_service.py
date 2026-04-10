import importlib
import sys
import types
import unittest
from unittest.mock import patch

from tests.support import install_discord_stub


class NotificationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_discord_stub()
        self.gmail_stub = types.ModuleType("discord_bot.integrations.gmail_gateway")
        self.gmail_stub.gmail_notify_enabled = lambda: False
        self.gmail_stub.send_sarn_notify = lambda record, to: "sarn-id"
        self.gmail_stub.send_error_notify = lambda error, record, to: "error-id"
        self.gmail_stub.send_deepfaker_notify = lambda record, to, subject: "deepfaker-id"

        self.forward_calls = []
        self.forwarder_stub = types.ModuleType("discord_bot.features.notifications.discord_forwarder")

        async def forward_notify_to_channel(**kwargs):
            self.forward_calls.append(kwargs)
            return True

        self.forwarder_stub.forward_notify_to_channel = forward_notify_to_channel

        self.original_modules = {
            "discord_bot.integrations.gmail_gateway": sys.modules.get("discord_bot.integrations.gmail_gateway"),
            "discord_bot.features.notifications.discord_forwarder": sys.modules.get("discord_bot.features.notifications.discord_forwarder"),
            "discord_bot.features.notifications.service": sys.modules.get("discord_bot.features.notifications.service"),
        }

        sys.modules["discord_bot.integrations.gmail_gateway"] = self.gmail_stub
        sys.modules["discord_bot.features.notifications.discord_forwarder"] = self.forwarder_stub
        sys.modules.pop("discord_bot.features.notifications.service", None)
        self.service_module = importlib.import_module("discord_bot.features.notifications.service")

    def tearDown(self):
        for name, module in self.original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    async def test_dispatch_without_gmail_still_forwards(self):
        await self.service_module.notification_service.dispatch(record={"command": "解答之書"})

        self.assertEqual(len(self.forward_calls), 1)
        self.assertIsNone(self.forward_calls[0]["email_sent"])
        self.assertEqual(self.forward_calls[0]["notify_type"], "success")

    async def test_dispatch_uses_success_mailer_when_enabled(self):
        self.gmail_stub.gmail_notify_enabled = lambda: True
        self.service_module = importlib.reload(self.service_module)

        with patch.dict("os.environ", {"GMAIL_SEND_TO": "demo@example.com"}, clear=False):
            await self.service_module.notification_service.dispatch(record={"command": "解答之書"})

        self.assertEqual(self.forward_calls[-1]["email_sent"], True)
        self.assertEqual(self.forward_calls[-1]["email_message_id"], "sarn-id")

    async def test_dispatch_uses_error_mailer_for_error_notifications(self):
        self.gmail_stub.gmail_notify_enabled = lambda: True
        self.service_module = importlib.reload(self.service_module)

        with patch.dict("os.environ", {"GMAIL_SEND_TO": "demo@example.com"}, clear=False):
            await self.service_module.notification_service.dispatch(
                record={"command": "解答之書"},
                error=RuntimeError("boom"),
            )

        self.assertEqual(self.forward_calls[-1]["notify_type"], "error")
        self.assertEqual(self.forward_calls[-1]["email_message_id"], "error-id")


if __name__ == "__main__":
    unittest.main()
