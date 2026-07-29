"""
SyncedAction — replay protection for the offline sync endpoint
==============================================================

``POST /sync`` accepts a batch of actions a worker queued while offline. Each
carries a ``client_id`` documented as "Client-generated unique ID to prevent
duplicate processing" — but nothing ever checked it against anything, so
replaying a batch (the normal consequence of a response lost on a flaky
connection, which is exactly the situation this endpoint exists for) reapplied
every action in it.

This table is the thing the ``client_id`` is checked against. One row per
(user, client_id) actually applied; the unique constraint makes the check
race-safe rather than a SELECT the second request can slip past.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class SyncedAction(Base):
    """Record that one offline action has already been applied."""

    __tablename__ = "synced_actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Opaque to us — whatever the client generated. Scoped by user so two
    # clients cannot collide with each other.
    client_id = Column(String(128), nullable=False)
    action = Column(String(50), nullable=False)
    # UUID of whatever the action created, when it created something.
    #
    # `create_issue` needs this. A client that files a report offline gets back
    # the new issue's id so it can upload the photos it could not send at the
    # time — but if the response is lost and the batch is replayed, the dedup
    # path returns `duplicate` and would otherwise have no id to hand back,
    # leaving the photos permanently orphaned. Storing it here means a replay
    # returns the same id as the original call.
    resource_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "client_id", name="uq_synced_action"),
    )

    def __repr__(self):
        return f"<SyncedAction {self.user_id} {self.client_id} {self.action}>"
