"""
LINE Bot - 車オークションシート自動広告生成
Claude API (Anthropic) 使用
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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

LINE_API = "https://api.line.me/v2/bot"
LINE_DATA_API = "https://api-data.line.me/v2/bot"
CLAUDE_API = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

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

def get_claude_headers():
    return {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
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
            f"{LINE_DATA_API}/message/{message_id}/content",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        )
        content = res.content
        if not content or len(content) == 0:
            raise Exception(f"画像データが空です。status: {res.status_code}")
        print(f"Image fetched: {len(content)} bytes, status: {res.status_code}")
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
