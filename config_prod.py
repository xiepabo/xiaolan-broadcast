"""
生产环境配置 · 从环境变量读取（GitHub Actions Secrets）
本地测试时用 config.py，部署后自动切换到这里
"""
import os
import json

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# OpenAI TTS
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# ElevenLabs
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# 声音参数
VOICE_STABILITY      = 0.35
VOICE_SIMILARITY     = 0.80
VOICE_STYLE          = 0.45
VOICE_SPEAKER_BOOST  = True

# Twilio
TWILIO_ACCOUNT_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"

# 收件人：从环境变量读取JSON数组
# 格式: ["whatsapp:+971XXXXXXXX","whatsapp:+971XXXXXXXX"]
_recipients_raw = os.environ.get("WHATSAPP_RECIPIENTS", "[]")
try:
    WHATSAPP_RECIPIENTS = json.loads(_recipients_raw)
except json.JSONDecodeError:
    WHATSAPP_RECIPIENTS = [_recipients_raw] if _recipients_raw else []

# 新闻源
NEWS_SOURCES = [
    "https://www.thenationalnews.com/rss/business.xml",
    "https://gulfnews.com/rss/business",
    "https://www.arabianbusiness.com/rss",
    "https://www.khaleejtimes.com/rss/business/property",
    "https://fintechnews.ae/feed/",
    "https://wam.ae/en/rss",
]
MAX_ARTICLES = 25

# 播报设置
BROADCAST_LANGUAGE   = "中文"
BROADCAST_STYLE      = "轻松幽默但不失深度，像一个在迪拜混了多年的金融老炮儿"
TARGET_DURATION_MIN  = 7
TARGET_DURATION_MAX  = 10
COMPANY_NAME         = "中建中东投资"

OUTPUT_DIR = "output"
