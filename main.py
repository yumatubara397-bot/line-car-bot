"""
Telegram Bot - 車オークションシート自動広告生成
Claude API (Anthropic) + Google Drive 使用
"""

import os
import json
import base64
import asyncio
import httpx
import io
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = FastAPI()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"
CLAUDE_API = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

user_photos = {}

def get_drive_service():
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def get_claude_headers():
    return {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }

async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )
        print(f"send_message status: {r.status_code}")

async def get_image_content(file_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(
            f"{TELEGRAM_API}/getFile",
            params={"file_id": file_id}
        )
        file_path = res.json()["result"]["file_path"]
        img_res = await client.get(f"{TELEGRAM_FILE_API}/{file_path}")
        return img_res.content

async def call_claude_vision(image_bytes: bytes, prompt: str) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            CLAUDE_API,
            headers=get_claude_headers(),
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 1000,
                "messages": [{
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
                        {"type": "text", "text": prompt}
                    ]
                }]
            }
        )
        data = res.json()
        if "error" in data:
            raise Exception(data["error"]["message"])
        return data["content"][0]["text"].strip()

async def call_claude_text(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            CLAUDE_API,
            headers=get_claude_headers(),
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 8000,
                "messages": [{
                    "role": "user",
                    "content": prompt
                }]
            }
        )
        data = res.json()
        if "error" in data:
            raise Exception(data["error"]["message"])
        return data["content"][0]["text"].strip()

async def extract_car_info(image_bytes: bytes) -> dict:
    prompt = """このオークションシートの画像から車の情報を読み取ってください。

【除外する情報（絶対に含めない）】
- オークション名・仕入先・出品者情報
- 価格・R券・落札金額などの金額情報

【抽出する情報】
- メーカー・ブランド
- モデル名・グレード
- 型式（例：ZN6、HE12など）
- 年式、走行距離、排気量
- ミッション種類（AT/MT）
- ボディカラー
- 主要装備・オプション
- 車検有効期限
- 修復歴の有無
- 状態・コンディション

以下のJSON形式で返してください（他のテキスト不要）：
{"model_code":"型式","car_info":"日本語の箇条書き情報"}"""

    result = await call_claude_vision(image_bytes, prompt)
    clean = result.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except:
        return {"model_code": "unknown", "car_info": result}

async def generate_ads(car_info: str) -> str:
    import random

    variations = [
        "コンテナ発送対応・まとめ購入歓迎の視点で",
        "中古部品も同時発送可能な事業者向けの視点で",
        "タイヤ・ホイール・エンジン部品もセット販売可能な視点で",
        "バンパー・外装部品・内装部品も一括輸出可能な視点で",
        "海外バイヤー向けの卸売・まとめ買い歓迎の視点で"
    ]
    variation = random.choice(variations)

    prompt = f"""あなたは国際中古車・部品輸出の事業者向けSNS広告のプロです。

【今回の広告の切り口】
{variation}

【厳守ルール】
1. 価格・金額は絶対に書かない
2. オークション・仕入先は書かない
3. JSONのみ返す
4. 事業者・バイヤー向けのプロフェッショナルなトーンで

【車情報】
{car_info}

【対応可能なサービス（広告に自然に含める）】
- コンテナ発送・まとめ購入対応
- 中古タイヤ・ホイール同時発送可
- エンジン・ミッション部品も取扱
- バンパー・外装・内装部品も一括輸出可
- 世界各地への輸出実績あり

【各SNSの仕様】
X(Twitter): 全角140文字以内、ハッシュタグ3〜5個、力強く簡潔
Facebook: 全角300〜500文字、ハッシュタグ3〜5個、ストーリー調絵文字使用
TikTok: 全角300〜500文字、ハッシュタグ3〜5個、エネルギッシュ
Instagram: 全角300〜350文字、ハッシュタグ3〜5個、ライフスタイル訴求
小紅書: 必ず中国語、全角300〜500文字、#标签形式、日記風絵文字多め

JSONのみ返してください：
{{"ja":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"zh":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"en":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"ru":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"fr":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}}}}"""

    raw = await call_claude_text(prompt)
    clean = raw.replace("```json", "").replace("```", "").strip()
    return clean

def create_drive_folder(service, folder_name: str, parent_id: str) -> str:
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(body=file_metadata, fields="id").execute()
    return folder.get("id")

def upload_to_drive(service, file_bytes: bytes, filename: str, folder_id: str, mime_type: str = "image/jpeg"):
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type)
    service.files().create(body=file_metadata, media_body=media, fields="id").execute()

def format_ads_message(ads_dict: dict, lang: str, flag: str, title: str) -> str:
    sns_labels = {
        "x": "X (Twitter)", "fb": "Facebook",
        "tt": "TikTok", "xhs": "小紅書 (RED)", "ig": "Instagram"
    }
    text = f"{flag} [{title}]\n" + "-"*20 + "\n\n"
    for sns_id, label in sns_labels.items():
        text += f"[{label}]\n{ads_dict.get(lang, {}).get(sns_id, '')}\n\n"
    return text.strip()

async def process_photos(chat_id: int, photos_data: list):
    try:
        await send_message(chat_id, "Processing... Please wait about 1 minute.")

        images = []
        for file_id in photos_data:
            img_bytes = await get_image_content(file_id)
            images.append(img_bytes)

        await send_message(chat_id, "Analyzing car information...")
        car_data = await extract_car_info(images[0])
        model_code = car_data.get("model_code", "unknown")
        car_info = car_data.get("car_info", "")

        date_str = datetime.now().strftime("%Y%m%d")
        folder_name = f"{date_str}_{model_code}"

        drive_service = get_drive_service()
        folder_id = create_drive_folder(drive_service, folder_name, GOOGLE_DRIVE_FOLDER_ID)

        for i, img_bytes in enumerate(images):
            filename = f"photo_{i+1}.jpg"
            upload_to_drive(drive_service, img_bytes, filename, folder_id)

        await send_message(chat_id,
            f"Car info extracted!\n\n{car_info}\n\nGenerating ads (5 languages x 5 SNS = 25 types)..."
        )

        ads_raw = await generate_ads(car_info)
        ads_dict = json.loads(ads_raw)

        ads_text = f"[Car Info]\n{car_info}\n\n"
        for lang, flag, title in [
            ("ja", "JP", "Japanese"), ("zh", "CN", "Chinese"),
            ("en", "EN", "English"), ("ru", "RU", "Russian"), ("fr", "FR", "French")
        ]:
            ads_text += format_ads_message(ads_dict, lang, flag, title) + "\n\n"

        upload_to_drive(
            drive_service,
            ads_text.encode("utf-8"),
            "ads.txt",
            folder_id,
            mime_type="text/plain"
        )

        await send_message(chat_id, f"Ads generated! Saved to Drive: {folder_name}")

        for lang, flag, title in [
            ("ja", "JP", "Japanese"), ("zh", "CN", "Chinese"),
            ("en", "EN", "English"), ("ru", "RU", "Russian"), ("fr", "FR", "French")
        ]:
            msg = format_ads_message(ads_dict, lang, flag, title)
            await send_message(chat_id, msg)

        await send_message(chat_id, "Done! Send photos of the next car.")

        user_photos.pop(chat_id, None)

    except Exception as e:
        print(f"Error: {e}")
        await send_message(chat_id, f"Error: {str(e)}\n\nPlease resend the photos.")
        user_photos.pop(chat_id, None)

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
        if chat_id not in user_photos:
            user_photos[chat_id] = []
        user_photos[chat_id].append(file_id)

        count = len(user_photos[chat_id])
        await send_message(chat_id,
            f"Photo {count} received! Send more photos or type OK when ready."
        )

    elif "text" in message:
        text = message["text"].strip().upper()

        if text == "OK":
            if chat_id in user_photos and len(user_photos[chat_id]) > 0:
                photos = user_photos[chat_id].copy()
                asyncio.create_task(process_photos(chat_id, photos))
            else:
                await send_message(chat_id, "No photos found. Please send photos first.")

        elif text in ["/START", "START", "/HELP"]:
            await send_message(chat_id,
                "Car Ad Generator Bot\n\n"
                "How to use:\n"
                "1. Send auction sheet photo\n"
                "2. Send car body photos\n"
                "3. Type OK\n"
                "4. Ads will be generated and saved to Drive!\n\n"
                "Languages: JP / CN / EN / RU / FR\n"
                "Platforms: X / Facebook / TikTok / Xiaohongshu / Instagram\n"
                "= 25 types of ads!\n\n"
                "Services:\n"
                "Container shipping available\n"
                "Parts also available: tires, engines, bumpers, etc."
            )
        else:
            await send_message(chat_id,
                "Please send auction sheet and car photos.\nType OK when ready."
            )

    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def health():
    return {"status": "running", "service": "Telegram Car Ad Generator Bot (Claude API + Google Drive)"}
