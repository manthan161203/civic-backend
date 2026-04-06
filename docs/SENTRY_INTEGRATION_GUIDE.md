# Sentry Integration Guide for Error Tracking

This guide enables automatic error tracking, performance monitoring, and alerting with Sentry.

## Installation

### Step 1: Install Sentry SDK

```bash
pip install sentry-sdk[fastapi,sqlalchemy]
```

### Step 2: Create Sentry Account

1. Go to [sentry.io](https://sentry.io)
2. Create a free account (5000 events/month free tier)
3. Create a new project for "FastAPI"
4. Copy the **DSN** (looks like: `https://[email protected]/12345678`)

### Step 3: Initialize Sentry in FastAPI

Add to `app/main.py` at the very top (before initializing FastAPI):

```python
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

# Initialize Sentry error tracking
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN", ""),  # Set via environment variable
    integrations=[
        FastApiIntegration(),
        SqlalchemyIntegration(),
        LoggingIntegration(
            level=logging.INFO,        # Capture info and above
            event_level=logging.ERROR  # Send errors to Sentry
        ),
    ],
    # Performance Monitoring (sample 10% of transactions)
    traces_sample_rate=0.1,
    # Capture release information
    release=os.getenv("APP_VERSION", "0.1.0"),
    # Environment flag
    environment=os.getenv("ENVIRONMENT", "development"),
    # Custom user context
    attach_stacktrace=True,
)
```

### Step 4: Set Environment Variables

In `.env` or deployment configuration:

```env
SENTRY_DSN=https://[your-key]@o1234567.ingest.sentry.io/1234567
ENVIRONMENT=production
APP_VERSION=1.0.0
```

## Usage

### 1. Automatic Error Capture

All unhandled exceptions are automatically captured. Our custom exceptions are clean and structured, so they'll show up clearly in Sentry.

### 2. Manual Event Capture

```python
from sentry_sdk import capture_message, capture_exception

# Capture a message
sentry_sdk.capture_message("User signup completed", level="info")

# Capture an exception
try:
    risky_operation()
except Exception as e:
    sentry_sdk.capture_exception(e)
```

### 3. Add User Context

```python
from sentry_sdk import set_user

@router.post("/login")
def login(credentials, db: Session):
    user = authenticate_user(credentials, db)
    if user:
        # Attach user context to all subsequent errors
        set_user({
            "id": str(user.id),
            "username": user.username,
            "email": user.email,
            "role": user.role
        })
    return user_response
```

### 4. Add Custom Tags & Context

```python
from sentry_sdk import set_tag, set_context

@router.post("/issues")
def create_issue(body: IssueCreate, current_user: User, db: Session):
    try:
        # Tag this operation for filtering in Sentry dashboard
        set_tag("feature", "issue_reporting")
        set_tag("issue_type", body.issue_type)
        set_tag("user_role", current_user.role)
        
        # Add detailed context
        set_context("issue_creation", {
            "description_length": len(body.description),
            "has_photos": bool(body.photos),
            "location": f"{body.latitude}, {body.longitude}",
            "is_sos": body.is_sos
        })
        
        # ... rest of operation
        
    except ValidationError as e:
        set_tag("error_type", "validation")
        raise
    except DatabaseError as e:
        set_tag("error_type", "database")
        raise
```

### 5. Performance Monitoring (Tracing)

```python
from sentry_sdk import start_transaction

@router.get("/dashboard")
def get_dashboard(current_user: User, db: Session):
    with start_transaction(op="view", name="Dashboard Load"):
        # This transaction will be automatically monitored
        stats = load_dashboard_stats(current_user, db)
        return stats
```

### 6. Integration with Our Error Handling

The new exception hierarchy works perfectly with Sentry:

```python
# In app/main.py, add exception handler
from fastapi import Request
from app.core.exceptions import CivicException

@app.exception_handler(CivicException)
async def civic_exception_handler(request: Request, exc: CivicException):
    # Automatically send to Sentry with proper level
    if exc.is_transient:
        sentry_sdk.capture_message(
            f"{exc.__class__.__name__}: {exc.message}",
            level="warning"  # Transient errors = warning
        )
    else:
        sentry_sdk.capture_exception(exc, level="error")
    
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict()
    )
```

## Dashboard Configuration

### Alerts (Notifications)

1. Go to Sentry Project → Alerts
2. Create alert: "Send notification when..."
3. Common alerts:
   - **Error spike**: More than 10 errors in 5 minutes
   - **SOS issues**: Any critical issue reports
   - **Database errors**: All database operation failures
   - **External service failures**: Storage, AI service, notification service

Set notification targets:
- Slack channel (recommended for team)
- Email (for critical alerts)
- SMS/PagerDuty (for on-call rotation)

### Dashboard Widget

Add to Sentry dashboard for quick visibility:

```python
# Dashboard shows:
- Error rate (errors per minute)
- Top 10 error types 
- Error trends (24h, 7d, 30d)
- Affected users (which user IDs hit errors)
- Performance metrics (response time percentiles)
```

## Best Practices

### 1. **Use Correct Error Levels**

```python
# Debug: Development-only info
sentry_sdk.capture_message("Cache hit", level="debug")

# Info: Normal operations
sentry_sdk.capture_message("User signup completed", level="info")

# Warning: Something unexpected but recoverable
logger.warning("AI service slow, using fallback", level="warning")

# Error: Something failed
sentry_sdk.capture_exception(e)  # Default level

# Fatal: Application-level failure
sentry_sdk.capture_exception(CriticalServiceError(), level="fatal")
```

### 2. **Sample High-Volume Events**

For noisy errors, set sample rate:

```python
sentry_sdk.capture_exception(
    e,
    sample_rate=0.1  # Capture 10% of rate-limit errors
)
```

### 3. **Exclude Irrelevant Errors**

Add to Sentry init:

```python
def before_send(event, hint):
    # Don't send 404 errors (normal in API usage)
    if event["exception"]["values"][0]["type"] == "ResourceNotFoundError":
        if event["response"]["status"] == 404:
            return None
    return event

sentry_sdk.init(..., before_send=before_send)
```

### 4. **Environment-Specific Configuration**

```python
if os.getenv("ENVIRONMENT") == "production":
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        traces_sample_rate=0.1,  # 10% in production
        environment="production"
    )
elif os.getenv("ENVIRONMENT") == "staging":
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        traces_sample_rate=0.5,  # 50% in staging (more data)
        environment="staging"
    )
else:
    # Development mode - don't send to Sentry
    pass
```

## Monitoring Query Patterns

### Track Database Performance

The Sqlalchemy integration automatically captures slow queries:

```python
# In Sentry, look for:
- Slow database queries (>1s)
- N+1 query problems
- Transaction duration
- Connection pool exhaustion
```

### Track External Service Calls

```python
# When calling AI, storage, or notification services:
with start_transaction(op="external_api", name="AI Classification"):
    result = await classify_issue(file_bytes, mime_type)

# Sentry will show:
- Request duration
- Success/failure rates
- Errors from service
```

## Issue Grouping & Filtering

In Sentry dashboard:

### Group By
- **Error Type** (ValidationError, DatabaseError, etc.)
- **Endpoint** (POST /issues, GET /admin/dashboard)
- **User** (track which users hit errors)
- **Release** (find regressions in new versions)

### Filter examples:
```
# Errors in last hour
is:unresolved age:-1h

# Database errors only
issue_type:DatabaseError

# POST /issues endpoint only
event.request.url:"/issues" http.method:POST

# Errors affecting more than 5 users
error.user_count:>5

# SOS-related errors
tags.issue_type:sos

# Recent regressions
releaseVersion:[1.0.0 TO latest]
```

## Metrics & Analytics

### KPIs to Track

1. **Error Rate**: Errors per 1000 requests
   - Target: <1 per 1000 (0.1%)
   - Alert if: >5 per 1000

2. **Response Time** (P95):
   - Target: <500ms
   - Alert if: >2s

3. **Database Query Time** (P95):
   - Target: <100ms
   - Alert if: >1s

4. **External Service Success Rate**:
   - Target: >99%
   - Alert if: <98%

5. **Affected Users** (daily):
   - Track how many unique users hit errors
   - Helps prioritize critical bugs

### Sentry Integration with Monitoring

```python
# Create custom metrics in Sentry
from sentry_sdk.metrics import timing, counter

# Counter for custom events
counter("issue.created", 1, tags={"type": "before-photo"})

# Timing for operations
with timing("db.query.duration"):
    issues = db.query(Issue).filter(...).all()
```

## Compliance & Privacy

### GDPR Compliance

Sentry can capture PII (names, emails, IPs, etc.). Configure:

```python
# In Sentry init
sentry_sdk.init(
    ...,
    before_send=lambda e, h: filter_sensitive_data(e)
)

def filter_sensitive_data(event):
    # Remove user emails from breadcrumbs
    if "breadcrumbs" in event:
        for bc in event["breadcrumbs"]:
            if "data" in bc:
                bc["data"].pop("user_email", None)
    return event
```

### Data Retention

In Sentry project settings, configure:
- Event retention: 30 days (default)
- Session retention: 90 days

## Cost Optimization

### Free Tier Usage

- 5,000 events/month (free)
- Perfect for development/staging
- Set `traces_sample_rate=0.01` (1%) to stay under quota

### Production (Paid)

- Typical app: 50,000-500,000 events/month
- Use sampling to control costs:
  - Debug events: sample 1%
  - Info events: sample 10%
  - Error events: 100% (capture all)
  - High-volume endpoints: dynamic sampling

```python
import random

def dynamic_sample_rate(transaction):
    # Sample rate-limit errors heavily
    if "429" in transaction.name:
        return 0.01  # 1%
    # Sample AI classification lightly
    if "AI" in transaction.name:
        return 0.1  # 10%
    # Capture all critical operations
    return 1.0  # 100%

sentry_sdk.init(..., traces_sample_rate=dynamic_sample_rate)
```

## Troubleshooting

### Sentry Not Receiving Events

```python
# Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)
sentry_logger = logging.getLogger("sentry")
sentry_logger.setLevel(logging.DEBUG)

sentry_sdk.init(..., debug=True)
```

### Events Missing Tags

```python
# Ensure set_tag/set_context called before exception
with sentry_sdk.push_scope() as scope:
    scope.set_tag("user_action", "issue_creation")
    scope.set_context("zone", {"name": "Ward 5"})
    dangerous_operation()  # Tags attached to any errors
```

### Performance Metrics Not Showing

- Ensure `traces_sample_rate > 0`
- Check transaction naming is consistent
- Verify integrations are installed: `sentry_sdk[fastapi,sqlalchemy]`

## Next Steps

1. ✅ Install Sentry SDK
2. ✅ Create Sentry project & get DSN
3. ✅ Initialize in app/main.py
4. ✅ Set environment variables
5. Configure alerts for team
6. Test with sample errors
7. Monitor dashboard in production
8. Refine alert thresholds based on patterns

## Resources

- [Sentry FastAPI Documentation](https://docs.sentry.io/platforms/python/integrations/fastapi/)
- [Performance Monitoring Guide](https://docs.sentry.io/product/performance/)
- [Alert Rules Documentation](https://docs.sentry.io/product/alerts-notifications/alert-rules/)
- [Release Tracking](https://docs.sentry.io/product/releases/)
