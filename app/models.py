from sqlalchemy import JSON

from .extensions import db
from .time_utils import cn_now_naive


class Article(db.Model):
    __tablename__ = "articles"

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    url_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    url = db.Column(db.String(1024), nullable=False)
    title = db.Column(db.String(255), nullable=False, default="")
    cover = db.Column(db.String(1024), nullable=False, default="")
    author = db.Column(db.String(128), nullable=False, default="")
    author_url = db.Column(db.String(1024), nullable=False, default="")

    publish_time_text = db.Column(db.String(64), nullable=False, default="")
    published_at = db.Column(db.DateTime, nullable=True, index=True)
    published_hours_ago = db.Column(db.Float, nullable=False, default=9999)

    followers = db.Column(db.Integer, nullable=False, default=0, index=True)
    view_count = db.Column(db.Integer, nullable=False, default=0, index=True)
    like_count = db.Column(db.Integer, nullable=False, default=0, index=True)
    comment_count = db.Column(db.Integer, nullable=False, default=0, index=True)

    source_html = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=cn_now_naive, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=cn_now_naive,
        onupdate=cn_now_naive,
    )
    last_seen_at = db.Column(db.DateTime, nullable=False, default=cn_now_naive, index=True)


class RewriteTask(db.Model):
    __tablename__ = "rewrite_tasks"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    url = db.Column(db.String(1024), nullable=False)
    article_id = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="processing", index=True)
    progress = db.Column(db.Integer, nullable=False, default=0)
    status_text = db.Column(db.String(255), nullable=False, default="任务创建中...")
    time_remaining = db.Column(db.Integer, nullable=False, default=8)

    source_html = db.Column(db.Text, nullable=False, default="")
    rewritten_body_html = db.Column(db.Text, nullable=True)
    original_title = db.Column(db.String(255), nullable=True)
    suggested_titles = db.Column(JSON, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=cn_now_naive)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=cn_now_naive,
        onupdate=cn_now_naive,
    )
    completed_at = db.Column(db.DateTime, nullable=True)
