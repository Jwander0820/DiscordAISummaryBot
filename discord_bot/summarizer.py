from datetime import datetime, timezone, timedelta
from google.generativeai.types import GenerationConfig
import discord
import logging
from .gemini_client import gemini_model
from .gemini_client import role_model
from .database import insert_summary
from .local_llm_client import resolve_prompt

logger = logging.getLogger('discord_digest_bot')


async def summarize_messages(messages: list[discord.Message], prompt_scope: str = "過去24小時") -> str:
    """Summarize the given messages using Gemini."""
    logger.info(f"Summarizing {len(messages)} messages...")
    TZ_8 = timezone(timedelta(hours=8))

    if not gemini_model:
        logger.warning("Gemini model not initialized or API key is missing/invalid.")
        return "Error: Summarization feature is not available."

    if not messages:
        return "No recent non-bot messages found to summarize."

    message_text = "\n".join([
        f"[{msg.created_at.astimezone(TZ_8).strftime('%H:%M')}] [id:{msg.author.name}] {msg.author.display_name}: {msg.content}"
        for msg in reversed(messages)
    ])

    # 建立角色導向內容
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

        if not response.parts:
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                return f"Summarization blocked due to policy: {response.prompt_feedback.block_reason}"
            return "Summarization failed: No response from Gemini."

        summary_text = response.text
        tz = timezone(timedelta(hours=8))
        call_time = datetime.now(tz).isoformat()
        channel_id = messages[0].channel.name
        user_id = messages[0].author.global_name
        record = {
            "channel_id": str(channel_id),
            "user_id": user_id,
            "command": f"{prompt_scope}總結",
            "question": "",
            "prompt": message_text,
            "summary": summary_text,
            "call_time": call_time,
        }

        insert_summary(record)

        logger.info("Summary saved successfully.")
        return summary_text.strip()

    except Exception as e:
        logger.error(f"Error generating summary: {e}", exc_info=True)
        return f"Error during summarization: {e}"


async def call_cloud_llm(prompt: str, role: str = "basic") -> str:
    """發送 prompt 給本地 LLM 並回傳回應文字。"""
    logger.info("call_cloud_llm handle role questions and answers")
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
