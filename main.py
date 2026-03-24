"""
Telegram Bot - 車オークションシート自動広告生成
Claude API (Anthropic) + Google Drive 使用
写真送信で自動処理開始
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

# ユーザーごとの受信バッファ（複数枚まとめ送信対応）
user_buffers = {}
user_timers = {}

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

以下のJSON形式のみで返してください：
{"model_code":"型式","car_info":"日本語の箇条書き情報"}"""

    result = await call_claude_vision(image_bytes, prompt)
    clean = result.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except:
        return {"model_code": "unknown", "car_info": result}

async def identify_auction_sheet(image_bytes: bytes) -> bool:
    prompt = """この画像はオークションシート（車の査定票・出品票）ですか？
オークションシートであれば「YES」、車体写真や他の画像であれば「NO」とだけ答えてください。"""
    result = await call_claude_vision(image_bytes, prompt)
    return "YES" in result.upper()

async def generate_ads(car_info: str) -> dict:
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
5. 必ず全5言語（ja/zh/en/ru/fr）全て生成すること

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

必ず以下のJSON形式で5言語全て返してください：
{{"ja":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"zh":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"en":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"ru":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"fr":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}}}}"""

    raw = await call_claude_text(prompt)
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

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

def format_ads_text(ads_dict: dict) -> str:
    sns_labels = {
        "x": "X (Twitter)",
        "fb": "Facebook",
        "tt": "TikTok",
        "xhs": "小紅書 (RED)",
        "ig": "Instagram"
    }
    langs = [
        ("ja", "🇯🇵 日本語"),
        ("zh", "🇨🇳 中国語"),
        ("en", "🇬🇧 English"),
        ("ru", "🇷🇺 Русский"),
        ("fr", "🇫🇷 Français")
    ]
    text = ""
    for lang, title in langs:
        text += f"\n{'='*30}\n{title}\n{'='*30}\n\n"
        lang_data = ads_dict.get(lang, {})
        for sns_id, label in sns_labels.items():
            content = lang_data.get(sns_id, "")
            text += f"[{label}]\n{content}\n\n"
    return text

async def process_photos(chat_id: int, file_ids: list):
    try:
        # 画像をダウンロード
        images = []
        for file_id in file_ids:
            img_bytes = await get_image_content(file_id)
            images.append(img_bytes)

        # オークションシートを特定
        auction_sheet = None
        for img in images:
            is_auction = await identify_auction_sheet(img)
            if is_auction:
                auction_sheet = img
                break

        # 見つからない場合は最初の画像を使用
        if auction_sheet is None:
            auction_sheet = images[0]

        # 車情報抽出
        car_data = await extract_car_info(auction_sheet)
        model_code = car_data.get("model_code", "unknown")
        car_info = car_data.get("car_info", "")

        # Driveフォルダ作成
        date_str = datetime.now().strftime("%Y%m%d")
        folder_name = f"{date_str}_{model_code}"
        drive_service = get_drive_service()
        folder_id = create_drive_folder(drive_service, folder_name, GOOGLE_DRIVE_FOLDER_ID)

        # 写真を保存
        for i, img_bytes in enumerate(images):
            upload_to_drive(drive_service, img_bytes, f"photo_{i+1}.jpg", folder_id)

        # 広告文生成
        ads_dict = await generate_ads(car_info)

        # 広告文テキスト作成・保存
        ads_text = f"【車情報】\n{car_info}\n"
        ads_text += format_ads_text(ads_dict)
        upload_to_drive(
            drive_service,
            ads_text.encode("utf-8"),
            "広告文.txt",
            folder_id,
            mime_type="text/plain"
        )

        # Telegramには完了通知のみ
        await send_message(chat_id, f"✅ 完了\n📁 {folder_name}")

        # バッファをリセット
        user_buffers.pop(chat_id, None)
        user_timers.pop(chat_id, None)

    except Exception as e:
        print(f"Error: {e}")
        await send_message(chat_id, f"❌ エラー: {str(e)}")
        user_buffers.pop(chat_id, None)
        user_timers.pop(chat_id, None)

async def delayed_process(chat_id: int):
    # 3秒待って追加写真を待つ
    await asyncio.sleep(3)
    if chat_id in user_buffers and len(user_buffers[chat_id]) > 0:
        file_ids = user_buffers[chat_id].copy()
        await process_photos(chat_id, file_ids)

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print(f"Webhook: {json.dumps(data)[:200]}")

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    if not chat_id:
        return JSONResponse(content={"status": "ok"})

    # 画像受信
    if "photo" in message:
        file_id = message["photo"][-1]["file_id"]

        if chat_id not in user_buffers:
            user_buffers[chat_id] = []
        user_buffers[chat_id].append(file_id)

        # 既存タイマーをキャンセルして再設定
        if chat_id in user_timers:
            user_timers[chat_id].cancel()

        timer = asyncio.create_task(delayed_process(chat_id))
        user_timers[chat_id] = timer

    # テキスト受信
    elif "text" in message:
        text = message["text"].strip().upper()
        if text in ["/START", "START", "/HELP"]:
            await send_message(chat_id,
                "🚗 車広告自動生成Bot\n\n"
                "【使い方】\n"
                "写真を送るだけで自動処理！\n"
                "（オークションシート＋車体写真）\n\n"
                "【生成物】\n"
                "5言語×5SNS=25種類の広告文\n"
                "Google Driveに自動保存\n\n"
                "【言語】🇯🇵 🇨🇳 🇬🇧 🇷🇺 🇫🇷\n"
                "【SNS】X/FB/TikTok/小紅書/IG"
            )

    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def health():
    return {"status": "running", "service": "Telegram Car Ad Generator Bot (Claude API + Google Drive)"}
