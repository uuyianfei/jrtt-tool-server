---
name: author-fans-claim
overview: 为解决 `metrics-reconcile` 中更新 `author_sources` 时出现的 MySQL 1205 锁等待超时，给 pending 模式的“作者粉丝刷新”增加分布式 claim（按 author_id 抢占），并将事务拆分为更短的多个 commit，降低锁持有时间。
todos:
  - id: add-author-fans-claim-model
    content: 在 `app/models.py` 新增 `AuthorFansClaim`（`author_id` unique、owner、expires_at、created_at/updated_at）。
    status: in_progress
  - id: add-config-for-author-fans-claim
    content: 在 `app/config.py` 与 `env.example` 增加 `AUTHOR_FANS_CLAIM_ENABLED`、`AUTHOR_FANS_CLAIM_LEASE_SECONDS`。
    status: pending
  - id: implement-author-claim-in-reconcile-once
    content: 在 `tools/reconcile_article_metrics.py` 的 `reconcile_once()` 中：实现 `claim_author_ids()` 并让 followers 刷新只处理 `claimed_author_ids`。
    status: pending
  - id: split-transactions-in-reconcile-once
    content: 在 `reconcile_once()` 中将步骤 0.2、步骤 1、步骤 2 拆分为多个 commit，降低锁持有时间并减少 autoflush 触发时机导致的 1205。
    status: pending
  - id: verification
    content: 重启 metrics-reconcile 容器后观察：`1205 Lock wait timeout exceeded` 频率下降，且 pending 文章在后续轮次被推进（`pending_author_followers_unavailable` 下降）。
    status: pending
isProject: false
---

## 背景与根因

- 你当前日志：`tools/reconcile_article_metrics.py` 在执行 `UPDATE author_sources ... WHERE author_sources.id = ...` 时遇到 `pymysql.err.OperationalError: (1205, 'Lock wait timeout exceeded; try restarting transaction')`。
- `reconcile_article_metrics.py` 已经对 `Article` 做了“文章级 claim”（`claim_pending_articles_for_refresh` / `with_for_update(skip_locked=True)`），但“作者粉丝刷新”对 `AuthorSource` 没有 claim：多个 worker 可能同时刷新同一个 `author_sources.id`，导致行锁竞争并触发 1205。

## 目标

- 不漏（最终一致）：如果某个 `author_id` 本轮没抢到 claim，本轮不刷新它的 `followers`，相关文章会短时保持 `metrics_status=pending`，后续会被重试补上（你已确认可接受）。
- 降低锁等待：通过“作者级分布式 claim + 更短事务 commit”，避免多个 worker 同时更新同一 `author_sources` 行。

## 方案设计（专业、工程化）

### 1) 新增作者粉丝 claim 表

- 文件：[app/models.py](app/models.py)
- 新增模型：`AuthorFansClaim`（`author_id` 唯一、`owner`、`expires_at`、`created_at/updated_at`）。
- 复用你已有的 fast-crawler claim 思路：用 MySQL `INSERT ... ON DUPLICATE KEY UPDATE` 做原子抢占。

### 2) pending 模式下增加 claim_author_ids

- 文件：[tools/reconcile_article_metrics.py](tools/reconcile_article_metrics.py)
- 只在 pending 模式（`reconcile_once`）的“步骤 1：Update author followers in batch”中加入 claim。
- checked-refresh 模式（`reconcile_checked_once`）当前不会更新 followers，不需要加入 claim。

### 3) 拆分事务为短事务

将 `reconcile_once` 内的单大事务改为至少 2~3 段短事务（只影响 pending 模式的 pending reconcile）：

- Step 0.2（作者映射/创建）完成后立刻 commit。
- Step 1（作者 followers 刷新）完成后 commit。
- Step 2（逐文章 metrics 更新）最后 commit。

这能显著缩短行锁持有时间，并减少 SQLAlchemy autoflush 在“查询 author”之前就触发 flush 导致的锁等待。

## 数据流示意（简化）

```mermaid
graph LR
  A[claim_pending_articles_for_refresh] --> B[Step0: map author_id (maybe insert AuthorSource)]
  B --> C[commit]
  C --> D[claim_author_ids(author_id) via author_fans_claims]
  D --> E[update followers for claimed authors only]
  E --> F[commit]
  F --> G[update Article metrics status using current followers]
  G --> H[commit]
```



## 具体改动点（待实现）

1. `[app/models.py](app/models.py)`：新增 `AuthorFansClaim`。
2. `[app/config.py](app/config.py)` + `[env.example](env.example)`：新增（或复用）配置项：
  - `AUTHOR_FANS_CLAIM_ENABLED`（默认 `true`）
  - `AUTHOR_FANS_CLAIM_LEASE_SECONDS`（默认 `240`）
3. `[tools/reconcile_article_metrics.py](tools/reconcile_article_metrics.py)`：
  - 在 `reconcile_once()` 内：
    - 统计 `author_ids` 后调用 `claim_author_ids(author_ids)`，得到 `claimed_author_ids`。
    - followers 刷新循环仅处理 `claimed_author_ids`。
    - 在 step 0.2 完成后 `db.session.commit()`。
    - 在 step 1 完成后 `db.session.commit()`。

## 验收标准

- `metrics-reconcile-*` 不再频繁出现 1205 锁等待超时；即使出现也应显著减少。
- 日志中的 `pending_author_followers_unavailable` 可能上升（短延迟），但后续轮次应该能下降（最终一致）。

## 风险与回退

- 风险：短时 pending 增加（已确认可接受）。
- 回退：配置 `AUTHOR_FANS_CLAIM_ENABLED=false` 可临时关闭 claim，仅保留事务拆分策略（仍应减轻锁竞争）。

