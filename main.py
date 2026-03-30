"""
Telegram Bot - 車オークションシート自動広告生成
Claude API (Anthropic) + Google Drive 使用
写真送信で自動処理開始
業者向け広告特化版
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

APP_VERSION = "2026-03-30-v2"

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
    async with httpx.AsyncClient(timeout=30.0) as client:
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
                "max_tokens": 1500,
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


def extract_json_block(text: str) -> str:
    text = text.strip()
    text = re.sub(r"```[a-zA-Z]*", "", text)
    text = text.replace("```", "").strip()
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
        return json.loads(fixed)
    except Exception:
        pass
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", raw)
        return json.loads(fixed)
    except Exception:
        pass
    return None


async def identify_auction_sheet(image_bytes: bytes) -> bool:
    prompt = """この画像はオークションシート（車の査定票・出品票）ですか？
オークションシートであれば「YES」、車体写真や他の画像であれば「NO」とだけ答えてください。"""
    result = await call_claude_vision(image_bytes, prompt)
    print("identify_auction_sheet:", result)
    return "YES" in result.upper()


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
- 状態・コンディション（オークション評価点）

返答形式：
{"model_code":"ZN6","car_info":"・メーカー：トヨタ\\n・モデル：86\\n・年式：2012年"}"""

    result = await call_claude_vision(image_bytes, prompt)
    print("extract_car_info raw:", result[:500])

    parsed = try_parse_json_loose(result)
    if isinstance(parsed, dict):
        model_code = str(parsed.get("model_code", "unknown")).strip() or "unknown"
        car_info = str(parsed.get("car_info", "")).strip()
        if not car_info:
            car_info = "・車両情報の抽出結果が空でした"
        return {"model_code": model_code, "car_info": car_info}

    return {
        "model_code": "unknown",
        "car_info": result.strip() if result.strip() else "・車両情報を抽出できませんでした"
    }


async def generate_ads(car_info: str) -> dict:
    import random

    variations = [
        {
            "theme": "dealer_sourcing",
            "desc": "日本から世界の業者向けに車を供給している業者として。忙しく仕入れている雰囲気で。"
        },
        {
            "theme": "container_shipping",
            "desc": "コンテナ発送・まとめ購入対応をアピール。海外業者へのBtoB供給者として。"
        },
        {
            "theme": "auction_quality",
            "desc": "日本オークション直仕入れの品質をアピール。クリーンな車・評価点をアピール。"
        },
        {
            "theme": "wholesale_price",
            "desc": "業者価格・卸売対応をアピール。大量購入・リピート業者歓迎の雰囲気で。"
        },
        {
            "theme": "parts_bundle",
            "desc": "タイヤ・エンジン部品・外装部品も同時発送可能な総合輸出業者として。"
        },
    ]
    variation = random.choice(variations)

    prompt = f"""あなたは在日本の中古車輸出業者として、海外の車ディーラー・業者向けにSNS広告を作成するプロです。

【今回の広告テーマ】
{variation["desc"]}

【参考にすべきトーン・スタイル】
- 短くて力強い業者向けメッセージ
- 英語と中国語（または現地語）を組み合わせるスタイル
- ハッシュタグは業界向け（#japanusedcars #carexport #cardealer #japanauction など）
- 例文スタイル：
  "Japanese used cars ready for export 🚗 / Good condition / auction grade cars / Dealers welcome."
  "日本二手车出口 / 车况好 / 拍卖车源 / 车商合作欢迎联系"
  "Busy day sourcing cars for my clients 🚗 / Direct from Japan auction / Dealers welcome."
  "Container shipping available / 可装柜发货"
  "Wholesale cars from Japan / 日本批发二手车 / 欢迎联系"

【厳守ルール】
1. 価格・金額は絶対に書かない
2. オークション名・仕入先は書かない
3. 必ず有効なJSONのみ返す
4. 全5言語（ja/zh/en/ru/fr）全て生成すること
5. JSON以外の説明文は一切書かない
6. コードブロック（```）は使わない
7. キー名も値も必ずダブルクォートを使う

【車情報】
{car_info}

【対応サービス（自然に含める）】
- Container shipping available / コンテナ発送対応
- Parts also available: tires, engines, bumpers / 部品も同時発送可
- Wholesale / dealer price / 卸売・業者価格対応
- Direct from Japan auction / 日本オークション直仕入れ
- Worldwide export / 世界各地への輸出実績

【各SNSの仕様】
X(Twitter): 140文字以内、ハッシュタグ3〜5個、短く力強く
Facebook: 300〜500文字、ハッシュタグ3〜5個、業者向けストーリー調
TikTok: 300〜500文字、ハッシュタグ3〜5個、エネルギッシュ
Instagram: 300〜350文字、ハッシュタグ3〜5個、プロフェッショナル
小紅書: 必ず中国語、300〜500文字、#标签形式、業者向け

必ず以下のJSON形式で返してください：
{{"ja":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"zh":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"en":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"ru":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"fr":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}}}}"""

    raw = await call_claude_text(prompt)
    print("generate_ads raw:", raw[:300])

    parsed = try_parse_json_loose(raw)
    if isinstance(parsed, dict):
        return parsed

    raise Exception(f"広告文JSONの解析に失敗: {raw[:300]}")


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


def format_ads_for_drive(ads_dict: dict, car_info: str) -> str:
    platforms = [
        ("x", "X (Twitter)"),
        ("fb", "Facebook"),
        ("tt", "TikTok"),
        ("ig", "Instagram"),
        ("xhs", "小紅書"),
    ]
    languages = [
        ("ja", "日本語"),
        ("zh", "中国語"),
        ("en", "English"),
        ("ru", "Русский"),
        ("fr", "Français"),
    ]
    text = f"【車情報】\n{car_info}\n\n"
    for lang_key, lang_name in languages:
        text += f"\n{'='*30}\n{lang_name}\n{'='*30}\n\n"
        for platform_key, platform_name in platforms:
            content = ads_dict.get(lang_key, {}).get(platform_key, "")
            text += f"[{platform_name}]\n{content}\n\n"
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

        # オークションシートを特定
        auction_sheet = None
        for img in images:
            if await identify_auction_sheet(img):
                auction_sheet = img
                break
        if auction_sheet is None:
            auction_sheet = images[0]

        # 車情報抽出
        car_data = await extract_car_info(auction_sheet)
        model_code = car_data.get("model_code", "unknown")
        car_info = car_data.get("car_info", "")
        safe_model_code = re.sub(r'[\\/:*?"<>|]+', "_", str(model_code)).strip() or "unknown"
        folder_name = f"{datetime.now().strftime('%Y%m%d')}_{safe_model_code}"

        # Google Drive保存
        drive_ok = False
        folder_id = None
        try:
            drive_service = get_drive_service()
            folder_id = create_drive_folder(drive_service, folder_name, GOOGLE_DRIVE_FOLDER_ID)
            for i, img_bytes in enumerate(images):
                upload_to_drive(drive_service, img_bytes, f"photo_{i+1}.jpg", folder_id)
            drive_ok = True
            print("Drive folder created:", folder_name)
        except Exception as e:
            print(f"Drive error: {e}")

        # 広告文生成
        ads_dict = await generate_ads(car_info)

        # Drive に広告文保存
        if drive_ok and folder_id:
            try:
                ads_text = format_ads_for_drive(ads_dict, car_info)
                upload_to_drive(
                    drive_service,
                    ads_text.encode("utf-8"),
                    "広告文.txt",
                    folder_id,
                    mime_type="text/plain"
                )
            except Exception as e:
                print(f"Drive ads upload error: {e}")

        # Telegramに送信
        drive_status = f"📁 {folder_name}" if drive_ok else "⚠️ Drive保存失敗"
        await send_message(chat_id, f"✅ 完了 {drive_status}\n\n【車情報】\n{car_info}")

        # 言語ごとに送信
        languages = [
            ("ja", "🇯🇵 日本語"),
            ("zh", "🇨🇳 中国語"),
            ("en", "🇬🇧 English"),
            ("ru", "🇷🇺 Русский"),
            ("fr", "🇫🇷 Français"),
        ]
        platforms = [
            ("x", "X"),
            ("fb", "Facebook"),
            ("tt", "TikTok"),
            ("ig", "Instagram"),
            ("xhs", "小紅書"),
        ]

        for lang_key, lang_name in languages:
            msg = f"{lang_name}\n{'='*20}\n\n"
            for platform_key, platform_name in platforms:
                content = ads_dict.get(lang_key, {}).get(platform_key, "")
                msg += f"【{platform_name}】\n{content}\n\n"
            await send_message(chat_id, msg.strip())
            await asyncio.sleep(0.5)

        user_buffers.pop(chat_id, None)
        user_timers.pop(chat_id, None)

    except Exception as e:
        print(f"Error: {e}")
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

        await send_message(chat_id, "📩 写真受信。解析中...")

    elif "text" in message:
        text = message["text"].strip().upper()
        if text in ["/START", "START", "/HELP"]:
            await send_message(
                chat_id,
                "🚗 車広告自動生成Bot\n\n"
                "写真を送るだけで自動処理！\n\n"
                "【出力】業者向け広告\n"
                "🇯🇵 🇨🇳 🇬🇧 🇷🇺 🇫🇷\n"
                "X / FB / TikTok / IG / 小紅書\n"
                "Google Driveに自動保存"
            )

    return JSONResponse(content={"status": "ok"})
