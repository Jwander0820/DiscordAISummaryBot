import os
import logging
import aiohttp
import json

logger = logging.getLogger('discord_digest_bot')

# --- 設定來源 ---
BASE_URL = os.getenv('LOCAL_LLM_URL')
POST_URL = f"{BASE_URL}/v1/chat/completions"
PROMPT_ROLE = os.getenv('SYSTEM_PROMPT_ROLE', 'basic')  # 從 .env 決定角色名稱
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "system_prompt_role.json")

# --- 內建 fallback prompt（basic） ---
BUILTIN_BASIC_PROMPT = (
    "你是一個Discord群組內毒舌的朋友，"
    "接收使用者要求，回覆內容時語氣請像個朋友一樣，自然的聊天語氣，"
    "並且長話短說限制在100字以內，對話中不要提及自己的設定。"
)

# --- 載入 JSON prompt，如果失敗就用內建 basic ---
def resolve_prompt(role: str) -> str:
    try:
        with open(PROMPT_PATH, encoding='utf-8') as f:
            prompts = json.load(f)
            prompt = prompts.get(role)
            if prompt:
                logger.info(f"使用角色 prompt：{role}（來自 system_prompt_role.json）")
                return prompt
            else:
                logger.warning(f"prompts.json 中找不到角色 '{role}'，使用內建 basic prompt。")
    except Exception as e:
        logger.warning(f"無法讀取 prompts.json：{e}，使用內建 basic prompt。")

    return BUILTIN_BASIC_PROMPT

system_prompt = resolve_prompt(PROMPT_ROLE)

async def query_local_llm(prompt: str) -> str:
    """發送 prompt 給本地 LLM 並回傳回應文字。"""
    url = POST_URL.rstrip('/')
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                # "model": "google/gemma-3-12b",  # 請替換為您實際使用的模型 ID
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 2048,
                "stream": False
            }

            resp = await session.post(url, json=payload)
            resp.raise_for_status()
            data = await resp.json()

            # 嘗試解析回應
            if isinstance(data, dict) and 'choices' in data:
                return data['choices'][0].get('message', {}).get('content', '').strip()

            return str(data)
    except Exception as e:
        logger.error(f"Error querying local LLM: {e}", exc_info=True)
        return f"Error contacting local LLM: {e}"
