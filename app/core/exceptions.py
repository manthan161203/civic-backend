"""
Custom Exception Hierarchy for Civic Application
================================================

Defines structured exceptions used throughout the application to provide
consistent error handling, logging, and API responses.

Exception categories:
- ValidationError: Input validation failures
- AuthenticationError: Authentication/authorization issues
- ResourceNotFoundError: Requested resource does not exist
- ConflictError: Resource conflict (duplicate, state mismatch)
- ExternalServiceError: Third-party service failures
- DatabaseError: Database operation failures
- ProcessingError: Business logic processing failures

Examples:
    >>> from app.core.exceptions import ValidationError
    >>> if not user.email:
    ...     raise ValidationError("email", "Email is required")

    >>> from app.core.exceptions import ExternalServiceError
    >>> try:
    ...     send_sms(phone, "message")
    ... except Exception as e:
    ...     raise ExternalServiceError("SMS", str(e), transient=True)
"""

from typing import Optional, Any


class CivicException(Exception):
    """Base exception for all Civic application errors.
    
    Attributes:
        message (str): User-friendly error message
        details (dict): Additional context for debugging
        status_code (int): HTTP status code for API responses
        is_transient (bool): Whether the error is temporary/retryable
    """
    
    def __init__(
        self,
        message: str,
        details: Optional[dict] = None,
        status_code: int = 500,
        is_transient: bool = False
    ):
        """Initialize CivicException.
        
        Args:
            message: Description of the error
            details: Additional debugging information
            status_code: HTTP status code (default 500)
            is_transient: Whether error is temporary/retryable (default False)
        """
        self.message = message
        self.details = details or {}
        self.status_code = status_code
        self.is_transient = is_transient
        super().__init__(self.message)
    
    def to_dict(self) -> dict:
        """Convert exception to API response format.
        
        Returns:
            dict: Structured error response with message and details
        """
        return {
            "error": self.__class__.__name__,
            "message": self.message,
            "details": self.details,
            "is_transient": self.is_transient,
        }


class ValidationError(CivicException):
    """Raised when input validation fails.
    
    Used for invalid request parameters, malformed data, or business rule violations.
    """
    
    def __init__(self, field: str, message: str, details: Optional[dict] = None):
        """Initialize ValidationError.
        
        Args:
            field: Name of the field that failed validation
            message: Description of the validation failure
            details: Additional error context
        """
        full_message = f"Invalid {field}: {message}"
        error_details = {"field": field, **(details or {})}
        super().__init__(full_message, error_details, status_code=422, is_transient=False)


class AuthorizationError(CivicException):
    """Raised when user lacks required permissions."""
    
    def __init__(self, action: str, resource: str, details: Optional[dict] = None):
        """Initialize AuthorizationError.
        
        Args:
            action: The action that was attempted (e.g., "delete", "edit")
            resource: The resource being accessed (e.g., "issue", "user")
            details: Additional context
        """
        message = f"Not authorized to {action} {resource}"
        super().__init__(message, details or {}, status_code=403, is_transient=False)


class AuthenticationError(CivicException):
    """Raised for authentication failures (invalid token, expired, etc)."""
    
    def __init__(self, message: str = "Authentication required", details: Optional[dict] = None):
        """Initialize AuthenticationError.
        
        Args:
            message: Description of the authentication failure
            details: Additional context
        """
        super().__init__(message, details or {}, status_code=401, is_transient=False)


class ResourceNotFoundError(CivicException):
    """Raised when a requested resource does not exist."""
    
    def __init__(self, resource_type: str, resource_id: Any, details: Optional[dict] = None):
        """Initialize ResourceNotFoundError.
        
        Args:
            resource_type: Type of resource (e.g., "Issue", "User", "Ward")
            resource_id: ID of the resource that was not found
            details: Additional context
        """
        message = f"{resource_type} not found: {resource_id}"
        error_details = {"resource_type": resource_type, "resource_id": str(resource_id), **(details or {})}
        super().__init__(message, error_details, status_code=404, is_transient=False)


class ConflictError(CivicException):
    """Raised when resource conflict prevents operation (duplicate, state mismatch, etc)."""
    
    def __init__(self, message: str, details: Optional[dict] = None):
        """Initialize ConflictError.
        
        Args:
            message: Description of the conflict
            details: Additional context
        """
        super().__init__(message, details or {}, status_code=409, is_transient=False)


class ExternalServiceError(CivicException):
    """Raised when third-party service call fails (SMS, email, AI model, etc).
    
    Distinguishes between transient failures (network, timeout) and permanent failures.
    """
    
    def __init__(
        self,
        service_name: str,
        error_message: str,
        transient: bool = False,
        details: Optional[dict] = None
    ):
        """Initialize ExternalServiceError.
        
        Args:
            service_name: Name of the external service (e.g., "SMS", "Gemini", "Cloudinary")
            error_message: Original error from the service
            transient: Whether the error is temporary/retryable
            details: Additional debugging information
        """
        message = f"{service_name} service error: {error_message}"
        error_details = {"service": service_name, **(details or {})}
        super().__init__(
            message,
            error_details,
            status_code=503 if transient else 500,
            is_transient=transient
        )


class DatabaseError(CivicException):
    """Raised when database operations fail.
    
    Distinguishes between transient failures (connection issues) and data issues.
    """
    
    def __init__(
        self,
        operation: str,
        message: str,
        transient: bool = False,
        details: Optional[dict] = None
    ):
        """Initialize DatabaseError.
        
        Args:
            operation: Database operation (e.g., "insert", "query", "commit")
            message: Description of the database error
            transient: Whether the error is temporary (connection issue)
            details: Additional context
        """
        full_message = f"Database {operation} failed: {message}"
        error_details = {"operation": operation, **(details or {})}
        super().__init__(
            full_message,
            error_details,
            status_code=503 if transient else 500,
            is_transient=transient
        )


class ProcessingError(CivicException):
    """Raised when business logic processing fails (e.g., issue resolution, escalation)."""
    
    def __init__(self, process: str, message: str, details: Optional[dict] = None):
        """Initialize ProcessingError.
        
        Args:
            process: Name of the process that failed (e.g., "issue_resolution", "escalation")
            message: Description of the failure
            details: Additional context
        """
        full_message = f"Error during {process}: {message}"
        error_details = {"process": process, **(details or {})}
        super().__init__(full_message, error_details, status_code=500, is_transient=False)
