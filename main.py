"""
Telegram Bot - 多商品自動広告生成（法人向け）
対応商品: 車 / パソコン / iPad / スマホ
Claude API (Anthropic) 使用
- 1回の送信に複数商品混在OK → 商品ごとに個別フォルダ保存
- 車のみオークションシートから情報読み取り
- 法人向けプロフェッショナルトーン
- 5言語 × 5SNS = 25種類の広告文
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

SNS_NAMES  = {"x": "X(Twitter)", "fb": "Facebook", "tt": "TikTok", "xhs": "小紅書", "ig": "Instagram"}
LANG_NAMES = {"ja": "🇯🇵 日本語", "zh": "🇨🇳 中国語", "en": "🇬🇧 English", "ru": "🇷🇺 Русский", "fr": "🇫🇷 Français"}

# ============================================================
# Google Drive（共有ドライブ対応）
# ============================================================
def get_drive_service():
    creds_json = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_json, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def verify_parent_folder(service, folder_id: str) -> bool:
    try:
        service.files().get(fileId=folder_id, supportsAllDrives=True, fields="id").execute()
        return True
    except Exception as e:
        print(f"[Drive] 親フォルダアクセス失敗: {e}")
        return False

def create_drive_folder(service, folder_name: str, parent_id: str) -> str:
    meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
    print(f"[Drive] フォルダ作成: {folder_name}")
    return folder["id"]

def upload_text_to_drive(service, folder_id: str, filename: str, content: str):
    media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype="text/plain; charset=utf-8")
    meta = {"name": filename, "parents": [folder_id]}
    service.files().create(body=meta, media_body=media, fields="id", supportsAllDrives=True).execute()
    print(f"[Drive] テキスト保存: {filename}")

def upload_image_to_drive(service, folder_id: str, filename: str, image_bytes: bytes):
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg")
    meta = {"name": filename, "parents": [folder_id]}
    service.files().create(body=meta, media_body=media, fields="id", supportsAllDrives=True).execute()
    print(f"[Drive] 画像保存: {filename}")

# ============================================================
# Telegram API
# ============================================================
async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})
        print(f"send_message: {r.status_code}")

async def get_file_url(file_id: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{TELEGRAM_API}/getFile?file_id={file_id}")
        return f"{TELEGRAM_FILE_API}/{r.json()['result']['file_path']}"

async def download_image(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        return (await client.get(url)).content

# ============================================================
# ユーティリティ
# ============================================================
def safe_name(text: str) -> str:
    for ch in [" ", "　", "/", "\\", ":", "*", "?", '"', "<", ">", "|"]:
        text = text.replace(ch, "_")
    return text

def shrink_for_detection(image_bytes: bytes, max_bytes: int = 150_000) -> str:
    data = image_bytes[:max_bytes] if len(image_bytes) > max_bytes else image_bytes
    return base64.standard_b64encode(data).decode("utf-8")

def extract_first_json(raw: str) -> str:
    start = raw.find("{")
    if start < 0:
        return raw
    raw = raw[start:]
    depth, in_string, escape = 0, False, False
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
                return raw[:i+1]
    return raw

# ============================================================
# Step1: 画像を商品グループに分類
# ============================================================
async def classify_images(images: list) -> list:
    """
    全画像を分析して商品グループに分類する。
    戻り値: [
      {"type": "car", "sheet_idx": 0, "photo_indices": [1,2,3], "lot_number": "5022", "item_name": "日産 セレナ"},
      {"type": "pc",  "sheet_idx": None, "photo_indices": [4,5], "lot_number": None, "item_name": "Apple MacBook Pro"},
      ...
    ]
    """
    n = len(images)
    candidate_count = min(n, MAX_IMAGES_FOR_DETECTION)

    content = []
    for i in range(candidate_count):
        b64 = shrink_for_detection(images[i])
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })

    content.append({
        "type": "text",
        "text": (
            f"There are {candidate_count} images (index 0 to {candidate_count-1}). "
            "Classify each image into product groups. Products can be: car, pc, ipad, smartphone, or other. "
            "A Japanese car auction sheet (出品表) has large lot number, Japanese text, inspection grades. "
            "Group images that belong to the same product together. "
            "For cars: identify the auction sheet image and car photos separately. "
            "Extract lot number (出品番号) from car auction sheets (large printed number like 5022). "
            "Extract product name for each group (e.g. 日産 セレナ, Apple MacBook Pro 14, iPhone 15 Pro). "
            "Reply ONLY in this JSON format, no other text:\n"
            '{"groups": ['
            '{"type": "car", "sheet_idx": 0, "photo_indices": [1,2,3], "lot_number": "5022", "item_name": "日産 セレナ"},'
            '{"type": "pc", "sheet_idx": null, "photo_indices": [4,5], "lot_number": null, "item_name": "Apple MacBook Pro"}'
            ']}'
        )
    })

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
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
            print(f"[Classify] raw={raw[:200]}")

            parsed = json.loads(extract_first_json(raw))
            groups = parsed.get("groups", [])
            print(f"[Classify] {len(groups)} groups found")
            return groups
    except Exception as e:
        print(f"[Classify] error: {e}")
        # フォールバック: 全画像を1グループとして処理
        return [{"type": "other", "sheet_idx": None, "photo_indices": list(range(n)), "lot_number": None, "item_name": "商品"}]

# ============================================================
# Step2: 商品ごとに広告文生成
# ============================================================
PRODUCT_PROMPTS = {
    "car": (
        "This is a Japanese car auction sheet. "
        "Read all car details: make, model, year, mileage, grade, color, displacement, transmission, and special features. "
        "The target audience is B2B automotive dealers and importers (法人バイヤー). "
    ),
    "pc": (
        "This is a used PC/laptop product photo. "
        "Identify: brand, model, specs (CPU, RAM, storage, display size), condition. "
        "The target audience is B2B corporate IT procurement managers (法人IT調達担当者). "
    ),
    "ipad": (
        "This is a used iPad/tablet product photo. "
        "Identify: model, generation, storage capacity, color, condition, accessories. "
        "The target audience is B2B corporate buyers and educational institutions (法人・教育機関). "
    ),
    "smartphone": (
        "This is a used smartphone product photo. "
        "Identify: brand, model, storage, color, condition, carrier lock status if visible. "
        "The target audience is B2B mobile device resellers and corporate buyers (法人・リセラー). "
    ),
    "other": (
        "This is a used product photo. "
        "Identify the product type, brand, model, condition and key features. "
        "The target audience is B2B corporate buyers (法人バイヤー). "
    ),
}

async def generate_ads_combined(item_images: list, item_names: list) -> dict:
    """
    複数商品の画像と名前を受け取り、全商品をまとめた1つの広告文を生成。
    冒頭に「商品A＋商品B」形式でタイトルを入れる。
    item_images: [(image_bytes, product_type), ...]
    item_names:  ["日産 セレナ", "MacBook Pro", ...]
    """
    combined_title = " ＋ ".join(item_names)

    system_prompt = """You are an expert B2B marketing copywriter specializing in used goods for corporate clients.

TONE: Professional, trustworthy, factual. Emphasize reliability, value, and business efficiency.
Use industry terminology appropriate for corporate procurement.

STRICT RULES (NEVER VIOLATE):
1. NEVER mention price, cost, or any monetary values
2. NEVER mention auction house, supplier, or acquisition source
3. NEVER include VIN, chassis number, serial number, or license plate
4. Return ONLY valid JSON — no markdown, no explanation
5. Every ad MUST start with the combined product title provided

Required JSON format (ALL 5 languages, ALL 5 platforms):
{"ja":{"x":"","fb":"","tt":"","xhs":"","ig":""},"zh":{"x":"","fb":"","tt":"","xhs":"","ig":""},"en":{"x":"","fb":"","tt":"","xhs":"","ig":""},"ru":{"x":"","fb":"","tt":"","xhs":"","ig":""},"fr":{"x":"","fb":"","tt":"","xhs":"","ig":""}}

PLATFORM RULES:
[x]   Start with combined title. 120 chars max + 3-5 hashtags. Sharp B2B hook.
[fb]  Start with combined title. 250-450 chars + 3-5 hashtags. Professional, 2-3 paragraphs covering all products.
[tt]  Start with combined title. 150-250 chars + 5-8 hashtags. Dynamic but professional.
[xhs] ALWAYS Chinese regardless of language key. Start with combined title in Chinese. 250-400 chars + 5 Chinese #hashtags.
[ig]  Start with combined title. 200-300 chars + 15-25 hashtags. Clean, professional aesthetic."""

    # 全商品の画像をまとめてメッセージに含める
    content = []
    for img_bytes, ptype in item_images:
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})

    product_descriptions = []
    for (_, ptype), name in zip(item_images, item_names):
        if ptype == "car":
            product_descriptions.append(f"Car: {name} (read specs from auction sheet image)")
        elif ptype == "pc":
            product_descriptions.append(f"PC/Laptop: {name} (read specs from photo)")
        elif ptype == "ipad":
            product_descriptions.append(f"iPad/Tablet: {name} (read specs from photo)")
        elif ptype == "smartphone":
            product_descriptions.append(f"Smartphone: {name} (read specs from photo)")
        else:
            product_descriptions.append(f"Product: {name} (read details from photo)")

    content.append({
        "type": "text",
        "text": (
            f"Combined product listing title: {combined_title}\n"
            f"Products in this listing:\n" + "\n".join(f"- {d}" for d in product_descriptions) + "\n\n"
            "Create ONE unified advertisement that covers ALL products together. "
            "Start every ad with the combined title. "
            "Highlight how these products complement each other for corporate buyers. "
            "Generate ads for all 5 languages × 5 platforms. Return ONLY the JSON object."
        )
    })

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 8192,
        "system": system_prompt,
        "messages": [{"role": "user", "content": content}]
    }

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(CLAUDE_API, headers=headers, json=payload)
        print(f"Claude API [combined]: {r.status_code}")
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

        return json.loads(extract_first_json(raw))

# ============================================================
# Step3: Driveに保存
# ============================================================
async def save_to_drive(drive, group: dict, images: list, ads: dict, date_str: str) -> str:
    """1商品グループをDriveに保存。フォルダ名を返す"""
    product_type = group.get("type", "other")
    item_name = group.get("item_name", "不明")
    lot_number = group.get("lot_number")
    sheet_idx = group.get("sheet_idx")
    photo_indices = group.get("photo_indices", [])

    # フォルダ名
    if product_type == "car" and lot_number:
        folder_name = f"{date_str}_{safe_name(lot_number)}_{safe_name(item_name)}"
    else:
        folder_name = f"{date_str}_{safe_name(product_type)}_{safe_name(item_name)}"

    sub_folder_id = create_drive_folder(drive, folder_name, GDRIVE_FOLDER_ID)

    # 写真保存
    if sheet_idx is not None and sheet_idx < len(images):
        upload_image_to_drive(drive, sub_folder_id, "00_auction_sheet.jpg", images[sheet_idx])

    for i, idx in enumerate(photo_indices):
        if idx < len(images):
            upload_image_to_drive(drive, sub_folder_id, f"photo_{i+1:02d}.jpg", images[idx])

    # 広告テキスト保存
    for lang_code, lang_name in LANG_NAMES.items():
        if lang_code not in ads:
            continue
        lines = [f"=== {lang_name} ===\n"]
        for sns_code, sns_name in SNS_NAMES.items():
            txt = ads[lang_code].get(sns_code, "")
            if txt:
                lines.append(f"【{sns_name}】\n{txt}\n")
        upload_text_to_drive(drive, sub_folder_id, f"{lang_code}.txt", "\n".join(lines))

    return folder_name

# ============================================================
# メイン処理
# ============================================================
PRODUCT_EMOJI = {"car": "🚗", "pc": "💻", "ipad": "📱", "smartphone": "📱", "other": "📦"}

async def process_photos(chat_id: int, file_ids: list):
    try:
        await send_message(chat_id, f"📷 {len(file_ids)}枚受信\n🔍 商品を識別中... (しばらくお待ちください)")

        # 全画像ダウンロード
        images = []
        for fid in file_ids:
            url = await get_file_url(fid)
            img_bytes = await download_image(url)
            images.append(img_bytes)
            print(f"Downloaded: {len(img_bytes)} bytes")

        # 商品グループに分類
        groups = await classify_images(images)
        print(f"Groups: {groups}")

        if not groups:
            await send_message(chat_id, "❌ 商品を識別できませんでした。\n写真を再送してください。")
            return

        await send_message(chat_id, f"✅ {len(groups)}商品を識別\n📝 まとめ広告を生成中...")

        # Driveサービス初期化
        drive_ok = False
        drive = None
        try:
            drive = get_drive_service()
            if not verify_parent_folder(drive, GDRIVE_FOLDER_ID):
                raise Exception("親フォルダにアクセスできません")
            drive_ok = True
        except Exception as e:
            print(f"[Drive init] {e}")

        date_str = datetime.now().strftime("%Y%m%d")

        # 全商品の「広告用画像」と「商品名」を収集
        item_images = []  # [(image_bytes, product_type), ...]
        item_names  = []  # ["日産 セレナ", "MacBook Pro", ...]

        for group in groups:
            product_type = group.get("type", "other")
            item_name    = group.get("item_name", "不明")
            sheet_idx    = group.get("sheet_idx")
            photo_indices = group.get("photo_indices", [])

            # 広告生成に使う代表画像（車はシート、他は最初の写真）
            if product_type == "car" and sheet_idx is not None and sheet_idx < len(images):
                ad_image = images[sheet_idx]
            elif photo_indices and photo_indices[0] < len(images):
                ad_image = images[photo_indices[0]]
            elif sheet_idx is not None and sheet_idx < len(images):
                ad_image = images[sheet_idx]
            else:
                ad_image = images[0]

            item_images.append((ad_image, product_type))
            item_names.append(item_name)

        # ── 広告文は全商品まとめて1回だけ生成 ──────────────
        combined_title = " ＋ ".join(item_names)
        print(f"Combined title: {combined_title}")

        ads = await generate_ads_combined(item_images, item_names)

        # ── 商品ごとに別フォルダでDrive保存（同じ広告文を入れる） ──
        results = []
        for group in groups:
            product_type  = group.get("type", "other")
            item_name     = group.get("item_name", "不明")
            lot_number    = group.get("lot_number", "")
            emoji         = PRODUCT_EMOJI.get(product_type, "📦")

            try:
                folder_name = "保存スキップ"
                if drive_ok:
                    folder_name = await save_to_drive(drive, group, images, ads, date_str)

                lot_info = f" #{lot_number}" if lot_number else ""
                results.append(f"{emoji} {item_name}{lot_info}\n   📁 {folder_name}")

            except Exception as e:
                print(f"[Save error] {item_name}: {e}")
                import traceback
                traceback.print_exc()
                results.append(f"{emoji} {item_name} → ❌ 保存エラー: {str(e)[:50]}")

        # 完了通知
        drive_status = "✅ Google Drive保存完了" if drive_ok else "⚠️ Drive保存失敗"
        result_text  = "\n".join(results)
        await send_message(
            chat_id,
            f"✅ 広告生成完了！\n\n"
            f"📢 {combined_title}\n\n"
            f"{result_text}\n\n"
            f"🌐 5言語×5SNS=25種類（全商品まとめ広告）\n"
            f"{drive_status}"
        )

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
        user_timers[chat_id] = asyncio.create_task(delayed_process(chat_id))

    elif "text" in message:
        text = message["text"].strip()
        if text in ["/start", "/help"]:
            await send_message(
                chat_id,
                "🏢 法人向け多商品広告自動生成Bot\n\n"
                "📷 写真を送るだけ！\n"
                "（複数商品まとめて送信OK）\n\n"
                "【対応商品】\n"
                "🚗 車（オークションシート対応）\n"
                "💻 パソコン\n"
                "📱 iPad / スマホ\n\n"
                "【生成内容】\n"
                "🇯🇵日本語 / 🇨🇳中国語 / 🇬🇧英語\n"
                "🇷🇺ロシア語 / 🇫🇷フランス語\n"
                "× X / Facebook / TikTok / 小紅書 / Instagram\n"
                "= 商品ごとに25種類\n\n"
                "✅ Google Driveに商品別フォルダで保存\n"
                "※仕入先・価格は自動で除外"
            )

    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def health():
    return {"status": "running", "service": "Multi-Product B2B Ad Generator (Claude API)"}
