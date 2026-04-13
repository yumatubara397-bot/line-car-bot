"""
Telegram Bot - 車オークションシート自動広告生成
Claude API (Anthropic) 使用
5言語 × 5SNS = 25種類の広告文を生成
Google Driveにフォルダ分けして保存
"""

import os
import json
import base64
import asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io
from datetime import datetime

app = FastAPI()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GDRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"
CLAUDE_API = "https://api.anthropic.com/v1/messages"

# ユーザーごとの写真バッファとタイマー
user_buffers = {}
user_timers = {}

# ============================================================
# Google Drive
# ============================================================
def get_drive_service():
    creds_json = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def create_drive_folder(service, folder_name, parent_id):
    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]

def upload_text_to_drive(service, folder_id, filename, content):
    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")),
        mimetype="text/plain"
    )
    meta = {"name": filename, "parents": [folder_id]}
    service.files().create(body=meta, media_body=media, fields="id").execute()

# ============================================================
# Telegram API
# ============================================================
async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )
        print(f"send_message: {r.status_code}")

async def get_file_url(file_id: str) -> str:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TELEGRAM_API}/getFile?file_id={file_id}")
        data = r.json()
        file_path = data["result"]["file_path"]
        return f"{TELEGRAM_FILE_API}/{file_path}"

async def download_image(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        return r.content

# ============================================================
# Claude API で広告文生成
# ============================================================
async def generate_ads(image_bytes: bytes) -> dict:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    system_prompt = """You are an expert automotive marketing copywriter for international social media.

STRICT RULES (NEVER VIOLATE):
1. NEVER mention price, cost, or any monetary values
2. NEVER mention auction house, supplier, or acquisition source
3. NEVER include VIN, chassis number, or license plate
4. Return ONLY valid JSON — no markdown fences, no explanation text

Required JSON format:
{"ja":{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."},"zh":{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."},"en":{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."},"ru":{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."},"fr":{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}}

=== PLATFORM SPECIFICATIONS ===

[X (Twitter)] Max 140 chars body + 3-5 hashtags on new line. Punchy, one hook.
[Facebook] 300-500 chars + 3-5 hashtags. Warm, storytelling, emojis, 2-3 paragraphs.
[TikTok] 150-300 chars + 5-10 hashtags. Energetic, trendy, hook in first line.
[小紅書 xhs] ALWAYS Chinese in ALL languages. 300-500 chars + 5-10 Chinese hashtags. Lifestyle diary style with emojis.
[Instagram] 200-300 chars + 20-30 hashtags in separate block. Aesthetic, aspirational.

Languages: ja=Japanese, zh=Chinese, en=English, ru=Russian, fr=French"""

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4000,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": "This is a Japanese car auction sheet. Extract the car info and generate ads in all 5 languages for all 5 platforms. Return ONLY JSON."
                    }
                ]
            }
        ]
    }

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(CLAUDE_API, headers=headers, json=payload)
        print(f"Claude API: {r.status_code}")
        data = r.json()
        text = data["content"][0]["text"].strip()
        # JSONフェンス除去
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())

# ============================================================
# メイン処理
# ============================================================
SNS_NAMES = {"x": "X(Twitter)", "fb": "Facebook", "tt": "TikTok", "xhs": "小紅書", "ig": "Instagram"}
LANG_NAMES = {"ja": "🇯🇵日本語", "zh": "🇨🇳中国語", "en": "🇬🇧English", "ru": "🇷🇺Русский", "fr": "🇫🇷Français"}

async def process_photos(chat_id: int, file_ids: list):
    try:
        await send_message(chat_id, f"📷 {len(file_ids)}枚受信\n🔍 車情報を解析中... (約30秒)")

        # 最初の写真（オークションシート）を使用
        url = await get_file_url(file_ids[0])
        image_bytes = await download_image(url)
        print(f"Image size: {len(image_bytes)} bytes")

        # 広告生成
        ads = await generate_ads(image_bytes)

        # Google Driveに保存
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"car_ad_{now}"

        try:
            drive = get_drive_service()
            sub_folder_id = create_drive_folder(drive, folder_name, GDRIVE_FOLDER_ID)

            # 言語ごとにテキストファイル保存
            for lang_code, lang_name in LANG_NAMES.items():
                if lang_code not in ads:
                    continue
                content = f"=== {lang_name} ===\n\n"
                for sns_code, sns_name in SNS_NAMES.items():
                    if sns_code in ads[lang_code]:
                        content += f"【{sns_name}】\n{ads[lang_code][sns_code]}\n\n"
                upload_text_to_drive(drive, sub_folder_id, f"{lang_code}_{now}.txt", content)

            drive_saved = True
        except Exception as e:
            print(f"Drive error: {e}")
            drive_saved = False

        # 完了通知のみ送信（広告文は送らない）
        drive_status = "✅ Google Driveに保存完了" if drive_saved else "⚠️ Drive保存失敗"
        await send_message(
            chat_id,
            f"✅ 広告生成完了！\n"
            f"📁 フォルダ: {folder_name}\n"
            f"🌐 5言語 × 5SNS = 25種類\n"
            f"{drive_status}"
        )

    except Exception as e:
        print(f"Error: {e}")
        await send_message(chat_id, f"❌ エラー: {str(e)}\n写真を再送してください。")
    finally:
        user_buffers.pop(chat_id, None)
        user_timers.pop(chat_id, None)

async def delayed_process(chat_id: int):
    await asyncio.sleep(5)
    if chat_id in user_buffers and len(user_buffers[chat_id]) > 0:
        file_ids = user_buffers[chat_id].copy()
        await process_photos(chat_id, file_ids)

# ============================================================
# Webhook
# ============================================================
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print(f"Webhook: {json.dumps(data)[:200]}")

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    if not chat_id:
        return JSONResponse(content={"status": "ok"})

    if "photo" in message:
        file_id = message["photo"][-1]["file_id"]

        if chat_id not in user_buffers:
            user_buffers[chat_id] = []
        user_buffers[chat_id].append(file_id)

        if chat_id in user_timers:
            user_timers[chat_id].cancel()

        timer = asyncio.create_task(delayed_process(chat_id))
        user_timers[chat_id] = timer

    elif "text" in message:
        text = message["text"].strip()
        if text in ["/start", "/help"]:
            await send_message(
                chat_id,
                "🚗 車広告自動生成Bot\n\n"
                "オークションシートの写真を送るだけ！\n\n"
                "【生成内容】\n"
                "🇯🇵日本語 / 🇨🇳中国語 / 🇬🇧英語\n"
                "🇷🇺ロシア語 / 🇫🇷フランス語\n"
                "× X / Facebook / TikTok / 小紅書 / Instagram\n"
                "= 25種類の広告文\n\n"
                "✅ Google Driveに自動保存\n"
                "※仕入先・価格は自動で除外"
            )

    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def health():
    return {"status": "running", "service": "Telegram Car Ad Generator (Claude API)"}
