import html
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from flask import current_app
from sqlalchemy import or_

from .crawler import ToutiaoCrawler, normalize_article_url
from .extensions import db
from .models import Article, RewriteTask
from .time_utils import cn_now_naive
from .utils import sha256_hex


logger = logging.getLogger(__name__)


def new_task_id() -> str:
    stamp = cn_now_naive().strftime("%Y%m%d%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"task-{stamp}-{suffix}"


def start_rewrite_task(task_id: str):
    app = current_app._get_current_object()
    worker = threading.Thread(target=_rewrite_worker, args=(app, task_id), daemon=True)
    worker.start()


def _rewrite_worker(app, task_id: str):
    with app.app_context():
        task = RewriteTask.query.filter_by(task_id=task_id).first()
        if not task:
            return
        try:
            fetch_timeout = max(10, int(current_app.config.get("REWRITE_FETCH_TIMEOUT_SECONDS", 45)))
            ai_timeout = max(30, int(current_app.config.get("REWRITE_AI_TIMEOUT_SECONDS", 180)))
            _update_task(task, 10, "正在获取原文内容...", 7)
            source_html, source_title, source_text = _run_with_timeout(
                _fetch_source,
                timeout_seconds=fetch_timeout,
                stage="fetch_source",
                url=task.url,
            )
            task.source_html = source_html
            db.session.commit()

            _update_task(task, 40, "AI 正在分析结构...", 5)
            time.sleep(1)
            _update_task(task, 70, "AI 正在深度改写中...", 3)

            rewritten_html, suggested_titles = _run_with_timeout(
                _rewrite_text,
                timeout_seconds=ai_timeout,
                stage="rewrite_ai",
                original_title=source_title,
                source_text=source_text,
                source_html=source_html,
            )
            rewritten_html, suggested_titles = _post_process_rewrite_output(
                rewritten_html, suggested_titles, source_title
            )
            if not _is_meaningful_rewrite_html(rewritten_html):
                logger.warning("rewrite html too short, fallback to source text task_id=%s", task_id)
                rewritten_html = _build_rewrite_fallback_html(source_html, source_text)
            rewritten_html = _inject_source_images(rewritten_html, source_html)
            time.sleep(1)
            _update_task(task, 95, "正在整理结果...", 1)

            task.original_title = source_title
            task.rewritten_body_html = rewritten_html
            task.suggested_titles = suggested_titles
            task.status = "completed"
            task.progress = 100
            task.status_text = "改写完成"
            task.time_remaining = 0
            task.completed_at = cn_now_naive()
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            latest = RewriteTask.query.filter_by(task_id=task_id).first()
            if latest:
                latest.error_message = str(exc)[:500]
                latest.status_text = "改写失败"
                latest.time_remaining = 0
                latest.progress = max(int(latest.progress or 0), 1)
                db.session.commit()
            logger.warning("rewrite task failed task_id=%s err=%s", task_id, exc)


def _update_task(task: RewriteTask, progress: int, text: str, remain: int):
    task.progress = progress
    task.status_text = text
    task.time_remaining = remain
    db.session.commit()


def _fetch_source(url: str):
    raw_url = _normalize_toutiao_input_url(url)
    resolved_url = _resolve_toutiao_short_url(raw_url)
    normalized_url = normalize_article_url(resolved_url or raw_url)
    conditions = [Article.url == raw_url]
    if resolved_url and resolved_url != raw_url:
        conditions.append(Article.url == resolved_url)
    if normalized_url and normalized_url != raw_url:
        conditions.append(Article.url == normalized_url)
    if normalized_url:
        conditions.append(Article.url_hash == sha256_hex(normalized_url))
    article = Article.query.filter(or_(*conditions)).order_by(Article.updated_at.desc()).first()
    if article and article.source_html:
        title = article.title or "原标题"
        html = _sanitize_image_inline_styles(article.source_html)
        text = _html_to_text(html)
        _ensure_valid_source_content(normalized_url or raw_url, html, text)
        return html, title, text

    # 未命中数据库时，改为使用 Selenium 爬虫抓取原文（与爬虫 job 同源）
    # 先做 URL 兜底校验，避免无效链接触发浏览器抓取卡顿
    if not _is_supported_toutiao_url(normalized_url):
        raise ValueError("仅支持头条文章链接，请检查链接是否完整可访问")

    # rewrite 走 API 服务，不依赖 Xvfb，固定使用 headless 以避免显示环境导致阻塞
    crawler = ToutiaoCrawler(headless=True)
    try:
        details = crawler._get_article_details(normalized_url)
    finally:
        crawler.close()

    html = _sanitize_image_inline_styles(details.get("article_html") or "")
    title = (details.get("title") or "").strip()
    if not title and html:
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else ""
    if not title:
        title = "原标题"

    final_url = normalize_article_url(details.get("final_url") or "")
    text = _html_to_text(html)
    _ensure_valid_source_content(final_url or normalized_url, html, text)
    return html, title, text


def _run_with_timeout(func, timeout_seconds: int, stage: str, **kwargs):
    app = current_app._get_current_object()

    def _run_in_app_context():
        with app.app_context():
            return func(**kwargs)

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_run_in_app_context)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(f"{stage} timeout after {timeout_seconds}s")
    finally:
        # 不等待超时线程自然结束，避免阻塞当前改写任务状态回写
        executor.shutdown(wait=False, cancel_futures=True)


def _is_supported_toutiao_url(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return False
    try:
        parts = urlsplit(text)
        host = (parts.netloc or "").lower()
        path = parts.path or ""
        if parts.scheme not in {"http", "https"}:
            return False
        # 头条常见文章/短链入口，含移动端短链和 v 短链
        if re.search(r"^/(?:article/\d+/?|i\d+/?|is/[A-Za-z0-9_-]+/?)", path):
            return host in {"www.toutiao.com", "m.toutiao.com", "v.toutiao.com", "toutiao.com"}
        # 兜底：只要是 toutiao 域名下的内容路径，也放行给后续抓取校验
        if host.endswith("toutiao.com") and path and path != "/":
            return True
    except Exception:
        return False
    return False


def _resolve_toutiao_short_url(url: str) -> str:
    """
    解析 m.toutiao.com/is/... 短链，拿到最终 article 链接。
    解析失败时返回原始 URL，不阻断后续 Selenium 跳转识别。
    """
    raw = _normalize_toutiao_input_url(url)
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
        host = (parts.netloc or "").lower()
        is_mobile_short = (
            parts.scheme in {"http", "https"}
            and host in {"m.toutiao.com", "v.toutiao.com"}
            and (parts.path or "").startswith("/is/")
        )
        if not is_mobile_short:
            return raw
        resp = requests.get(
            raw,
            allow_redirects=True,
            timeout=12,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13; Mobile) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
        )
        final_url = (resp.url or "").strip()
        if final_url:
            return final_url
    except Exception as exc:
        logger.info("resolve toutiao short url failed url=%s err=%s", raw, exc)
    return raw


def _normalize_toutiao_input_url(url: str) -> str:
    text = (url or "").strip().strip("'\"")
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    if re.match(r"^[A-Za-z0-9.-]+/is/[A-Za-z0-9_-]+/?$", text):
        # 兼容用户粘贴 m.toutiao.com/is/xxxxx（无协议）
        return f"https://{text}"
    if re.match(r"^(?:m|www|v)\.toutiao\.com/", text):
        return f"https://{text}"
    return text


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    texts = [p.get_text(strip=True) for p in soup.find_all(["p", "h1", "h2", "h3"]) if p.get_text(strip=True)]
    if texts:
        return "\n".join(texts)
    return soup.get_text("\n", strip=True)


def _ensure_valid_source_content(url: str, html: str, text: str):
    """
    严格校验原文是否有效，避免“无正文链接”被 AI 凭空改写。
    """
    normalized_url = (url or "").strip()
    # 头条文章链接通常包含 /article/{id}/
    looks_like_article_url = bool(re.search(r"/article/\d+/?", normalized_url))
    has_article_container = bool(
        re.search(r'class=["\'][^"\']*(syl-article-base|tt-article-content)[^"\']*["\']', html or "")
        or re.search(r"<article\b", html or "", flags=re.IGNORECASE)
    )
    text_len = len((text or "").strip())

    # 非文章链接或正文过短都判为无效，阻止进入改写
    if (not looks_like_article_url) or (not has_article_container) or text_len < 120:
        raise ValueError("未能抓取到有效原文内容，请检查文章链接是否正确")


def _count_source_paragraphs(source_html: str, source_text: str) -> int:
    soup = BeautifulSoup(source_html or "", "html.parser")
    blocks = [
        node.get_text(" ", strip=True)
        for node in soup.find_all(["p", "h1", "h2", "h3", "h4", "li"])
        if node.get_text(" ", strip=True)
    ]
    if blocks:
        return len(blocks)
    text_lines = [line.strip() for line in (source_text or "").splitlines() if line.strip()]
    return len(text_lines)


def _rewrite_text(original_title: str, source_text: str, source_html: str):
    source_text = source_text[:3000]
    api_key = current_app.config["DEEPSEEK_API_KEY"]
    if api_key:
        image_guidance = _build_image_guidance(source_html)
        paragraph_count = _count_source_paragraphs(source_html, source_text)
        image_count = len(_extract_source_image_points(BeautifulSoup(source_html or "", "html.parser"))[0])
        if paragraph_count < 7:
            paragraph_rule = (
                f"原文约 {paragraph_count} 段，改写后必须扩写到至少 7 段（建议 7-9 段），"
                "保证层次更清晰。"
            )
        else:
            paragraph_rule = (
                f"原文约 {paragraph_count} 段，改写后段落数必须接近原文，允许误差 ±2 段。"
            )
        prompt = (
            "你将收到一篇中文原文，请把它改写成高可读、强传播、强原创的版本。\n"
            "严格只输出 JSON（不要 markdown 代码块）："
            '{"rewrittenBodyHtml":"<p>...</p>","suggestedTitles":["标题1","标题2","标题3"]}\n'
            "要求：\n"
            "1) rewrittenBodyHtml 只放正文，不要包含“标题建议”等说明文字。\n"
            "2) 原创度必须非常高：通过乱序、插叙、倒叙、换人称、同义替换、句式重组、补充细节等方式重写，"
            "与原文相似度必须低于 10%。\n"
            "3) 语言必须口语化、接地气，去掉 AI 腔；避免“与此同时、此外、综上所述、然而”等模板连接词。\n"
            f"4) {paragraph_rule}\n"
            f"5) 原文图片总数为 {image_count} 张。改写结果必须保留全部图片，数量必须完全一致，不允许减少。\n"
            "6) 必须在 rewrittenBodyHtml 中使用 <img src=\"...\"> 插图，且 src 必须完全使用给定原图 URL，"
            "不要改写、不要替换、不要省略。\n"
            "7) 图片应尽量按原文位置对应插入到改写正文。\n"
            "8) suggestedTitles 提供 3 个中文标题，单个标题建议控制在 24-30 个中文字符，风格有吸引力但不过度夸张。\n"
            "9) 改写内容必须完整通顺、无明显语病和错别字。\n"
            f"{image_guidance}\n"
            f"原标题：{original_title}\n"
            f"原文：{source_text}"
        )
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": current_app.config["DEEPSEEK_MODEL"],
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是一个干了十年的爆文编辑，擅长把普通文章改成高传播、高互动的内容。"
                        "你写作风格口语化、短句、有人味，能明显降低与原文相似度。"
                        "你必须严格遵守用户给出的结构约束，并且只输出合法 JSON。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.8,
            "max_tokens": 1800,
        }
        resp = requests.post(
            current_app.config["DEEPSEEK_API_URL"],
            headers=headers,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        rewritten_html, titles = _parse_ai_result(content, original_title)
        return rewritten_html, titles

    # 无 AI Key 时给出可联调的兜底结果
    lines = [line.strip() for line in source_text.splitlines() if line.strip()][:8]
    if not lines:
        lines = ["原文内容为空，未能提取到正文。"]
    rewritten_html = "".join([f"<p>{line}</p>" for line in lines])
    titles = [
        f"{original_title}：换个角度再看",
        f"{original_title}：这几点最值得关注",
        f"{original_title}：一个更好理解的版本",
    ]
    return rewritten_html, titles


def _parse_ai_result(content: str, original_title: str):
    # 1) 优先按 JSON 解析
    for candidate in [content, _extract_json_block(content)]:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                html = str(data.get("rewrittenBodyHtml") or "").strip()
                titles = data.get("suggestedTitles") or []
                if not html:
                    continue
                return _normalize_html(html), _normalize_titles(titles, original_title)
        except Exception:
            pass

    # 1.5) 兼容“半 JSON / 转义 JSON”文本
    recovered_html = _extract_rewritten_body_from_text(content)
    recovered_titles = _extract_suggested_titles_from_text(content)
    if recovered_html:
        return _normalize_html(recovered_html), _normalize_titles(recovered_titles, original_title)

    # 2) 回退：按文本拆分
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    titles = _extract_titles_from_text(lines)
    body_lines = [ln for ln in lines if not _is_title_line(ln)]
    body_lines = [ln for ln in body_lines if not _looks_like_json_fragment(ln)]
    html = "".join([f"<p>{ln}</p>" for ln in body_lines[:30]]) or "<p>暂无改写内容</p>"
    return _normalize_html(html), _normalize_titles(titles, original_title)


def _extract_json_block(text: str):
    m = re.search(r"\{[\s\S]*\}", text)
    return m.group(0) if m else ""


def _extract_titles_from_text(lines):
    titles = []
    for ln in lines:
        if _looks_like_json_fragment(ln):
            continue
        cleaned = re.sub(r"^[\-\d\.\)、\s]+", "", ln).strip()
        if len(cleaned) < 6:
            continue
        if any(k in cleaned for k in ["标题建议", "推荐标题", "备选标题"]):
            continue
        if cleaned not in titles:
            titles.append(cleaned)
        if len(titles) >= 3:
            break
    return titles


def _is_title_line(line: str) -> bool:
    return bool(re.search(r"(标题建议|推荐标题|备选标题|^第?\d+[\.、\)]?)", line))


def _normalize_titles(titles, original_title: str):
    normalized = []
    for t in (titles or []):
        text = str(t).strip()
        text = re.sub(r"^[\-\d\.\)、\s]+", "", text)
        if _looks_like_json_fragment(text):
            continue
        if text and text not in normalized:
            normalized.append(text)
        if len(normalized) >= 3:
            break
    if len(normalized) < 3:
        fallback = [
            f"{original_title}：换个角度看真相",
            f"{original_title}：3个最值得关注的点",
            f"{original_title}：事情背后没那么简单",
        ]
        for t in fallback:
            if t not in normalized:
                normalized.append(t)
            if len(normalized) >= 3:
                break
    return normalized[:3]


def _normalize_html(html: str):
    text = (html or "").strip()
    recovered = _extract_rewritten_body_from_text(text)
    if recovered:
        text = recovered.strip()
    if "<p" in text or "<div" in text or "<article" in text:
        return text
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "".join([f"<p>{ln}</p>" for ln in lines]) or "<p>暂无改写内容</p>"


def _extract_rewritten_body_from_text(text: str) -> str:
    if not text:
        return ""
    # 先尝试键值对解析，兼容 value 是转义字符串
    m = re.search(r'"rewrittenBodyHtml"\s*:\s*"((?:\\.|[^"\\])*)"', text, flags=re.DOTALL)
    if m:
        raw = m.group(1)
        try:
            return json.loads(f'"{raw}"')
        except Exception:
            return (
                raw.replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace("\\/", "/")
            )
    # 再尝试非引号值（少见）
    m = re.search(r'"rewrittenBodyHtml"\s*:\s*(\{[\s\S]*\}|<[\s\S]*>)', text, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _extract_suggested_titles_from_text(text: str):
    if not text:
        return []
    m = re.search(r'"suggestedTitles"\s*:\s*(\[[\s\S]*?\])', text, flags=re.DOTALL)
    if m:
        arr_text = m.group(1)
        try:
            data = json.loads(arr_text)
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:
            pass
    return []


def _looks_like_json_fragment(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if t in {"{", "}", "[", "]", "```", "json"}:
        return True
    if any(k in t for k in ['"rewrittenBodyHtml"', '"suggestedTitles"', '{"', '}', '":']):
        return True
    if "<p>" in t and len(t) > 80:
        return True
    return False


def _is_meaningful_rewrite_html(rewritten_html: str) -> bool:
    if not rewritten_html:
        return False
    soup = BeautifulSoup(rewritten_html, "html.parser")
    text = soup.get_text(" ", strip=True)
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 60:
        return False
    # 排除几乎只有符号的异常内容（例如单个 "{"）
    if re.fullmatch(r"[\{\}\[\]`'\".,:;!?，。！？：；、\-_=+()（）<>/\\\s]+", text or ""):
        return False
    return True


def _build_rewrite_fallback_html(source_html: str, source_text: str) -> str:
    """
    当 AI 返回异常（正文过短/仅符号）时，使用原文文本兜底，避免只剩图片。
    """
    lines = []
    soup = BeautifulSoup(source_html or "", "html.parser")
    for node in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 2:
            continue
        lines.append(text)
        if len(lines) >= 30:
            break
    if not lines:
        lines = [line.strip() for line in (source_text or "").splitlines() if line.strip()][:20]
    if not lines:
        lines = ["原文提取成功，但改写结果异常，已启用兜底文本。"]
    return "".join([f"<p>{html.escape(line)}</p>" for line in lines])


def _build_image_guidance(source_html: str) -> str:
    source = BeautifulSoup(source_html or "", "html.parser")
    img_points, _ = _extract_source_image_points(source)
    if not img_points:
        return "原文无图片，可不插图。"
    total = len(img_points)
    lines = [
        f"原文图片清单（共 {total} 张）：",
        "以下所有图片必须全部出现在改写结果中，数量必须与原文完全一致，不允许减少。",
        "请按出现顺序和相对段落位置进行插入：",
    ]
    for idx, (para_idx, src) in enumerate(img_points, start=1):
        lines.append(f"{idx}. 段落索引={para_idx}, src={src}")
        if idx >= 10:
            break
    if total > 10:
        lines.append(f"...（还有 {total - 10} 张图片，同样必须保留）")
    return "\n".join(lines)


def _post_process_rewrite_output(rewritten_html: str, suggested_titles, original_title: str):
    """
    强制修正 AI 结果：
    1) 从正文中剥离“标题建议”区块
    2) 纠正双层 <p><p> 结构
    3) 优先使用正文中抽到的真实标题，替换占位“改写版”标题
    """
    html = rewritten_html or ""

    titles_from_html = []
    li_titles = re.findall(r"<li[^>]*>(.*?)</li>", html, flags=re.IGNORECASE | re.DOTALL)
    for raw in li_titles:
        text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        text = re.sub(r"^[\-\d\.\)、\s]+", "", text).strip()
        if len(text) >= 6 and text not in titles_from_html:
            titles_from_html.append(text)
        if len(titles_from_html) >= 3:
            break

    # 去掉“标题建议”及其后面的内容（正文只保留改写文章）
    marker_match = re.search(r"(标题建议|推荐标题|备选标题)", html)
    if marker_match:
        html = html[: marker_match.start()]

    # 处理嵌套段落：<p><p>xxx</p></p> -> <p>xxx</p>
    html = re.sub(r"<p>\s*<p>", "<p>", html, flags=re.IGNORECASE)
    html = re.sub(r"</p>\s*</p>", "</p>", html, flags=re.IGNORECASE)
    html = _normalize_html(html)

    clean_titles = _normalize_titles(suggested_titles, original_title)
    looks_placeholder = all(("改写版" in t) for t in clean_titles)
    if titles_from_html and (looks_placeholder or not clean_titles):
        clean_titles = _normalize_titles(titles_from_html, original_title)
    return html, clean_titles


def _inject_source_images(rewritten_html: str, source_html: str):
    if not source_html:
        return rewritten_html
    rewritten = BeautifulSoup(rewritten_html or "", "html.parser")
    source = BeautifulSoup(source_html, "html.parser")

    img_points, source_para_total = _extract_source_image_points(source)
    if not img_points:
        return str(rewritten)
    source_img_srcs = [_normalize_image_src(src) for _, src in img_points]
    source_img_srcs = [s for s in source_img_srcs if s]
    if not source_img_srcs:
        return _sanitize_image_inline_styles(str(rewritten))

    rewritten_img_srcs = []
    for img in rewritten.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("original-src")
            or ""
        )
        normalized = _normalize_image_src(src)
        if normalized:
            rewritten_img_srcs.append(normalized)
    rewritten_set = set(rewritten_img_srcs)

    # 只补齐“缺失图片”，避免 AI 已保留图片被重复插入
    missing_points = []
    for source_para_index, src in img_points:
        normalized_src = _normalize_image_src(src)
        if not normalized_src:
            continue
        if normalized_src not in rewritten_set:
            missing_points.append((source_para_index, normalized_src))
            rewritten_set.add(normalized_src)
    if not missing_points:
        return _sanitize_image_inline_styles(str(rewritten))

    rewritten_paras = rewritten.find_all("p")
    rewritten_para_total = len(rewritten_paras)

    if rewritten_para_total == 0:
        for _, src in missing_points:
            p = rewritten.new_tag("p")
            p.append(rewritten.new_tag("img", src=src))
            rewritten.append(p)
        return _sanitize_image_inline_styles(str(rewritten))

    inserted_after_index = -1
    for source_para_index, src in missing_points:
        if source_para_total <= 0:
            target_index = rewritten_para_total - 1
        else:
            # 按原文段落相对位置映射到改写段落位置
            ratio = min(1.0, max(0.0, source_para_index / float(source_para_total)))
            target_index = int(round(ratio * (rewritten_para_total - 1)))
        target_index = max(target_index, inserted_after_index)
        target_index = min(target_index, rewritten_para_total - 1)

        target_p = rewritten_paras[target_index]
        img_p = rewritten.new_tag("p")
        img_p.append(rewritten.new_tag("img", src=src))
        target_p.insert_after(img_p)
        inserted_after_index = target_index
    return _sanitize_image_inline_styles(str(rewritten))


def _normalize_image_src(src: str) -> str:
    text = html.unescape((src or "").strip())
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    return text


def _sanitize_image_inline_styles(html: str) -> str:
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        if img.has_attr("style"):
            del img["style"]
    return str(soup)


def _extract_source_image_points(source_soup: BeautifulSoup):
    """
    返回 [(source_para_index, img_src), ...]。
    source_para_index 表示该图片在原文第几个段落之后出现（相对位置映射用）。
    """
    container = source_soup.select_one("article") or source_soup.body or source_soup
    para_idx = 0
    img_points = []
    seen = set()

    for node in container.descendants:
        if getattr(node, "name", None) in {"p", "h1", "h2", "h3", "h4"}:
            text = node.get_text(strip=True)
            if text:
                para_idx += 1
        if getattr(node, "name", None) == "img":
            src = (
                node.get("src")
                or node.get("data-src")
                or node.get("data-original")
                or node.get("original-src")
                or ""
            ).strip()
            if not src:
                continue
            if src.startswith("//"):
                src = f"https:{src}"
            if src in seen:
                continue
            seen.add(src)
            img_points.append((para_idx, src))

    return img_points, max(para_idx, 1)
