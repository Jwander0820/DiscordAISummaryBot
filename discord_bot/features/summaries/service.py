from google.generativeai.types import GenerationConfig
import discord
import logging
from ...db.repository import summary_repository
from ...features.chat.history import format_message_history
from ...features.chat.records import build_summary_record
from ...features.notifications.service import notification_service
from ...integrations.gemini_client import gemini_model
from ...integrations.gemini_client import role_model
from ...integrations.local_llm import resolve_prompt
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger('discord_digest_bot')


async def summarize_messages(messages: list[discord.Message], prompt_scope: str = "過去24小時", user_id: str = None) -> str:
    """摘要 service 主流程。

    負責：
    1. 將 Discord 訊息整理成 prompt
    2. 呼叫 Gemini
    3. 寫入 summaries repository
    4. 觸發通知
    """
    logger.info(f"Summarizing {len(messages)} messages...")

    # 先組好 record skeleton，讓成功與失敗通知都能共用同一份資料。
    record = build_summary_record(
        channel_id=messages[0].channel.name if messages else None,
        user_id=user_id,
        command=f"{prompt_scope}總結",
    )

    if not gemini_model:
        logger.warning("Gemini model not initialized or API key is missing/invalid.")
        return "Error: Summarization feature is not available."

    if not messages:
        return "No recent non-bot messages found to summarize."

    message_text = format_message_history(messages, include_author_id=True)
    record["prompt"] = message_text
    # prompt 統一在 service 層組裝，避免 command/cog 各自拼 prompt。
    contents = [
        {
            "role": "model",
            "parts": [
                f"你是一位觀察 Discord 頻道的對話分析師，擅長用詼諧的繁體中文總結對話主題與參與狀況。聊天格式為 [HH:MM] 使用者: 訊息內容。請針對給定的訊息，歸納主要討論點，挑出幾句代表性發言，最後點名最積極的參與者"
            ]
        },
        {
            "role": "user",
            "parts": [
                f"""請協助我總結{prompt_scope}內的對話：
            
                聊天紀錄:
                {message_text}
            
                請提供總結：
                """
            ]
        }
    ]

    logger.info(f"Sending prompt to Gemini (length: {len(contents[1]['parts'][0])} chars)")

    try:
        response = await gemini_model.generate_content_async(contents=contents)

        # 先攔截被封鎖的情況
        if not response.candidates:
            reason = getattr(response.prompt_feedback, "block_reason", "UNKNOWN")
            return f"Summarization blocked by policy: {reason}"

        summary_text = response.text
        record["summary"] = summary_text
        summary_repository.insert_summary(record)

        logger.info("Summary saved successfully.")

        await notification_service.dispatch(
            record=record,
            guild=messages[0].guild if messages else None,
        )

        return summary_text.strip()

    except Exception as e:
        logger.error(f"Error generating summary: {e}", exc_info=True)
        await notification_service.dispatch(
            record=record,
            guild=messages[0].guild if messages else None,
            error=e,
        )
        return f"Error during summarization: {e}"


async def call_cloud_llm(prompt: str, role: str = "basic") -> str:
    """角色問答用的雲端 LLM 包裝。

    給 cogs 用來處理 `ROLE_MODE=cloud` 的對話路徑。
    """
    logger.info("call_cloud_llm handle role questions and answers")
    if not role_model:
        return "Summarization feature is unavailable (missing API key)."
    role_prompt = resolve_prompt(role)
    try:
        contents = [
            {
                "role": "model",
                "parts": [role_prompt]
            },
            {
                "role": "user",
                "parts": [prompt]
            }
        ]
        gen_config = GenerationConfig(temperature=0.7)

        response = await role_model.generate_content_async(contents=contents, generation_config=gen_config)

        if not response.parts:
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                return f"Summarization blocked due to policy: {response.prompt_feedback.block_reason}"
            return "Summarization failed: No response from Gemini."

        summary_text = response.text
        return summary_text.strip()
    except Exception as e:
        logger.error(f"Error generating summary: {e}", exc_info=True)
        return f"Error during summarization: {e}"
