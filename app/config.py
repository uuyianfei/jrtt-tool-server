import os

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
    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}"
        "?charset=utf8mb4"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    CRAWL_INTERVAL_SECONDS = int(os.getenv("CRAWL_INTERVAL_SECONDS", "120"))
    CLEANUP_INTERVAL_MINUTES = int(os.getenv("CLEANUP_INTERVAL_MINUTES", "30"))
    CRAWL_TARGET_COUNT = int(os.getenv("CRAWL_TARGET_COUNT", "20"))
    CRAWL_MAX_HOURS = float(os.getenv("CRAWL_MAX_HOURS", "24"))
    CRAWL_MAX_FANS = int(os.getenv("CRAWL_MAX_FANS", "10000"))
    CRAWL_HEADLESS = os.getenv("CRAWL_HEADLESS", "true").lower() == "true"
    TOUTIAO_URL = os.getenv("TOUTIAO_URL", "https://www.toutiao.com/")

    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
