import json
import re
import threading
import time
import uuid

import requests
from bs4 import BeautifulSoup
from flask import current_app

from .crawler import ToutiaoCrawler
from .extensions import db
from .models import Article, RewriteTask
from .time_utils import cn_now_naive


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
            _update_task(task, 10, "正在获取原文内容...", 7)
            source_html, source_title, source_text = _fetch_source(task.url)
            task.source_html = source_html
            db.session.commit()

            _update_task(task, 40, "AI 正在分析结构...", 5)
            time.sleep(1)
            _update_task(task, 70, "AI 正在深度改写中...", 3)

            rewritten_html, suggested_titles = _rewrite_text(source_title, source_text, source_html)
            rewritten_html, suggested_titles = _post_process_rewrite_output(
                rewritten_html, suggested_titles, source_title
            )
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
            task.error_message = str(exc)
            db.session.commit()


def _update_task(task: RewriteTask, progress: int, text: str, remain: int):
    task.progress = progress
    task.status_text = text
    task.time_remaining = remain
    db.session.commit()


def _fetch_source(url: str):
    article = Article.query.filter_by(url=url).first()
    if article and article.source_html:
        title = article.title or "原标题"
        html = _sanitize_image_inline_styles(article.source_html)
        text = _html_to_text(html)
        _ensure_valid_source_content(url, html, text)
        return html, title, text

    # 未命中数据库时，改为使用 Selenium 爬虫抓取原文（与爬虫 job 同源）
    crawler = ToutiaoCrawler(headless=current_app.config.get("CRAWL_HEADLESS", True))
    try:
        details = crawler._get_article_details(url)
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

    text = _html_to_text(html)
    _ensure_valid_source_content(url, html, text)
    return html, title, text


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
    if any(k in t for k in ['"rewrittenBodyHtml"', '"suggestedTitles"', '{"', '}', '":']):
        return True
    if "<p>" in t and len(t) > 80:
        return True
    return False


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

    # 优先让 AI 自行插入原图；只有 AI 漏图时才做兜底注入
    if rewritten.find("img"):
        return _sanitize_image_inline_styles(str(rewritten))

    rewritten_paras = rewritten.find_all("p")
    rewritten_para_total = len(rewritten_paras)

    if rewritten_para_total == 0:
        for _, src in img_points:
            p = rewritten.new_tag("p")
            p.append(rewritten.new_tag("img", src=src))
            rewritten.append(p)
        return _sanitize_image_inline_styles(str(rewritten))

    inserted_after_index = -1
    for source_para_index, src in img_points:
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
