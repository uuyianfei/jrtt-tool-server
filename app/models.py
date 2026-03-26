from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import relationship

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
    author_id = db.Column(db.Integer, ForeignKey("author_sources.id"), nullable=True, index=True)

    publish_time_text = db.Column(db.String(64), nullable=False, default="")
    published_at = db.Column(db.DateTime, nullable=True, index=True)
    published_hours_ago = db.Column(db.Float, nullable=False, default=9999)

    view_count = db.Column(db.Integer, nullable=False, default=0, index=True)
    like_count = db.Column(db.Integer, nullable=False, default=0, index=True)
    comment_count = db.Column(db.Integer, nullable=False, default=0, index=True)
    # Backward-compat: 旧版表结构可能仍保留 articles.followers。
    # 当 author_id 未能关联到 author_sources 时，列表接口需要用它兜底展示。
    followers = db.Column(db.Integer, nullable=False, default=0, index=True)
    metrics_status = db.Column(db.String(32), nullable=False, default="pending", index=True)
    metrics_checked_at = db.Column(db.DateTime, nullable=True, index=True)
    metrics_error = db.Column(db.String(512), nullable=False, default="")

    source_html = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=cn_now_naive, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=cn_now_naive,
        onupdate=cn_now_naive,
    )
    last_seen_at = db.Column(db.DateTime, nullable=False, default=cn_now_naive, index=True)
    author_ref = relationship("AuthorSource", foreign_keys=[author_id])


class FastCrawlClaim(db.Model):
    """
    Distributed claim (lease) for fast-crawler gid processing.

    Goal: avoid cross-region feed asymmetry causing "gid is seen but never processed"
    when multiple workers run concurrently.
    """

    __tablename__ = "fast_crawl_claims"

    id = db.Column(db.Integer, primary_key=True)
    gid = db.Column(db.String(64), unique=True, nullable=False, index=True)
    owner = db.Column(db.String(128), nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)

    created_at = db.Column(db.DateTime, nullable=False, default=cn_now_naive, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=cn_now_naive,
        onupdate=cn_now_naive,
    )


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


class AuthorSource(db.Model):
    __tablename__ = "author_sources"

    id = db.Column(db.Integer, primary_key=True)
    # MySQL utf8mb4 indexed varchar length must keep within index byte limit.
    author_url = db.Column(db.String(512), unique=True, nullable=False, index=True)
    author_name = db.Column(db.String(128), nullable=False, default="")
    followers = db.Column(db.Integer, nullable=False, default=0, index=True)
    status = db.Column(db.String(32), nullable=False, default="active", index=True)
    lease_owner = db.Column(db.String(128), nullable=False, default="", index=True)
    lease_until = db.Column(db.DateTime, nullable=True, index=True)
    fail_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(512), nullable=False, default="")

    first_seen_at = db.Column(db.DateTime, nullable=False, default=cn_now_naive, index=True)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=cn_now_naive, index=True)
    last_crawled_at = db.Column(db.DateTime, nullable=True, index=True)

    created_at = db.Column(db.DateTime, nullable=False, default=cn_now_naive, index=True)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=cn_now_naive,
        onupdate=cn_now_naive,
    )
