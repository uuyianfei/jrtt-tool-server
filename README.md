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
docker compose up -d --build
```

说明：当前 `docker-compose.yml` 启动三个服务：
- `api`：仅提供接口，不跑定时爬虫任务
- `author-collect`：仅负责推荐页作者采集与作者池更新
- `author-articles`：仅负责作者文章抓取、文章入库与过期清理

两条爬虫链路完全解耦，并通过作者租约锁避免重复抓同一作者。
MySQL 使用外部云数据库。
`docker compose` 会自动读取项目根目录 `.env`，请先把
`MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB` 配成你的云数据库信息。

### 查看日志

```bash
docker compose logs -f api
docker compose logs -f author-collect
docker compose logs -f author-articles
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

建议每次优化后固定观察 10 分钟窗口，按以下指标对比：

- `每10分钟新增文章数`
- `每10分钟处理作者数`
- `锁冲突异常数（1205/1213）`
- `PendingRollbackError 数`

### 日志侧快速统计

```bash
docker compose logs --since=10m author-articles | grep -c "upsert success article_id="
docker compose logs --since=10m author-articles | grep -c "author articles summary"
docker compose logs --since=10m author-collect | grep -c "author collect summary"
docker compose logs --since=10m author-articles | grep -E "1205|1213|PendingRollbackError"
```

### 数据库侧快速统计

```sql
-- 最近 10 分钟新增文章
SELECT COUNT(*) AS new_articles_10m
FROM articles
WHERE created_at >= NOW() - INTERVAL 10 MINUTE;

-- 最近 10 分钟被处理过的作者
SELECT COUNT(*) AS processed_authors_10m
FROM author_sources
WHERE last_crawled_at >= NOW() - INTERVAL 10 MINUTE;

-- 当前租约中作者（用于观测多实例是否在正常领取）
SELECT COUNT(*) AS leased_authors_now
FROM author_sources
WHERE lease_until IS NOT NULL AND lease_until > NOW();
```
