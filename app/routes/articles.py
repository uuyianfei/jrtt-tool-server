from datetime import datetime

from flask import Blueprint, request

from ..models import Article
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


@articles_bp.post("/articles/search")
def search_articles():
    body = request.get_json(silent=True) or {}

    page_no = int(body.get("pageNo", 1))
    page_size = int(body.get("pageSize", 6))
    sort_field = body.get("sortField", "time")
    sort_order = body.get("sortOrder", "desc")
    max_hours = body.get("maxPublishedHours")

    if page_no < 1 or page_size < 1:
        return error_response(4001, "分页参数无效")
    if sort_field not in {"followers", "views", "time"}:
        return error_response(4001, "sortField 仅支持 followers|views|time")
    if sort_order not in {"asc", "desc"}:
        return error_response(4001, "sortOrder 仅支持 asc|desc")

    q = Article.query.filter(Article.published_hours_ago <= 24)
    if max_hours is not None:
        try:
            max_hours = float(max_hours)
            q = q.filter(Article.published_hours_ago <= max_hours)
        except ValueError:
            return error_response(4001, "maxPublishedHours 参数无效")

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

    total = q.count()
    rows = q.offset((page_no - 1) * page_size).limit(page_size).all()
    now = datetime.utcnow()

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
                "time": f"{int(hours_ago)}小时前发布",
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
