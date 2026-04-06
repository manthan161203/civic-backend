"""
FastAPI Dependencies
====================
Reusable dependency functions for authentication and role-based access control.

Admin Role Hierarchy (ascending privilege):
    citizen < worker < ward_admin < taluka_admin < district_admin < admin

Usage in route functions:
    current_user: User = Depends(get_current_user)               # any authenticated user
    current_user: User = Depends(require_role("admin"))          # super-admin only
    current_user: User = Depends(require_role("worker", "admin"))# worker or admin
    current_user: User = Depends(require_any_admin)              # any admin role
    current_user: User = Depends(require_min_role("ward_admin")) # ward_admin or above
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logger import get_logger
from app.core.security import decode_access_token
from app.database import get_db
from app.models.user import User
from app.models.location import Ward, Taluka
from app.services.utils import user_has_role

logger = get_logger("deps")

bearer_scheme = HTTPBearer()

# Ordered from least to most privileged
ROLE_HIERARCHY = ["citizen", "worker", "ward_admin", "taluka_admin", "district_admin", "admin"]

# All roles that have some admin authority
ADMIN_ROLES = {"ward_admin", "taluka_admin", "district_admin", "admin"}


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Validate the Bearer token and return the authenticated user."""
    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload:
        logger.warning("Token validation failed: invalid or expired JWT")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = payload.get("sub")
    if not user_id:
        logger.warning("Token validation failed: missing 'sub' claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        logger.warning(f"Token references unknown or inactive user: {user_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    return user


def require_role(*roles: str):
    """Dependency factory that enforces exact role-based access control.

    Args:
        *roles: One or more allowed role strings.

    Raises:
        HTTPException 401: Not authenticated.
        HTTPException 403: Role not in allowed list.
    """

    def _check(current_user: User = Depends(get_current_user)) -> User:
        if not user_has_role(current_user, list(roles)):
            logger.warning(
                f"Access denied for user {current_user.id} "
                f"(role={current_user.role}, required={roles})"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {', '.join(roles)}",
            )
        return current_user

    return _check


def require_any_admin(current_user: User = Depends(get_current_user)) -> User:
    """Allow any admin role (ward_admin, taluka_admin, district_admin, admin).

    Raises:
        HTTPException 403: If the user is not an admin of any level.
    """
    if not user_has_role(current_user, list(ADMIN_ROLES)):
        logger.warning(f"Admin access denied for user {current_user.id} (role={current_user.role})")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Admin role required.",
        )
    return current_user


def require_min_role(min_role: str):
    """Dependency factory: allow the given role and all higher-privilege roles.

    E.g. require_min_role("ward_admin") allows ward_admin, taluka_admin, district_admin, admin.

    Args:
        min_role: Minimum required role string.
    """
    try:
        min_level = ROLE_HIERARCHY.index(min_role)
    except ValueError:
        raise ValueError(f"Unknown role: {min_role}")

    def _check(current_user: User = Depends(get_current_user)) -> User:
        try:
            user_level = ROLE_HIERARCHY.index(current_user.role)
        except ValueError:
            user_level = -1

        if user_level < min_level:
            logger.warning(
                f"Insufficient privilege for user {current_user.id} "
                f"(role={current_user.role}, required>={min_role})"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Minimum role required: {min_role}",
            )
        return current_user

    return _check


def get_admin_scope_filter(admin_user: User) -> dict:
    """Return a dict of Issue field filters matching the admin's geographic scope.

    Returns:
        Dict suitable for use with SQLAlchemy filter kwargs, e.g.
        ``{"ward_id": uuid}``  for a ward_admin,
        ``{}``                 for a super-admin (no filter = see everything).
    """
    if admin_user.role == "admin":
        return {}
    if admin_user.role == "district_admin" and admin_user.district_id:
        return {"district_id": admin_user.district_id}
    if admin_user.role == "taluka_admin" and admin_user.taluka_id:
        return {"taluka_id": admin_user.taluka_id}
    if admin_user.role == "ward_admin" and admin_user.ward_id:
        return {"ward_id": admin_user.ward_id}
    return {}


def apply_admin_scope(query, admin_user: User, model):
    """Apply the admin's geographic scope as a WHERE clause on a SQLAlchemy query.

    Handles models that store all three FK levels (district_id, taluka_id, ward_id)
    as well as models that only have ward_id (e.g. Issue), where taluka/district
    scoping is resolved via a subquery through the Ward → Taluka hierarchy.

    Args:
        query:      Active SQLAlchemy query.
        admin_user: The authenticated admin user.
        model:      The ORM model being queried.

    Returns:
        The filtered query.
    """
    scope = get_admin_scope_filter(admin_user)
    if not scope:
        return query

    if "ward_id" in scope:
        query = query.filter(model.ward_id == scope["ward_id"])
    elif "taluka_id" in scope:
        if hasattr(model, "taluka_id"):
            query = query.filter(model.taluka_id == scope["taluka_id"])
        else:
            # Model only has ward_id — resolve via Ward subquery
            ward_ids = select(Ward.id).where(Ward.taluka_id == scope["taluka_id"]).scalar_subquery()
            query = query.filter(model.ward_id.in_(ward_ids))
    elif "district_id" in scope:
        if hasattr(model, "district_id"):
            query = query.filter(model.district_id == scope["district_id"])
        else:
            # Model only has ward_id — resolve via Ward → Taluka subquery
            taluka_ids = select(Taluka.id).where(
                Taluka.district_id == scope["district_id"]
            ).scalar_subquery()
            ward_ids = select(Ward.id).where(
                Ward.taluka_id.in_(taluka_ids)
            ).scalar_subquery()
            query = query.filter(model.ward_id.in_(ward_ids))
    return query
