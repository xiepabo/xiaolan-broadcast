#!/usr/bin/env python3
"""
小蓝人播报系统 · 主程序
中建中东投资 · 每日17:30自动运行

流程：
1. 抓取RSS新闻
2. Claude生成"今日预告"（一句话报菜名）
3. Claude生成完整播报稿
4. OpenAI TTS合成预告语音 + 正文语音
5. Twilio发送文字摘要 + 语音到WhatsApp
"""

import os
import sys
import json
import time
import datetime
import requests
import feedparser
from pathlib import Path
from anthropic import Anthropic
from openai import OpenAI
from twilio.rest import Client

# 加载配置
sys.path.insert(0, str(Path(__file__).parent))
if os.environ.get("ANTHROPIC_API_KEY"):
    import config_prod as config
else:
    import config
from prompts.broadcast_prompt import SYSTEM_PROMPT, PREVIEW_PROMPT, BROADCAST_PROMPT

# ── 工具函数 ─────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def today_str() -> str:
    now = datetime.datetime.now()
    weekdays = ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"]
    wd = weekdays[now.weekday()]
    return now.strftime(f"%Y年%m月%d日 {wd}")

def ensure_output_dir():
    Path(config.OUTPUT_DIR).mkdir(exist_ok=True)

# ── 第一步：抓取新闻 ──────────────────────────────────────────

def fetch_news() -> list:
    log("📡 开始抓取新闻RSS...")
    articles = []
    today = datetime.date.today()

    for url in config.NEWS_SOURCES:
        try:
            feed = feedparser.parse(url)
            source_name = feed.feed.get("title", url)
            count = 0
            for entry in feed.entries[:8]:
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    pub_date = datetime.date(*published[:3])
                    if (today - pub_date).days > 1:
                        continue

                title   = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                import re
                summary = re.sub(r"<[^>]+>", "", summary)[:500]

                if title:
                    articles.append({
                        "source": source_name,
                        "title":  title,
                        "summary": summary,
                        "link":   entry.get("link", ""),
                    })
                    count += 1

            log(f"  ✓ {source_name}: {count}条")
        except Exception as e:
            log(f"  ✗ 抓取失败 {url}: {e}")

    seen, unique = set(), []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)

    unique = unique[:config.MAX_ARTICLES]
    log(f"📰 共获取 {len(unique)} 条不重复新闻")
    return unique

# ── 第二步：Claude生成预告 + 播报稿 ──────────────────────────

def generate_scripts(articles: list) -> tuple:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    date   = today_str()

    news_list_short = "\n".join(
        f"{i+1}. 【{a['source']}】{a['title']}"
        for i, a in enumerate(articles)
    )
    news_data_full = "\n\n".join(
        f"来源：{a['source']}\n标题：{a['title']}\n摘要：{a['summary']}"
        for a in articles
    )

    log("🤖 Claude正在生成今日预告...")
    preview_prompt = PREVIEW_PROMPT.format(
        date=date,
        count=len(articles),
        news_list=news_list_short,
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": preview_prompt}],
    )
    preview_text = resp.content[0].text.strip()
    log(f"  ✓ 预告词生成完成（{len(preview_text)}字）")

    log("🤖 Claude正在生成完整播报稿（需要约30秒）...")
    min_chars = config.TARGET_DURATION_MIN * 200
    max_chars = config.TARGET_DURATION_MAX * 200
    broadcast_prompt = BROADCAST_PROMPT.format(
        style=config.BROADCAST_STYLE,
        min_min=config.TARGET_DURATION_MIN,
        max_min=config.TARGET_DURATION_MAX,
        min_chars=min_chars,
        max_chars=max_chars,
        date=date,
        news_data=news_data_full,
    )
    resp2 = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": broadcast_prompt}],
    )
    broadcast_text = resp2.content[0].text.strip()
    log(f"  ✓ 播报稿生成完成（{len(broadcast_text)}字，约{len(broadcast_text)//200}分钟）")

    return preview_text, broadcast_text

# ── 第三步：OpenAI TTS语音合成 ────────────────────────────────

def text_to_speech(text: str, filename: str) -> str:
    log(f"🎙️  OpenAI TTS合成语音：{filename}...")
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY") or config.OPENAI_API_KEY)

    # 超过4096字符需要分段
    MAX_CHARS = 4000
    filepath = os.path.join(config.OUTPUT_DIR, filename)

    if len(text) <= MAX_CHARS:
        response = client.audio.speech.create(
            model="tts-1-hd",
            voice="onyx",        # onyx=低沉男声，适合财经播报
            input=text,
            speed=1.0,
        )
        response.stream_to_file(filepath)
    else:
        # 分段合成再合并
        import math
        chunks = []
        words = text.split("。")
        current = ""
        chunk_files = []

        for i, w in enumerate(words):
            if len(current) + len(w) < MAX_CHARS:
                current += w + "。"
            else:
                chunks.append(current)
                current = w + "。"
        if current:
            chunks.append(current)

        log(f"  文本较长，分{len(chunks)}段合成...")
        audio_data = b""
        for idx, chunk in enumerate(chunks):
            resp = client.audio.speech.create(
                model="tts-1-hd",
                voice="onyx",
                input=chunk,
                speed=1.0,
            )
            audio_data += resp.content

        with open(filepath, "wb") as f:
            f.write(audio_data)

    size_kb = os.path.getsize(filepath) // 1024
    log(f"  ✓ 语音文件：{filepath}（{size_kb} KB）")
    return filepath

def synthesize_audio(preview_text: str, broadcast_text: str) -> tuple:
    ensure_output_dir()
    date_tag = datetime.date.today().strftime("%Y%m%d")
    preview_file   = text_to_speech(preview_text,   f"xiaolan_preview_{date_tag}.mp3")
    broadcast_file = text_to_speech(broadcast_text, f"xiaolan_broadcast_{date_tag}.mp3")
    return preview_file, broadcast_file

# ── 第四步：上传音频 ──────────────────────────────────────────

def upload_to_twilio(filepath: str) -> str:
    """上传音频到Twilio媒体库，返回URL"""
    log(f"☁️  上传音频到Twilio：{os.path.basename(filepath)}...")
    from twilio.rest import Client as TwilioClient
    tc = TwilioClient(
        os.environ.get("TWILIO_ACCOUNT_SID") or config.TWILIO_ACCOUNT_SID,
        os.environ.get("TWILIO_AUTH_TOKEN") or config.TWILIO_AUTH_TOKEN,
    )
    with open(filepath, "rb") as f:
        media = tc.media.v1.media_processor.list()
    # 用requests直接上传到Twilio Assets
    import base64
    sid = os.environ.get("TWILIO_ACCOUNT_SID") or config.TWILIO_ACCOUNT_SID
    token = os.environ.get("TWILIO_AUTH_TOKEN") or config.TWILIO_AUTH_TOKEN
    with open(filepath, "rb") as f:
        audio_data = f.read()
    resp = requests.post(
        f"https://mcs.us1.twilio.com/v1/Services",
        auth=(sid, token),
        json={"FriendlyName": os.path.basename(filepath)},
        timeout=30,
    )
    service_sid = resp.json().get("sid", "")
    resp2 = requests.post(
        f"https://mcs.us1.twilio.com/v1/Services/{service_sid}/Assets",
        auth=(sid, token),
        data={"FriendlyName": os.path.basename(filepath), "Visibility": "public"},
        timeout=30,
    )
    asset_sid = resp2.json().get("sid", "")
    resp3 = requests.post(
        f"https://mcs.us1.twilio.com/v1/Services/{service_sid}/Assets/{asset_sid}/Versions",
        auth=(sid, token),
        files={"Content": (os.path.basename(filepath), open(filepath,"rb"), "audio/mpeg")},
        data={"Path": f"/{os.path.basename(filepath)}", "Visibility": "public"},
        timeout=120,
    )
    url = f"https://mcs.us1.twilio.com/v1/Services/{service_sid}/Assets/{asset_sid}/Versions/{resp3.json().get('sid','')}"
    log(f"  ✓ 上传成功")
    return url

# ── 第五步：发送到WhatsApp ────────────────────────────────────


def send_whatsapp_with_files(preview_text, broadcast_text, preview_file, broadcast_file):
    """直接用Twilio发送音频文件（无需上传到第三方）"""
    from twilio.rest import Client as TC
    import base64

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID") or config.TWILIO_ACCOUNT_SID
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")  or config.TWILIO_AUTH_TOKEN
    recipients  = config.WHATSAPP_RECIPIENTS
    from_num    = config.TWILIO_WHATSAPP_FROM
    date        = today_str()

    client = TC(account_sid, auth_token)

    # 文字摘要
    summary_lines = []
    for line in broadcast_text.split("\n"):
        line = line.strip()
        if line.startswith("【") and line.endswith("】"):
            summary_lines.append(line)
        if len(summary_lines) >= 6:
            break

    text_msg = (
        f"🔵 *小蓝人播报* · {date}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{chr(10).join(summary_lines)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👇 语音版：先听预告，再听全文"
    )

    # 上传音频到 Cloudinary（免费CDN，Twilio兼容）
    def cloudinary_upload(fpath):
        fname = os.path.basename(fpath)
        cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME") or config.CLOUDINARY_CLOUD_NAME
        api_key    = os.environ.get("CLOUDINARY_API_KEY")    or config.CLOUDINARY_API_KEY
        api_secret = os.environ.get("CLOUDINARY_API_SECRET") or config.CLOUDINARY_API_SECRET
        import hashlib, time as t
        timestamp = str(int(t.time()))
        sig_str = f"public_id={fname}&timestamp={timestamp}{api_secret}"
        signature = hashlib.sha1(sig_str.encode()).hexdigest()
        with open(fpath, "rb") as f:
            resp = requests.post(
                f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload",
                data={
                    "api_key": api_key,
                    "timestamp": timestamp,
                    "signature": signature,
                    "public_id": fname.replace(".mp3",""),
                    "resource_type": "video",
                },
                files={"file": f},
                timeout=180,
            )
        data = resp.json()
        url = data.get("secure_url","")
        return url

    log("  📤 上传预告音频到Cloudinary...")
    try:
        preview_url = cloudinary_upload(preview_file)
        log(f"  ✓ 预告URL: {preview_url}")
    except Exception as e:
        log(f"  ⚠️ 上传失败: {e}")
        preview_url = None

    log("  📤 上传正文音频到Cloudinary...")
    try:
        broadcast_url = cloudinary_upload(broadcast_file)
        log(f"  ✓ 正文URL: {broadcast_url}")
    except Exception as e:
        log(f"  ⚠️ 上传失败: {e}")
        broadcast_url = None

    for recipient in recipients:
        try:
            client.messages.create(from_=from_num, to=recipient, body=text_msg)
            time.sleep(1)
            if preview_url:
                client.messages.create(from_=from_num, to=recipient,
                    body="🎙️ 今日预告（30秒）", media_url=[preview_url])
                time.sleep(1)
            if broadcast_url:
                client.messages.create(from_=from_num, to=recipient,
                    body="📻 完整播报（7-10分钟）", media_url=[broadcast_url])
            log(f"  ✓ 已发送至 {recipient}")
        except Exception as e:
            log(f"  ✗ 发送失败 {recipient}: {e}")

def send_whatsapp(preview_text, broadcast_text, preview_audio_url, broadcast_audio_url):
    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    date   = today_str()

    summary_lines = []
    for line in broadcast_text.split("\n"):
        line = line.strip()
        if line.startswith("【") and line.endswith("】"):
            summary_lines.append(line)
        if len(summary_lines) >= 6:
            break

    text_msg = (
        f"🔵 *小蓝人播报* · {date}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{chr(10).join(summary_lines)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👇 语音版：先听预告，再听全文"
    )

    for recipient in config.WHATSAPP_RECIPIENTS:
        try:
            client.messages.create(
                from_=config.TWILIO_WHATSAPP_FROM,
                to=recipient,
                body=text_msg,
            )
            time.sleep(1)
            client.messages.create(
                from_=config.TWILIO_WHATSAPP_FROM,
                to=recipient,
                body="🎙️ 今日预告（30秒）",
                media_url=[preview_audio_url],
            )
            time.sleep(1)
            client.messages.create(
                from_=config.TWILIO_WHATSAPP_FROM,
                to=recipient,
                body="📻 完整播报（7-10分钟）",
                media_url=[broadcast_audio_url],
            )
            log(f"  ✓ 已发送至 {recipient}")
        except Exception as e:
            log(f"  ✗ 发送失败 {recipient}: {e}")

# ── 主流程 ────────────────────────────────────────────────────

def main():
    log("=" * 50)
    log("🔵 小蓝人播报系统启动")
    log(f"   {today_str()}")
    log("=" * 50)

    start = time.time()

    try:
        articles = fetch_news()
        if not articles:
            log("❌ 没有抓到新闻，退出")
            sys.exit(1)

        preview_text, broadcast_text = generate_scripts(articles)

        ensure_output_dir()
        date_tag = datetime.date.today().strftime("%Y%m%d")
        with open(f"{config.OUTPUT_DIR}/script_{date_tag}.txt", "w", encoding="utf-8") as f:
            f.write("=== 今日预告 ===\n\n")
            f.write(preview_text)
            f.write("\n\n=== 完整播报稿 ===\n\n")
            f.write(broadcast_text)
        log(f"💾 文本备份已保存")

        preview_file, broadcast_file = synthesize_audio(preview_text, broadcast_text)

        log("📱 发送WhatsApp消息...")
        send_whatsapp_with_files(preview_text, broadcast_text, preview_file, broadcast_file)

        elapsed = int(time.time() - start)
        log("=" * 50)
        log(f"✅ 全部完成！耗时 {elapsed} 秒")
        log("=" * 50)

    except Exception as e:
        log(f"❌ 运行出错：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
