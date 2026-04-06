"""
RefreshToken Model — DB-backed JWT refresh token tracking
==========================================================
Stores a SHA-256 hash of each issued refresh token to support per-token revocation:
  - On logout        : the matching record is marked ``is_revoked=True``.
  - On account delete: all records for that user are revoked atomically.
  - On token rotation: old record revoked, new record created.

A token is valid only if its hash exists in this table with ``is_revoked=False``
AND its JWT signature has not expired. Expired and old revoked records are
purged hourly by the auto-escalation background task (records older than 30 days).
"""

import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)  # SHA-256 hex digest
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_revoked = Column(Boolean, server_default=sa.false(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
