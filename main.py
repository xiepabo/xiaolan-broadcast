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
    """用Claude API搜索当天UAE新闻，不依赖RSS"""
    log("📡 开始抓取当天新闻...")
    today = datetime.date.today()
    date_str = today.strftime("%Y年%m月%d日")

    # 先用requests抓几个新闻网站的内容
    articles = []
    
    # 方案1：尝试RSS（有就用）
    import feedparser
    rss_sources = config.NEWS_SOURCES
    for url in rss_sources:
        try:
            feed = feedparser.parse(url)
            source_name = feed.feed.get("title", url)
            for entry in feed.entries[:8]:
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub:
                    pub_date = datetime.date(*pub[:3])
                    if (today - pub_date).days > 3:
                        continue
                title = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                import re
                summary = re.sub(r"<[^>]+>", "", summary)[:500]
                if title:
                    articles.append({
                        "source": source_name,
                        "title": title,
                        "summary": summary,
                        "link": entry.get("link", ""),
                    })
            if articles:
                log(f"  ✓ RSS抓取成功：{len(articles)}条")
        except Exception as e:
            log(f"  ⚠ RSS失败 {url[:40]}: {e}")

    # 方案2：如果RSS没内容，用Claude搜索当天新闻
    if len(articles) < 3:
        log("  RSS内容不足，改用AI搜索当天新闻...")
        from anthropic import Anthropic
        client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
        
        search_prompt = f"""今天是{date_str}，请你根据你知道的最新信息，列出今天或最近几天阿联酋（UAE）和海湾地区的重要新闻，重点包括：
1. 金融和银行业动态
2. 经济政策和数据
3. 房地产市场
4. 地缘政治（尤其是霍尔木兹海峡、伊朗、以色列等影响海湾的局势）
5. 重要企业动态（Emaar、ADNOC、FAB等）

请列出8-15条新闻，每条格式：
标题：[新闻标题]
摘要：[2-3句话的详细说明]
---

只输出新闻列表，不要其他内容。"""

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": search_prompt}],
        )
        
        raw = resp.content[0].text.strip()
        # 解析输出
        import re
        items = raw.split("---")
        for item in items:
            item = item.strip()
            if not item:
                continue
            title_match = re.search(r"标题[：:]\s*(.+)", item)
            summary_match = re.search(r"摘要[：:]\s*(.+)", item, re.DOTALL)
            if title_match:
                title = title_match.group(1).strip()
                summary = summary_match.group(1).strip() if summary_match else ""
                articles.append({
                    "source": "AI综合新闻",
                    "title": title,
                    "summary": summary[:500],
                    "link": "",
                })
        log(f"  ✓ AI搜索获取：{len(articles)}条新闻")

    # 去重
    seen, unique = set(), []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)

    unique = unique[:config.MAX_ARTICLES]
    log(f"📰 共获取 {len(unique)} 条不重复新闻")
    return unique


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
            voice="nova",        # onyx=低沉男声，适合财经播报
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
                voice="nova",
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
    # 合并预告和正文为一个语音文件
    combined_text = preview_text + "\n\n" + broadcast_text
    combined_file = text_to_speech(combined_text, f"xiaolan_broadcast_{date_tag}.mp3")
    # preview_file和broadcast_file都返回同一个文件
    return combined_file, combined_file

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

    # 上传音频到 GitHub Releases（稳定可靠，不需要第三方）
    def github_upload(fpath):
        import base64, hashlib
        fname = os.path.basename(fpath)
        gh_token = os.environ.get("GITHUB_TOKEN","")
        gh_repo  = os.environ.get("GITHUB_REPOSITORY","")
        
        # 用GitHub API上传到release
        # 先获取或创建release
        headers = {
            "Authorization": f"token {gh_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        tag = f"audio-{datetime.date.today().strftime('%Y%m%d')}"
        
        # 创建release
        rel_resp = requests.post(
            f"https://api.github.com/repos/{gh_repo}/releases",
            headers=headers,
            json={"tag_name": tag, "name": f"播报音频 {tag}", "draft": False, "prerelease": False},
            timeout=30,
        )
        if rel_resp.status_code == 422:
            # release已存在，获取它
            rel_resp = requests.get(
                f"https://api.github.com/repos/{gh_repo}/releases/tags/{tag}",
                headers=headers, timeout=30,
            )
        rel_data = rel_resp.json()
        upload_url = rel_data.get("upload_url","").replace("{?name,label}","")
        rel_id = rel_data.get("id","")
        
        # 删除同名旧asset
        assets_resp = requests.get(
            f"https://api.github.com/repos/{gh_repo}/releases/{rel_id}/assets",
            headers=headers, timeout=30,
        )
        for asset in assets_resp.json():
            if asset.get("name") == fname:
                requests.delete(
                    f"https://api.github.com/repos/{gh_repo}/releases/assets/{asset['id']}",
                    headers=headers, timeout=30,
                )
        
        # 上传文件
        with open(fpath, "rb") as f:
            up_resp = requests.post(
                f"{upload_url}?name={fname}",
                headers={**headers, "Content-Type": "audio/mpeg"},
                data=f,
                timeout=180,
            )
        asset_data = up_resp.json()
        url = asset_data.get("browser_download_url","")
        return url

    log("  📤 上传预告音频到GitHub...")
    try:
        preview_url = github_upload(preview_file)
        log(f"  ✓ 预告URL: {preview_url}")
    except Exception as e:
        log(f"  ⚠️ 上传失败: {e}")
        preview_url = None

    log("  📤 上传正文音频到GitHub...")
    try:
        broadcast_url = github_upload(broadcast_file)
        log(f"  ✓ 正文URL: {broadcast_url}")
    except Exception as e:
        log(f"  ⚠️ 上传失败: {e}")
        broadcast_url = None

    for recipient in recipients:
        try:
            # 消息1：文字摘要
            summary_lines = []
            for line in broadcast_text.split("\n"):
                line = line.strip()
                if line.startswith("【") and "】" in line:
                    summary_lines.append(line)
                if len(summary_lines) >= 6:
                    break
            if not summary_lines:
                summary_lines = [broadcast_text[:200] + "..."]

            text_msg = (
                f"🔵 *小蓝人播报* · {date}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"{chr(10).join(summary_lines)}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"👇 语音版：先听预告，再听全文"
            )
            client.messages.create(from_=from_num, to=recipient, body=text_msg)
            time.sleep(2)

            # 消息2：完整语音（预告+正文合并）
            if broadcast_url:
                client.messages.create(
                    from_=from_num, to=recipient,
                    body="🎙️ 小蓝人语音播报（预告+全文）",
                    media_url=[broadcast_url]
                )

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
