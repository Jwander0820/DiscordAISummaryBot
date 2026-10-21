import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from tests.support import isolated_discord_suite

from discord_bot.cogs import social_duplicate_cog as cog_module
from discord_bot.features.social_preview import duplicate_patrol as patrol
from discord_bot.features.social_preview import facebook_preview, instagram_preview, threads_preview
from discord_bot.features.social_preview.duplicate_service import DuplicateMatch
from discord_bot.features.social_preview.patrol_records import PatrolDelivery
from discord_bot.db.social_duplicate_repository import ShareEvent

load_tests = isolated_discord_suite(__name__)


def fake_audit():
    return SimpleNamespace(insert_event=Mock(return_value=True), finish_event=Mock(return_value=True), backend="sqlite")


def previous():
    return ShareEvent("1", "10", "threads", "100", "40", "Demo001",
                      "https://www.threads.com/@demo/post/Demo001", "1100", 1.0)


def message():
    return SimpleNamespace(id=101, guild=SimpleNamespace(id=1),
                           channel=SimpleNamespace(id=10, send=AsyncMock()),
                           author=SimpleNamespace(id=42, bot=False), content="", reply=AsyncMock())


def interaction(*, guild=True, manager=True):
    return SimpleNamespace(guild=SimpleNamespace(id=1) if guild else None,
                           user=SimpleNamespace(id=42, guild_permissions=SimpleNamespace(manage_guild=manager)),
                           response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()))


class DuplicatePatrolTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_reply_payload_mentions_only_current_sharer(self):
        source = message()
        await patrol._announce(source, previous())
        kwargs = source.channel.send.await_args.kwargs
        from discord.http import handle_message_parameters
        with handle_message_parameters(content=source.channel.send.await_args.args[0],
                                       message_reference=kwargs["reference"].to_dict(),
                                       allowed_mentions=kwargs["allowed_mentions"], mention_author=False) as params:
            self.assertEqual(params.payload["message_reference"]["message_id"], 1100)
            self.assertEqual(params.payload["message_reference"]["channel_id"], 10)
            self.assertEqual(params.payload["allowed_mentions"],
                             {"users": [42], "parse": [], "replied_user": False})
            self.assertIn("<@42>", params.payload["content"])
            self.assertNotIn("<@40>", params.payload["content"])

    async def test_deleted_original_falls_back_to_normal_notice(self):
        source = message()
        response = SimpleNamespace(status=404, reason="Not Found")
        source.channel.send.side_effect = [discord.NotFound(response, {"code": 10008, "message": "test"}), None]
        await patrol._announce(source, previous())
        self.assertEqual(source.channel.send.await_count, 2)
        self.assertNotIn("reference", source.channel.send.await_args.kwargs)
        self.assertIn("不存在", source.channel.send.await_args.args[0])

    async def test_registers_resolved_identity_first_page_and_duplicate_reply(self):
        source = message()
        service = SimpleNamespace(register=AsyncMock(return_value=DuplicateMatch(previous(), 2)), patrol_repository=fake_audit())
        with patch.object(patrol, "social_duplicate_service", service):
            await patrol.record_delivered_preview(
                source, platform="threads", url="https://www.threads.com/@demo/post/Demo001?xmt=test",
                sent_messages=[SimpleNamespace(id=1200), SimpleNamespace(id=1201)])
        saved = service.register.await_args.args[0]
        self.assertEqual(saved.preview_message_id, "1200")
        self.assertEqual(saved.source_message_id, "101")
        self.assertEqual(saved.post_key, "Demo001")
        self.assertNotIn("?", saved.canonical_url)
        source.channel.send.assert_awaited_once()

    async def test_error_card_unresolved_share_dm_and_no_sent_messages_are_skipped(self):
        service = SimpleNamespace(register=AsyncMock())
        for overrides in ({"preview_error": "no_content"}, {"sent_messages": []},
                          {"url": "https://www.threads.com/share/Demo"}, {"dm": True}):
            source = message()
            options = dict(platform="threads", url="https://www.threads.com/@demo/post/Demo001",
                           sent_messages=[SimpleNamespace(id=1200)])
            if overrides.get("dm"):
                source.guild = None
            else:
                options.update(overrides)
            with patch.object(patrol, "social_duplicate_service", service):
                await patrol.record_delivered_preview(source, **options)
        service.register.assert_not_awaited()

    async def test_notice_failure_does_not_escape_or_change_successful_event(self):
        source = message()
        source.channel.send.side_effect = RuntimeError("synthetic failure")
        service = SimpleNamespace(register=AsyncMock(return_value=DuplicateMatch(previous(), 2)), patrol_repository=fake_audit())
        with patch.object(patrol, "social_duplicate_service", service):
            await patrol.record_delivered_preview(source, platform="threads",
                url="https://www.threads.com/@demo/post/Demo001", sent_messages=[SimpleNamespace(id=1200)])
        service.register.assert_awaited_once()

    async def test_passes_atomic_share_count_to_webhook_announcement(self):
        source = message()
        service = SimpleNamespace(register=AsyncMock(return_value=DuplicateMatch(previous(), 3)), patrol_repository=fake_audit())
        with patch.object(patrol, "social_duplicate_service", service), \
                patch.object(patrol, "_announce", AsyncMock(return_value=PatrolDelivery("webhook", "900"))) as announce:
            await patrol.record_delivered_preview(source, platform="threads",
                url="https://www.threads.com/@demo/post/Demo001", sent_messages=[SimpleNamespace(id=1200)])
        announce.assert_awaited_once_with(source, previous(), share_count=3)


class DuplicateCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_permissions_and_dm_are_checked_before_database(self):
        cog = cog_module.SocialDuplicateCog(None)
        service = SimpleNamespace(configure=AsyncMock())
        for request in (interaction(guild=False), interaction(manager=False)):
            with patch.object(cog_module, "social_duplicate_service", service):
                await cog.configure_patrol.callback(cog, request, days=11)
            request.response.send_message.assert_awaited_once()
            request.response.defer.assert_not_awaited()
        service.configure.assert_not_awaited()

    async def test_arbitrary_days_deferred_response_and_failure_status(self):
        cog = cog_module.SocialDuplicateCog(None)
        service = SimpleNamespace(configure=AsyncMock(return_value=False))
        request = interaction()
        with patch.object(cog_module, "social_duplicate_service", service):
            await cog.configure_patrol.callback(cog, request, days=11)
        request.response.defer.assert_awaited_once_with(ephemeral=True)
        service.configure.assert_awaited_once_with("1", state=None, days=11, updated_by="42")
        self.assertIn("未完整套用", request.followup.send.await_args.args[0])

    async def test_success_and_status_report_effective_setting_source(self):
        cog = cog_module.SocialDuplicateCog(None)
        service = SimpleNamespace(configure=AsyncMock(return_value=True), ready=True,
                                  effective=Mock(return_value=(True, 11)), settings={"1": (True, 11)})
        request = interaction()
        with patch.object(cog_module, "social_duplicate_service", service):
            await cog.configure_patrol.callback(cog, request, days=11)
            status_request = interaction()
            await cog.patrol_status.callback(cog, status_request)
        self.assertIn("11 天", request.followup.send.await_args.args[0])
        self.assertIn("伺服器設定", status_request.response.send_message.await_args.args[0])

    async def test_invalid_days_are_reported_and_degraded_cache_never_claims_default(self):
        cog = cog_module.SocialDuplicateCog(None)
        service = SimpleNamespace(configure=AsyncMock(side_effect=ValueError("天數錯誤")), ready=False)
        request = interaction()
        with patch.object(cog_module, "social_duplicate_service", service):
            await cog.configure_patrol.callback(cog, request, days=0)
            self.assertIn("尚未就緒", cog._status("1"))
            self.assertNotIn("環境預設", cog._status("1"))
        self.assertIn("天數錯誤", request.followup.send.await_args.args[0])

    async def test_load_rebuilds_before_return_and_unload_cancels_recovery(self):
        service = SimpleNamespace(rebuild=AsyncMock(return_value=True), close=AsyncMock(),
                                  recover=AsyncMock(side_effect=lambda: asyncio.Event().wait()))
        # A real waiting coroutine verifies cancellation and orderly close.
        async def recover():
            await asyncio.Event().wait()
        service.recover = recover
        cog = cog_module.SocialDuplicateCog(None)
        with patch.object(cog_module, "social_duplicate_service", service):
            await cog.cog_load()
            service.rebuild.assert_awaited_once()
            task = cog._recovery_task
            await asyncio.sleep(0)
            await cog.cog_unload()
        self.assertTrue(task.cancelled())
        service.close.assert_awaited_once()


class DuplicatePreviewIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_threads_and_facebook_use_resolved_url_after_complete_group(self):
        for module, platform, send_name, preview_class, resolved in (
            (threads_preview, "threads", "_send_threads_preview", threads_preview.PreviewResult,
             "https://www.threads.com/@demo/post/Demo001"),
            (facebook_preview, "facebook", "_send_facebook_preview", facebook_preview.FacebookPreview,
             "https://www.facebook.com/demo/posts/123"),
        ):
            with self.subTest(platform=platform):
                embed = discord.Embed(title="Example", description="Example text", url=resolved)
                embed.set_author(name="Example")
                preview = preview_class(embed=embed, files=[])
                sent = [SimpleNamespace(id=1200)]
                with patch.object(module, "send_preview_group", AsyncMock(return_value=sent)), \
                        patch.object(module, "record_delivered_preview", AsyncMock()) as record:
                    await getattr(module, send_name)(message(), preview, original_url="https://example.invalid/share/Demo",
                                                     commentary="", spoiler=False)
                self.assertEqual(record.await_args.kwargs["url"], resolved)
                self.assertIs(record.await_args.kwargs["sent_messages"], sent)

    async def test_failed_group_never_records_share(self):
        preview = threads_preview.PreviewResult(
            embed=discord.Embed(title="Example", url="https://www.threads.com/@demo/post/Demo001"), files=[])
        preview.embed.set_author(name="Example")
        with patch.object(threads_preview, "send_preview_group", AsyncMock(side_effect=RuntimeError("synthetic failure"))), \
                patch.object(threads_preview, "record_delivered_preview", AsyncMock()) as record:
            with self.assertRaises(RuntimeError):
                await threads_preview._send_threads_preview(message(), preview, original_url=preview.embed.url,
                                                            commentary="", spoiler=False)
        record.assert_not_awaited()

    async def test_instagram_records_then_cleans_up(self):
        source = message()
        source.content = "https://www.instagram.com/reel/Demo001/"
        preview = instagram_preview.InstagramPreview(
            discord.Embed(title="Example", description="Example text", url="https://www.instagram.com/p/Demo001/"))
        preview.embed.set_author(name="Example")
        operations = []
        async def record(*args, **kwargs):
            operations.append("record")
        async def cleanup(*args, **kwargs):
            operations.append("cleanup")
        with patch.object(instagram_preview, "build_instagram_preview", AsyncMock(return_value=preview)), \
                patch.object(instagram_preview, "notify_preview_failure", AsyncMock()), \
                patch.object(instagram_preview, "send_preview_group", AsyncMock(return_value=[SimpleNamespace(id=1200)])), \
                patch.object(instagram_preview, "record_delivered_preview", AsyncMock(side_effect=record)) as registered, \
                patch.object(instagram_preview, "cleanup_source_message", AsyncMock(side_effect=cleanup)):
            self.assertTrue(await instagram_preview.handle_instagram_in_message(source))
        self.assertEqual(operations, ["record", "cleanup"])
        self.assertEqual(registered.await_args.kwargs["url"], preview.embed.url)
