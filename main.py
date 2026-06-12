#!/usr/bin/env python3
"""
小蓝人播报系统 · 主程序
中建中东投资 · 每日17:30自动运行

流程：
1. 抓取RSS新闻
2. Claude生成"今日预告"（一句话报菜名）
3. Claude生成完整播报稿
4. ElevenLabs合成预告语音 + 正文语音
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
from elevenlabs import ElevenLabs, VoiceSettings
from twilio.rest import Client

# 加载配置
sys.path.insert(0, str(Path(__file__).parent))
# 自动判断环境：有环境变量用生产配置，否则用本地config.py
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

def fetch_news() -> list[dict]:
    """从RSS源抓取今日新闻，返回文章列表"""
    log("📡 开始抓取新闻RSS...")
    articles = []
    today = datetime.date.today()

    for url in config.NEWS_SOURCES:
        try:
            feed = feedparser.parse(url)
            source_name = feed.feed.get("title", url)
            count = 0
            for entry in feed.entries[:8]:
                # 过滤：只要今天或昨天的
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    pub_date = datetime.date(*published[:3])
                    if (today - pub_date).days > 1:
                        continue

                title   = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                # 去除HTML标签
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

    # 去重（按标题）
    seen, unique = set(), []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)

    # 限制数量
    unique = unique[:config.MAX_ARTICLES]
    log(f"📰 共获取 {len(unique)} 条不重复新闻")
    return unique

# ── 第二步：Claude生成预告 + 播报稿 ──────────────────────────

def generate_scripts(articles: list[dict]) -> tuple[str, str]:
    """
    返回 (preview_text, broadcast_text)
    preview_text  = 今日预告（报菜名）
    broadcast_text = 完整播报正文
    """
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    date   = today_str()

    # 格式化新闻数据
    news_list_short = "\n".join(
        f"{i+1}. 【{a['source']}】{a['title']}"
        for i, a in enumerate(articles)
    )
    news_data_full = "\n\n".join(
        f"来源：{a['source']}\n标题：{a['title']}\n摘要：{a['summary']}"
        for a in articles
    )

    # —— 生成预告词 ——
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

    # —— 生成完整播报稿 ——
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

# ── 第三步：ElevenLabs语音合成 ────────────────────────────────

def text_to_speech(text: str, filename: str) -> str:
    """将文本转为语音，返回文件路径"""
    log(f"🎙️  ElevenLabs合成语音：{filename}...")
    from elevenlabs import VoiceSettings
    client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)

    audio = client.text_to_speech.convert(
        text=text,
        voice_id=config.ELEVENLABS_VOICE_ID,
        model_id="eleven_multilingual_v2",
        voice_settings=VoiceSettings(
            stability=config.VOICE_STABILITY,
            similarity_boost=config.VOICE_SIMILARITY,
            style=config.VOICE_STYLE,
            use_speaker_boost=config.VOICE_SPEAKER_BOOST,
        )
    )
    filepath = os.path.join(config.OUTPUT_DIR, filename)
    with open(filepath, "wb") as f:
        for chunk in audio:
            if chunk:
                f.write(chunk)
    size_kb = os.path.getsize(filepath) // 1024
    log(f"  ✓ 语音文件：{filepath}（{size_kb} KB）")
    return filepath

def synthesize_audio(preview_text: str, broadcast_text: str) -> tuple[str, str]:
    """合成预告音频 + 正文音频"""
    ensure_output_dir()
    date_tag = datetime.date.today().strftime("%Y%m%d")

    preview_file   = text_to_speech(preview_text,   f"xiaolan_preview_{date_tag}.mp3")
    broadcast_file = text_to_speech(broadcast_text, f"xiaolan_broadcast_{date_tag}.mp3")
    return preview_file, broadcast_file

# ── 第四步：上传音频到可访问URL ───────────────────────────────

def upload_audio(filepath: str) -> str:
    """
    上传MP3到文件托管服务，返回公开URL
    这里用 file.io（免费，24小时有效，够用）
    如果你有自己的服务器/OSS，可以替换这里
    """
    log(f"☁️  上传音频到云端：{os.path.basename(filepath)}...")
    with open(filepath, "rb") as f:
        resp = requests.post(
            "https://file.io",
            files={"file": f},
            data={"expires": "1d", "autoDelete": "true"},
            timeout=60,
        )
    data = resp.json()
    if data.get("success"):
        url = data["link"]
        log(f"  ✓ 上传成功：{url}")
        return url
    else:
        raise RuntimeError(f"文件上传失败：{data}")

# ── 第五步：发送到WhatsApp ────────────────────────────────────

def send_whatsapp(
    preview_text:    str,
    broadcast_text:  str,
    preview_audio_url:   str,
    broadcast_audio_url: str,
):
    """发送文字摘要 + 两段语音到WhatsApp"""
    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    date   = today_str()

    # —— 消息1：文字版播报摘要（前500字，方便快速扫）——
    # 截取前几个板块标题作为文字摘要
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

    # —— 消息2：预告语音 ——
    # —— 消息3：完整播报语音 ——

    for recipient in config.WHATSAPP_RECIPIENTS:
        try:
            # 文字摘要
            client.messages.create(
                from_=config.TWILIO_WHATSAPP_FROM,
                to=recipient,
                body=text_msg,
            )
            time.sleep(1)

            # 预告语音
            client.messages.create(
                from_=config.TWILIO_WHATSAPP_FROM,
                to=recipient,
                body="🎙️ 今日预告（30秒）",
                media_url=[preview_audio_url],
            )
            time.sleep(1)

            # 完整播报
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
        # 1. 抓取新闻
        articles = fetch_news()
        if not articles:
            log("❌ 没有抓到新闻，退出")
            sys.exit(1)

        # 2. 生成脚本
        preview_text, broadcast_text = generate_scripts(articles)

        # 保存文本备份
        ensure_output_dir()
        date_tag = datetime.date.today().strftime("%Y%m%d")
        with open(f"{config.OUTPUT_DIR}/script_{date_tag}.txt", "w", encoding="utf-8") as f:
            f.write("=== 今日预告 ===\n\n")
            f.write(preview_text)
            f.write("\n\n=== 完整播报稿 ===\n\n")
            f.write(broadcast_text)
        log(f"💾 文本备份已保存")

        # 3. 合成语音
        preview_file, broadcast_file = synthesize_audio(preview_text, broadcast_text)

        # 4. 上传音频
        preview_url   = upload_audio(preview_file)
        broadcast_url = upload_audio(broadcast_file)

        # 5. 发送WhatsApp
        log("📱 发送WhatsApp消息...")
        send_whatsapp(preview_text, broadcast_text, preview_url, broadcast_url)

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
