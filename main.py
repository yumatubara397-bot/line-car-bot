"""
Telegram Bot - 車オークションシート自動広告生成
Claude API (Anthropic) 使用
複数写真から自動でオークションシートを判別して広告生成
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
    return build("drive", "v3", credentials=creds, cache_discovery=False)

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
        mimetype="text/plain; charset=utf-8"
    )
    meta = {"name": filename, "parents": [folder_id]}
    service.files().create(body=meta, media_body=media, fields="id").execute()

# ============================================================
# Telegram API
# ============================================================
async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )
        print(f"send_message: {r.status_code}")

async def get_file_url(file_id: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{TELEGRAM_API}/getFile?file_id={file_id}")
        data = r.json()
        file_path = data["result"]["file_path"]
        return f"{TELEGRAM_FILE_API}/{file_path}"

async def download_image(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        return r.content

# ============================================================
# Step1: オークションシートを判別
# ============================================================
async def find_auction_sheet(images_b64: list) -> int:
    """複数画像からオークションシートのインデックスを返す"""
    if len(images_b64) == 1:
        return 0

    content = []
    for i, img_b64 in enumerate(images_b64):
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}
        })
    content.append({
        "type": "text",
        "text": f"These are {len(images_b64)} images. Which image is a Japanese car auction sheet (出品表/オークションシート)? It typically has inspection grades, mileage, car condition marks, and vehicle details in Japanese. Reply with ONLY a single number (0-based index). If none is clearly an auction sheet, reply 0."
    })

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": content}]
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(CLAUDE_API, headers=headers, json=payload)
        data = r.json()
        text = data["content"][0]["text"].strip()
        try:
            idx = int(text)
            return min(idx, len(images_b64) - 1)
        except:
            return 0

# ============================================================
# Step2: 広告文生成
# ============================================================
async def generate_ads(image_bytes: bytes) -> dict:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    system_prompt = """You are an expert automotive marketing copywriter for international social media.

STRICT RULES (NEVER VIOLATE):
1. NEVER mention price, cost, or any monetary values
2. NEVER mention auction house, supplier, or acquisition source  
3. NEVER include VIN, chassis number, or license plate
4. Return ONLY valid JSON — no markdown, no explanation

Required JSON format (ALL 5 languages, ALL 5 platforms):
{"ja":{"x":"","fb":"","tt":"","xhs":"","ig":""},"zh":{"x":"","fb":"","tt":"","xhs":"","ig":""},"en":{"x":"","fb":"","tt":"","xhs":"","ig":""},"ru":{"x":"","fb":"","tt":"","xhs":"","ig":""},"fr":{"x":"","fb":"","tt":"","xhs":"","ig":""}}

PLATFORM RULES:
[x] 120 chars max + 3-5 hashtags. Single punchy line.
[fb] 200-400 chars + 3-5 hashtags. Warm, 2-3 paragraphs with emojis.
[tt] 100-200 chars + 5-8 hashtags. Energetic, trendy hook.
[xhs] ALWAYS Chinese regardless of language key. 200-400 chars + 5 Chinese #hashtags. Lifestyle diary with emojis.
[ig] 150-250 chars + 15-25 hashtags on new line. Aesthetic, aspirational."""

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 6000,
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
                        "text": "This is a Japanese car auction sheet. Read all car details (make, model, year, mileage, grade, color, features). Generate ads for all 5 languages × 5 platforms. Return ONLY the JSON object, nothing else."
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

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(CLAUDE_API, headers=headers, json=payload)
        print(f"Claude API: {r.status_code}")
        data = r.json()
        raw = data["content"][0]["text"].strip()
        print(f"Raw response length: {len(raw)}")

        # JSONフェンス除去
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                if part.startswith("json"):
                    raw = part[4:].strip()
                    break
                elif part.strip().startswith("{"):
                    raw = part.strip()
                    break

        # JSON開始・終了を探す
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]

        return json.loads(raw)

# ============================================================
# メイン処理
# ============================================================
SNS_NAMES = {"x": "X(Twitter)", "fb": "Facebook", "tt": "TikTok", "xhs": "小紅書", "ig": "Instagram"}
LANG_NAMES = {"ja": "🇯🇵 日本語", "zh": "🇨🇳 中国語", "en": "🇬🇧 English", "ru": "🇷🇺 Русский", "fr": "🇫🇷 Français"}

async def process_photos(chat_id: int, file_ids: list):
    try:
        await send_message(chat_id, f"📷 {len(file_ids)}枚受信\n🔍 オークションシートを解析中... (約30秒)")

        # 全画像をダウンロード
        images = []
        for fid in file_ids:
            url = await get_file_url(fid)
            img_bytes = await download_image(url)
            images.append(img_bytes)
            print(f"Downloaded image: {len(img_bytes)} bytes")

        # オークションシートを判別
        if len(images) > 1:
            images_b64 = [base64.standard_b64encode(img).decode("utf-8") for img in images]
            sheet_idx = await find_auction_sheet(images_b64)
            print(f"Auction sheet index: {sheet_idx}")
        else:
            sheet_idx = 0

        auction_image = images[sheet_idx]

        # 広告生成
        ads = await generate_ads(auction_image)

        # Google Driveに保存
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"car_ad_{now}"
        drive_saved = False
        drive_error = ""

        try:
            drive = get_drive_service()
            sub_folder_id = create_drive_folder(drive, folder_name, GDRIVE_FOLDER_ID)

            for lang_code, lang_name in LANG_NAMES.items():
                if lang_code not in ads:
                    continue
                lines = [f"=== {lang_name} ===\n"]
                for sns_code, sns_name in SNS_NAMES.items():
                    txt = ads[lang_code].get(sns_code, "")
                    if txt:
                        lines.append(f"【{sns_name}】\n{txt}\n")
                content = "\n".join(lines)
                upload_text_to_drive(drive, sub_folder_id, f"{lang_code}.txt", content)

            drive_saved = True
            print("Drive save: OK")
        except Exception as e:
            drive_error = str(e)
            print(f"Drive error: {e}")

        # 完了通知
        drive_status = "✅ Google Drive保存完了" if drive_saved else f"⚠️ Drive保存失敗\n({drive_error[:80]})"
        await send_message(
            chat_id,
            f"✅ 広告生成完了！\n"
            f"📁 {folder_name}\n"
            f"🌐 5言語 × 5SNS = 25種類\n"
            f"{drive_status}"
        )

    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        await send_message(chat_id, "❌ 広告文の生成に失敗しました。\nオークションシートの写真だけを1枚送ってみてください。")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        await send_message(chat_id, f"❌ エラー: {str(e)[:100]}\n写真を再送してください。")
    finally:
        user_buffers.pop(chat_id, None)
        user_timers.pop(chat_id, None)

async def delayed_process(chat_id: int):
    await asyncio.sleep(5)
    if chat_id in user_buffers and user_buffers[chat_id]:
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

        # タイマーリセット（5秒間待って一括処理）
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
                "📷 写真を送るだけ！\n"
                "（出品表＋車体写真をまとめて送ってOK）\n"
                "→ 自動でオークションシートを判別します\n\n"
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
