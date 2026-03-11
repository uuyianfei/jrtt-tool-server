# 新闻爬虫 Bug 分析报告

## Bug 1：部分新闻 `source_html` 为空

### 根因分析

问题出在 [crawler.py](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py) 的两处逻辑不一致：

1. **[_get_article_details](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#519-630)** 方法内部（L598-617）用 [_is_meaningful_article_html()](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#448-453) 判断正文是否有效（至少 80 字符纯文本），在判断无效时会进行重试
2. 但 **[upsert_articles](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#847-1059)** 方法（L996）入库前的检查只用了 `if not article_html`（仅判断字符串是否为空）

> [!IMPORTANT]
> 这意味着当页面加载缓慢时，容器元素（如 `<article class="syl-article-base"><div></div></article>`）虽然已经存在于 DOM 中，但 JavaScript 还没有把正文内容渲染进去。此时：
> - [_extract_article_container()](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#424-447) 会返回这个**空壳 HTML 容器**（字符串非空）
> - `if not article_html` 判为 False → 通过检查
> - 结果 → 空壳 HTML 被存入 `source_html` 字段

此外，[_get_article_details](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#519-630) 方法在重试后，即使内容仍然不够有意义，也不会把 [article_html](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#448-453) 清空，导致"半成品"HTML 被原样返回。

### 修复方案
1. [upsert_articles](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#847-1059) 中将 `if not article_html` 改为基于文本内容长度的有意义性检查
2. [_get_article_details](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#519-630) 中，在所有重试结束后如果正文仍不满足最低标准，将 [article_html](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#448-453) 清空为 `""`

---

## Bug 2：标题出现 "评论8"、"评论1" 等异常数据

### 根因分析

问题出在 [crawl_author_recent_articles](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#L788-L844)：

```python
links = soup.find_all("a", href=True)  # 太宽泛！获取了页面上所有链接
for link in links:
    href = (link.get("href") or "").strip()
    if "/article/" not in href:
        continue
    title = (link.get_text(strip=True) or link.get("title") or "").strip()
```

> [!WARNING]
> 在头条作者主页，**评论按钮**也是 `<a>` 标签，href 中同样包含 `/article/xxxxx/`，文本内容就是 "评论8" 这样的格式。由于代码没有过滤非标题链接，这些评论按钮被误识别为文章链接。

然后在 [upsert_articles](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#847-1059)（L1009）中：
```python
article.title = details.get("title") or base.get("title") or "无标题"
```
当文章详情页也加载失败时（`details["title"]` 为空），就会退而使用 `base["title"]="评论8"` 作为标题。

### 修复方案
1. [crawl_author_recent_articles](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#788-845) 中过滤掉匹配 `评论\d+` 等模式的伪标题
2. 对标题添加最低长度检查（标题太短不太可能是真实文章标题）
3. 在 [upsert_articles](file:///d:/Project/WeChat/ideara/jrtt-tool-server/app/crawler.py#847-1059) 中添加标题合法性兜底校验
