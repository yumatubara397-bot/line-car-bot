"""
Telegram Bot - 車オークションシート自動広告生成
Google Gemini API + Google Drive保存
写真を送るだけで広告文を生成し、Googleドライブに自動保存
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

# ============================================================
# 環境変数
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "0ANll_6Bs9PULUk9PVA")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent"

# ============================================================
# Google Drive クライアント
# ============================================================
def get_drive_service():
    creds_json = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

# ============================================================
# Telegram APIヘルパー
# ============================================================
async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )
        print(f"send_message: {r.status_code}")

async def get_file_url(file_id: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{TELEGRAM_API}/getFile?file_id={file_id}")
        data = r.json()
        file_path = data["result"]["file_path"]
        return f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"

async def download_file(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
        return r.content

# ============================================================
# Gemini API
# ============================================================
async def call_gemini_vision(image_bytes: bytes, prompt: str) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            f"{GEMINI_API}?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                        {"text": prompt}
                    ]
                }],
                "generationConfig": {"maxOutputTokens": 1000, "temperature": 0.3}
            }
        )
        data = res.json()
        print(f"Gemini vision: {json.dumps(data)[:200]}")
        if "error" in data:
            raise Exception(data["error"]["message"])
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

async def call_gemini_text(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            f"{GEMINI_API}?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 4000, "temperature": 0.7}
            }
        )
        data = res.json()
        print(f"Gemini text: {json.dumps(data)[:200]}")
        if "error" in data:
            raise Exception(data["error"]["message"])
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

# ============================================================
# 車情報抽出
# ============================================================
async def extract_car_info(image_bytes: bytes) -> str:
    prompt = """このオークションシートの画像から車の情報を読み取り、以下の形式でまとめてください。

【除外する情報（絶対に含めない）】
- オークション名・仕入先・出品者情報
- 価格・R券・落札金額などの金額情報
- 車台番号・登録番号・バーコード番号

【抽出する情報】
- メーカー・ブランド
- モデル名・グレード
- 年式、走行距離、排気量
- ミッション種類（AT/MT）
- ボディカラー
- 主要装備・オプション
- 車検有効期限
- 修復歴の有無
- 状態・コンディション（外装・内装グレード）
- その他特記事項

日本語の箇条書きで出力してください。"""
    return await call_gemini_vision(image_bytes, prompt)

# ============================================================
# 車種名を短く取得（フォルダ名用）
# ============================================================
async def get_car_name(car_info: str) -> str:
    prompt = f"""以下の車情報から「メーカー名 モデル名」だけを短く抽出してください。
例：「スズキ スペーシア」「トヨタ ランドクルーザー」「ホンダ フィット」
20文字以内で、他の情報は一切含めないでください。

車情報：
{car_info}"""
    try:
        name = await call_gemini_text(prompt)
        # ファイル名に使えない文字を除去
        name = name.strip().replace("/", "").replace("\\", "").replace(":", "").replace("*", "").replace("?", "").replace('"', "").replace("<", "").replace(">", "").replace("|", "")
        return name[:30]
    except:
        return "不明車種"

# ============================================================
# 広告文生成
# ============================================================
async def generate_ads(car_info: str) -> dict:
    prompt = f"""あなたは国際的なSNS広告のプロのコピーライターです。

【厳守ルール】
1. 価格・金額は絶対に書かない
2. オークション・仕入先・入手経路は絶対に書かない
3. 車台番号・登録番号は書かない
4. JSONのみ返す（```不要）

【車情報】
{car_info}

【各SNSの仕様】
X(Twitter): 本文全角140文字以内、ハッシュタグ3〜5個（末尾改行）、力強く簡潔
Facebook: 本文全角300〜500文字、ハッシュタグ3〜5個（末尾改行）、ストーリー調絵文字使用
TikTok: 本文全角300〜500文字、ハッシュタグ3〜5個（末尾改行）、エネルギッシュ・トレンド感
Instagram: 本文全角300〜350文字、ハッシュタグ3〜5個（末尾改行）、ライフスタイル訴求絵文字使用
小紅書: 全セクション必ず中国語、本文全角300〜500文字、#标签形式3〜5個（末尾）、日記風絵文字多め✨🚗💫

JSONのみ返してください：
{{"zh":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"en":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}},"ru":{{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}}}}"""

    raw = await call_gemini_text(prompt)
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ============================================================
# 広告文をテキスト形式に整形
# ============================================================
def format_ads_as_text(ads: dict, car_info: str) -> str:
    sns_labels = {
        "x": "X (Twitter)",
        "fb": "Facebook",
        "tt": "TikTok",
        "xhs": "小紅書 (RED)",
        "ig": "Instagram"
    }
    
    text = "=" * 50 + "\n"
    text += "車広告文 自動生成\n"
    text += f"生成日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    text += "=" * 50 + "\n\n"
    
    text += "【車情報】\n"
    text += car_info + "\n\n"
    
    for lang, flag, title in [("zh", "🇨🇳", "中国語"), ("en", "🇬🇧", "English"), ("ru", "🇷🇺", "Русский")]:
        text += "=" * 50 + "\n"
        text += f"{flag} {title}\n"
        text += "=" * 50 + "\n\n"
        for sns_id, label in sns_labels.items():
            text += f"【{label}】\n"
            text += ads[lang].get(sns_id, "") + "\n\n"
    
    return text

# ============================================================
# Googleドライブに保存
# ============================================================
def save_to_drive(image_bytes: bytes, ad_text: str, car_name: str):
    service = get_drive_service()
    
    # 日付_車種名でフォルダ作成
    today = datetime.now().strftime("%Y-%m-%d")
    folder_name = f"{today}_{car_name}"
    
    # フォルダ作成
    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [GDRIVE_FOLDER_ID]
    }
    folder = service.files().create(body=folder_metadata, fields="id").execute()
    folder_id = folder["id"]
    
    # 写真をアップロード
    image_metadata = {
        "name": "オークションシート.jpg",
        "parents": [folder_id]
    }
    image_media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg")
    service.files().create(body=image_metadata, media_body=image_media, fields="id").execute()
    
    # 広告文テキストをアップロード
    text_metadata = {
        "name": "広告文.txt",
        "parents": [folder_id]
    }
    text_media = MediaIoBaseUpload(
        io.BytesIO(ad_text.encode("utf-8")),
        mimetype="text/plain"
    )
    service.files().create(body=text_metadata, media_body=text_media, fields="id").execute()
    
    return folder_name

# ============================================================
# メイン処理
# ============================================================
async def process_image(chat_id: int, file_id: str):
    try:
        # 画像ダウンロード
        file_url = await get_file_url(file_id)
        image_bytes = await download_file(file_url)
        print(f"Image size: {len(image_bytes)} bytes")

        # 車情報抽出
        car_info = await extract_car_info(image_bytes)
        print(f"Car info: {car_info[:100]}")

        # 車種名取得（フォルダ名用）
        car_name = await get_car_name(car_info)
        print(f"Car name: {car_name}")

        # 広告文生成
        ads = await generate_ads(car_info)

        # テキスト整形
        ad_text = format_ads_as_text(ads, car_info)

        # Googleドライブに保存
        folder_name = await asyncio.get_event_loop().run_in_executor(
            None, save_to_drive, image_bytes, ad_text, car_name
        )

        # Telegramに完了通知のみ送信
        await send_message(chat_id, f"✅ 完了しました！\n\n📁 Googleドライブに保存：\n{folder_name}")

    except Exception as e:
        print(f"Error: {e}")
        await send_message(chat_id, f"❌ エラーが発生しました。\n{str(e)}\n\n写真を再送してください。")

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

    # 写真を受信
    if "photo" in message:
        # 一番高解像度の写真を取得
        photo = message["photo"][-1]
        file_id = photo["file_id"]

        await send_message(chat_id, "📋 オークションシートを受信しました！\n\n🔍 車情報を解析中...\n⏳ 少々お待ちください（約30秒）")

        asyncio.create_task(process_image(chat_id, file_id))

    # テキストを受信
    elif "text" in message:
        text = message.get("text", "")
        if text == "/start":
            await send_message(chat_id,
                "🚗 車広告自動生成Bot\n\n"
                "オークションシートの写真を送ってください！\n\n"
                "✅ 広告文を自動生成\n"
                "✅ Googleドライブに自動保存\n"
                "✅ 中国語・英語・ロシア語 × 5SNS\n\n"
                "※仕入先・価格は自動で除外されます"
            )
        else:
            await send_message(chat_id, "📷 オークションシートの写真を送ってください！")

    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def health():
    return {"status": "running", "service": "Telegram Car Ad Generator Bot"}
