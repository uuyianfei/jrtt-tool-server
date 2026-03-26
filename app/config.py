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
    CRAWL_DIRECT_RECOMMEND_ENABLED = os.getenv("CRAWL_DIRECT_RECOMMEND_ENABLED", "false").lower() == "true"
    CLEANUP_INTERVAL_MINUTES = int(os.getenv("CLEANUP_INTERVAL_MINUTES", "30"))
    CRAWL_JOB_ENABLED = os.getenv("CRAWL_JOB_ENABLED", "true").lower() == "true"
    CLEANUP_JOB_ENABLED = os.getenv("CLEANUP_JOB_ENABLED", "true").lower() == "true"
    AUTHOR_COLLECT_JOB_ENABLED = os.getenv("AUTHOR_COLLECT_JOB_ENABLED", "true").lower() == "true"
    AUTHOR_ARTICLES_JOB_ENABLED = os.getenv("AUTHOR_ARTICLES_JOB_ENABLED", "true").lower() == "true"
    CRAWL_TARGET_COUNT = int(os.getenv("CRAWL_TARGET_COUNT", "20"))
    CRAWL_LIST_SCROLL_ROUNDS = int(os.getenv("CRAWL_LIST_SCROLL_ROUNDS", "6"))
    CRAWL_LIST_INITIAL_WAIT_SECONDS = float(os.getenv("CRAWL_LIST_INITIAL_WAIT_SECONDS", "1.0"))
    CRAWL_LIST_REFRESH_WAIT_SECONDS = float(os.getenv("CRAWL_LIST_REFRESH_WAIT_SECONDS", "1.0"))
    CRAWL_LIST_SCROLL_WAIT_SECONDS = float(os.getenv("CRAWL_LIST_SCROLL_WAIT_SECONDS", "0.9"))
    CRAWL_LIST_NO_GROWTH_EARLY_STOP_ROUNDS = int(os.getenv("CRAWL_LIST_NO_GROWTH_EARLY_STOP_ROUNDS", "2"))
    CRAWL_DETAIL_WORKERS = int(os.getenv("CRAWL_DETAIL_WORKERS", "3"))
    CRAWL_MAX_HOURS = float(os.getenv("CRAWL_MAX_HOURS", "24"))
    CRAWL_MAX_FANS = int(os.getenv("CRAWL_MAX_FANS", "10000"))
    AUTHOR_COLLECT_INTERVAL_SECONDS = int(os.getenv("AUTHOR_COLLECT_INTERVAL_SECONDS", "120"))
    AUTHOR_COLLECT_TARGET_COUNT = int(os.getenv("AUTHOR_COLLECT_TARGET_COUNT", "200"))
    AUTHOR_COLLECT_FANS_WORKERS = int(os.getenv("AUTHOR_COLLECT_FANS_WORKERS", "1"))
    AUTHOR_CRAWL_INTERVAL_SECONDS = int(os.getenv("AUTHOR_CRAWL_INTERVAL_SECONDS", "120"))
    AUTHOR_RECRAWL_INTERVAL_HOURS = float(os.getenv("AUTHOR_RECRAWL_INTERVAL_HOURS", "5"))
    AUTHOR_ARTICLES_CONTINUOUS_ENABLED = os.getenv("AUTHOR_ARTICLES_CONTINUOUS_ENABLED", "true").lower() == "true"
    AUTHOR_ARTICLES_IDLE_SLEEP_SECONDS = int(os.getenv("AUTHOR_ARTICLES_IDLE_SLEEP_SECONDS", "30"))
    AUTHOR_ARTICLES_RUN_UNTIL_EXHAUSTED = os.getenv("AUTHOR_ARTICLES_RUN_UNTIL_EXHAUSTED", "true").lower() == "true"
    AUTHOR_TRIGGER_ARTICLES_ON_COLLECT = os.getenv("AUTHOR_TRIGGER_ARTICLES_ON_COLLECT", "true").lower() == "true"
    AUTHOR_CRAWL_BATCH_SIZE = int(os.getenv("AUTHOR_CRAWL_BATCH_SIZE", "20"))
    AUTHOR_PER_AUTHOR_TARGET_COUNT = int(os.getenv("AUTHOR_PER_AUTHOR_TARGET_COUNT", "20"))
    AUTHOR_ARTICLE_SCROLL_ROUNDS = int(os.getenv("AUTHOR_ARTICLE_SCROLL_ROUNDS", "4"))
    AUTHOR_ARTICLE_MAX_HOURS = float(os.getenv("AUTHOR_ARTICLE_MAX_HOURS", "24"))
    AUTHOR_ARTICLE_MIN_VIEWS = int(os.getenv("AUTHOR_ARTICLE_MIN_VIEWS", "2000"))
    AUTHOR_READ_COUNT_FALLBACK_ENABLED = os.getenv("AUTHOR_READ_COUNT_FALLBACK_ENABLED", "false").lower() == "true"
    AUTHOR_MAX_FAILS = int(os.getenv("AUTHOR_MAX_FAILS", "5"))
    AUTHOR_LEASE_SECONDS = int(os.getenv("AUTHOR_LEASE_SECONDS", "240"))
    AUTHOR_COLLECT_COMMIT_BATCH_SIZE = int(os.getenv("AUTHOR_COLLECT_COMMIT_BATCH_SIZE", "50"))
    JOB_JITTER_SECONDS = int(os.getenv("JOB_JITTER_SECONDS", "10"))
    WORKER_ROLE = os.getenv("WORKER_ROLE", "")
    CRAWL_BLOCK_AUTHOR_KEYWORDS = os.getenv(
        "CRAWL_BLOCK_AUTHOR_KEYWORDS",
        "政府,检察院,法院,公安,交警,消防,税务,纪委,共青团,网信办,发布,融媒体中心",
    )
    CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "")
    CHROME_BINARY_PATH = os.getenv("CHROME_BINARY_PATH", "")
    CRAWL_USER_AGENT = os.getenv("CRAWL_USER_AGENT", "").strip()
    CRAWL_HEADLESS = os.getenv("CRAWL_HEADLESS", "true").lower() == "true"
    DETAIL_PAGE_READY_TIMEOUT_SECONDS = int(os.getenv("DETAIL_PAGE_READY_TIMEOUT_SECONDS", "12"))
    BLANK_PAGE_RECOVERY_MAX_ROUNDS = int(os.getenv("BLANK_PAGE_RECOVERY_MAX_ROUNDS", "2"))
    TOUTIAO_URL = os.getenv("TOUTIAO_URL", "https://www.toutiao.com/")

    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # --- Fast HTTP Crawler ---
    FAST_CRAWL_ENABLED = os.getenv("FAST_CRAWL_ENABLED", "false").lower() == "true"
    FAST_CRAWL_INTERVAL_SECONDS = int(os.getenv("FAST_CRAWL_INTERVAL_SECONDS", "120"))
    FAST_CRAWL_CHANNELS = os.getenv(
        "FAST_CRAWL_CHANNELS",
        "__all__,news_hot,news_tech,news_finance,news_entertainment,news_sports,"
        "news_society,news_car,news_health,news_military,news_world,news_travel,news_history",
    )
    FAST_CRAWL_CONCURRENCY = int(os.getenv("FAST_CRAWL_CONCURRENCY", "10"))
    FAST_CRAWL_MAX_HOURS = float(os.getenv("FAST_CRAWL_MAX_HOURS", "24"))
    FAST_CRAWL_MAX_PAGES_PER_CHANNEL = int(os.getenv("FAST_CRAWL_MAX_PAGES_PER_CHANNEL", "50"))
    FAST_CRAWL_REQUEST_DELAY = float(os.getenv("FAST_CRAWL_REQUEST_DELAY", "0.3"))
    FAST_CRAWL_LOOP_JITTER_SECONDS = int(os.getenv("FAST_CRAWL_LOOP_JITTER_SECONDS", "15"))
    FAST_CRAWL_STARTUP_JITTER_SECONDS = int(os.getenv("FAST_CRAWL_STARTUP_JITTER_SECONDS", "20"))
    FAST_CRAWL_MIN_CONTENT_LENGTH = int(os.getenv("FAST_CRAWL_MIN_CONTENT_LENGTH", "80"))
    FAST_CRAWL_MAX_FANS = int(os.getenv("FAST_CRAWL_MAX_FANS", "0"))  # 0 = no limit
    METRICS_RECONCILE_ENABLED = os.getenv("METRICS_RECONCILE_ENABLED", "false").lower() == "true"
    METRICS_RECONCILE_INTERVAL_SECONDS = int(os.getenv("METRICS_RECONCILE_INTERVAL_SECONDS", "60"))
    METRICS_RECONCILE_BATCH_SIZE = int(os.getenv("METRICS_RECONCILE_BATCH_SIZE", "30"))
    METRICS_RECONCILE_MAX_HOURS = float(os.getenv("METRICS_RECONCILE_MAX_HOURS", "24"))
    METRICS_RECONCILE_REQUEST_DELAY = float(os.getenv("METRICS_RECONCILE_REQUEST_DELAY", "0.25"))

    # --- Reconcile pending "author followers" distributed claim ---
    # Prevent multiple reconcile workers from updating the same `author_sources` row concurrently.
    AUTHOR_FANS_CLAIM_ENABLED = os.getenv("AUTHOR_FANS_CLAIM_ENABLED", "true").lower() == "true"
    AUTHOR_FANS_CLAIM_LEASE_SECONDS = int(os.getenv("AUTHOR_FANS_CLAIM_LEASE_SECONDS", "240"))

    # --- Fast HTTP crawler distributed claim (no-miss mode) ---
    # When enabled, `fast_crawler` will ignore FAST_CRAWL_SHARD_COUNT/INDEX hard filtering
    # and instead use `fast_crawl_claims` lease rows to ensure each gid is processed by
    # at most one worker at a time (claim can be re-acquired after expiry).
    FAST_CRAWL_CLAIM_ENABLED = os.getenv("FAST_CRAWL_CLAIM_ENABLED", "true").lower() == "true"
    FAST_CRAWL_CLAIM_LEASE_SECONDS = int(os.getenv("FAST_CRAWL_CLAIM_LEASE_SECONDS", "300"))

    # --- db.create_all distributed mutex (avoid MySQL 1684 concurrent DDL) ---
    DB_CREATE_ALL_LOCK_ENABLED = os.getenv("DB_CREATE_ALL_LOCK_ENABLED", "true").lower() == "true"
    DB_CREATE_ALL_LOCK_NAME = os.getenv("DB_CREATE_ALL_LOCK_NAME", "jrtt_db_create_all")
    DB_CREATE_ALL_LOCK_TIMEOUT_SECONDS = int(os.getenv("DB_CREATE_ALL_LOCK_TIMEOUT_SECONDS", "20"))
    DB_CREATE_ALL_LOCK_MAX_TRIES = int(os.getenv("DB_CREATE_ALL_LOCK_MAX_TRIES", "6"))
    DB_CREATE_ALL_LOCK_SLEEP_SECONDS = float(os.getenv("DB_CREATE_ALL_LOCK_SLEEP_SECONDS", "2.0"))
