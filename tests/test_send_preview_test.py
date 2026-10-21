"""Offline tests for the standalone sender; never read real credentials."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools import send_preview_test as tool


class PayloadTests(unittest.TestCase):
    def test_extra_layouts(self):
        for layout in tool.EXTRA_LAYOUTS:
            with self.subTest(layout=layout):
                payload = tool.build_payload(layout, "https://example.com/test.gif",
                                             "測試", "如題", "https://example.com")
                self.assertEqual(payload["allowed_mentions"], {"parse": []})
                if layout in ("author-icon", "footer-icon"):
                    key = "author" if layout == "author-icon" else "footer"
                    self.assertEqual(payload["embeds"][0][key]["icon_url"], "https://example.com/test.gif")
                    self.assertNotIn("thumbnail", payload["embeds"][0])
                else:
                    self.assertEqual(payload["flags"], 32768)
                    if layout == "v2-section":
                        self.assertEqual(payload["components"][0]["type"], 9)
                    else:
                        accessory = payload["components"][0]["components"][0]["accessory"]
                        self.assertTrue(accessory["spoiler"])

    def test_layouts_and_mentions(self):
        for layout in tool.LAYOUTS:
            with self.subTest(layout=layout):
                payload = tool.build_payload(layout, "https://example.com/test.gif",
                                             "測試", "@everyone", "https://example.com")
                self.assertEqual(payload["allowed_mentions"], {"parse": []})
                self.assertNotIn("username", payload)
                if layout.startswith("v2"):
                    self.assertEqual(payload["flags"], 32768)
                    self.assertNotIn("content", payload)
                    self.assertNotIn("embeds", payload)
                    children = payload["components"][0]["components"]
                    if layout == "v2-thumbnail":
                        self.assertEqual(children[0]["type"], 9)
                        self.assertEqual(children[0]["accessory"]["type"], 11)
                    else:
                        self.assertEqual(children[1]["type"], 12)
                else:
                    self.assertEqual(payload["embeds"][0][layout]["url"], "https://example.com/test.gif")

    def test_dry_run_does_not_load_settings_or_send(self):
        with patch.object(tool, "load_settings") as load, patch.object(tool, "send_payloads") as send:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(tool.main(["--gif-url", "https://example.com/test.gif", "--layout", "all"]), 0)
            load.assert_not_called()
            send.assert_not_called()
            self.assertIn("v2-thumbnail", output.getvalue())

    def test_local_attachment_reference(self):
        with tempfile.TemporaryDirectory(dir=tool.ROOT) as tmp:
            path = Path(tmp) / "test.gif"
            path.write_bytes(b"GIF89a" + b"\x00" * 12)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(tool.main(["--gif-file", str(path)]), 0)
            payload = json.loads(output.getvalue().split("\n離線預覽")[0])[0]
            self.assertEqual(payload["attachments"], [{"id": 0, "filename": "preview.gif"}])
            self.assertEqual(payload["embeds"][0]["thumbnail"]["url"], "attachment://preview.gif")
            path.write_bytes(b"not a gif")
            with self.assertRaises(tool.TestSendError):
                tool.load_gif(path)

    def test_error_does_not_disclose_exception(self):
        with patch.object(tool, "load_settings", side_effect=RuntimeError("secret-marker")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(tool.main(["--gif-url", "https://example.com/test.gif", "--send"]), 1)
            self.assertNotIn("secret-marker", output.getvalue())


class SettingsTests(unittest.TestCase):
    def test_precedence_and_explicit_empty(self):
        with tempfile.TemporaryDirectory(dir=tool.ROOT) as tmp:
            root = Path(tmp)
            (root / "discord_bot").mkdir()
            (root / ".env").write_text("DISCORD_BOT_TOKEN=root-token\nDISCORD_NOTIFY_FORWARD_CHANNEL_ID=111\n", encoding="utf-8")
            (root / "discord_bot" / ".env").write_text("DISCORD_BOT_TOKEN=fallback-token\nDISCORD_NOTIFY_FORWARD_GUILD_ID=222\n", encoding="utf-8")
            with patch.object(tool, "ROOT", root), patch.dict(tool.os.environ, {"DISCORD_BOT_TOKEN": "env-token"}, clear=True):
                settings = tool.load_settings()
                self.assertEqual(settings[tool.KEYS[0]], "env-token")
                self.assertEqual(settings[tool.KEYS[1]], "111")
                self.assertEqual(settings[tool.KEYS[2]], "222")
                tool.os.environ[tool.KEYS[0]] = ""
                with self.assertRaises(tool.TestSendError):
                    tool.load_settings()


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_guild_never_posts(self):
        settings = dict(zip(tool.KEYS, ("fake-token", "111", "222")))
        request = AsyncMock(return_value={"guild_id": "333"})
        with patch("aiohttp.ClientSession") as factory, patch.object(tool, "api_request", request):
            factory.return_value.__aenter__.return_value = MagicMock()
            with self.assertRaises(tool.TestSendError):
                await tool.send_payloads(settings, [{}], None)
            self.assertEqual(request.await_count, 1)
            self.assertEqual(request.await_args.args[1], "GET")

    async def test_one_post_and_safe_link(self):
        settings = dict(zip(tool.KEYS, ("fake-token", "111", "222")))
        request = AsyncMock(side_effect=[{"guild_id": "222"}, {"id": "444"}])
        payload = tool.build_payload("thumbnail", "https://example.com/a.gif", "測試", "如題", "https://example.com")
        with patch("aiohttp.ClientSession"), patch.object(tool, "api_request", request):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                await tool.send_payloads(settings, [payload], None)
            self.assertEqual(request.await_count, 2)
            self.assertEqual(request.await_args.kwargs["json"], payload)
            self.assertIn("/222/111/444", output.getvalue())
            self.assertNotIn("fake-token", output.getvalue())

    async def test_attachment_uses_multipart(self):
        settings = dict(zip(tool.KEYS, ("fake-token", "111", "222")))
        request = AsyncMock(side_effect=[{"guild_id": "222"}, {"id": "444"}])
        with patch("aiohttp.ClientSession"), patch.object(tool, "api_request", request):
            with contextlib.redirect_stdout(io.StringIO()):
                await tool.send_payloads(settings, [{"attachments": [{"id": 0, "filename": "preview.gif"}]}], b"GIF89a")
            self.assertIn("data", request.await_args.kwargs)
            self.assertNotIn("json", request.await_args.kwargs)

    async def test_failed_post_not_retried(self):
        settings = dict(zip(tool.KEYS, ("fake-token", "111", "222")))
        request = AsyncMock(side_effect=[{"guild_id": "222"}, tool.TestSendError("Discord HTTP 500，已停止。")])
        with patch("aiohttp.ClientSession"), patch.object(tool, "api_request", request):
            with self.assertRaises(tool.TestSendError):
                await tool.send_payloads(settings, [{}, {}], None)
            self.assertEqual(request.await_count, 2)


if __name__ == "__main__":
    unittest.main()
