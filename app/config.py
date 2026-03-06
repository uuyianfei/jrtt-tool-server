import os
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()


class Config:
    APP_ENV = os.getenv("APP_ENV", "dev")
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"

    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "root")
    MYSQL_DB = os.getenv("MYSQL_DB", "jrtt_tool")
    MYSQL_USER_ENCODED = quote_plus(MYSQL_USER)
    MYSQL_PASSWORD_ENCODED = quote_plus(MYSQL_PASSWORD)
    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{MYSQL_USER_ENCODED}:{MYSQL_PASSWORD_ENCODED}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}"
        "?charset=utf8mb4"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    CRAWL_INTERVAL_SECONDS = int(os.getenv("CRAWL_INTERVAL_SECONDS", "120"))
    CLEANUP_INTERVAL_MINUTES = int(os.getenv("CLEANUP_INTERVAL_MINUTES", "30"))
    CRAWL_JOB_ENABLED = os.getenv("CRAWL_JOB_ENABLED", "true").lower() == "true"
    CLEANUP_JOB_ENABLED = os.getenv("CLEANUP_JOB_ENABLED", "true").lower() == "true"
    CRAWL_TARGET_COUNT = int(os.getenv("CRAWL_TARGET_COUNT", "20"))
    CRAWL_LIST_SCROLL_ROUNDS = int(os.getenv("CRAWL_LIST_SCROLL_ROUNDS", "6"))
    CRAWL_DETAIL_WORKERS = int(os.getenv("CRAWL_DETAIL_WORKERS", "3"))
    CRAWL_MAX_HOURS = float(os.getenv("CRAWL_MAX_HOURS", "24"))
    CRAWL_MAX_FANS = int(os.getenv("CRAWL_MAX_FANS", "10000"))
    CRAWL_BLOCK_AUTHOR_KEYWORDS = os.getenv(
        "CRAWL_BLOCK_AUTHOR_KEYWORDS",
        "政府,检察院,法院,公安,交警,消防,税务,纪委,共青团,网信办,发布,融媒体中心",
    )
    CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "")
    CHROME_BINARY_PATH = os.getenv("CHROME_BINARY_PATH", "")
    CRAWL_HEADLESS = os.getenv("CRAWL_HEADLESS", "true").lower() == "true"
    TOUTIAO_URL = os.getenv("TOUTIAO_URL", "https://www.toutiao.com/")

    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
