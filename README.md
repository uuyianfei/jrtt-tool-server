# jrtt-tool-server

基于 Flask + MySQL 的今日头条爆文后端，包含：

- 小程序接口：`POST /articles/search`、`POST /rewrite/start`、`GET /rewrite/status`
- 定时爬虫：抓取今日头条推荐页，按环境变量过滤 `24h内 + 粉丝数阈值`
- 数据去重：按 `article_id/url` 唯一约束更新
- 定时清理：移除数据库中超过 24 小时的新闻

## 1. 安装依赖

```bash
pip install -r requirements.txt
```

## 2. 配置环境变量

复制 `env.example` 为 `.env`，并修改 MySQL 等参数。

关键配置：

- `CRAWL_MAX_HOURS`：最多抓取几小时内新闻（默认 24）
- `CRAWL_MAX_FANS`：粉丝量阈值（默认 10000）
- `CRAWL_INTERVAL_SECONDS`：爬虫执行间隔秒数
- `CLEANUP_INTERVAL_MINUTES`：清理任务执行间隔分钟数

## 3. 启动

```bash
python run.py
```

服务默认监听 `http://127.0.0.1:5000`。

## 4. 接口响应规范

统一返回：

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

- `code=0` 成功
- `code!=0` 失败

## 5. Docker Compose 部署

已提供：

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`

### 启动

```bash
docker compose --profile fast-baseline up -d --build
```

说明：`docker-compose.yml` 已支持 Fast Crawler 三种运行模式：
- `fast-baseline`：单实例全频道（基线）
- `fast-canary`：3 实例分片（`hot/tech/finance`）
- `fast-full`：5 实例分片（`hot/tech/finance/entertainment/sports`）

多实例模式下每个实例抓取互斥频道，避免同配置多实例重复抓取。
`docker compose` 会自动读取项目根目录 `.env`，请先配置
`MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB`。

### 分阶段启动示例

```bash
# 1) 基线：单实例
docker compose --profile fast-baseline up -d --build

# 2) 灰度：3 分片（先停基线）
docker compose --profile fast-baseline down
docker compose --profile fast-canary up -d --build

# 3) 全量：5 分片
docker compose --profile fast-canary down
docker compose --profile fast-full up -d --build
```

### 查看日志

```bash
docker compose logs -f api
docker compose logs -f fast-crawler
docker compose logs -f fast-crawler-hot fast-crawler-tech fast-crawler-finance
```

### 停止并清理

```bash
docker compose down
```

如需连容器内 Chromium，请在环境变量中保留：

- `CHROMEDRIVER_PATH=/usr/bin/chromedriver`
- `CHROME_BINARY_PATH=/usr/bin/chromium`

## 6. GitHub 自动部署（SSH 账号密码）

已提供工作流：`.github/workflows/deploy.yml`

触发方式：

- push 到 `main` 分支自动部署
- 在 GitHub Actions 页面手动点击 `Run workflow`

### 需要配置的 GitHub Secrets

- `SSH_HOST`：服务器地址
- `SSH_USER`：SSH 登录账号
- `SSH_PASSWORD`：SSH 登录密码

默认内置：

- `DEPLOY_APP_DIR=/home/jrtt-toolserver`
- `DEPLOY_BRANCH=main`

因此无需再配置 `DEPLOY_APP_DIR`、`DEPLOY_BRANCH`。
SSH 端口固定使用默认 `22`，无需配置 `SSH_PORT`。

### 服务器前置要求

- 服务器已安装 `docker` 和 `docker compose`
- `DEPLOY_APP_DIR` 已经是一个 git 仓库（先手动 `git clone` 一次）
- 项目目录里有可用 `.env`（云数据库配置等）

### 工作流执行内容

1. SSH 登录服务器（用户名+密码）
2. `git fetch` + `git reset --hard origin/<DEPLOY_BRANCH>`
3. `docker compose down`
4. `docker compose up -d --build`

## 7. 吞吐与稳定性验收基线

建议每次扩容或调参后固定观察 10 分钟窗口，按以下指标对比：

- `created_per_min`（新增入库速率）
- `upserted_per_min`（总写入速率）
- `avg_elapsed_seconds` 与 `FAST_CRAWL_INTERVAL_SECONDS` 的比值
- `rate_limited`（429）与 `errors`

### 日志侧快速统计

```bash
# 基线模式
docker compose logs --since=10m fast-crawler \
  | python tools/fast_crawler_metrics.py --window-minutes 10

# 灰度/全量分片模式（按服务分别统计）
docker compose logs --since=10m fast-crawler-hot \
  | python tools/fast_crawler_metrics.py --window-minutes 10
docker compose logs --since=10m fast-crawler-tech \
  | python tools/fast_crawler_metrics.py --window-minutes 10
docker compose logs --since=10m fast-crawler-finance \
  | python tools/fast_crawler_metrics.py --window-minutes 10
```

### 数据库侧快速统计（可选）

```sql
-- 最近 10 分钟新增文章
SELECT COUNT(*) AS new_articles_10m
FROM articles
WHERE created_at >= NOW() - INTERVAL 10 MINUTE;

-- 最近 10 分钟被处理过的作者
SELECT COUNT(*) AS processed_authors_10m
FROM author_sources
WHERE last_crawled_at >= NOW() - INTERVAL 10 MINUTE;

-- 最近 10 分钟被 fast crawler 更新过的文章
SELECT COUNT(*) AS seen_articles_10m
FROM articles
WHERE last_seen_at >= NOW() - INTERVAL 10 MINUTE;
```

### 逐步调参建议（一次只改一个变量）

1. 先调 `FAST_CRAWL_MAX_PAGES_PER_CHANNEL_*`（每次 +10）
2. 再调 `FAST_CRAWL_CONCURRENCY_*`（每次 +2）
3. 每次调整后观察至少 1 个 10 分钟窗口
4. 若 `avg_elapsed_seconds` 接近 interval、`rate_limited` 上升、`created_ratio` 下降，则回退

### 加固项（已内置）

- 多实例下新增 `conflict_retry` 统计：同一文章并发写入冲突会自动重试更新，减少整批回滚风险。
- 新增 `FAST_CRAWL_STARTUP_JITTER_SECONDS` 与 `FAST_CRAWL_LOOP_JITTER_SECONDS`，用于多实例错峰，降低突发请求峰值。
