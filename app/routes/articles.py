from io import BytesIO

from flask import Blueprint, request
from flask import send_file
from openpyxl import Workbook

from ..models import Article
from ..time_utils import cn_now_naive
from ..utils import error_response, format_compact_number, success_response

articles_bp = Blueprint("articles", __name__)


def _apply_numeric_filter(query, column, cfg):
    if not cfg or not cfg.get("enabled", False):
        return query
    op = cfg.get("op")
    val = cfg.get("value")
    if val is None:
        return query
    if op == ">":
        return query.filter(column > val)
    if op == "<":
        return query.filter(column < val)
    if op == "=":
        return query.filter(column == val)
    return query


def _build_filtered_query(body):
    sort_field = body.get("sortField", "time")
    sort_order = body.get("sortOrder", "desc")
    max_hours = body.get("maxPublishedHours")

    if sort_field not in {"followers", "views", "time"}:
        return None, error_response(4001, "sortField 仅支持 followers|views|time")
    if sort_order not in {"asc", "desc"}:
        return None, error_response(4001, "sortOrder 仅支持 asc|desc")

    q = Article.query.filter(Article.published_hours_ago <= 24)
    if max_hours is not None:
        try:
            max_hours = float(max_hours)
            q = q.filter(Article.published_hours_ago <= max_hours)
        except ValueError:
            return None, error_response(4001, "maxPublishedHours 参数无效")

    q = _apply_numeric_filter(q, Article.followers, body.get("followerFilter"))
    q = _apply_numeric_filter(q, Article.view_count, body.get("viewFilter"))
    q = _apply_numeric_filter(q, Article.like_count, body.get("likeFilter"))
    q = _apply_numeric_filter(q, Article.comment_count, body.get("commentFilter"))

    if sort_field == "followers":
        sort_col = Article.followers
    elif sort_field == "views":
        sort_col = Article.view_count
    else:
        sort_col = Article.published_at
    q = q.order_by(sort_col.asc() if sort_order == "asc" else sort_col.desc())
    return q, None


def _format_publish_time(hours_ago: float) -> str:
    value = max(0.0, float(hours_ago or 0.0))
    if value < 1:
        minutes = max(1, int(round(value * 60)))
        return f"{minutes}分钟前发布"
    return f"{int(value)}小时前发布"


@articles_bp.post("/articles/search")
def search_articles():
    body = request.get_json(silent=True) or {}

    page_no = int(body.get("pageNo", 1))
    page_size = int(body.get("pageSize", 6))

    if page_no < 1 or page_size < 1:
        return error_response(4001, "分页参数无效")
    q, err_resp = _build_filtered_query(body)
    if err_resp:
        return err_resp

    total = q.count()
    rows = q.offset((page_no - 1) * page_size).limit(page_size).all()
    now = cn_now_naive()

    items = []
    for row in rows:
        hours_ago = row.published_hours_ago
        if row.published_at:
            hours_ago = max(0, (now - row.published_at).total_seconds() / 3600)
        item_id = row.article_id or f"a-{row.id}"
        items.append(
            {
                "id": item_id,
                "title": row.title,
                "cover": row.cover,
                "likes": format_compact_number(row.like_count),
                "comments": format_compact_number(row.comment_count),
                "views": format_compact_number(row.view_count),
                "time": _format_publish_time(hours_ago),
                "link": row.url,
                "followers": row.followers,
                "viewCount": row.view_count,
                "likeCount": row.like_count,
                "commentCount": row.comment_count,
                "publishedHoursAgo": round(float(hours_ago), 2),
                "sourceHtml": row.source_html or "",
            }
        )

    data = {
        "list": items,
        "total": total,
        "pageNo": page_no,
        "pageSize": page_size,
        "hasMore": page_no * page_size < total,
    }
    return success_response(data)


@articles_bp.post("/articles/export")
def export_articles():
    body = request.get_json(silent=True) or {}
    q, err_resp = _build_filtered_query(body)
    if err_resp:
        return err_resp

    rows = q.all()
    now = cn_now_naive()

    wb = Workbook()
    ws = wb.active
    ws.title = "articles"
    ws.append(
        [
            "文章ID",
            "标题",
            "链接",
            "作者",
            "粉丝数",
            "阅读数",
            "点赞数",
            "评论数",
            "发布时间文本",
            "发布时间(小时前)",
        ]
    )

    for row in rows:
        hours_ago = row.published_hours_ago
        if row.published_at:
            hours_ago = max(0, (now - row.published_at).total_seconds() / 3600)
        ws.append(
            [
                row.article_id or f"a-{row.id}",
                row.title or "",
                row.url or "",
                row.author or "",
                int(row.followers or 0),
                int(row.view_count or 0),
                int(row.like_count or 0),
                int(row.comment_count or 0),
                row.publish_time_text or "",
                round(float(hours_ago or 0), 2),
            ]
        )

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"articles_export_{now.strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
