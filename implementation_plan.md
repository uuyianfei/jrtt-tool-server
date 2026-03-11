# 多功能改进：爬虫过滤 + Excel导出 + 改文提示词 + 图片保留

用户提出了多个关联需求，涉及爬虫筛选条件、Excel 导出优化、改文提示词增强（原创度 + 图片保留）。

## User Review Required

> [!IMPORTANT]
> 以下几点需要确认：
> 1. **改文提示词修改范围**：用户引用了 [article_change.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/example/article_change.py) 的提示词，但改文功能在服务端也有一套（[rewrite_service.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/rewrite_service.py) 的 [_rewrite_text](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/rewrite_service.py#129-179)）。**两者都需要改吗？** 还是只改 [rewrite_service.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/rewrite_service.py)（服务端）？
> 2. **"参考原来发的改文程序"**：指的是 [article_modify.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/example/article_modify.py) 吗？两个文件的提示词对比差异主要在于 [article_change.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/example/article_change.py) 新增了标题字数限制和文章质量要求。
> 3. **阅读量 ≥ 2000 条件**：这个条件是仅在从作者主页抓文章时过滤，还是也适用于推荐页？

## Proposed Changes

### 1. 爬虫过滤 — 排除0粉丝作者

---

#### [MODIFY] [crawler.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py)

- **[collect_authors_from_recommend](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#1120-1197)**：在入库前增加 `fans <= 0` 的跳过逻辑（目前只有 `fans >= max_fans` 的上限检查）
- **[crawl_from_author_pool](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#1199-1284)** / [acquire_author_leases](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#101-144)：在 lease 查询中追加 `AuthorSource.followers > 0` 过滤
- **[upsert_articles](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#884-1118)**：新增 `fans <= 0` 跳过逻辑

---

### 2. 爬虫过滤 — 作者主页文章需 24h 内阅读量 ≥ 2000

---

#### [MODIFY] [crawler.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py)

在 [upsert_articles](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#884-1118) 中，对来自作者主页的文章（即 [crawl_from_author_pool](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#1199-1284) 调用路径），增加阅读量门槛检查：
- 新增配置项 `AUTHOR_ARTICLE_MIN_VIEWS`（默认 2000）
- 在 enrich 阶段获取到 [read_count](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#689-741) 后，如果 `read_count < min_views`，标记为 `skip_views` 跳过

#### [MODIFY] [config.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/config.py)

新增配置项：
```python
AUTHOR_ARTICLE_MIN_VIEWS = int(os.getenv("AUTHOR_ARTICLE_MIN_VIEWS", "2000"))
```

---

### 3. Excel 导出 — 删除封面和原文HTML列

---

#### [MODIFY] [articles.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/routes/articles.py)

在 [export_articles](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/routes/articles.py#115-176) 函数中：
- 表头删除"封面"和"原文HTML"两列
- 数据行对应删除 `row.cover` 和 `row.source_html`

修改后表头：
```python
["文章ID", "标题", "链接", "作者", "粉丝数", "阅读数", "点赞数", "评论数", "发布时间文本", "发布时间(小时前)"]
```

---

### 4. 改文提示词优化 — 提升原创度 + 保留所有图片

---

#### [MODIFY] [rewrite_service.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/rewrite_service.py)

**[_rewrite_text](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/rewrite_service.py#129-179) 函数 prompt 改造**，参考 [article_change.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/example/article_change.py) 的提示词风格，增强以下方面：

1. **提升原创度**：
   - 引入"乱序、插叙、倒叙、换人称、同义词替换、句式变换"等具体降低相似度的手法指导
   - 明确要求"和原文相似度低于10%"
   - 强调口语化、去 AI 腔

2. **图片数量保留**：
   - 统计原文的段落数和图片数，在 prompt 中明确告知 AI
   - 要求 AI 改写后必须保留**全部**原文图片，按位置对应插入
   - 明确"图片数量必须与原文完全一致，不允许减少"

3. **段落数控制**：
   - 将原文段落数传入 prompt，要求改写结果段落数相近（±2段）
   - 原文段落少于7段时，要求扩写到至少7段

4. **增强 system prompt**：融合 [article_change.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/example/article_change.py) 中的"十年爆文编辑"角色设定

**[_build_image_guidance](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/rewrite_service.py#323-334) 函数优化**：
- 增加图片总数声明
- 明确要求"以下所有图片必须全部出现在改写结果中"

---

## Verification Plan

### 手动验证

1. **爬虫过滤**：启动爬虫后观察日志，确认：
   - 出现 `skip ... reason=zero_fans` 的日志记录
   - 出现 `skip ... reason=low_views` 的日志记录（阅读量 < 2000）

2. **Excel 导出**：通过 API 调用 `POST /articles/export`，下载 Excel 文件，确认：
   - 只有 10 列（无"封面"和"原文HTML"列）

3. **改文效果**：使用改写 API 或前端触发改写任务，对比：
   - 改文结果的原创度（在文皮皮等平台对比）
   - 改文结果是否保留了原文的所有图片
   - 段落数是否与原文接近

### 语法检查

```powershell
.venv\Scripts\python.exe -c "import py_compile; py_compile.compile('app/crawler.py', doraise=True); py_compile.compile('app/rewrite_service.py', doraise=True); py_compile.compile('app/routes/articles.py', doraise=True); py_compile.compile('app/config.py', doraise=True); print('All OK')"
```
