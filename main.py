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
import re
from ast import literal_eval
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = FastAPI()

APP_VERSION = "2026-03-28-v4"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"
CLAUDE_API = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

user_buffers = {}
user_timers = {}


@app.on_event("startup")
async def startup_event():
    print(f"BOOT STARTED: {APP_VERSION}")


@app.get("/")
async def health():
    return {
        "status": "running",
        "service": "Telegram Car Ad Generator Bot (Claude API + Google Drive)",
        "version": APP_VERSION,
    }


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
        "Content-Type": "application/json",
    }


async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=20.0) as client:
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
        res.raise_for_status()
        data = res.json()
        file_path = data["result"]["file_path"]
        img_res = await client.get(f"{TELEGRAM_FILE_API}/{file_path}")
        img_res.raise_for_status()
        return img_res.content


async def call_claude_vision(image_bytes: bytes, prompt: str) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            CLAUDE_API,
            headers=get_claude_headers(),
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 1200,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            },
        )
        res.raise_for_status()
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
                    "content": prompt,
                }],
            },
        )
        res.raise_for_status()
        data = res.json()
        if "error" in data:
            raise Exception(data["error"]["message"])
        return data["content"][0]["text"].strip()


def strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.replace("```json", "").replace("```", "").strip()
    return text


def extract_json_block(text: str) -> str:
    text = strip_code_fences(text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def try_parse_json_loose(text: str):
    raw = extract_json_block(text)

    try:
        return json.loads(raw)
    except Exception:
        pass

    try:
        return literal_eval(raw)
    except Exception:
        pass

    try:
        fixed = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)', r'\1"\2"\3', raw)
        fixed = fixed.replace("\n", " ")
        return json.loads(fixed)
    except Exception:
        pass

    return None


async def extract_car_info(image_bytes: bytes) -> dict:
    prompt = """このオークションシートの画像から車の情報を読み取ってください。

【重要ルール】
- 必ず有効なJSONのみを返してください
- JSON以外の文章は一切不要です
- コードブロック（```）は使わないでください
- キー名も値も必ずダブルクォートを使ってください

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

【返答形式】
{"model_code":"型式","car_info":"・メーカー...\n・モデル...\n・年式..."}"""

    result = await call_claude_vision(image_bytes, prompt)
    print("extract_car_info raw:", result)

    # コードフェンスと余分なテキストを除去してからパース
    cleaned = result.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```$", "", cleaned)
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    parsed = try_parse_json_loose(cleaned)
    if isinstance(parsed, dict):
        model_code = str(parsed.get("model_code", "unknown")).strip() or "unknown"
        car_info = str(parsed.get("car_info", "")).strip()
        if not car_info:
            car_info = "・車両情報の抽出結果が空でした"
        return {"model_code": model_code, "car_info": car_info}

    return {
        "model_code": "unknown",
        "car_info": cleaned.strip() if cleaned.strip() else "・車両情報を抽出できませんでした"
    }


async def identify_auction_sheet(image_bytes: bytes) -> bool:
    prompt = """この画像はオークションシート（車の査定票・出品票）ですか？
オークションシートであれば「YES」、車体写真や他の画像であれば「NO」とだけ答えてください。"""
    result = await call_claude_vision(image_bytes, prompt)
    print("identify_auction_sheet:", result)
    return "YES" in result.upper()


async def generate_ads(car_info: str) -> dict:
    import random

    variations = [
        "コンテナ発送対応・まとめ購入歓迎の視点で",
        "中古部品も同時発送可能な事業者向けの視点で",
        "タイヤ・ホイール・エンジン部品もセット販売可能な視点で",
        "バンパー・外装部品・内装部品も一括輸出可能な視点で",
        "海外バイヤー向けの卸売・まとめ買い歓迎の視点で",
    ]
    variation = random.choice(variations)

    prompt = f"""あなたは国際中古車・部品輸出の事業者向けSNS広告のプロです。

【今回の広告の切り口】
{variation}

【厳守ルール】
1. 価格・金額は絶対に書かない
2. オークション・仕入先は書かない
3. 必ず有効なJSONのみ返す
4. 事業者・バイヤー向けのプロフェッショナルなトーンで
5. 必ず全5言語（ja/zh/en/ru/fr）全て生成すること
6. JSON以外の説明文は一切書かない
7. キー名も値も必ずダブルクォートを使う
8. コードブロック（```）は使わない

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

必ず以下のJSON形式で返してください：
{{"ja":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"zh":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"en":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"ru":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"fr":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}}}}"""

    raw = await call_claude_text(prompt)
    print("generate_ads raw:", raw[:200])

    parsed = try_parse_json_loose(raw)
    if isinstance(parsed, dict):
        return parsed

    raise Exception(f"広告文JSONの解析に失敗: {raw[:200]}")


def create_drive_folder(service, folder_name: str, parent_id: str) -> str:
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
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
        "ig": "Instagram",
    }
    langs = [
        ("ja", "日本語"),
        ("zh", "中国語"),
        ("en", "English"),
        ("ru", "Русский"),
        ("fr", "Français"),
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
        images = []
        for file_id in file_ids:
            img_bytes = await get_image_content(file_id)
            images.append(img_bytes)

        if not images:
            await send_message(chat_id, "❌ 画像が取得できませんでした。")
            user_buffers.pop(chat_id, None)
            user_timers.pop(chat_id, None)
            return

        auction_sheet = None
        for img in images:
            is_auction = await identify_auction_sheet(img)
            if is_auction:
                auction_sheet = img
                break

        if auction_sheet is None:
            auction_sheet = images[0]

        car_data = await extract_car_info(auction_sheet)
        model_code = car_data.get("model_code", "unknown")
        car_info = car_data.get("car_info", "")

        safe_model_code = re.sub(r'[\\/:*?"<>|]+', "_", str(model_code)).strip()
        if not safe_model_code:
            safe_model_code = "unknown"

        date_str = datetime.now().strftime("%Y%m%d")
        folder_name = f"{date_str}_{safe_model_code}"

        drive_service = get_drive_service()
        folder_id = create_drive_folder(drive_service, folder_name, GOOGLE_DRIVE_FOLDER_ID)

        for i, img_bytes in enumerate(images):
            upload_to_drive(drive_service, img_bytes, f"photo_{i+1}.jpg", folder_id)

        ads_dict = await generate_ads(car_info)

        ads_text = f"【車情報】\n{car_info}\n"
        ads_text += format_ads_text(ads_dict)

        upload_to_drive(
            drive_service,
            ads_text.encode("utf-8"),
            "広告文.txt",
            folder_id,
            mime_type="text/plain"
        )

        await send_message(chat_id, f"✅ 完了\n📁 {folder_name}")

        user_buffers.pop(chat_id, None)
        user_timers.pop(chat_id, None)

    except Exception as e:
        print(f"Error in process_photos: {e}")
        await send_message(chat_id, f"❌ エラー: {str(e)}")
        user_buffers.pop(chat_id, None)
        user_timers.pop(chat_id, None)


async def delayed_process(chat_id: int):
    await asyncio.sleep(3)
    if chat_id in user_buffers and len(user_buffers[chat_id]) > 0:
        file_ids = user_buffers[chat_id].copy()
        await process_photos(chat_id, file_ids)


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print(f"Webhook: {json.dumps(data, ensure_ascii=False)[:500]}")

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
        text = message["text"].strip().upper()
        if text in ["/START", "START", "/HELP"]:
            await send_message(
                chat_id,
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
