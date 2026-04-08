"""
HTML Sanitization Module
========================

Provides HTML sanitization utilities to prevent XSS attacks.

MEDIUM PRIORITY BUG FIX #7: HTML Sanitization

This module uses the bleach library to safely sanitize user-generated HTML content.
It removes potentially dangerous tags and attributes while preserving formatting.
"""

import bleach
from typing import Optional


# Safe tags and attributes for user-generated content
ALLOWED_TAGS = [
    "p", "br", "strong", "em", "u", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "code", "pre", "a",
    "b", "i", "strike", "span", "div"
]

ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "span": ["class"],
    "div": ["class"],
    "code": ["class"],
}

# Strip dangerous tags instead of escaping
STRIP_COMMENTS = True


def sanitize_html(content: Optional[str], max_length: int = 10000) -> Optional[str]:
    """
    Sanitize HTML content to prevent XSS attacks.
    
    Removes potentially dangerous tags and attributes, keeping only safe formatting.
    
    Args:
        content: Raw HTML/text content from user
        max_length: Maximum character length allowed (default 10KB)
        
    Returns:
        Sanitized HTML content, or None if input is None
        
    Raises:
        ValueError: If content exceeds max_length
        
    Examples:
        >>> sanitize_html("<p>Hello <script>alert('xss')</script></p>")
        '<p>Hello </p>'
        
        >>> sanitize_html("<p>Safe <strong>content</strong></p>")
        '<p>Safe <strong>content</strong></p>'
    """
    if content is None:
        return None
    
    if not isinstance(content, str):
        return None
    
    content = content.strip()
    if not content:
        return None
    
    # Check length limit
    if len(content) > max_length:
        raise ValueError(f"Content exceeds maximum length of {max_length} characters")
    
    # Clean the HTML
    cleaned = bleach.clean(
        content,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
        strip_comments=STRIP_COMMENTS
    )
    
    return cleaned if cleaned else None


def sanitize_text(content: Optional[str], max_length: int = 10000) -> Optional[str]:
    """
    Sanitize plain text content (escape all HTML).
    
    Args:
        content: Plain text content from user
        max_length: Maximum character length allowed
        
    Returns:
        HTML-escaped text content, or None if input is None
        
    Raises:
        ValueError: If content exceeds max_length
    """
    if content is None:
        return None
    
    if not isinstance(content, str):
        return None
    
    content = content.strip()
    if not content:
        return None
    
    # Check length limit
    if len(content) > max_length:
        raise ValueError(f"Content exceeds maximum length of {max_length} characters")
    
    # Escape all HTML
    cleaned = bleach.clean(
        content,
        tags=[],  # No tags allowed
        strip=True,
        strip_comments=True
    )
    
    return cleaned if cleaned else None


def sanitize_comment(body: Optional[str]) -> Optional[str]:
    """
    Sanitize comment content specifically.
    
    Comments allow basic formatting but no links or scripts.
    
    Args:
        body: Comment text from user
        
    Returns:
        Sanitized comment text
        
    Raises:
        ValueError: If content exceeds 5000 characters
    """
    return sanitize_text(body, max_length=5000)


def sanitize_description(description: Optional[str]) -> Optional[str]:
    """
    Sanitize issue or report description.
    
    Descriptions allow more HTML formatting for rich text editing.
    
    Args:
        description: Description text from user
        
    Returns:
        Sanitized description HTML
        
    Raises:
        ValueError: If content exceeds 5000 characters
    """
    return sanitize_html(description, max_length=5000)


def sanitize_message(message: Optional[str]) -> Optional[str]:
    """
    Sanitize private message or notification text.
    
    Messages should only allow very basic formatting.
    
    Args:
        message: Message text from user
        
    Returns:
        Sanitized message text
        
    Raises:
        ValueError: If content exceeds 1000 characters
    """
    return sanitize_text(message, max_length=1000)
