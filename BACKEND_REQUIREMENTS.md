# JRTT Tool 后端需求开发文档（V1）

## 1. 目标与范围
- 为现有小程序前端提供真实后端接口，替换当前 mock。
- 支持两大核心能力：
  - 爆文检索列表（筛选、排序、分页）
  - AI 改写任务（创建任务、轮询状态、返回改写结果）
- 本期仅覆盖前端已实现功能；`保存结果` 当前前端仍为“开发中”，后端可先预留。

## 2. 前端已对接的接口（必须兼容）
前端真实模式固定调用以下 3 个接口：

1. `POST /articles/search`
2. `POST /rewrite/start`
3. `GET /rewrite/status?taskId=xxx`

说明：
- 基础域名来自前端配置 `serviceConfig.realApiBaseUrl`
- 返回格式必须是统一包裹结构：`{ code, message, data }`
- 仅当 `code === 0` 前端才视为成功

## 3. 统一响应规范（必须）

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

- `code = 0`：成功
- `code != 0`：失败（前端会直接提示 `message`）
- HTTP 200 且 `code != 0` 也会被前端当失败处理
- 建议错误码：
  - `4001` 参数错误
  - `4004` 资源不存在（如 taskId 不存在）
  - `5000` 服务异常
  - `5001` AI 服务失败或超时

## 4. 接口详细定义

### 4.1 爆文搜索接口
`POST /articles/search`

#### 请求体

```json
{
  "pageNo": 1,
  "pageSize": 6,
  "maxPublishedHours": 12,
  "sortField": "time",
  "sortOrder": "desc",
  "followerFilter": { "op": ">", "value": 1000, "enabled": true },
  "viewFilter": { "op": ">", "value": 20000, "enabled": true },
  "likeFilter": { "op": ">", "value": 500, "enabled": false },
  "commentFilter": { "op": ">", "value": 100, "enabled": false }
}
```

#### 字段约束
- `sortField`: `followers | views | time`
- `sortOrder`: `asc | desc`
- `NumericFilter.op`: `> | < | =`
- 当 `enabled=false` 时忽略该筛选条件
- `maxPublishedHours` 可空；即使为空也必须只返回 24 小时内爆文

#### 返回 `data`

```json
{
  "list": [
    {
      "id": "a-1001",
      "title": "文章标题",
      "cover": "https://...",
      "likes": "1.2w",
      "comments": "860",
      "views": "3.5w",
      "time": "8小时前发布",
      "link": "https://mp.weixin.qq.com/s/xxx",
      "followers": 6400,
      "viewCount": 28600,
      "likeCount": 1320,
      "commentCount": 190,
      "publishedHoursAgo": 8,
      "sourceHtml": "<p>...</p>"
    }
  ],
  "total": 50,
  "pageNo": 1,
  "pageSize": 6,
  "hasMore": true
}
```

说明：前端当前同时使用格式化字段（`likes/comments/views/time`）和原始数值字段（`followers/viewCount/likeCount/commentCount/publishedHoursAgo`），需完整返回。

### 4.2 创建改写任务
`POST /rewrite/start`

#### 请求体

```json
{
  "url": "https://mp.weixin.qq.com/s/xxx",
  "articleId": "a-1001"
}
```

- `url` 必填
- `articleId` 选填（从列表页进入改写时会带，手动输入链接时可能不带）

#### 返回 `data`

```json
{
  "taskId": "task-20260302120000-abc123"
}
```

#### 行为要求
- 创建任务后立即返回 `taskId`（异步执行改写）
- 同一 `url` 可以重复创建新任务（前端支持“重新改写”）

### 4.3 查询改写状态
`GET /rewrite/status?taskId=...`

#### 返回 `data`

```json
{
  "taskId": "task-xxx",
  "status": "processing",
  "progress": 45,
  "statusText": "AI 正在深度改写中...",
  "timeRemaining": 5,
  "sourceHtml": "<p>原文...</p>",
  "result": {
    "rewrittenBodyHtml": "<p>改写后...</p><img src='...'>",
    "originalTitle": "原标题",
    "suggestedTitles": ["推荐1", "推荐2", "推荐3"]
  }
}
```

#### 字段约束
- `status`: 当前前端仅定义 `processing | completed`
- `progress`: `0-100`，建议单调递增
- `sourceHtml`: 必返（用于原文预览）
- `result`: 仅 `completed` 时返回；`processing` 时可省略

#### 轮询特性
- 前端每 1.5 秒轮询一次，直到 `status=completed`
- 若接口报错，前端会停止轮询并提示失败

## 5. 业务规则（重点）
- 爆文列表口径：仅 24 小时内数据
- 前端发布时间筛选目前是 `1~23小时`；后端建议支持 `<=24` 的通用能力
- 改写结果需返回 HTML（正文可含 `<img>`），因为前端支持图文预览和复制图片链接
- `taskId` 不存在时返回明确错误（如 `4004 + 改写任务不存在`）

## 6. 后端实现建议
- 模块划分：
  - `article`：搜索、筛选、排序、分页
  - `rewrite`：任务创建、状态查询、结果存储
- 任务机制：
  - 建议队列（Redis Stream / RabbitMQ / 内存队列）+ worker 执行 AI 改写
  - 任务表保存 `status/progress/result/error`
- 缓存建议：
  - 爆文搜索可按条件短缓存（30~120 秒）
- 可观测性：
  - 记录 `traceId`、任务耗时、AI 调用耗时、失败原因

## 7. 验收标准（联调清单）
- `POST /articles/search` 可正常分页，`hasMore` 正确
- 不管是否传 `maxPublishedHours`，都不会返回超过 24h 数据
- `POST /rewrite/start` 返回 `taskId`，1 秒内响应
- `GET /rewrite/status` 处理中返回 `processing`，完成后返回 `completed + result`
- 所有接口统一响应结构 `{code,message,data}`
- 错误时 `message` 可直接给用户展示（中文可读）

## 8. 最小联调闭环
1. 调用 `POST /articles/search` 获取文章列表
2. 选择一篇调用 `POST /rewrite/start` 获取 `taskId`
3. 轮询 `GET /rewrite/status` 直到 `completed`
4. 前端展示原文、改写正文、推荐标题并支持复制全文

