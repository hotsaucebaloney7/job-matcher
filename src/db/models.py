"""SQLAlchemy 2.0 ORM schema for the job_postings table (MySQL 8.0+)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import CHAR, DATETIME, JSON, Enum, Float, Integer, String, Text, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base class."""


WORKPLACE_ENUM = Enum("Remote", "Local", "Hybrid", name="workplace_type_enum")
STATUS_ENUM = Enum("new", "reviewed", "applied", "rejected", name="job_status_enum")
DESCRIPTION_TEXT_TYPE = Text().with_variant(LONGTEXT(), "mysql")


class JobPosting(Base):
    """Normalized job posting record with hybrid scoring and review status tracking."""

    __tablename__ = "job_postings"

    job_hash: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    company: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    location_raw: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    workplace_type: Mapped[str] = mapped_column(WORKPLACE_ENUM, nullable=False, index=True)
    salary_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    url: Mapped[str] = mapped_column(String(768), unique=True, nullable=False)
    description_text: Mapped[str] = mapped_column(DESCRIPTION_TEXT_TYPE, nullable=False)
    date_posted: Mapped[Optional[datetime]] = mapped_column(DATETIME, nullable=True)
    date_fetched: Mapped[datetime] = mapped_column(
        DATETIME,
        nullable=False,
        server_default=func.current_timestamp(),
        default=datetime.utcnow,
    )
    date_last_seen: Mapped[datetime] = mapped_column(
        DATETIME,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        default=datetime.utcnow,
    )
    keyword_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    semantic_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)
    matched_keywords: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        STATUS_ENUM,
        nullable=False,
        server_default="new",
        default="new",
        index=True,
    )
    notion_page_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<JobPosting(job_hash={self.job_hash[:8]!r}, company={self.company!r}, "
            f"title={self.title!r}, workplace_type={self.workplace_type!r}, match_score={self.match_score})>"
        )
