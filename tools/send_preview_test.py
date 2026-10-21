"""Standalone Discord GIF layout tester. Offline unless --send is specified."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
API = "https://discord.com/api/v10"
LAYOUTS = ("thumbnail", "image", "v2-thumbnail", "v2-gallery")
EXTRA_LAYOUTS = ("v2-section", "author-icon", "footer-icon", "v2-spoiler")
KEYS = ("DISCORD_BOT_TOKEN", "DISCORD_NOTIFY_FORWARD_CHANNEL_ID",
        "DISCORD_NOTIFY_FORWARD_GUILD_ID")
MAX_GIF_BYTES = 10 * 1024 * 1024


class TestSendError(Exception):
    """Only fixed, non-sensitive messages may be used here."""


def https_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname
                 and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("請提供有效的 HTTPS 網址。")
    return value


def build_payload(layout: str, media: str, title: str, text: str, link: str) -> dict:
    """Build an API payload without importing the application or loading secrets."""
    if layout not in (*LAYOUTS, *EXTRA_LAYOUTS):
        raise ValueError("Unknown layout")
    body = f"{text}\n\n[網址連結文字]({link})"
    payload = {"allowed_mentions": {"parse": []}}
    if layout in ("thumbnail", "image", "author-icon", "footer-icon"):
        embed = {
            "author": {"name": title}, "description": body, "color": 25500,
            "footer": {"text": f"GIF 版型測試 · {layout}"},
        }
        if layout in ("thumbnail", "image"):
            embed[layout] = {"url": media}
        else:
            embed["author" if layout == "author-icon" else "footer"]["icon_url"] = media
        payload["embeds"] = [embed]
    else:
        display = {"type": 10, "content": f"**{title}**\n{body}"}
        if layout in ("v2-thumbnail", "v2-section", "v2-spoiler"):
            children = [{"type": 9, "components": [display], "accessory": {
                "type": 11, "media": {"url": media}, "description": "測試 GIF",
            }}]
            if layout == "v2-spoiler":
                children[0]["accessory"]["spoiler"] = True
        else:
            children = [display, {"type": 12, "items": [{
                "media": {"url": media}, "description": "測試 GIF",
            }]}]
        children.append({"type": 10, "content": f"-# GIF 版型測試 · {layout}"})
        payload.update(flags=1 << 15, components=[{
            "type": 17, "accent_color": 25500, "components": children,
        }])
        if layout == "v2-section":
            payload["components"] = children
    return payload


def load_settings() -> dict:
    from dotenv import dotenv_values

    # Do not mutate os.environ or interpolate unrelated environment variables.
    settings = {key: os.environ[key] for key in KEYS if key in os.environ}
    for path in (ROOT / ".env", ROOT / "discord_bot" / ".env"):
        if path.is_file() and len(settings) < len(KEYS):
            values = dotenv_values(path, interpolate=False)
            for key in KEYS:
                if key not in settings and values.get(key) is not None:
                    settings[key] = values[key]
    settings = {key: value.strip() for key, value in settings.items()}
    if any(not settings.get(key) for key in KEYS):
        raise TestSendError("缺少 bot token 或通知頻道／伺服器設定。")
    for key in KEYS[1:]:
        value = settings[key]
        if not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise TestSendError("通知頻道／伺服器 ID 格式錯誤。")
    return settings


def load_gif(path: Path) -> bytes:
    with path.open("rb") as source:
        data = source.read(MAX_GIF_BYTES + 1)
    if len(data) > MAX_GIF_BYTES:
        raise TestSendError("本機 GIF 超過工具的 10 MiB 上限。")
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        raise TestSendError("本機檔案不是 GIF。")
    return data


async def api_request(session, method: str, route: str, **kwargs) -> dict:
    # No redirects, raw response logging, or automatic retries of POST requests.
    async with session.request(method, API + route, allow_redirects=False, **kwargs) as response:
        if not 200 <= response.status < 300:
            hints = {401: "bot token 無效。", 403: "機器人缺少頻道或發送權限。",
                     404: "找不到目標頻道。", 429: "觸及速率限制，請稍後再試。"}
            raise TestSendError(hints.get(response.status, f"Discord HTTP {response.status}，已停止。"))
        return await response.json()


async def send_payloads(settings: dict, payloads: list[dict], gif: bytes | None) -> None:
    import aiohttp

    channel = settings[KEYS[1]]
    guild = settings[KEYS[2]]
    headers = {"Authorization": "Bot " + settings[KEYS[0]],
               "User-Agent": "DiscordBot (https://docs.discord.com, 1.0)"}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as session:
        target = await api_request(session, "GET", f"/channels/{channel}")
        if str(target.get("guild_id")) != guild:
            raise TestSendError("頻道不屬於設定的伺服器，未發送訊息。")
        for payload in payloads:
            if gif is None:
                result = await api_request(session, "POST", f"/channels/{channel}/messages", json=payload)
            else:
                form = aiohttp.FormData()
                form.add_field("payload_json", json.dumps(payload), content_type="application/json")
                form.add_field("files[0]", gif, filename="preview.gif", content_type="image/gif")
                result = await api_request(session, "POST", f"/channels/{channel}/messages", data=form)
            message_id = str(result.get("id", ""))
            if not message_id.isascii() or not message_id.isdecimal():
                raise TestSendError("回應缺少訊息 ID；可能已送達，請檢查頻道再重試。")
            print(f"已送達：https://discord.com/channels/{guild}/{channel}/{message_id}")
            if len(payloads) > 1:
                await asyncio.sleep(1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--gif-url", type=https_url, help="GIF 媒體直連，不是 Giphy 分享頁")
    source.add_argument("--gif-file", type=Path, help="本機 GIF（最多 10 MiB）")
    parser.add_argument("--layout", choices=(*LAYOUTS, *EXTRA_LAYOUTS, "all", "extras"), default="thumbnail")
    parser.add_argument("--title", default="有新人")
    parser.add_argument("--text", default="如題")
    parser.add_argument("--link", type=https_url, default="https://example.com")
    parser.add_argument("--send", action="store_true", help="實際發送；all / extras 各送四則")
    args = parser.parse_args(argv)
    if not 1 <= len(args.title.encode("utf-16-le")) // 2 <= 256:
        parser.error("標題須為 1～256 UTF-16 units。")
    if len((args.text + args.link).encode("utf-16-le")) // 2 > 3000:
        parser.error("內文與連結合計不可超過 3000 UTF-16 units。")
    try:
        gif = load_gif(args.gif_file) if args.gif_file else None
        layouts = (LAYOUTS if args.layout == "all" else EXTRA_LAYOUTS
                   if args.layout == "extras" else (args.layout,))
        payloads = [build_payload(layout, args.gif_url or "attachment://preview.gif",
                                  args.title, args.text, args.link) for layout in layouts]
        if gif is not None:
            for payload in payloads:
                payload["attachments"] = [{"id": 0, "filename": "preview.gif"}]
        if not args.send:
            print(json.dumps(payloads, ensure_ascii=False, indent=2))
            print("離線預覽；未讀取 .env、未連線、未發送。加 --send 才實際發送。")
            return 0
        asyncio.run(send_payloads(load_settings(), payloads, gif))
        return 0
    except TestSendError as exc:
        print(f"測試停止：{exc}")
    except Exception:
        # Exceptions may contain request URLs, local paths, or credentials.
        print("測試停止：檔案、設定或網路錯誤；若已開始發送，請先檢查頻道再重試。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
