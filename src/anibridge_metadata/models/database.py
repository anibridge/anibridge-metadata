"""SQLAlchemy ORM models for cached metadata."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Text, UniqueConstraint
from sqlalchemy import String as SqlString
from sqlalchemy.orm import Mapped, mapped_column

from anibridge_metadata.core.db import Base


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class MetadataRecord(Base):
    """Cached normalized metadata for a descriptor lookup."""

    __tablename__ = "metadata_records"
    __table_args__ = (UniqueConstraint("descriptor", name="uq_metadata_descriptor"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    descriptor: Mapped[str] = mapped_column(SqlString(255), nullable=False, index=True)
    normalized_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    not_found: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
