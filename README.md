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
