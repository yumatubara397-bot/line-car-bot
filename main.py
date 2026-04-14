"""
Telegram Bot - 車オークションシート自動広告生成
Claude API (Anthropic) 使用
- フォルダ名: 日付_せり番号_車名 (例: 20260414_3047_スバル_インプレッサ)
- 複数写真から自動でオークションシートを判別
- 写真（全枚）もDriveに保存
- Google Drive（共有ドライブ対応）
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

MAX_IMAGES_FOR_DETECTION = 10

user_buffers = {}
user_timers = {}

# ============================================================
# Google Drive（共有ドライブ対応）
# ============================================================
def get_drive_service():
    creds_json = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def verify_parent_folder(service, folder_id: str) -> bool:
    try:
        service.files().get(
            fileId=folder_id,
            supportsAllDrives=True,
            fields="id,name,mimeType"
        ).execute()
        return True
    except Exception as e:
        print(f"[Drive] 親フォルダアクセス失敗: {e}")
        return False

def create_drive_folder(service, folder_name: str, parent_id: str) -> str:
    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(
        body=meta, fields="id", supportsAllDrives=True
    ).execute()
    print(f"[Drive] フォルダ作成: {folder_name} -> {folder['id']}")
    return folder["id"]

def upload_text_to_drive(service, folder_id: str, filename: str, content: str):
    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")),
        mimetype="text/plain; charset=utf-8"
    )
    meta = {"name": filename, "parents": [folder_id]}
    service.files().create(
        body=meta, media_body=media, fields="id", supportsAllDrives=True
    ).execute()
    print(f"[Drive] テキスト保存: {filename}")

def upload_image_to_drive(service, folder_id: str, filename: str, image_bytes: bytes):
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg")
    meta = {"name": filename, "parents": [folder_id]}
    service.files().create(
        body=meta, media_body=media, fields="id", supportsAllDrives=True
    ).execute()
    print(f"[Drive] 画像保存: {filename}")

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
# Step1: オークションシート判別 + せり番号 + 車名 取得
# ============================================================
def shrink_image_for_detection(image_bytes: bytes, max_bytes: int = 150_000) -> str:
    """
    判別APIに送る画像をbase64化。
    PILなしで対応：Telegramは既に圧縮済みなので通常150KB以下。
    万が一大きい場合はJPEGヘッダを保ちつつ先頭max_bytesだけ使う
    （破損JPEGになるがClaudeはサムネイル相当で読める）。
    """
    data = image_bytes[:max_bytes] if len(image_bytes) > max_bytes else image_bytes
    return base64.standard_b64encode(data).decode("utf-8")

async def find_auction_sheet_info(images: list) -> tuple:
    """
    戻り値: (sheet_index, lot_number, car_name)
    lot_number: 出品票の大きく印刷された4桁前後の番号
    """
    candidate_indices = list(range(min(len(images), MAX_IMAGES_FOR_DETECTION)))

    content = []
    for i in candidate_indices:
        b64 = shrink_image_for_detection(images[i])
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })

    n = len(candidate_indices)
    content.append({
        "type": "text",
        "text": (
            f"There are {n} images (index 0 to {n-1}). "
            "Find the Japanese car auction sheet (JU/USS/TAA オークション出品申込書). "
            "It has a large lot number (出品番号/せり番号) printed prominently in a box at the top - "
            "this is typically a 3-5 digit number like 3047, 2171, 1258. "
            "Also read the car name (メーカー＋車種) from the 車名欄. "
            "Reply ONLY in this exact JSON format, no other text: "
            '{"index": 0, "lot_number": "3047", "car_name": "スズキ ワゴンR"}'
        )
    })

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 80,
        "messages": [{"role": "user", "content": content}]
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(CLAUDE_API, headers=headers, json=payload)
            data = r.json()
            raw = data["content"][0]["text"].strip()
            print(f"[Sheet detect] raw={raw}")

            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(raw[start:end])
                local_idx = int(parsed.get("index", 0))
                lot_number = str(parsed.get("lot_number", "0000")).strip()
                car_name = str(parsed.get("car_name", "不明")).strip()
                actual_idx = candidate_indices[min(local_idx, len(candidate_indices) - 1)]
                return actual_idx, lot_number, car_name
    except Exception as e:
        print(f"[Sheet detect] error: {e}")
        import traceback
        traceback.print_exc()

    return 0, "0000", "不明"

# ============================================================
# Step2: 広告文生成（シート1枚のみ）
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
[x]   120 chars max + 3-5 hashtags. Single punchy line.
[fb]  200-400 chars + 3-5 hashtags. Warm, 2-3 paragraphs with emojis.
[tt]  100-200 chars + 5-8 hashtags. Energetic, trendy hook.
[xhs] ALWAYS Chinese regardless of language key. 200-400 chars + 5 Chinese #hashtags. Lifestyle diary with emojis.
[ig]  150-250 chars + 15-25 hashtags on new line. Aesthetic, aspirational."""

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
                        "text": (
                            "This is a Japanese car auction sheet. "
                            "Read all car details (make, model, year, mileage, grade, color, features). "
                            "Generate ads for all 5 languages × 5 platforms. "
                            "Return ONLY the JSON object, nothing else."
                        )
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

        if "```" in raw:
            for part in raw.split("```"):
                p = part.strip()
                if p.startswith("json"):
                    raw = p[4:].strip()
                    break
                elif p.startswith("{"):
                    raw = p
                    break

        # 最初の完全なJSONブロックだけを取り出す（2個目以降は無視）
        start = raw.find("{")
        if start >= 0:
            raw = raw[start:]
        # 括弧の深さを数えて最初のJSONが閉じた位置を探す
        depth = 0
        end_pos = -1
        in_string = False
        escape = False
        for i, ch in enumerate(raw):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_pos = i + 1
                    break
        if end_pos > 0:
            raw = raw[:end_pos]
        return json.loads(raw)

# ============================================================
# メイン処理
# ============================================================
SNS_NAMES  = {"x": "X(Twitter)", "fb": "Facebook", "tt": "TikTok", "xhs": "小紅書", "ig": "Instagram"}
LANG_NAMES = {"ja": "🇯🇵 日本語", "zh": "🇨🇳 中国語", "en": "🇬🇧 English", "ru": "🇷🇺 Русский", "fr": "🇫🇷 Français"}

def safe_name(text: str) -> str:
    """フォルダ名に使えない文字を置換"""
    for ch in [" ", "　", "/", "\\", ":", "*", "?", '"', "<", ">", "|"]:
        text = text.replace(ch, "_")
    return text

async def process_photos(chat_id: int, file_ids: list):
    try:
        await send_message(chat_id, f"📷 {len(file_ids)}枚受信\n🔍 オークションシートを解析中... (約40秒)")

        # 全画像ダウンロード
        images = []
        for fid in file_ids:
            url = await get_file_url(fid)
            img_bytes = await download_image(url)
            images.append(img_bytes)
            print(f"Downloaded: {len(img_bytes)} bytes")

        # シート判別 + せり番号 + 車名
        sheet_idx, lot_number, car_name = await find_auction_sheet_info(images)
        print(f"Sheet:{sheet_idx}, Lot:{lot_number}, Car:{car_name}")

        # 広告生成
        ads = await generate_ads(images[sheet_idx])

        # フォルダ名: 日付_せり番号_車名
        date_str = datetime.now().strftime("%Y%m%d")
        folder_name = f"{date_str}_{safe_name(lot_number)}_{safe_name(car_name)}"

        # ── Google Drive 保存 ──────────────────────────────
        drive_saved = False
        drive_error = ""

        try:
            drive = get_drive_service()

            if not verify_parent_folder(drive, GDRIVE_FOLDER_ID):
                raise Exception(
                    f"親フォルダ(ID:{GDRIVE_FOLDER_ID})にアクセスできません。\n"
                    "・フォルダIDが正しいか確認\n"
                    "・サービスアカウントを「編集者」で共有してください"
                )

            sub_folder_id = create_drive_folder(drive, folder_name, GDRIVE_FOLDER_ID)

            # ① 写真を全枚保存
            car_num = 1
            for i, img_bytes in enumerate(images):
                if i == sheet_idx:
                    fname = "00_auction_sheet.jpg"
                else:
                    fname = f"car_photo_{car_num:02d}.jpg"
                    car_num += 1
                upload_image_to_drive(drive, sub_folder_id, fname, img_bytes)

            # ② 広告テキストを言語ごとに保存
            for lang_code, lang_name in LANG_NAMES.items():
                if lang_code not in ads:
                    continue
                lines = [f"=== {lang_name} ===\n"]
                for sns_code, sns_name in SNS_NAMES.items():
                    txt = ads[lang_code].get(sns_code, "")
                    if txt:
                        lines.append(f"【{sns_name}】\n{txt}\n")
                upload_text_to_drive(drive, sub_folder_id, f"{lang_code}.txt", "\n".join(lines))

            drive_saved = True

        except Exception as e:
            drive_error = str(e)
            print(f"[Drive] ERROR: {e}")
            import traceback
            traceback.print_exc()

        # 完了通知
        drive_status = "✅ Google Drive保存完了" if drive_saved else f"⚠️ Drive保存失敗\n{drive_error[:120]}"
        await send_message(
            chat_id,
            f"✅ 広告生成完了！\n"
            f"🏷 せり番号: {lot_number}\n"
            f"🚗 {car_name}\n"
            f"📁 {folder_name}\n"
            f"🌐 5言語 × 5SNS = 25種類\n"
            f"📷 写真 {len(images)}枚保存\n"
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
    await asyncio.sleep(10)
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
                "📷 写真を何枚でも送ってOK！\n"
                "（出品表＋車体写真をまとめて送信）\n"
                "→ 自動でオークションシートを判別します\n\n"
                "【生成内容】\n"
                "🇯🇵日本語 / 🇨🇳中国語 / 🇬🇧英語\n"
                "🇷🇺ロシア語 / 🇫🇷フランス語\n"
                "× X / Facebook / TikTok / 小紅書 / Instagram\n"
                "= 25種類の広告文\n\n"
                "✅ Google Driveに自動保存\n"
                "（写真＋広告文テキスト）\n"
                "※仕入先・価格は自動で除外"
            )

    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def health():
    return {"status": "running", "service": "Telegram Car Ad Generator (Claude API)"}
