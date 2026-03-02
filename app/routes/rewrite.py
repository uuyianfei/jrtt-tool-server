from flask import Blueprint, request

from ..models import RewriteTask
from ..rewrite_service import new_task_id, start_rewrite_task
from ..utils import error_response, success_response
from ..extensions import db

rewrite_bp = Blueprint("rewrite", __name__)


@rewrite_bp.post("/rewrite/start")
def rewrite_start():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    article_id = (body.get("articleId") or "").strip() or None
    if not url:
        return error_response(4001, "url 不能为空")

    task_id = new_task_id()
    task = RewriteTask(
        task_id=task_id,
        url=url,
        article_id=article_id,
        status="processing",
        progress=0,
        status_text="任务排队中...",
        time_remaining=8,
    )
    db.session.add(task)
    db.session.commit()
    start_rewrite_task(task_id)
    return success_response({"taskId": task_id})


@rewrite_bp.get("/rewrite/status")
def rewrite_status():
    task_id = (request.args.get("taskId") or "").strip()
    if not task_id:
        return error_response(4001, "taskId 不能为空")
    task = RewriteTask.query.filter_by(task_id=task_id).first()
    if not task:
        return error_response(4004, "改写任务不存在")
    if task.error_message:
        return error_response(5001, f"改写失败：{task.error_message}")

    data = {
        "taskId": task.task_id,
        "status": task.status,
        "progress": task.progress,
        "statusText": task.status_text,
        "timeRemaining": task.time_remaining,
        "sourceHtml": task.source_html or "",
    }
    if task.status == "completed":
        data["result"] = {
            "rewrittenBodyHtml": task.rewritten_body_html or "",
            "originalTitle": task.original_title or "",
            "suggestedTitles": task.suggested_titles or [],
        }
    return success_response(data)
