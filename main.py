"""
LINE Bot - 車オークションシート自動広告生成
写真を送るだけで、中国語・英語・ロシア語 × 5SNS の広告文を自動生成
"""

import os
import json
import base64
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import hashlib
import hmac

app = FastAPI()

# ============================================================
# 環境変数（Railwayで設定）
# ============================================================
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "0eb6435384105bd699e2c8a6fb0b1a78")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")  # Developersコンソールから取得
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

LINE_API = "https://api.line.me/v2/bot"
CLAUDE_API = "https://api.anthropic.com/v1/messages"

# ============================================================
# 署名検証（セキュリティ）
# ============================================================
def verify_signature(body: bytes, signature: str) -> bool:
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    return hmac.compare_digest(base64.b64encode(hash_val).decode(), signature)

# ============================================================
# LINE APIヘルパー
# ============================================================
def get_line_headers():
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

async def reply_message(reply_token: str, messages: list):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{LINE_API}/message/reply",
            headers=get_line_headers(),
            json={"replyToken": reply_token, "messages": messages}
        )

async def push_message(user_id: str, messages: list):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{LINE_API}/message/push",
            headers=get_line_headers(),
            json={"to": user_id, "messages": messages}
        )

async def get_image_content(message_id: str) -> bytes:
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{LINE_API}/message/{message_id}/content",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            timeout=30.0
        )
        return res.content

# ============================================================
# Claude API呼び出し
# ============================================================
async def call_claude(messages: list, system: str, max_tokens: int = 4000) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            CLAUDE_API,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "system": system,
                "messages": messages
            }
        )
        data = res.json()
        if "error" in data:
            raise Exception(data["error"]["message"])
        return data["content"][0]["text"].strip()

# ============================================================
# ステップ1: 写真から車情報を抽出
# ============================================================
async def extract_car_info(image_bytes: bytes) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    return await call_claude(
        messages=[{
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
                    "text": """このオークションシートの画像から車の情報を読み取り、以下の形式でまとめてください。

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
- その他特記事項

日本語の箇条書きで出力してください。"""
                }
            ]
        }],
        system="You are an expert at reading Japanese vehicle auction inspection sheets. Extract vehicle information accurately, excluding all pricing, auction house, and identifying information.",
        max_tokens=1000
    )

# ============================================================
# ステップ2: 広告文を生成
# ============================================================
async def generate_ads(car_info: str) -> dict:
    system_prompt = """You are an expert automotive marketing copywriter for international social media.

STRICT RULES:
1. NEVER mention price, cost, or any monetary values
2. NEVER mention auction house, supplier, or source
3. NEVER include VIN, chassis number, or license plate
4. Return ONLY valid JSON, no markdown, no explanation

Required JSON format:
{"zh":{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."},"en":{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."},"ru":{"x":"...","fb":"...","tt":"...","xhs":"...","ig":"..."}}

Platform styles:
- x: punchy, 3-5 hashtags, under 280 chars
- fb: detailed, storytelling, emojis, minimal hashtags
- tt: trendy, energetic, many hashtags, youth appeal
- xhs: ALWAYS in Chinese (小紅書 is Chinese platform), lifestyle, many emojis
- ig: visual lifestyle, 10-15 hashtags"""

    raw = await call_claude(
        messages=[{
            "role": "user",
            "content": f"""以下の車情報を基に全5SNS×全3言語の広告文を生成してください。

【車情報】
{car_info}

JSONのみ返してください。"""
        }],
        system=system_prompt,
        max_tokens=4000
    )

    # Parse JSON
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ============================================================
# 広告文をLINEメッセージ形式に整形
# ============================================================
def format_ads_for_line(ads: dict) -> list:
    """LINEに送るメッセージリストを作成（最大5メッセージ）"""
    
    sns_labels = {
        "x": "𝕏 X (Twitter)",
        "fb": "f Facebook",
        "tt": "♪ TikTok",
        "xhs": "✿ 小紅書 (RED)",
        "ig": "◎ Instagram"
    }
    
    messages = []
    
    # メッセージ1: ヘッダー + 中国語
    zh_text = "🇨🇳 【中国語広告文】\n" + "─" * 20 + "\n\n"
    for sns_id, label in sns_labels.items():
        zh_text += f"【{label}】\n{ads['zh'].get(sns_id, '')}\n\n"
    messages.append({"type": "text", "text": zh_text.strip()})
    
    # メッセージ2: 英語
    en_text = "🇬🇧 【English Ad Copy】\n" + "─" * 20 + "\n\n"
    for sns_id, label in sns_labels.items():
        en_text += f"【{label}】\n{ads['en'].get(sns_id, '')}\n\n"
    messages.append({"type": "text", "text": en_text.strip()})
    
    # メッセージ3: ロシア語
    ru_text = "🇷🇺 【Русский язык】\n" + "─" * 20 + "\n\n"
    for sns_id, label in sns_labels.items():
        ru_text += f"【{label}】\n{ads['ru'].get(sns_id, '')}\n\n"
    messages.append({"type": "text", "text": ru_text.strip()})
    
    return messages

# ============================================================
# メイン Webhook エンドポイント
# ============================================================
@app.post("/webhook")
async def webhook(request: Request):
    # 署名検証
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")
    
    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    data = json.loads(body)
    
    for event in data.get("events", []):
        event_type = event.get("type")
        reply_token = event.get("replyToken")
        user_id = event.get("source", {}).get("userId")
        
        # 画像メッセージを受信
        if event_type == "message" and event.get("message", {}).get("type") == "image":
            message_id = event["message"]["id"]
            
            # 受信確認メッセージ
            await reply_message(reply_token, [{
                "type": "text",
                "text": "📋 オークションシートを受信しました！\n\n🔍 車情報を解析中...\n⏳ 少々お待ちください（約30秒）"
            }])
            
            try:
                # 画像取得
                image_bytes = await get_image_content(message_id)
                
                # ステップ1: 車情報抽出
                car_info = await extract_car_info(image_bytes)
                
                # 解析結果を送信
                await push_message(user_id, [{
                    "type": "text",
                    "text": f"✅ 解析完了！\n\n【車情報（仕入先・価格除外済み）】\n{car_info}\n\n📝 広告文を生成中..."
                }])
                
                # ステップ2: 広告文生成
                ads = await generate_ads(car_info)
                
                # 広告文を送信
                ad_messages = format_ads_for_line(ads)
                
                # ヘッダーメッセージ
                await push_message(user_id, [{
                    "type": "text",
                    "text": "🎉 広告文生成完了！\n中国語・英語・ロシア語 × 5SNS = 15種類"
                }])
                
                # 言語ごとに送信（LINEは1回5件まで）
                for i in range(0, len(ad_messages), 5):
                    await push_message(user_id, ad_messages[i:i+5])
                
                # 完了メッセージ
                await push_message(user_id, [{
                    "type": "text",
                    "text": "✨ 完了！各SNSにコピー＆ペーストしてご使用ください。\n\n次の車の写真を送ってください 🚗"
                }])
                
            except Exception as e:
                await push_message(user_id, [{
                    "type": "text",
                    "text": f"❌ エラーが発生しました。\n{str(e)}\n\n写真を再送してください。"
                }])
        
        # テキストメッセージ（使い方案内）
        elif event_type == "message" and event.get("message", {}).get("type") == "text":
            await reply_message(reply_token, [{
                "type": "text",
                "text": "🚗 AUTO AD GENERATOR\n\nオークションシートの写真を送ってください！\n自動で以下を生成します：\n\n🇨🇳 中国語\n🇬🇧 English\n🇷🇺 Русский\n\n× X / Facebook / TikTok / 小紅書 / Instagram\n\n= 15種類の広告文を自動生成！\n※仕入先・価格は自動で除外されます"
            }])
    
    return JSONResponse(content={"status": "ok"})

@app.get("/")
async def health():
    return {"status": "running", "service": "LINE Car Ad Generator Bot"}
