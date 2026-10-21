"""Independent webhook patrol mockup. No production bot modules or databases."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

try:
    from .send_preview_test import API, KEYS, TestSendError, api_request, https_url, load_settings
except ImportError:
    from send_preview_test import API, KEYS, TestSendError, api_request, https_url, load_settings

NAME = "不看訊息警察"
HOOK_NAME = "digest-patrol-layout-test"
LOCAL_SETTINGS_PATH = Path(__file__).with_name(".patrol-preview.local.json")


def count_comment(count: int) -> str:
    """Mirror the current patrol wording without importing production modules."""
    if count < 3:
        return ""
    taunts = {3: "三回啊三回！", 4: "你是只看自己傳的訊息是不是！",
              5: "警察都認得這篇了，你還不認得！"}
    if count in taunts:
        return f" 第 {count} 次了！{taunts[count]}"
    return f" 第 {count} 次了……好，你開心就好。"


def build_patrol_payloads(media: str, mention: str, reference: str, *, avatar_gif=False,
                         allow_user: str | None = None, no_heading=False,
                         avatar_url=None, author_icon=False, share_count=None, final_style=False) -> list[dict]:
    body = f"🚨 逼逼！{mention}！你已經被不看訊息警察逮捕了，逼波逼波逼波。"
    link_label = "查看原始連結" if final_style else "查看原始連結（模擬）"
    link = f"⬆️ [{link_label}]({reference})"
    if share_count is not None:
        link += count_comment(share_count)
    shared = {"username": NAME, "allowed_mentions": {"parse": []}}
    if allow_user:
        shared["allowed_mentions"]["users"] = [allow_user]
    if avatar_gif:
        shared["avatar_url"] = media
    if avatar_url:
        shared["avatar_url"] = avatar_url
    payloads = [
        {**shared, "flags": 1 << 15, "components": [{
            "type": 17, "accent_color": 0xFF6600, "components": [
                {"type": 9, "components": [
                    {"type": 10, "content": f"**{NAME}**"},
                    {"type": 10, "content": body},
                    {"type": 10, "content": link},
                ], "accessory": {"type": 11, "media": {"url": media}, "description": "警報狗 GIF"}},
                {"type": 10, "content": "-# 視覺測試 · Webhook / V2 · 非真實出警"},
            ],
        }]},
        {**shared, "embeds": [{
            "author": {"name": NAME}, "description": body + "\n\n" + link,
            "color": 0xFF6600, "thumbnail": {"url": media},
            "footer": {"text": "視覺測試 · Webhook / Embed · 非真實出警"},
        }]},
    ]
    if no_heading:
        payloads[0]["components"][0]["components"][0]["components"].pop(0)
        payloads[1]["embeds"][0].pop("author")
    if author_icon:
        embed = payloads[1]["embeds"][0]
        embed["author"] = {"name": NAME, "icon_url": media}
        embed.pop("thumbnail")
        embed["footer"]["text"] = "視覺測試 · Webhook / Embed 作者列 GIF · 非真實出警"
    if share_count is not None:
        payloads[0]["components"][0]["components"][-1]["content"] += f" · 第 {share_count} 次"
        payloads[1]["embeds"][0]["footer"]["text"] += f" · 第 {share_count} 次"
    if final_style:
        payloads[0]["components"][0]["components"].pop()
        payloads[1]["embeds"][0].pop("footer")
    return payloads


async def send_demo(settings: dict, media: str, mention_user: str | None, avatar_gif: bool,
                    *, no_heading=False, layout="both", avatar_url=None, share_counts=None,
                    reference_url="https://example.com", final_style=False):
    import aiohttp

    channel, guild = settings[KEYS[1]], settings[KEYS[2]]
    headers = {"Authorization": "Bot " + settings[KEYS[0]],
               "User-Agent": "DiscordBot (https://docs.discord.com, 1.0)"}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as session:
        target = await api_request(session, "GET", f"/channels/{channel}")
        if str(target.get("guild_id")) != guild:
            raise TestSendError("頻道不屬於設定的伺服器，未發送訊息。")
        if target.get("type") != 0:
            raise TestSendError("此獨立示範只支援一般文字頻道。")
        me = await api_request(session, "GET", "/users/@me")
        hooks = await api_request(session, "GET", f"/channels/{channel}/webhooks")
        hook = next((h for h in hooks if h.get("name") == HOOK_NAME
                     and h.get("type") == 1 and h.get("user", {}).get("id") == me["id"]
                     and h.get("token")), None)
        if hook is None:
            hook = await api_request(session, "POST", f"/channels/{channel}/webhooks", json={"name": HOOK_NAME})
            print("已建立獨立測試 Webhook；不修改正式警報 Webhook。")
        mention = f"<@{mention_user or me['id']}>"
        jobs = []
        for count in share_counts or [None]:
            payloads = build_patrol_payloads(media, mention, reference_url,
                                         avatar_gif=avatar_gif,
                                         allow_user=me["id"] if mention_user is None else None,
                                         no_heading=no_heading, avatar_url=avatar_url,
                                         author_icon=layout == "author-icon", share_count=count,
                                         final_style=final_style)
            jobs.extend((label, payload, count) for label, payload in zip(("V2", "Embed"), payloads)
                        if layout == "both" or label.lower() == ("embed" if layout == "author-icon" else layout))
        route = f"/webhooks/{hook['id']}/{hook['token']}?wait=true&with_components=true"
        for label, payload, count in jobs:
            result = await api_request(session, "POST", route, json=payload)
            if not str(result.get("id", "")).isdecimal():
                raise TestSendError("回應缺少訊息 ID；可能已送達，請先檢查頻道。")
            count_label = f" 第 {count} 次" if count is not None else ""
            print(f"{label}{count_label} 已送達：https://discord.com/channels/{guild}/{channel}/{result['id']}")
            if label == "V2":
                print("V2 回應驗證：" + json.dumps({
                    "webhook": bool(result.get("webhook_id")),
                    "components_v2": bool(result.get("flags", 0) & (1 << 15)),
                    "container": any(c.get("type") == 17 for c in result.get("components", [])),
                    "mention_parsed": any(str(u.get("id")) == str(mention_user or me["id"])
                                          for u in result.get("mentions", [])),
                    "avatar_present": bool(result.get("author", {}).get("avatar")),
                }, ensure_ascii=False))
                await asyncio.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gif-url", type=https_url, required=True)
    parser.add_argument("--mention-user", help="Optional user ID; mentions never notify in this demo")
    avatar = parser.add_mutually_exclusive_group()
    avatar.add_argument("--avatar-gif", action="store_true", help="Also pass GIF as per-message avatar_url")
    avatar.add_argument("--avatar-url", type=https_url, help="Static per-message webhook avatar URL")
    parser.add_argument("--no-heading", action="store_true", help="Omit the title inside the card")
    parser.add_argument("--layout", choices=("v2", "embed", "author-icon", "both"), default="both")
    parser.add_argument("--channel-id", help="Override target channel for this run only; never write .env")
    parser.add_argument("--guild-id", help="Override expected guild for this run only; never write .env")
    parser.add_argument("--share-counts", type=int, nargs="+", help="Simulate specific repeat counts")
    parser.add_argument("--reference-url", type=https_url, default="https://example.com",
                        help="Mock original link; defaults to a public example, never channel information")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--final-style", action="store_true", help="Omit simulation labels from the message")
    args = parser.parse_args(argv)
    if args.channel_id and (not args.channel_id.isascii() or not args.channel_id.isdecimal() or int(args.channel_id) <= 0):
        parser.error("頻道 ID 必須是正整數。")
    if args.guild_id and (not args.guild_id.isascii() or not args.guild_id.isdecimal() or int(args.guild_id) <= 0):
        parser.error("伺服器 ID 必須是正整數。")
    if args.share_counts and (len(args.share_counts) > 10 or any(n < 2 for n in args.share_counts)):
        parser.error("次數須至少為 2，一次最多指定 10 個情境。")
    if args.no_heading and args.layout == "author-icon":
        parser.error("作者列小圖示版需要保留作者列。")
    if args.mention_user and (not args.mention_user.isascii() or not args.mention_user.isdecimal()):
        parser.error("使用者 ID 必須是數字。")
    if not args.send:
        selected = []
        for count in args.share_counts or [None]:
            payloads = build_patrol_payloads(args.gif_url, f"<@{args.mention_user}>" if args.mention_user
                                        else "@測試對象", args.reference_url, avatar_gif=args.avatar_gif,
                                        no_heading=args.no_heading, avatar_url=args.avatar_url,
                                        author_icon=args.layout == "author-icon", share_count=count,
                                        final_style=args.final_style)
            selected.extend(payload for label, payload in zip(("v2", "embed"), payloads)
                            if args.layout == "both" or label == ("embed" if args.layout == "author-icon" else args.layout))
        print(json.dumps(selected, ensure_ascii=False, indent=2))
        print("離線預覽；未讀 .env 或連線。")
        return 0
    try:
        settings = load_settings()
        if LOCAL_SETTINGS_PATH.is_file():
            local = json.loads(LOCAL_SETTINGS_PATH.read_text(encoding="utf-8"))
            for name, key in (("channel_id", KEYS[1]), ("guild_id", KEYS[2])):
                if name in local:
                    value = str(local[name])
                    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
                        raise TestSendError("本機測試頻道設定格式錯誤。")
                    settings[key] = value
        if args.channel_id:
            settings[KEYS[1]] = args.channel_id
        if args.guild_id:
            settings[KEYS[2]] = args.guild_id
        asyncio.run(send_demo(settings, args.gif_url, args.mention_user, args.avatar_gif,
                              no_heading=args.no_heading, layout=args.layout, avatar_url=args.avatar_url,
                              share_counts=args.share_counts, reference_url=args.reference_url,
                              final_style=args.final_style))
        return 0
    except TestSendError as exc:
        print(f"測試停止：{exc}")
    except Exception:
        print("測試停止：設定或網路錯誤；請先檢查頻道再重試。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
