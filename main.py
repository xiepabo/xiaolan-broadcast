#!/usr/bin/env python3
"""
小蓝人播报系统 · 主程序
中建中东投资 · 每日17:30自动运行

流程：
1. 抓取RSS新闻
2. Claude生成"今日预告"（一句话报菜名）
3. Claude生成完整播报稿
4. OpenAI TTS合成语音 + lo-fi背景音乐混音
5. 音频 push 到 GitHub audio 分支，获得 raw.githubusercontent.com 直链
6. Twilio发送文字 + 语音到WhatsApp
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
    log("📡 开始抓取当天新闻...")
    today = datetime.date.today()
    date_str = today.strftime("%Y年%m月%d日")
    articles = []

    for url in config.NEWS_SOURCES:
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

    if len(articles) < 3:
        log("  RSS内容不足，改用AI搜索当天新闻...")
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
        import re
        for item in raw.split("---"):
            item = item.strip()
            if not item:
                continue
            title_match = re.search(r"标题[：:]\s*(.+)", item)
            summary_match = re.search(r"摘要[：:]\s*(.+)", item, re.DOTALL)
            if title_match:
                articles.append({
                    "source": "AI综合新闻",
                    "title": title_match.group(1).strip(),
                    "summary": (summary_match.group(1).strip() if summary_match else "")[:500],
                    "link": "",
                })
        log(f"  ✓ AI搜索获取：{len(articles)}条新闻")

    seen, unique = set(), []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)
    unique = unique[:config.MAX_ARTICLES]
    log(f"📰 共获取 {len(unique)} 条不重复新闻")
    return unique


# ── 第二步：生成播报稿 ────────────────────────────────────────

def generate_scripts(articles: list) -> tuple:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    date = today_str()

    news_list_short = "\n".join(
        f"{i+1}. 【{a['source']}】{a['title']}" for i, a in enumerate(articles)
    )
    news_data_full = "\n\n".join(
        f"来源：{a['source']}\n标题：{a['title']}\n摘要：{a['summary']}" for a in articles
    )

    log("🤖 Claude正在生成今日预告...")
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": PREVIEW_PROMPT.format(
            date=date, count=len(articles), news_list=news_list_short,
        )}],
    )
    preview_text = resp.content[0].text.strip()
    log(f"  ✓ 预告词生成完成（{len(preview_text)}字）")

    log("🤖 Claude正在生成完整播报稿（需要约30秒）...")
    min_chars = config.TARGET_DURATION_MIN * 200
    max_chars = config.TARGET_DURATION_MAX * 200
    resp2 = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": BROADCAST_PROMPT.format(
            style=config.BROADCAST_STYLE,
            min_min=config.TARGET_DURATION_MIN,
            max_min=config.TARGET_DURATION_MAX,
            min_chars=min_chars,
            max_chars=max_chars,
            date=date,
            news_data=news_data_full,
        )}],
    )
    broadcast_text = resp2.content[0].text.strip()
    log(f"  ✓ 播报稿生成完成（{len(broadcast_text)}字，约{len(broadcast_text)//200}分钟）")

    return preview_text, broadcast_text


# ── 第三步：OpenAI TTS语音合成 ────────────────────────────────

def text_to_speech(text: str, filename: str) -> str:
    log(f"🎙️  OpenAI TTS合成语音：{filename}...")
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY") or config.OPENAI_API_KEY)
    MAX_CHARS = 4000
    filepath = os.path.join(config.OUTPUT_DIR, filename)

    if len(text) <= MAX_CHARS:
        with client.audio.speech.with_streaming_response.create(
            model="tts-1-hd", voice="nova", input=text, speed=1.0,
        ) as response:
            response.stream_to_file(filepath)
    else:
        chunks, current = [], ""
        for w in text.split("。"):
            if len(current) + len(w) < MAX_CHARS:
                current += w + "。"
            else:
                chunks.append(current)
                current = w + "。"
        if current:
            chunks.append(current)

        log(f"  文本较长，分{len(chunks)}段合成...")
        audio_data = b""
        for chunk in chunks:
            with client.audio.speech.with_streaming_response.create(
                model="tts-1-hd", voice="nova", input=chunk, speed=1.0,
            ) as response:
                audio_data += response.read()
        with open(filepath, "wb") as f:
            f.write(audio_data)

    size_kb = os.path.getsize(filepath) // 1024
    log(f"  ✓ 语音文件：{filepath}（{size_kb} KB）")
    return filepath


# ── 第三步b：lo-fi背景音乐混音 ───────────────────────────────

def mix_lofi_background(speech_path: str, output_path: str) -> str:
    """
    从 config.LOFI_PLAYLIST 随机选一首，下载缓存后混音。
    背景音量 -23 dB（更安静），淡入淡出 3 秒。
    失败则回退原始语音。
    """
    try:
        import random
        from pydub import AudioSegment
        log("🎵 混入 lo-fi 背景音乐...")

        speech = AudioSegment.from_mp3(speech_path)
        duration_ms = len(speech)

        # 从歌单随机选一首
        playlist = getattr(config, "LOFI_PLAYLIST", [])
        if not playlist:
            # 兜底：内置一首
            playlist = ["https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"]

        chosen_url = random.choice(playlist)
        # 缓存文件名用 URL hash，不同曲子互不覆盖
        import hashlib
        url_hash = hashlib.md5(chosen_url.encode()).hexdigest()[:8]
        lofi_local = os.path.join(config.OUTPUT_DIR, f"lofi_bg_{url_hash}.mp3")

        if not os.path.exists(lofi_local):
            log(f"  ↓ 下载背景音乐：{chosen_url}")
            r = requests.get(chosen_url, timeout=60)
            r.raise_for_status()
            with open(lofi_local, "wb") as f:
                f.write(r.content)
            log("  ✓ 背景音乐已缓存")
        else:
            log(f"  ✓ 使用缓存：{lofi_local}")

        bg = AudioSegment.from_mp3(lofi_local)
        bg_loop = (bg * ((duration_ms // len(bg)) + 2))[:duration_ms]
        # -23 dB：比之前的 -18 dB 再安静 5 dB，刚好作为底噪感存在
        bg_quiet = (bg_loop - 23).fade_in(3000).fade_out(3000)
        speech.overlay(bg_quiet).export(output_path, format="mp3", bitrate="128k")
        size_kb = os.path.getsize(output_path) // 1024
        log(f"  ✓ 混音完成：{output_path}（{size_kb} KB）")
        return output_path

    except ImportError:
        log("  ⚠️ pydub 未安装，跳过背景音乐")
        return speech_path
    except Exception as e:
        log(f"  ⚠️ 背景音乐混音失败，使用原始语音：{e}")
        return speech_path


def synthesize_audio(preview_text: str, broadcast_text: str) -> tuple:
    ensure_output_dir()
    date_tag = datetime.date.today().strftime("%Y%m%d")
    combined_text = preview_text + "\n\n" + broadcast_text
    raw_file = text_to_speech(combined_text, f"xiaolan_broadcast_raw_{date_tag}.mp3")
    mixed_path = os.path.join(config.OUTPUT_DIR, f"xiaolan_broadcast_{date_tag}.mp3")
    combined_file = mix_lofi_background(raw_file, mixed_path)
    return combined_file, combined_file


# ── 第四步：上传音频到 GitHub audio 分支，获取 raw 直链 ────────

def upload_audio_to_github(filepath: str) -> str:
    """
    把 mp3 用 GitHub Contents API 写入仓库的 audio 分支。
    返回 raw.githubusercontent.com 直链（无重定向，Twilio 可直接访问）。

    audio 分支如果不存在会自动基于默认分支创建。
    同名文件会覆盖（需要先拿旧文件 sha）。
    """
    import base64

    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_repo  = os.environ.get("GITHUB_REPOSITORY", "")  # "owner/repo"
    if not gh_token or not gh_repo:
        raise RuntimeError("GITHUB_TOKEN 或 GITHUB_REPOSITORY 未设置")

    fname = os.path.basename(filepath)
    branch = "audio"
    api_base = f"https://api.github.com/repos/{gh_repo}"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }

    log(f"  ☁️  上传音频到 GitHub audio 分支：{fname}...")

    # 1. 确保 audio 分支存在（不存在则从默认分支创建）
    branch_resp = requests.get(f"{api_base}/branches/{branch}", headers=headers, timeout=15)
    if branch_resp.status_code == 404:
        log("  → audio 分支不存在，正在创建...")
        # 获取默认分支的最新 commit sha
        default_resp = requests.get(f"{api_base}", headers=headers, timeout=15)
        default_branch = default_resp.json().get("default_branch", "main")
        ref_resp = requests.get(
            f"{api_base}/git/refs/heads/{default_branch}", headers=headers, timeout=15
        )
        base_sha = ref_resp.json().get("object", {}).get("sha", "")
        # 创建分支
        requests.post(
            f"{api_base}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            timeout=15,
        )
        log(f"  ✓ audio 分支已创建")

    # 2. 检查同名文件是否已存在（获取 sha，覆盖时需要）
    file_path_in_repo = f"audio/{fname}"
    existing_resp = requests.get(
        f"{api_base}/contents/{file_path_in_repo}",
        headers=headers,
        params={"ref": branch},
        timeout=15,
    )
    old_sha = existing_resp.json().get("sha", "") if existing_resp.status_code == 200 else ""

    # 3. 读取文件并 base64 编码
    with open(filepath, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    # 4. PUT 写入文件
    put_body = {
        "message": f"audio: {fname}",
        "content": content_b64,
        "branch": branch,
    }
    if old_sha:
        put_body["sha"] = old_sha  # 覆盖已有文件需要旧 sha

    put_resp = requests.put(
        f"{api_base}/contents/{file_path_in_repo}",
        headers=headers,
        json=put_body,
        timeout=180,
    )

    if put_resp.status_code not in (200, 201):
        raise RuntimeError(f"GitHub 写入失败 {put_resp.status_code}: {put_resp.text[:300]}")

    # 5. 构造 raw 直链
    owner, repo = gh_repo.split("/", 1)
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/audio/{fname}"
    size_kb = os.path.getsize(filepath) // 1024
    log(f"  ✓ 上传成功（{size_kb} KB）→ {raw_url}")
    return raw_url


# ── 第五步：发送到WhatsApp ────────────────────────────────────

def send_whatsapp_with_files(preview_text, broadcast_text, preview_file, broadcast_file):
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID") or config.TWILIO_ACCOUNT_SID
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")  or config.TWILIO_AUTH_TOKEN
    recipients  = config.WHATSAPP_RECIPIENTS
    from_num    = config.TWILIO_WHATSAPP_FROM
    date        = today_str()

    client = Client(account_sid, auth_token)

    log("  📤 上传音频...")
    try:
        audio_url = upload_audio_to_github(broadcast_file)
    except Exception as e:
        log(f"  ⚠️ 上传失败: {e}")
        audio_url = None

    for recipient in recipients:
        try:
            # 消息1：文字正文（播报稿前500字）
            preview_body = broadcast_text[:500].rstrip()
            if len(broadcast_text) > 500:
                preview_body += "……"

            text_msg = (
                f"🔵 *小蓝人播报* · {date}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"{preview_body}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"👇 语音版点击下方收听"
            )
            client.messages.create(from_=from_num, to=recipient, body=text_msg)
            time.sleep(2)

            # 消息2：语音
            if audio_url:
                client.messages.create(
                    from_=from_num, to=recipient,
                    body="🎙️ 小蓝人语音播报（预告+全文）",
                    media_url=[audio_url],
                )
                time.sleep(1)

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
        log("💾 文本备份已保存")

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
