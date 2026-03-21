"""
LINE Bot - 車オークションシート自動広告生成
Google Gemini API（無料）使用
"""

import os
import json
import base64
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
import hashlib
import hmac

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

LINE_API = "https://api.line.me/v2/bot"
# 画像解析用（vision対応）
GEMINI_VISION_API = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro-vision:generateContent"
# テキスト生成用
GEMINI_TEXT_API = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"

def verify_signature(body: bytes, signature: str) -> bool:
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    return hmac.compare_digest(base64.b64encode(hash_val).decode(), signature)

def get_line_headers():
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

async def reply_message(reply_token: str, messages: list):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{LINE_API}/message/reply",
            headers=get_line_headers(),
            json={"replyToken": reply_token, "messages": messages}
        )
        print(f"reply_message status: {r.status_code} {r.text[:200]}")

async def push_message(user_id: str, messages: list):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{LINE_API}/message/push",
            headers=get_line_headers(),
            json={"to": user_id, "messages": messages}
        )
        print(f"push_message status: {r.status_code} {r.text[:200]}")

async def get_image_content(message_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(
            f"{LINE_API}/message/{message_id}/content",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        )
        return res.content

async def call_gemini_vision(image_bytes: bytes, prompt: str) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            f"{GEMINI_VISION_API}?key={GEMINI_API_KEY}",
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
        print(f"Gemini vision response: {json.dumps(data)[:300]}")
        if "error" in data:
            raise Exception(data["error"]["message"])
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

async def call_gemini_text(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            f"{GEMINI_TEXT_API}?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 4000, "temperature": 0.7}
            }
        )
        data = res.json()
        print(f"Gemini text response: {json.dumps(data)[:300]}")
        if "error" in data:
            raise Exception(data["error"]["message"])
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

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

def format_ads_for_line(ads: dict) -> list:
    sns_labels = {
        "x": "𝕏 X (Twitter)", "fb": "f Facebook",
        "tt": "♪ TikTok", "xhs": "✿ 小紅書 (RED)", "ig": "◎ Instagram"
    }
    messages = []
    for lang, flag, title in [("zh","🇨🇳","中国語広告文"), ("en","🇬🇧","English Ad Copy"), ("ru","🇷🇺","Русский язык")]:
        text = f"{flag} 【{title}】\n" + "─"*20 + "\n\n"
        for sns_id, label in sns_labels.items():
            text += f"【{label}】\n{ads[lang].get(sns_id, '')}\n\n"
        messages.append({"type": "text", "text": text.strip()})
    return messages

async def process_image(user_id: str, message_id: str):
    try:
        image_bytes = await get_image_content(message_id)
        print(f"Image size: {len(image_bytes)} bytes")

        car_info = await extract_car_info(image_bytes)
        print(f"Car info extracted: {car_info[:100]}")

        await push_message(user_id, [{
            "type": "text",
            "text": f"✅ 解析完了！\n\n【車情報（仕入先・価格除外済み）】\n{car_info}\n\n📝 広告文を生成中..."
        }])

        ads = await generate_ads(car_info)
        ad_messages = format_ads_for_line(ads)

        await push_message(user_id, [{
            "type": "text",
            "text": "🎉 広告文生成完了！\n中国語・英語・ロシア語 × 5SNS = 15種類"
        }])

        for i in range(0, len(ad_messages), 5):
            await push_message(user_id, ad_messages[i:i+5])

        await push_message(user_id, [{
            "type": "text",
            "text": "✨ 完了！各SNSにコピー＆ペーストしてご使用ください。\n\n次の車の写真を送ってください 🚗"
        }])

    except Exception as e:
        print(f"Error in process_image: {e}")
        await push_message(user_id, [{
            "type": "text",
            "text": f"❌ エラーが発生しました。\n{str(e)}\n\n写真を再送してください。"
        }])

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = json.loads(body)
    print(f"Webhook received: {json.dumps(data)[:200]}")

    for event in data.get("events", []):
        event_type = event.get("type")
        reply_token = event.get("replyToken")
        user_id = event.get("source", {}).get("userId")

        if event_type == "message" and event.get("message", {}).get("type") == "image":
            message_id = event["message"]["id"]
            await reply_message(reply_token, [{
                "type": "text",
                "text": "📋 オークションシートを受信しました！\n\n🔍 車情報を解析中...\n⏳ 少々お待ちください（約30秒）"
            }])
            background_tasks.add_task(process_image, user_id, message_id)

        elif event_type == "message" and event.get("message", {}).get("type") == "text":
            await reply_message(reply_token, [{
                "type": "text",
                "text": "🚗 AUTO AD GENERATOR\n\nオークションシートの写真を送ってください！\n自動で以下を生成します：\n\n🇨🇳 中国語\n🇬🇧 English\n🇷🇺 Русский\n\n× X / Facebook / TikTok / 小紅書 / Instagram\n\n= 15種類の広告文を自動生成！\n※仕入先・価格は自動で除外されます"
            }])

    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def health():
    return {"status": "running", "service": "LINE Car Ad Generator Bot (Gemini Free)"}
