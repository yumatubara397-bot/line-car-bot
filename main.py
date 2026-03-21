"""
Telegram Bot - 車オークションシート自動広告生成
Claude API (Anthropic) 使用
"""

import os
import json
import base64
import asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"
CLAUDE_API = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

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
        print(f"send_message status: {r.status_code} {r.text[:200]}")

async def get_image_content(file_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=30.0) as client:
        # まずfile_pathを取得
        res = await client.get(
            f"{TELEGRAM_API}/getFile",
            params={"file_id": file_id}
        )
        data = res.json()
        print(f"getFile response: {json.dumps(data)[:200]}")
        file_path = data["result"]["file_path"]

        # 画像をダウンロード
        img_res = await client.get(f"{TELEGRAM_FILE_API}/{file_path}")
        content = img_res.content
        print(f"Image fetched: {len(content)} bytes")
        return content

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
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }]
            }
        )
        data = res.json()
        print(f"Claude vision response: {json.dumps(data)[:300]}")
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
                "max_tokens": 4000,
                "messages": [{
                    "role": "user",
                    "content": prompt
                }]
            }
        )
        data = res.json()
        print(f"Claude text response: {json.dumps(data)[:300]}")
        if "error" in data:
            raise Exception(data["error"]["message"])
        return data["content"][0]["text"].strip()

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
    return await call_claude_vision(image_bytes, prompt)

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

    raw = await call_claude_text(prompt)
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

def format_ads(ads: dict) -> list:
    sns_labels = {
        "x": "𝕏 X (Twitter)", "fb": "f Facebook",
        "tt": "♪ TikTok", "xhs": "✿ 小紅書 (RED)", "ig": "◎ Instagram"
    }
    messages = []
    for lang, flag, title in [("zh","🇨🇳","中国語広告文"), ("en","🇬🇧","English Ad Copy"), ("ru","🇷🇺","Русский язык")]:
        text = f"{flag} 【{title}】\n" + "─"*20 + "\n\n"
        for sns_id, label in sns_labels.items():
            text += f"【{label}】\n{ads[lang].get(sns_id, '')}\n\n"
        messages.append(text.strip())
    return messages

async def process_image(chat_id: int, file_id: str):
    try:
        image_bytes = await get_image_content(file_id)
        print(f"Image size: {len(image_bytes)} bytes")

        car_info = await extract_car_info(image_bytes)
        print(f"Car info extracted: {car_info[:100]}")

        await send_message(chat_id,
            f"✅ 解析完了！\n\n【車情報（仕入先・価格除外済み）】\n{car_info}\n\n📝 広告文を生成中..."
        )

        ads = await generate_ads(car_info)
        ad_messages = format_ads(ads)

        await send_message(chat_id, "🎉 広告文生成完了！\n中国語・英語・ロシア語 × 5SNS = 15種類")

        for msg in ad_messages:
            await send_message(chat_id, msg)

        await send_message(chat_id, "✨ 完了！各SNSにコピー＆ペーストしてご使用ください。\n\n次の車の写真を送ってください 🚗")

    except Exception as e:
        print(f"Error in process_image: {e}")
        await send_message(chat_id, f"❌ エラーが発生しました。\n{str(e)}\n\n写真を再送してください。")

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print(f"Webhook received: {json.dumps(data)[:200]}")

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    if not chat_id:
        return JSONResponse(content={"status": "ok"})

    # 画像メッセージ
    if "photo" in message:
        # 最高解像度の画像を取得
        file_id = message["photo"][-1]["file_id"]
        await send_message(chat_id,
            "📋 オークションシートを受信しました！\n\n🔍 車情報を解析中...\n⏳ 少々お待ちください（約30秒）"
        )
        asyncio.create_task(process_image(chat_id, file_id))

    # テキストメッセージ
    elif "text" in message:
        await send_message(chat_id,
            "🚗 AUTO AD GENERATOR\n\nオークションシートの写真を送ってください！\n自動で以下を生成します：\n\n🇨🇳 中国語\n🇬🇧 English\n🇷🇺 Русский\n\n× X / Facebook / TikTok / 小紅書 / Instagram\n\n= 15種類の広告文を自動生成！\n※仕入先・価格は自動で除外されます"
        )

    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def health():
    return {"status": "running", "service": "Telegram Car Ad Generator Bot (Claude API)"}
