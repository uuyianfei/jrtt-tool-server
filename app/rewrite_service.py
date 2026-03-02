import threading
import time
import uuid
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import current_app

from .extensions import db
from .models import Article, RewriteTask


def new_task_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
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

            rewritten_html, suggested_titles = _rewrite_text(source_title, source_text)
            time.sleep(1)
            _update_task(task, 95, "正在整理结果...", 1)

            task.original_title = source_title
            task.rewritten_body_html = rewritten_html
            task.suggested_titles = suggested_titles
            task.status = "completed"
            task.progress = 100
            task.status_text = "改写完成"
            task.time_remaining = 0
            task.completed_at = datetime.utcnow()
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
        html = article.source_html
        return html, title, _html_to_text(html)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, timeout=20, headers=headers)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.select_one("article") or soup.select_one(".syl-article-base") or soup.body
    html = str(container) if container else ""
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else "原标题"
    return html, title, _html_to_text(html)


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    texts = [p.get_text(strip=True) for p in soup.find_all(["p", "h1", "h2", "h3"]) if p.get_text(strip=True)]
    if texts:
        return "\n".join(texts)
    return soup.get_text("\n", strip=True)


def _rewrite_text(original_title: str, source_text: str):
    source_text = source_text[:3000]
    api_key = current_app.config["DEEPSEEK_API_KEY"]
    if api_key:
        prompt = (
            "请把下面文章改写成口语化、可读性强的中文内容，输出 HTML 段落格式。"
            "另外返回 3 个标题建议。\n"
            f"原标题：{original_title}\n"
            f"原文：{source_text}"
        )
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": current_app.config["DEEPSEEK_MODEL"],
            "messages": [
                {"role": "system", "content": "你是资深新媒体编辑。"},
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
        rewritten_html = "".join(
            [f"<p>{line.strip()}</p>" for line in content.splitlines() if line.strip()][:20]
        )
        titles = [f"{original_title}-改写版1", f"{original_title}-改写版2", f"{original_title}-改写版3"]
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
