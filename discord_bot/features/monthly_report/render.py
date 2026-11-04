"""Plain Discord markdown pages for one fixed monthly report, in the trial issue's style."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata

from .aggregation import MonthlyReport, ZERO_COLLISION_MIN_SHARES
from .periods import parse_month, taipei_datetime

MESSAGE_LIMIT = 2000  # Measured conservatively in UTF-16 code units.
NAME_LIMIT = 32
INLINE_TIES = 3  # More tied members than this get one line each so pages can split.
RANK_LABELS = {1: "第一", 2: "第二", 3: "第三"}
NO_RECORD = "本月無符合紀錄"
_MARKDOWN = re.compile(r"([\\*_~`|<>\[\]()#\-.:@!])")
_BIDI = frozenset("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def escape_name(value: str) -> str:
    """Names are data: strip controls, bound length, and neutralize markdown/mentions."""
    text = re.sub(r"\s+", " ", str(value or ""))
    text = "".join(char for char in text
                   if char not in _BIDI and not unicodedata.category(char).startswith("C")).strip()
    if len(text) > NAME_LIMIT:
        text = text[:NAME_LIMIT - 1] + "…"
    return _MARKDOWN.sub(r"\\\1", text)


def fallback_name(user_id: str) -> str:
    return f"未知成員（ID 末四碼 {str(user_id)[-4:]}）"


def _count(value: int) -> str:
    return f"{value:,}"


def _duration(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小時 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _within(seconds: float) -> str:
    total = int(seconds)
    if total < 3600:
        return f"不到 {total // 60 + 1} 分鐘"
    return f"不到 {total // 3600 + 1} 小時"


def _link(url: str) -> str:
    if url.startswith(("https://", "http://")) and not re.search(r"[\s<>]", url) and len(url) <= 512:
        return f"<{url}>"
    return escape_name(url)[:200]


@dataclass
class Section:
    lines: list[str]
    flavor: list[str] = field(default_factory=list)

    def render(self, *, flavor: bool) -> list[str]:
        return self.lines + (self.flavor if flavor else [])


def _sections(report: MonthlyReport, names: dict[str, str]) -> list[Section]:
    def name(user_id):
        for candidate in (report.snapshot_names.get(user_id), names.get(user_id)):
            escaped = escape_name(candidate) if candidate else ""
            if escaped:
                return escaped
        return fallback_name(user_id)

    def winners(items, template):
        """`template` receives (count, tied); ties share one line, many ties get one line each."""
        if not items:
            return [NO_RECORD]
        value = items[0].value
        if len(items) <= INLINE_TIES:
            joined = "、".join(name(item.user_id) for item in items)
            return [f"**{joined} — {template(value, len(items) > 1)}**"]
        return [f"**{name(item.user_id)} — {template(value, False)}**" for item in items]

    _, month = parse_month(report.report_month)
    sections = [Section([
        f"**🚨 {month} 月不看訊息警察月報**",
        "",
        f"這期記錄到 **{_count(report.share_users)} 位成員、{_count(report.share_rows)} 次分享**，"
        f"警察共成功出勤 **{_count(report.patrol_sent)} 次**。來看看獎落誰家！",
    ])]
    sections.append(Section(["🚓 **被出警王**", *winners(report.most_patrolled, lambda n, t: f"{'各 ' if t else ''}{_count(n)} 次")],
                            ["本期最常與警察見面的人。"]))
    sections.append(Section(["📣 **分享王**", *winners(report.top_sharers, lambda n, t: f"{'各 ' if t else ''}{_count(n)} 次分享")],
                            ["情報補給擔當，聊天室的內容不能斷！"]))
    zero = [f"{name(item.user_id)} · **{_count(item.value)} 筆**" for item in report.zero_collision] or [NO_RECORD]
    sections.append(Section([
        "🏅 **零撞車分享榜**", *zero,
        f"-# 當月分享超過 {ZERO_COLLISION_MIN_SHARES} 筆且沒有任何判重事件，依 Bot 當時啟用的判重範圍。"]))
    sections.append(Section(
        ["🧭 **跨區撞車王**", *winners(report.cross_channel, lambda n, t: f"{'各 ' if t else ''}{_count(n)} 次跨頻道警報")],
        ["換了一個頻道，還是遇見熟悉的警察。"]))
    sections.append(Section(
        ["🌱 **首發帶動王**", *winners(report.first_movers, lambda n, t: f"{'各' if t else ''}引發別人 {_count(n)} 次重貼警報")],
        ["你先分享的內容，總有人忍不住再分享一次。"]))
    if report.fastest:
        gap = report.fastest[0].seconds
        fastest = [f"**{'、'.join(name(item.user_id) for item in report.fastest)} — 約 {_duration(gap)}**"]
        flavor = [f"前一位分享後，{_within(gap)}就再次登場！"]
    else:
        fastest, flavor = [NO_RECORD], []
    sections.append(Section(["⚡ **光速撞車**", *fastest], flavor))
    posts = []
    for index, post in enumerate(report.popular_posts, start=1):
        tie = f"（並列{RANK_LABELS.get(post.rank, f'第 {post.rank} 名')}）" if post.tied else ""
        posts.append(f"{index}. {_link(post.canonical_url)} · **{_count(post.shares)} 次分享**{tie}")
    sections.append(Section(["🔥 **最常被分享的貼文**", *(posts or [NO_RECORD]),
                             *(["-# 分享次數包含首次。"] if posts else [])]))
    if report.busiest_days:
        count = report.busiest_days[0].patrols
        days = "、".join(f"{day.day.month} 月 {day.day.day} 日" for day in report.busiest_days)
        busiest = [f"**{days} — {'各' if len(report.busiest_days) > 1 else '一天'}出勤 {_count(count)} 次**"]
        if len(report.busiest_days) > 1:
            flavor = ["這幾天警察特別忙。"]
        elif count * 2 > report.patrol_sent:
            flavor = ["本期超過一半的警報，都集中在這一天。"]
        elif count * 3 > report.patrol_sent:
            flavor = ["本期超過三分之一的警報，都集中在這一天。"]
        else:
            flavor = ["這天警察特別忙。"]
    else:
        busiest, flavor = [NO_RECORD], []
    sections.append(Section(["📅 **警察最忙日**", *busiest], flavor))
    sections.append(Section([], ["下次分享前，記得往上滑一下。也可能下一期的主角就是你。🚨"]))

    start, end = taipei_datetime(report.period_start), taipei_datetime(report.period_end)
    footer = f"統計區間 {start:%Y/%m/%d %H:%M} – {end:%Y/%m/%d %H:%M}（台北時間）"
    firsts = [value for value in (report.share_first, report.patrol_first) if value is not None]
    if firsts and taipei_datetime(min(firsts)).date() != start.date():
        first = taipei_datetime(min(firsts))
        footer += f"，資料自 {first.month}/{first.day} 起"
    footer += "。「光速撞車」僅計算不同成員之間的重貼；分享數為結算時留存的登記。"
    sections.append(Section([f"*{footer}*"]))
    return sections


def _marker(page: int, total: int) -> str:
    return f"-# （第 {page}/{total} 頁）"


def _paginate(blocks: list[list[str]], budget: int) -> list[list[str]]:
    """Greedy section packing; sizes are tracked incrementally ("\\n" joins count as one unit)."""
    pages, current, used = [], [], 0

    def size(lines):
        return sum(utf16_len(line) for line in lines) + max(len(lines) - 1, 0)

    def flush():
        nonlocal current, used
        if current:
            pages.append(current)
        current, used = [], 0

    for block in blocks:
        block_size = size(block)
        joined = used + (2 if current else 0) + block_size  # Blank separator line.
        if joined <= budget:
            current = current + ([""] if current else []) + block
            used = joined
            continue
        flush()
        if block_size <= budget:
            current, used = list(block), block_size
            continue
        # One oversized section: keep the heading on each continuation page.
        heading, rest = block[0], block[1:]
        current, used = [heading], utf16_len(heading)
        for line in rest:
            line_size = utf16_len(line)
            if used + 1 + line_size > budget:
                flush()
                current, used = [heading + "（續）"], utf16_len(heading + "（續）")
            current.append(line)
            used += 1 + line_size
    flush()
    return pages


def render_pages(report: MonthlyReport, names: dict[str, str], *, limit: int = MESSAGE_LIMIT) -> list[str]:
    """One message when it fits; otherwise drop flavor text first, then paginate."""
    sections = _sections(report, names)
    full = "\n\n".join("\n".join(lines) for lines in (s.render(flavor=True) for s in sections) if lines)
    if utf16_len(full) <= limit:
        return [full]
    blocks = [lines for lines in (s.render(flavor=False) for s in sections) if lines]
    plain = "\n\n".join("\n".join(lines) for lines in blocks)
    if utf16_len(plain) <= limit:
        return [plain]
    pages = _paginate(blocks, limit - utf16_len(_marker(99, 99)) - 1)
    return ["\n".join(lines) + "\n" + _marker(index, len(pages)) for index, lines in enumerate(pages, start=1)]
