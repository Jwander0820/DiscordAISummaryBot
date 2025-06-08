import os
import logging
import aiohttp
import json

logger = logging.getLogger('discord_digest_bot')

# --- 設定來源 ---
BASE_URL = os.getenv('LOCAL_LLM_URL')
POST_URL = f"{BASE_URL}/v1/chat/completions"
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "system_prompt_role.json")

# --- 內建 fallback prompt（basic） ---
BUILTIN_BASIC_PROMPT = ("你是 Discord 群組裡一位毒舌但熟悉的朋友，會以直率、機靈又帶點調侃的語氣回應大家的問題或要求。"
                        "回答請像熟人一樣自然，不要裝模作樣，也不要自稱機器人或講自己的設定。回覆保持在 100 字內，嘴砲可以，但還是要講重點。")


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


async def query_local_llm(prompt: str, role: str = "basic") -> str:
    """發送 prompt 給本地 LLM 並回傳回應文字。"""
    url = POST_URL.rstrip('/')
    role_prompt = resolve_prompt(role)
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                # "model": "google/gemma-3-12b",  # 請替換為您實際使用的模型 ID
                "messages": [
                    {"role": "system", "content": role_prompt},
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
