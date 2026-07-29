"""
Chat Routes
===========
Multilingual civic assistant powered by Gemini AI.

Frontend Integration Notes:
- Supports English, Hindi, and Gujarati — responds in the same language the user writes in.
- Optionally pass ``issue_id`` to give the AI context about a specific complaint.
- The chat is stateless (no conversation history) — each request is independent.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.time import now_utc
from app.core.deps import get_current_user
from app.core.logger import get_logger
from app.database import get_db
from app.models.issue import Issue
from app.models.user import User
from app.services.ai_service import chat_response

logger = get_logger("chat")

router = APIRouter(prefix="/chat", tags=["Chat"])


class ChatRequest(BaseModel):
    """Request body for ``POST /chat``.

    Attributes:
        message:  User's message in any supported language (English, Hindi, Gujarati).
        issue_id: Optional issue UUID to give the AI context about a specific complaint.
                  The AI will know the issue type, status, ward, and resolution notes.
    """

    message: str = Field(..., min_length=1, max_length=2000, description="User's message text")
    issue_id: Optional[UUID] = Field(None, description="Optional issue UUID for context")


class ChatResponse(BaseModel):
    """Response from the civic assistant.

    Attributes:
        reply: AI-generated response in the same language as the user's message.
    """

    reply: str


@router.post("", response_model=ChatResponse)
def civic_chat(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Chat with the multilingual civic assistant.

    The AI responds in the same language the user writes in (English / Hindi / Gujarati).
    Optionally pass ``issue_id`` to ground the conversation around a specific complaint.

    The AI can answer questions about:
    - Issue status, timeline, and next steps
    - How to report issues or track existing ones
    - Civic information and responsibilities
    - Rewards and gamification features
    - Ward, taluka, and district information

    Role-based issue access:
    - **Citizens**: can only reference their own issues.
    - **Workers**: can only reference their assigned issues.
    - **Admins**: can reference any issue.

    Returns:
        ``ChatResponse`` with the AI's reply.

    Raises:
        401: Not authenticated.
        500: AI service unavailable.
    """
    context = f"User Role: {current_user.role}\nUser Ward: {current_user.ward or 'Not specified'}\n"

    if body.issue_id:
        q = db.query(Issue).filter(Issue.id == body.issue_id)
        if current_user.role == "citizen":
            q = q.filter(Issue.reporter_id == current_user.id)
        elif current_user.role == "worker":
            q = q.filter(Issue.assigned_worker_id == current_user.id)
        # admin: no restriction
        issue = q.first()
        if issue:
            time_since_report = (now_utc() - issue.created_at).days
            context += (
                f"\n--- ISSUE BEING DISCUSSED ---\n"
                f"Issue ID: {issue.id}\n"
                f"Type: {issue.issue_type}\n"
                f"Severity: {issue.severity}\n"
                f"Priority: {issue.priority}\n"
                f"Status: {issue.status}\n"
                f"Is Escalated: {'Yes' if issue.is_escalated else 'No'}\n"
                f"Ward: {issue.ward or 'unknown'}\n"
                f"Description: {issue.description}\n"
                f"Days Since Report: {time_since_report}\n"
                f"Reported by: Citizen\n"
                f"Assigned Worker: {'Yes - ' + (issue.assigned_worker.name if issue.assigned_worker else 'Unknown') if issue.assigned_worker_id else 'Not yet assigned'}\n"
                f"Reported at: {issue.created_at.strftime('%d %b %Y, %I:%M %p')}\n"
                f"Resolved at: {issue.resolved_at.strftime('%d %b %Y, %I:%M %p') if issue.resolved_at else 'Not yet resolved'}\n"
                f"Resolution Notes: {issue.resolution_notes or 'None'}\n"
                # before_photos / after_photos — the singular *_photo_url
                # attributes referenced here do not exist on Issue, so every
                # /chat request carrying an issue_id raised AttributeError.
                # This sits outside the try below, so it escaped as a 500.
                f"Has Before Photo: {'Yes' if issue.before_photos else 'No'}\n"
                f"Has After Photo: {'Yes' if issue.after_photos else 'No'}"
            )

    # Build additional dynamic context from database based on user intent
    from app.services.ai_service import build_dynamic_context
    enriched_context = build_dynamic_context(body.message, current_user, db, context)

    try:
        reply = chat_response(body.message, issue_context=enriched_context, db=db)
    except Exception as e:
        logger.error(f"Chat AI error for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI assistant is temporarily unavailable. Please try again.",
        )

    logger.info(f"Chat response generated for user {current_user.id}, issue={body.issue_id}")
    return ChatResponse(reply=reply)
