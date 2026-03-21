"""
Telegram Bot - 車オークションシート自動広告生成
Claude API (Anthropic) 使用
"""

import os
import json
import base64
import asyncio
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"
CLAUDE_API = "https://api.anthropic.com/v1/messages"

# いま使えているモデル名をそのまま維持
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# TelegramのsendMessage上限は4096文字。余裕を持たせる
TELEGRAM_MESSAGE_LIMIT = 3500


def get_claude_headers() -> Dict[str, str]:
    return {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }


def extract_text_from_claude_response(data: Dict[str, Any]) -> str:
    """
    Claudeレスポンスのcontent配列からtextを安全に連結する
    """
    if "error" in data:
        raise Exception(data["error"].get("message", "Unknown Claude API error"))

    content = data.get("content", [])
    texts: List[str] = []

    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))

    text = "\n".join(t for t in texts if t).strip()
    if not text:
        raise Exception(f"Claude response text is empty: {json.dumps(data)[:1000]}")
    return text


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> List[str]:
    """
    Telegram送信用に長文を分割する
    できるだけ段落区切り・改行区切りで切る
    """
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text

    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit

        chunk = remaining[:split_at].strip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = limit

        chunks.append(chunk)
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


async def send_message(chat_id: int, text: str):
    """
    長文は自動分割して送る
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        for part in split_message(text):
            r = await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": part,
                    "disable_web_page_preview": True,
                },
            )
            print(f"send_message status: {r.status_code} {r.text[:300]}")


async def get_image_content(file_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=30.0) as client:
        # file_path取得
        res = await client.get(
            f"{TELEGRAM_API}/getFile",
            params={"file_id": file_id},
        )
        data = res.json()
        print(f"getFile response: {json.dumps(data)[:300]}")

        if not data.get("ok") or "result" not in data or "file_path" not in data["result"]:
            raise Exception(f"Telegram getFile failed: {json.dumps(data)[:1000]}")

        file_path = data["result"]["file_path"]

        # 画像ダウンロード
        img_res = await client.get(f"{TELEGRAM_FILE_API}/{file_path}")
        img_res.raise_for_status()

        content = img_res.content
        print(f"Image fetched: {len(content)} bytes")
        return content


def detect_media_type(image_bytes: bytes) -> str:
    """
    簡易メディアタイプ判定
    """
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:20]:
        return "image/webp"
    # Telegramのphotoは通常jpegなのでデフォルトをjpegにする
    return "image/jpeg"


async def call_claude_vision(image_bytes: bytes, prompt: str) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    media_type = detect_media_type(image_bytes)

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            CLAUDE_API,
            headers=get_claude_headers(),
            json=payload,
        )
        data = res.json()
        print(f"Claude vision response: {json.dumps(data)[:1000]}")
        return extract_text_from_claude_response(data)


async def call_claude_text(prompt: str, max_tokens: int = 4000) -> str:
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            CLAUDE_API,
            headers=get_claude_headers(),
            json=payload,
        )
        data = res.json()
        print(f"Claude text response: {json.dumps(data)[:1000]}")
        return extract_text_from_claude_response(data)


def extract_json_block(text: str) -> str:
    """
    Claude返答から最初の{ ... 最後の }を抜き出す
    code block混在でも対応しやすくする
    """
    cleaned = text.replace("```json", "").replace("```", "").strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"JSON object not found in Claude response: {cleaned[:1000]}")

    return cleaned[start:end + 1]


async def repair_broken_json(broken_json_text: str) -> dict:
    """
    Claudeが返した壊れたJSONをClaude自身に修復させる
    """
    repair_prompt = f"""次のテキストは壊れたJSONです。
意味を変えずに、有効なJSONへ修正してください。

【厳守】
- JSONのみ返す
- コードブロック禁止
- 文字列中の改行は \\n にする
- 文字列中のダブルクォーテーションは適切にエスケープする
- キー名は変更しない
- 値を勝手に省略しない

{broken_json_text}
"""

    repaired_raw = await call_claude_text(repair_prompt, max_tokens=5000)
    print(f"REPAIRED RAW: {repaired_raw[:2000]}")
    repaired_json_text = extract_json_block(repaired_raw)
    return json.loads(repaired_json_text)


async def extract_car_info(image_bytes: bytes) -> str:
    prompt = """このオークションシートの画像から車の情報を読み取り、以下の形式でまとめてください。

【除外する情報（絶対に含めない）】
- オークション名・仕入先・出品者情報
- 価格・R券・落札金額などの金額情報
- 車台番号・登録番号・バーコード番号

【抽出する情報】
- メーカー・ブランド
- モデル名・グレード
- 年式
- 走行距離
- 排気量
- ミッション種類（AT/MT）
- ボディカラー
- 主要装備・オプション
- 車検有効期限
- 修復歴の有無
- 状態・コンディション（外装・内装グレード）
- ハンドル位置
- その他特記事項

【出力ルール】
- 日本語
- 箇条書き
- 不明な項目は「不明」と書く
- 推測しすぎない
"""
    return await call_claude_vision(image_bytes, prompt)


async def generate_ads(car_info: str) -> dict:
    prompt = f"""あなたは国際的なSNS広告のプロのコピーライターです。

【厳守ルール】
1. 価格・金額は絶対に書かない
2. オークション・仕入先・入手経路は絶対に書かない
3. 車台番号・登録番号は書かない
4. 必ず有効なJSONのみ返す
5. コードブロックは使わない
6. JSONの前後に説明文を一切付けない
7. 文字列中の改行は必ず \\n で表現する
8. 文字列中の " は必ず \\" にする
9. 全ての値はJSON stringにする
10. 小紅書は必ず中国語で書く

【車情報】
{car_info}

【各SNSの仕様】
X(Twitter): 本文全角140文字以内、ハッシュタグ3〜5個、力強く簡潔
Facebook: 本文全角300〜500文字、ハッシュタグ3〜5個、ストーリー調、絵文字使用
TikTok: 本文全角300〜500文字、ハッシュタグ3〜5個、エネルギッシュ・トレンド感
Instagram: 本文全角300〜350文字、ハッシュタグ3〜5個、ライフスタイル訴求、絵文字使用
小紅書: 全セクション必ず中国語、本文全角300〜500文字、#标签形式3〜5個、日記風、絵文字多め

【重要】
- ハッシュタグは本文末尾に入れる
- 本文中に改行を入れてよいが、JSON文字列内では必ず \\n を使う
- 必ず次の形で返すこと

{{
  "zh": {{
    "x": "...",
    "fb": "...",
    "tt": "...",
    "xhs": "...",
    "ig": "..."
  }},
  "en": {{
    "x": "...",
    "fb": "...",
    "tt": "...",
    "xhs": "...",
    "ig": "..."
  }},
  "ru": {{
    "x": "...",
    "fb": "...",
    "tt": "...",
    "xhs": "...",
    "ig": "..."
  }}
}}
"""

    raw = await call_claude_text(prompt, max_tokens=5000)
    print(f"RAW ADS RESPONSE: {raw[:3000]}")

    json_text = extract_json_block(raw)

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        print(f"JSON parse failed: {e}")
        print(f"BROKEN JSON: {json_text[:4000]}")
        return await repair_broken_json(json_text)


def format_ads(ads: dict) -> list:
    sns_labels = {
        "x": "𝕏 X (Twitter)",
        "fb": "f Facebook",
        "tt": "♪ TikTok",
        "xhs": "✿ 小紅書 (RED)",
        "ig": "◎ Instagram",
    }

    messages = []

    for lang, flag, title in [
        ("zh", "🇨🇳", "中国語広告文"),
        ("en", "🇬🇧", "English Ad Copy"),
        ("ru", "🇷🇺", "Русский язык"),
    ]:
        lang_ads = ads.get(lang, {})
        text = f"{flag} 【{title}】\n" + "─" * 20 + "\n\n"

        for sns_id, label in sns_labels.items():
            value = lang_ads.get(sns_id, "")
            text += f"【{label}】\n{value}\n\n"

        messages.append(text.strip())

    return messages


async def process_image(chat_id: int, file_id: str):
    try:
        image_bytes = await get_image_content(file_id)
        print(f"Image size: {len(image_bytes)} bytes")

        car_info = await extract_car_info(image_bytes)
        print(f"Car info extracted: {car_info[:500]}")

        await send_message(
            chat_id,
            f"✅ 解析完了！\n\n【車情報（仕入先・価格除外済み）】\n{car_info}\n\n📝 広告文を生成中..."
        )

        ads = await generate_ads(car_info)
        ad_messages = format_ads(ads)

        await send_message(
            chat_id,
            "🎉 広告文生成完了！\n中国語・英語・ロシア語 × 5SNS = 15種類"
        )

        for msg in ad_messages:
            await send_message(chat_id, msg)

        await send_message(
            chat_id,
            "✨ 完了！各SNSにコピー＆ペーストしてご使用ください。\n\n次の車の写真を送ってください 🚗"
        )

    except Exception as e:
        print(f"Error in process_image: {repr(e)}")
        await send_message(
            chat_id,
            f"❌ エラーが発生しました。\n{str(e)}\n\n写真を再送してください。"
        )


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print(f"Webhook received: {json.dumps(data)[:500]}")

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    if not chat_id:
        return JSONResponse(content={"status": "ok"})

    # ボット自身のメッセージは無視
    from_user = message.get("from", {})
    if from_user.get("is_bot"):
        return JSONResponse(content={"status": "ok"})

    # 画像メッセージ
    if "photo" in message and message["photo"]:
        file_id = message["photo"][-1]["file_id"]

        await send_message(
            chat_id,
            "📋 オークションシートを受信しました！\n\n🔍 車情報を解析中...\n⏳ 少々お待ちください（約30秒）"
        )

        asyncio.create_task(process_image(chat_id, file_id))

    # テキストメッセージ
    elif "text" in message:
        await send_message(
            chat_id,
            "🚗 AUTO AD GENERATOR\n\n"
            "オークションシートの写真を送ってください！\n"
            "自動で以下を生成します：\n\n"
            "🇨🇳 中国語\n"
            "🇬🇧 English\n"
            "🇷🇺 Русский\n\n"
            "× X / Facebook / TikTok / 小紅書 / Instagram\n\n"
            "= 15種類の広告文を自動生成！\n"
            "※仕入先・価格は自動で除外されます"
        )

    return JSONResponse(content={"status": "ok"})


@app.get("/")
async def health():
    return {
        "status": "running",
        "service": "Telegram Car Ad Generator Bot (Claude API)",
        "model": CLAUDE_MODEL,
        "has_anthropic_key": bool(ANTHROPIC_API_KEY),
        "has_telegram_token": bool(TELEGRAM_BOT_TOKEN),
    }
