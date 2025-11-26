import re
import os
import json
import pickle
import base64
import mimetypes
from typing import Union
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv, set_key
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import encode_rfc2231

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# 載入 .env 變數
load_dotenv()

# 權限範圍
SCOPES = ['https://www.googleapis.com/auth/gmail.send']


def generate_gmail_env_tokens(port: int = 8080, env_path: str = '.env') -> None:
    """
    第一次執行，啟動 OAuth 認證：
      1. 用 client_secret.json 走 run_local_server() 取得憑證並存成 token.pickle
      2. 從 token.pickle 取出 refresh_token
      3. 從 client_secret.json 取出 client_id / client_secret
      4. 將三行 GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN 以及發行時間 GMAIL_REFRESH_TOKEN_ISSUED_AT 寫回 .env
    執行後，請將輸出貼到你的 .env：
      GMAIL_CLIENT_ID=...
      GMAIL_CLIENT_SECRET=...
      GMAIL_REFRESH_TOKEN=...
    """
    # 讀取憑證檔路徑
    secret_path = os.getenv('GMAIL_CLIENT_SECRET_PATH', '../client_secret.json')
    if not os.path.exists(secret_path):
        raise FileNotFoundError(f"找不到 client_secret.json：{secret_path}")

    # 1. OAuth 認證並快取
    flow = InstalledAppFlow.from_client_secrets_file(secret_path, SCOPES)
    creds = flow.run_local_server(port=port)
    with open('../token.pickle', 'wb') as f:
        pickle.dump(creds, f)

    # 2. 取出 refresh_token
    refresh_token = creds.refresh_token

    # 3. 從 client_secret.json 取 client_id / client_secret
    with open(secret_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    info = data.get('installed') or data.get('web')
    client_id = info.get('client_id')
    client_secret = info.get('client_secret')

    # 4. 寫回 .env（只有在抓到值才寫入）
    # 4.1 讀取並寫入 client_id, client_secret, refresh_token
    set_key(env_path, 'GMAIL_CLIENT_ID', client_id)
    set_key(env_path, 'GMAIL_CLIENT_SECRET', client_secret)

    # 4.2 refresh_token 可能是 None
    tz_8 = timezone(timedelta(hours=8))
    issued_at = datetime.now(tz_8).isoformat()
    if refresh_token is not None:
        set_key(env_path, 'GMAIL_REFRESH_TOKEN', refresh_token)
        set_key(env_path, 'GMAIL_REFRESH_TOKEN_ISSUED_AT', issued_at)
    else:
        print("⚠️ Warning: 無法取得 refresh_token，未寫入 GMAIL_REFRESH_TOKEN")

    # 5. 印出結果
    print(f"\n已更新 {env_path}：")
    print(f"  GMAIL_CLIENT_ID={client_id}")
    print(f"  GMAIL_CLIENT_SECRET={client_secret}")
    if refresh_token:
        print(f"  GMAIL_REFRESH_TOKEN={refresh_token}")
        print(f"  GMAIL_REFRESH_TOKEN_ISSUED_AT={issued_at}\n")
    else:
        print(f"  ⚠️ refresh_token 未更新（目前仍有效），跳過寫入\n")


def extract_gmail_env_tokens() -> dict:
    """
    如果你已經有 token.pickle，直接從裡面 + client_secret.json 抽出三個參數，
    並印出可貼 .env 的格式，回傳一個 dict。
    """
    if not os.path.exists('../token.pickle'):
        raise FileNotFoundError("找不到 token.pickle，請先呼叫 generate_gmail_env_tokens() 產生一次")

    # 1. 讀 token.pickle 裡的 refresh_token
    with open('../token.pickle', 'rb') as f:
        creds = pickle.load(f)
    refresh_token = creds.refresh_token

    # 2. 讀 client_secret.json 裡的 client_id / client_secret
    secret_path = os.getenv('GMAIL_CLIENT_SECRET_PATH', '../client_secret.json')
    if not os.path.exists(secret_path):
        raise FileNotFoundError(f"找不到 client_secret.json：{secret_path}")

    with open(secret_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    info = data.get('installed') or data.get('web')
    client_id = info['client_id']
    client_secret = info['client_secret']

    # 印出 .env 格式
    print("\n請將以下三行貼到你的 .env：\n")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={refresh_token}\n")

    return {
        'GMAIL_CLIENT_ID': client_id,
        'GMAIL_CLIENT_SECRET': client_secret,
        'GMAIL_REFRESH_TOKEN': refresh_token
    }


def gmail_build_service():
    """
    建立可在任何環境（含 Render）使用的 Gmail Service：
      - 直接從環境變數讀取 client_id, client_secret, refresh_token
      - 自動用 Refresh Token 換取 Access Token
    """
    client_id     = os.getenv('GMAIL_CLIENT_ID')
    client_secret = os.getenv('GMAIL_CLIENT_SECRET')
    refresh_token = os.getenv('GMAIL_REFRESH_TOKEN')
    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError(
            "請先在 .env 中設定 GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN"
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES
    )
    # 自動向 Google 換新的 access token
    creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds)


def _attach_token_expiry_notice(body: str) -> str:
    """
    在郵件內容尾端附上：
      1. Refresh Token 過期時間（發行日 + 7 天）
      2. 距離過期還剩多少天
    時間都以 UTC+8 顯示。
    """
    issued_str = os.getenv('GMAIL_REFRESH_TOKEN_ISSUED_AT')
    if not issued_str:
        return body

    # 解析發行時間
    tz8 = timezone(timedelta(hours=8))
    try:
        issued_at = datetime.fromisoformat(issued_str)
    except ValueError:
        # 如果沒有時區標記就補上
        issued_at = datetime.fromisoformat(issued_str).replace(tzinfo=tz8)

    # 計算過期時間與剩餘天數
    expiry    = issued_at + timedelta(days=7)
    now       = datetime.now(tz8)
    diff = expiry - now
    total_seconds = int(diff.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    notice = (
        "\n\n----\n"
        f"⚠️ Refresh Token 過期時間：{expiry.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)\n"
        f"⌛️ 距離過期還有：{days} 天 {hours} 小時 {minutes} 分鐘\n"
    )
    return body + notice


def send_email(
    to, subject, body,
    *,
    attachment_path=None,
    attachment_data=None,
    attachment_filename=None
):
    service = gmail_build_service()

    # 附上 token 過期提醒
    body = _attach_token_expiry_notice(body)

    msg = MIMEMultipart()
    msg['to'], msg['from'], msg['subject'] = to, 'me', subject
    msg.attach(MIMEText(body, 'plain'))

    # 處理記憶體檔案 or 實體檔案
    if attachment_path:
        maintype, subtype = mimetypes.guess_type(attachment_path)[0].split('/',1)
        with open(attachment_path,'rb') as f:
            part = MIMEBase(maintype, subtype)
            part.set_payload(f.read())
    elif attachment_data and attachment_filename:
        # 推斷或指定 MIME type
        ctype = mimetypes.guess_type(attachment_filename)[0] or 'application/octet-stream'
        maintype, subtype = ctype.split('/',1)
        part = MIMEBase(maintype, subtype)
        payload = attachment_data if isinstance(attachment_data, bytes) else attachment_data.encode('utf-8')
        part.set_payload(payload)
    else:
        part = None

    if part:
        encoders.encode_base64(part)
        # 用 RFC2231 編碼中文檔名
        fn = attachment_filename or os.path.basename(attachment_path)
        fn_hdr = encode_rfc2231(fn, 'utf-8')
        part.add_header('Content-Disposition', f'attachment; filename*={fn_hdr}')
        msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = service.users().messages().send(userId='me', body={'raw': raw}).execute()
    return sent.get('id')


def send_sarn_notify(record: dict, to: str) -> str:
    """
    根據 record 自動
      1. 組出 subject、body
      2. 將 record 序列化為 JSON 並依 call_time+command+channel_id 產生檔名
      3. 呼叫 send_email() 發信並回傳 messageId

    record 應包含：
      - user_id, channel_id, command, question, summary, call_time (ISO str)
    to: 收件人 email
    """
    # 1. 拆欄位
    user_id    = record["user_id"]
    channel_id = record["channel_id"]
    command    = record["command"]
    question   = record["question"]
    summary    = record["summary"]
    call_time  = record["call_time"]  # ISO 格式 e.g. "2025-06-09T16:00:00+08:00"

    # 2. subject & body
    subject = f"【SERN Notify】{user_id} 在 {channel_id} 使用了 {command}"
    body = (
        f"📣 SERN Notify\n"
        f"🔹 使用者    : {user_id}\n"
        f"🔹 頻道      : {channel_id}\n"
        f"🔹 指令      : {command}\n\n"
        f"📝 使用者提問\n"
        f"> {question}\n\n"
        f"💡 AI 回覆\n"
        f"> {summary}\n\n"
        f"⏰ 執行時間\n"
        f"> {call_time}\n"
    )

    # 3. 把 record 轉 JSON 串
    json_str = json.dumps(record, ensure_ascii=False, indent=2)

    # 4. 產生檔名：把 ISO call_time 轉成 YYYYMMDDHHMMSS
    try:
        dt = datetime.fromisoformat(call_time)
    except ValueError:
        # 如果含時區不被支援，可去掉時區後再 parse
        dt = datetime.fromisoformat(call_time.rstrip("Z"))
    ts = dt.strftime("%Y%m%d%H%M%S")
    filename = f"{ts}-{command}-{channel_id}.json"

    # 5. 呼叫 send_email，attachment_data + attachment_filename
    msg_id = send_email(
        to=to,
        subject=subject,
        body=body,
        attachment_data=json_str,
        attachment_filename=filename
    )
    return msg_id


def send_error_notify(error: Exception, record: dict, to: str) -> str:
    """
    發送錯誤通知信：
      - error: 捕捉到的 Exception
      - record: 原本的 record dict (包含 user_id, channel_id, command, question...)
      - to: 收件人 email
    """
    # 取得 GMT+8 當前時間
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz).isoformat()

    # 組 subject
    subject = f"【SERN Error】 指令 {record.get('command')} 執行失敗"

    # 組 body，用 Emoji + 短標題
    body = (
        "🔴 SERN Error Notify\n"
        f"🔹 指令      : {record.get('command')}\n"
        f"🔹 使用者    : {record.get('user_id')}\n"
        f"🔹 頻道      : {record.get('channel_id')}\n\n"
        f"❗ 錯誤內容\n"
        f"> {error}\n\n"
        f"⏰ 時間\n"
        f"> {now}\n\n"
    )

    # 也把 record 序列化附檔，方便事後追蹤
    json_str = json.dumps(record, ensure_ascii=False, indent=2)
    # 用時間+指令+頻道當檔名，並 sanitize（去掉冒號等）
    ts = now.replace(":", "").split("+")[0].replace("-", "")
    safe_cmd = record.get("command").replace(" ", "_")
    safe_ch  = record.get("channel_id").replace(" ", "_")
    filename = f"{ts}-{safe_cmd}-{safe_ch}-ERROR.json"

    # 呼叫 send_email
    return send_email(
        to=to,
        subject=subject,
        body=body,
        attachment_data=json_str,
        attachment_filename=filename
    )


def send_deepfaker_notify(record: dict, to: str) -> str:
    """
    根據 record 自動
      1. 組出 subject、body
      2. 將 record 序列化為 JSON 並依 call_time+command+channel_id 產生檔名
      3. 呼叫 send_email() 發信並回傳 messageId

    record 應包含：
      - user_id, channel_id, command, question, summary, call_time (ISO str)
    to: 收件人 email
    """
    # 1. 拆欄位
    user_id    = record["user_id"]
    channel_id = record["channel_id"]
    command    = record["command"]
    question   = record["question"]
    prompt     = record["prompt"]
    summary    = record["summary"]
    call_time  = record["call_time"]  # ISO 格式 e.g. "2025-06-09T16:00:00+08:00"

    # 2. subject & body
    subject = f"【SERN Notify】{user_id} 在 {channel_id} 使用了 {command}"
    body = (
        f"📣 SERN Notify\n"
        f"🔹 使用者    : {user_id}\n"
        f"🔹 頻道      : {channel_id}\n"
        f"🔹 指令      : {command}\n\n"
        f"📝 {user_id} 寫了 :\n"
        f"> {prompt}\n\n"
        f"💡 log\n"
        f"> {question}\n\n"
        f"> {summary}\n\n"
        f"⏰ 執行時間\n"
        f"> {call_time}\n"
    )

    # 3. 把 record 轉 JSON 串
    json_str = json.dumps(record, ensure_ascii=False, indent=2)

    # 4. 產生檔名：把 ISO call_time 轉成 YYYYMMDDHHMMSS
    try:
        dt = datetime.fromisoformat(call_time)
    except ValueError:
        # 如果含時區不被支援，可去掉時區後再 parse
        dt = datetime.fromisoformat(call_time.rstrip("Z"))
    ts = dt.strftime("%Y%m%d%H%M%S")
    filename = f"{ts}-{command}-{channel_id}.json"

    # 5. 呼叫 send_email，attachment_data + attachment_filename
    msg_id = send_email(
        to=to,
        subject=subject,
        body=body,
        attachment_data=json_str,
        attachment_filename=filename
    )
    return msg_id


if __name__ == "__main__":
    generate_gmail_env_tokens()
