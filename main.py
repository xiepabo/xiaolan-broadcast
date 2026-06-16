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
    import re
    now = datetime.datetime.utcnow()

    # 周一(weekday=0)覆盖周末，窗口72小时；其他工作日24小时
    hours_back = 72 if now.weekday() == 0 else 24
    cutoff = now - datetime.timedelta(hours=hours_back)
    log(f"  时间窗口：过去{hours_back}小时（UTC {cutoff.strftime('%m-%d %H:%M')} 至今）")

    articles = []
    for url in config.NEWS_SOURCES:
        try:
            feed = feedparser.parse(url)
            source_name = feed.feed.get("title", url)
            count_before = len(articles)
            for entry in feed.entries[:15]:
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub:
                    pub_dt = datetime.datetime(*pub[:6])
                    if pub_dt < cutoff:
                        continue
                title = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                summary = re.sub(r"<[^>]+>", "", summary)[:500]
                if title:
                    articles.append({
                        "source": source_name,
                        "title": title,
                        "summary": summary,
                        "link": entry.get("link", ""),
                    })
            added = len(articles) - count_before
            if added:
                log(f"  ✓ {source_name[:30]}：{added}条")
        except Exception as e:
            log(f"  ⚠ RSS失败 {url[:40]}: {e}")

    # 去重
    seen, unique = set(), []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)
    unique = unique[:config.MAX_ARTICLES]

    if not unique:
        log("⚠️ 时间窗口内无新闻")
    else:
        log(f"📰 共获取 {len(unique)} 条不重复新闻")
    return unique



# ── 第一步b：拉取实时行情数据 ────────────────────────────────

def fetch_market_data() -> str:
    """
    拉取两类数据：
    A. 大宗商品 + 宏观：布伦特原油、金价、美元指数
    B. UAE市场：DFM指数、DFM房地产指数、Emaar、Aldar、ALEC
    涨跌幅超过±10%标记为数据异常。
    """
    log("📊 拉取实时行情数据...")
    try:
        import yfinance as yf

        commodities = {
            "布伦特原油(USD/桶)": ["BZ=F", "BZ=F"],
            "现货金价(USD/盎司)":  ["GC=F", "GLD"],
            "美元指数":            ["DX-Y.NYB", "UUP"],   # 备用：UUP ETF
        }

        uae_stocks = {
            "DFM综合指数":   ["DFMGI.AE"],
            "DFM房地产指数": ["DFMREI.AE"],
            "Emaar(AED)":    ["EMAAR.AE"],
            "Aldar(AED)":    ["ALDAR.AE", "ALDAR.DU"],   # 备用代码
            "ALEC建筑(AED)": ["ALEC.AE"],
        }

        def get_quote(symbols: list, decimals: int = 2) -> str:
            for symbol in symbols:
                try:
                    t = yf.Ticker(symbol)
                    hist = t.history(period="5d", interval="1d")
                    if "Volume" in hist.columns:
                        hist = hist[hist["Volume"] >= 0]  # 保留成交量0的数据（指数无成交量）
                    hist = hist.dropna(subset=["Close"])
                    if hist.empty:
                        continue
                    price = hist["Close"].iloc[-1]
                    if price <= 0:
                        continue
                    if len(hist) >= 2:
                        prev = hist["Close"].iloc[-2]
                        if prev <= 0:
                            return f"{price:.{decimals}f}"
                        chg  = price - prev
                        pct  = chg / prev * 100
                        # 涨跌超10%标记异常
                        if abs(pct) > 10:
                            return f"{price:.{decimals}f}（数据异常{pct:+.1f}%，请核实）"
                        arrow = "↑" if chg >= 0 else "↓"
                        return f"{price:.{decimals}f}  {arrow}{abs(chg):.{decimals}f}（{pct:+.2f}%）"
                    return f"{price:.{decimals}f}"
                except Exception:
                    continue
            return "暂缺"

        sections = []

        a_lines = ["【大宗商品与宏观】"]
        for name, symbols in commodities.items():
            result = get_quote(symbols)
            a_lines.append(f"  {name}：{result}")
        sections.append("\n".join(a_lines))

        b_lines = ["【UAE市场】"]
        for name, symbols in uae_stocks.items():
            dec = 0 if "指数" in name else 2
            result = get_quote(symbols, dec)
            b_lines.append(f"  {name}：{result}")
        sections.append("\n".join(b_lines))

        result = "\n\n".join(sections)
        log("  ✓ 行情数据：")
        for line in result.split("\n"):
            if line.strip():
                log(f"    {line.strip()}")
        return result

    except ImportError:
        log("  ⚠ yfinance 未安装，行情数据跳过")
        return "（行情数据暂缺，需安装 yfinance）"
    except Exception as e:
        log(f"  ⚠ 行情数据获取失败：{e}")
        return f"（行情数据获取失败：{e}）"


# ── 第一步c：Claude网络搜索补充新闻 ─────────────────────────

def web_search_news(date_str: str) -> list:
    """
    用 Anthropic web_search 工具分两轮搜索：
    第一轮：海湾地缘安全（优先级最高）
    第二轮：经济/建筑/中国中东
    返回文章列表，格式与 fetch_news() 一致，方便合并。
    """
    log("🌐 Claude网络搜索补充新闻...")

    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    search_rounds = [
        {
            "label": "地缘安全",
            "prompt": f"""今天是{date_str}。请搜索以下主题的最新新闻（48小时内），每个主题搜一次：
1. Iran US nuclear deal sanctions latest news
2. Iran Gulf Strait of Hormuz tension
3. Israel Gaza Lebanon war update
4. Houthi Red Sea attack shipping
5. Saudi Arabia Iran UAE relations

搜索后整理为JSON数组，每条格式：
{{"source":"来源网站","title":"标题（翻译成中文）","summary":"2-3句摘要（中文），重点说对海湾局势的影响"}}

只输出JSON数组，不要其他内容。""",
        },
        {
            "label": "经济与建筑",
            "prompt": f"""今天是{date_str}。请搜索以下主题的最新新闻（48小时内），每个主题搜一次：
1. UAE construction project contract awarded 2026
2. Dubai Abu Dhabi infrastructure development news
3. China Middle East investment cooperation project
4. OPEC oil output UAE Saudi decision
5. RMB yuan Middle East trade settlement

搜索后整理为JSON数组，每条格式：
{{"source":"来源网站","title":"标题（翻译成中文）","summary":"2-3句摘要（中文）"}}

只输出JSON数组，不要其他内容。""",
        },
    ]

    all_articles = []

    for round_info in search_rounds:
        label = round_info["label"]
        try:
            log(f"  🔍 搜索：{label}...")
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": round_info["prompt"]}],
            )

            # 提取最终文本回复（跳过 tool_use / tool_result block）
            text_output = ""
            for block in resp.content:
                if hasattr(block, "text"):
                    text_output += block.text

            # 解析 JSON
            import re, json
            json_match = re.search(r"\[.*\]", text_output, re.DOTALL)
            if json_match:
                items = json.loads(json_match.group())
                for item in items:
                    if item.get("title"):
                        all_articles.append({
                            "source": f"网络搜索·{item.get('source', label)}",
                            "title":  item.get("title", ""),
                            "summary": item.get("summary", "")[:500],
                            "link":   "",
                        })
                log(f"  ✓ {label}：找到 {len(items)} 条")
            else:
                log(f"  ⚠ {label}：未找到JSON，跳过")

        except Exception as e:
            log(f"  ⚠ {label} 搜索失败：{e}")

    log(f"🌐 网络搜索共补充 {len(all_articles)} 条新闻")

    # 保存搜索结果到日志文件，方便排查
    try:
        ensure_output_dir()
        date_tag = datetime.date.today().strftime("%Y%m%d")
        search_log = f"{config.OUTPUT_DIR}/search_{date_tag}.json"
        with open(search_log, "w", encoding="utf-8") as f:
            json.dump(all_articles, f, ensure_ascii=False, indent=2)
        log(f"  💾 搜索结果已保存：{search_log}")
    except Exception:
        pass

    return all_articles



def generate_scripts(articles: list, market_data: str = "", search_articles: list = None, recent_context: str = "") -> tuple:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    date = today_str()

    # 合并 RSS + 网络搜索，去重（按标题）
    all_articles = list(articles)
    if search_articles:
        existing_titles = {a["title"] for a in all_articles}
        for a in search_articles:
            if a["title"] not in existing_titles:
                all_articles.append(a)
                existing_titles.add(a["title"])
    log(f"  📋 合并后共 {len(all_articles)} 条新闻（RSS {len(articles)} + 搜索 {len(search_articles or [])}）")

    if all_articles:
        news_list_short = "\n".join(
            f"{i+1}. 【{a['source']}】{a['title']}" for i, a in enumerate(all_articles)
        )
        news_data_full = "\n\n".join(
            f"来源：{a['source']}\n标题：{a['title']}\n摘要：{a['summary']}" for a in all_articles
        )
    else:
        news_list_short = "（今日无新闻数据）"
        news_data_full = "（今日无新闻数据，请在各板块按实际情况播报无动态）"

    log("🤖 Claude正在生成今日预告（一句话）...")

    # 在Python里算好时长，不靠Claude估
    # 每条新闻约1分钟，行情固定1分钟，结尾固定1分钟
    estimated_minutes = max(3, round(len(all_articles) * 1 + 2))

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": PREVIEW_PROMPT.format(
            date=date,
            count=len(all_articles),
            estimated_minutes=estimated_minutes,
            news_list=news_list_short,
        )}],
    )
    preview_text = resp.content[0].text.strip()
    log(f"  ✓ 预告：{preview_text}")

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
            market_data=market_data or "（行情数据暂缺）",
            recent_context=recent_context or "（暂无历史记忆，今日为首次播报）",
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


# ── 记忆系统：读取/存储近7天播报摘要 ────────────────────────

def _github_api(method: str, path: str, **kwargs) -> requests.Response:
    """统一的 GitHub Contents API 调用"""
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_repo  = os.environ.get("GITHUB_REPOSITORY", "")
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    url = f"https://api.github.com/repos/{gh_repo}/{path}"
    return requests.request(method, url, headers=headers, timeout=30, **kwargs)


def _ensure_branch(branch: str):
    """确保指定分支存在，不存在则从默认分支创建"""
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    resp = _github_api("GET", f"branches/{branch}")
    if resp.status_code == 404:
        default = _github_api("GET", "").json().get("default_branch", "main")
        sha = _github_api("GET", f"git/refs/heads/{default}").json().get("object", {}).get("sha", "")
        _github_api("POST", "git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
        log(f"  ✓ 分支 {branch} 已创建")


def load_memory() -> str:
    """
    从 GitHub memory 分支读取最近7天的播报摘要。
    返回格式化字符串，供提示词使用。
    """
    import base64
    log("🧠 读取近期记忆...")

    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_repo  = os.environ.get("GITHUB_REPOSITORY", "")
    if not gh_token or not gh_repo:
        log("  ⚠ 无GitHub凭证，跳过记忆读取")
        return ""

    branch = "memory"
    memories = []

    # 读取最近7个工作日的摘要
    today = datetime.date.today()
    for i in range(1, 11):  # 往前找10天，取到7个有记录的
        day = today - datetime.timedelta(days=i)
        date_tag = day.strftime("%Y%m%d")
        resp = _github_api("GET", f"contents/daily/{date_tag}.json", params={"ref": branch})
        if resp.status_code == 200:
            try:
                content = base64.b64decode(resp.json().get("content", "")).decode("utf-8")
                data = json.loads(content)
                memories.append(data)
                if len(memories) >= 7:
                    break
            except Exception:
                continue

    if not memories:
        log("  → 暂无历史记忆（首次运行）")
        return ""

    log(f"  ✓ 读取到 {len(memories)} 天的记忆")

    # 格式化为提示词上下文
    lines = ["以下是过去几天的播报摘要，供你参考，播报时可以引用和延续：\n"]
    for m in reversed(memories):  # 从旧到新
        lines.append(f"【{m.get('date', '')}】")
        if m.get("highlights"):
            for h in m["highlights"]:
                lines.append(f"  · {h}")
        if m.get("market_snapshot"):
            lines.append(f"  行情：{m['market_snapshot']}")
        if m.get("conclusion"):
            lines.append(f"  小蓝人说：{m['conclusion']}")
        lines.append("")

    return "\n".join(lines)


def summarize_for_memory(broadcast_text: str, market_data: str, date: str) -> dict:
    """
    用 Claude 把播报稿压缩成结构化摘要存入记忆。
    """
    log("🧠 生成今日记忆摘要...")
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": f"""把以下播报稿压缩成结构化摘要，输出JSON格式：

{{
  "date": "{date}",
  "highlights": ["最重要的3-5条事件，每条一句话"],
  "market_snapshot": "油价/金价/DFM指数的关键数据，一句话",
  "conclusion": "今天小蓝人说的核心判断，一句话"
}}

只输出JSON，不要其他内容。

播报稿：
{broadcast_text[:3000]}

行情数据：
{market_data}"""}],
    )

    import re
    text = resp.content[0].text.strip()
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            log(f"  ✓ 摘要生成完成：{len(data.get('highlights', []))}条要点")
            return data
        except Exception as e:
            log(f"  ⚠ JSON解析失败：{e}")

    # 兜底：返回简单结构
    return {"date": date, "highlights": [broadcast_text[:100]], "market_snapshot": "", "conclusion": ""}


def save_memory(summary: dict):
    """
    把今日摘要写入 GitHub memory 分支的 daily/YYYYMMDD.json。
    同时清理14天前的旧文件。
    """
    import base64
    log("🧠 保存今日记忆...")

    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_repo  = os.environ.get("GITHUB_REPOSITORY", "")
    if not gh_token or not gh_repo:
        log("  ⚠ 无GitHub凭证，跳过记忆保存")
        return

    try:
        _ensure_branch("memory")

        date_tag = datetime.date.today().strftime("%Y%m%d")
        file_path = f"daily/{date_tag}.json"
        content_str = json.dumps(summary, ensure_ascii=False, indent=2)
        content_b64 = base64.b64encode(content_str.encode()).decode()

        # 检查是否已存在（覆盖需要 sha）
        existing = _github_api("GET", f"contents/{file_path}", params={"ref": "memory"})
        old_sha = existing.json().get("sha", "") if existing.status_code == 200 else ""

        body = {
            "message": f"memory: {date_tag}",
            "content": content_b64,
            "branch": "memory",
        }
        if old_sha:
            body["sha"] = old_sha

        put = _github_api("PUT", f"contents/{file_path}", json=body)
        if put.status_code in (200, 201):
            log(f"  ✓ 记忆已保存：{file_path}")
        else:
            log(f"  ⚠ 保存失败：{put.status_code}")

        # 清理14天前的旧记忆
        cutoff = datetime.date.today() - datetime.timedelta(days=14)
        cutoff_tag = cutoff.strftime("%Y%m%d")
        list_resp = _github_api("GET", "contents/daily", params={"ref": "memory"})
        if list_resp.status_code == 200:
            for f in list_resp.json():
                fname = f.get("name", "")
                if fname.endswith(".json") and fname.replace(".json", "") < cutoff_tag:
                    _github_api("DELETE", f"contents/daily/{fname}", json={
                        "message": f"cleanup: {fname}",
                        "sha": f.get("sha", ""),
                        "branch": "memory",
                    })
                    log(f"  🗑 清理旧记忆：{fname}")

    except Exception as e:
        log(f"  ⚠ 记忆保存失败：{e}")



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
        recent_context = load_memory()
        articles = fetch_news()
        search_articles = web_search_news(today_str())
        market_data = fetch_market_data()
        preview_text, broadcast_text = generate_scripts(articles, market_data, search_articles, recent_context)

        ensure_output_dir()
        date_tag = datetime.date.today().strftime("%Y%m%d")
        with open(f"{config.OUTPUT_DIR}/script_{date_tag}.txt", "w", encoding="utf-8") as f:
            f.write(f"=== 今日预告 ===\n\n{preview_text}\n\n")
            f.write(f"=== 完整播报稿 ===\n\n{broadcast_text}\n\n")
            f.write(f"=== 数据来源 ===\n\n")
            f.write(f"RSS新闻：{len(articles)}条\n")
            f.write(f"网络搜索：{len(search_articles)}条\n")
            f.write(f"行情数据：\n{market_data}\n")
        log("💾 文本备份已保存")

        preview_file, broadcast_file = synthesize_audio(preview_text, broadcast_text)

        # 保存今日记忆
        summary = summarize_for_memory(broadcast_text, market_data, today_str())
        save_memory(summary)

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
