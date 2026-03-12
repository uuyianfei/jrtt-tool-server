# Project structure exploration for jrtt-tool-server
_Exported on 3/12/2026 at 16:22:30 GMT+8 from Cursor (2.6.18)_

---

**User**

Explore the project structure of d:\Project\WeChat\ideara\jrtt-tool-server thoroughly. I need to understand:
1. The overall directory layout and key files
2. What framework is used (FastAPI, Flask, etc.)
3. Configuration files (docker-compose.yml, .env, requirements.txt, etc.)
4. Database models and schemas

Return a comprehensive directory tree and summary of key files.

---

**Cursor**


正在确认完整目录结构并查找 `rewrite_service`：



下面是整理后的项目结构和使用说明。

---

# jrtt-tool-server 项目结构概览

## 1. 目录结构

```
jrtt-tool-server/
├── app/
│   ├── __init__.py          # Flask 应用入口、create_app、蓝本注册
│   ├── config.py            # 配置（MySQL、爬虫、DeepSeek 等）
│   ├── models.py            # 数据模型
│   ├── extensions.py        # SQLAlchemy db 扩展
│   ├── crawler.py           # 今日头条爬虫
│   ├── scheduler.py         # APScheduler 定时任务
│   ├── rewrite_service.py   # 文章改写（AI）服务
│   ├── time_utils.py        # 上海时区与时间处理
│   ├── utils.py             # 通用工具、success/error 响应等
│   └── routes/
│       ├── __init__.py
│       ├── articles.py      # 文章搜索、导出接口
│       └── rewrite.py       # 改写任务接口
├── .cursor/plans/           # 计划文档
├── example/                 # 示例脚本
├── run.py                   # Web 服务入口（Flask）
├── run_crawler.py           # 爬虫 Worker 入口
├── start_local_crawlers.py  # 本地爬虫启动
├── start_local_articles_only.py
├── import_author_sources.py # 导入作者源
├── check_db.py              # 数据库检查
├── test_toutiao_request.py  # 今日头条请求测试
├── requirements.txt
├── env.example
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
├── README.md
├── BACKEND_REQUIREMENTS.md
├── implementation_plan.md
├── crawler_bug_analysis.md
├── good_authors.json
└── test.json
```

---

## 2. 技术框架

- **Web 框架**：Flask 3.0.3  
- **ORM**：Flask-SQLAlchemy 3.1.1  
- **数据库**：MySQL（驱动 PyMySQL 1.1.1）  
- **定时任务**：APScheduler 3.10.4  
- **爬虫**：Selenium 4.27.1 + BeautifulSoup 4.12.3  
- **其他**：python-dotenv、requests、openpyxl、python-docx  

**API 响应格式**：统一 `{ code, message, data }`（`code=0` 表示成功）。

---

## 3. 配置文件

| 文件 | 作用 |
|------|------|
| **env.example** | 环境变量示例，复制为 `.env` 后修改 |
| **requirements.txt** | Python 依赖 |
| **app/config.py** | 从 `.env` 加载配置，含 MySQL、爬虫、DeepSeek 等 |
| **docker-compose.yml** | 4 个服务：api / author-collect / author-articles / recommend-news |
| **Dockerfile** | Python 3.11-slim + Chromium + Xvfb |

**主要环境变量**：

- `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DB`
- `CRAWL_JOB_ENABLED` / `AUTHOR_COLLECT_JOB_ENABLED` / `AUTHOR_ARTICLES_JOB_ENABLED` / `CLEANUP_JOB_ENABLED`
- `CRAWL_INTERVAL_SECONDS` / `CRAWL_MAX_HOURS` / `CRAWL_MAX_FANS`
- `DEEPSEEK_API_KEY` / `DEEPSEEK_API_URL` / `DEEPSEEK_MODEL`

---

## 4. 数据库模型与 Schema

### 4.1 SQLAlchemy 模型（`app/models.py`）

**Article（文章）**

- `id`, `article_id`, `url_hash`, `url`, `title`, `cover`
- `author`, `author_url`
- `publish_time_text`, `published_at`, `published_hours_ago`
- `followers`, `view_count`, `like_count`, `comment_count`
- `source_html`, `created_at`, `updated_at`, `last_seen_at`

**RewriteTask（改写任务）**

- `id`, `task_id`, `url`, `article_id`, `status`, `progress`, `status_text`, `time_remaining`
- `source_html`, `rewritten_body_html`, `original_title`, `suggested_titles`, `error_message`
- `created_at`, `updated_at`, `completed_at`

**AuthorSource（作者源）**

- `id`, `author_url`, `author_name`, `followers`, `status`
- `lease_owner`, `lease_until`（租约锁）
- `fail_count`, `last_error`
- `first_seen_at`, `last_seen_at`, `last_crawled_at`, `created_at`, `updated_at`

### 4.2 请求/响应 Schema

项目未使用 Pydantic 或 Marshmallow，接口通过 `request.get_json()` 获取参数并手动校验、返回 `jsonify()`。

---

## 5. 关键文件与入口

| 文件 | 说明 |
|------|------|
| `run.py` | 启动 Flask 应用，端口 5000 |
| `run_crawler.py` | 爬虫 Worker，按 `WORKER_ROLE` 跑 author-collect / author-articles / recommend-news |
| `app/__init__.py` | `create_app()`，注册蓝本，初始化 Scheduler |
| `app/routes/articles.py` | `POST /articles/search`、`POST /articles/export` |
| `app/routes/rewrite.py` | `POST /rewrite/start`、`GET /rewrite/status`、`POST /rewrite/export-docx` |
| `app/scheduler.py` | 定时任务：作者采集、作者文章抓取、推荐流抓取、清理过期文章 |
| `app/crawler.py` | Selenium 爬虫实现 |
| `app/rewrite_service.py` | 抓取原文 + DeepSeek API 改写，后台线程执行 |

---

## 6. Docker 服务（docker-compose.yml）

| 服务 | 功能 |
|------|------|
| **api** | 只跑 HTTP API，不跑爬虫 |
| **author-collect** | 从推荐页采集作者，更新作者池 |
| **author-articles** | 抓取作者文章、入库、清理过期 |
| **recommend-news** | 抓取今日头条推荐流 |
| **import-authors** | 已注释：从 `good_authors.json` 导入作者 |

---

## 7. 其他说明

- 使用 `db.create_all()` 创建表，无 Alembic 迁移。
- API 服务默认关闭 Scheduler（`enable_scheduler=False`），爬虫由独立 Worker 容器执行。
- 通过 `lease_owner` / `lease_until` 实现作者租约，避免多实例重复抓取同一作者。

