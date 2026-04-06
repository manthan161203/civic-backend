"""AI Service — Multi-Model AI Integration
===========================================
Provides AI-powered features for civic issue management:

- **Chat Assistant**: Uses Groq (openai/gpt-oss-120b) for multilingual chatbot (English, Hindi, Gujarati).
- **SQL Agent**: Groq (openai/gpt-oss-120b) for database queries with tool calling.
- **Issue Classification**: Gemini Vision for analyzing before-photos.
- **Resolution Verification**: Gemini Vision for after-photos.
- **Duplicate Detection**: Finds similar issues within 50m in the last 48 hours.

All AI features degrade gracefully - if API keys are not set or the API
fails, empty/default results are returned and the system continues to function.

Providers:
- Groq (ChatGroq): openai/gpt-oss-120b for text inference (chat, SQL agent)
- Gemini: Vision-based image analysis for issue classification and verification
"""

import json
import math
import re
from datetime import datetime, timedelta
from typing import Optional, Any

from google import genai
from google.genai import types
from sqlalchemy import text, inspect, MetaData
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_core.prompts import PromptTemplate
from langchain_community.tools.sql_database.tool import (
    InfoSQLDatabaseTool,
    ListSQLDatabaseTool,
    QuerySQLDatabaseTool,
    QuerySQLCheckerTool,
)
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain_core.messages import SystemMessage

from app.core.config import settings
from app.core.logger import get_logger
from app.core.prompts import CHATBOT_SYSTEM_PROMPT, CLASSIFY_ISSUE_PROMPT, VERIFY_RESOLUTION_PROMPT
from app.database import engine as db_engine

logger = get_logger("ai")

_client: Optional[genai.Client] = None

_EMPTY_CLASSIFICATION = {"issue_type": None, "severity": None, "confidence": None, "tags": [], "suggested_description": None}
_EMPTY_VERIFICATION = {"is_resolved": None, "confidence": None, "resolution_quality": None, "notes": None}


def _get_client() -> Optional[genai.Client]:
    """Get or initialize the Gemini API client (singleton).

    Returns:
        genai.Client if GEMINI_API_KEY is configured, None otherwise.
    """
    global _client
    if _client is None and settings.GEMINI_API_KEY:
        try:
            _client = genai.Client(api_key=settings.GEMINI_API_KEY)
            logger.info("Gemini API client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}", exc_info=True)
            return None
    return _client


# ============================================================================
# LangChain SQL Agent with Tool Calling
# ============================================================================

def execute_sql_agent(user_message: str, current_user, db, engine) -> str:
    """
    Run a LangChain SQL Agent that uses proper tool calling to interact with the database.
    
    This agent:
    1. Uses LangChain's SQL tools (list tables, get schema, check query, execute query)
    2. Employs ReAct pattern (Reasoning + Acting)
    3. Properly validates queries before execution
    4. Returns formatted results with natural language interpretation
    5. Has access to ALL tables in the database
    
    Args:
        user_message: Natural language question about the database
        current_user: Current user object (role, ward, id)
        db: SQLAlchemy session
        engine: SQLAlchemy engine
        
    Returns:
        Agent execution output with query results and interpretation
    """
    # Tables the agent is allowed to access (exclude sensitive/internal tables)
    ALLOWED_TABLES = [
        "issues", "users", "announcements", "notifications",
        "worker_shifts", "issue_comments", "issue_votes", "issue_flags",
        "reward_transactions", "user_badges", "ward_subscriptions",
        "districts", "talukas", "wards",
    ]
    # Columns that must NEVER appear in any query
    BLOCKED_COLUMNS = [
        "aadhar_hash", "google_id", "fcm_token", "token_hash", "code",
    ]

    try:
        # Create LangChain SQLDatabase wrapper restricted to safe tables only
        langchain_db = SQLDatabase(engine, include_tables=ALLOWED_TABLES)

        # Initialize Groq LLM for SQL Agent
        llm = ChatGroq(
            model="openai/gpt-oss-120b",
            temperature=0,
            api_key=settings.GROQ_API_KEY,
        )

        # Create SQL tools
        tools = [
            ListSQLDatabaseTool(db=langchain_db),
            InfoSQLDatabaseTool(db=langchain_db),
            QuerySQLCheckerTool(db=langchain_db, llm=llm),
            QuerySQLDatabaseTool(db=langchain_db),
        ]

        # ── Build role-aware access control section ──────────────────────
        role = current_user.role
        user_id = current_user.id
        ward_id = getattr(current_user, "ward_id", None)
        taluka_id = getattr(current_user, "taluka_id", None)
        district_id = getattr(current_user, "district_id", None)

        if role == "citizen":
            access_rules = f"""Access Control (CITIZEN):
- You may only query data that belongs to this user.
- On `issues`: always filter with  WHERE reporter_id = '{user_id}'
- On `issue_comments`, `issue_votes`: only rows linked to issues this user reported.
- On `notifications`, `reward_transactions`, `user_badges`, `ward_subscriptions`: filter by user_id = '{user_id}'
- Public/aggregate data (total issues count per ward, announcements) is allowed without user filter."""
        elif role == "worker":
            access_rules = f"""Access Control (WORKER):
- On `issues`: filter by  assigned_worker_id = '{user_id}'  OR  reporter_id = '{user_id}'
- On `worker_shifts`: filter by  worker_id = '{user_id}'
- On `notifications`, `reward_transactions`, `user_badges`: filter by user_id = '{user_id}'
- May view public/aggregate data (announcements, ward info) without filter."""
        elif role == "ward_admin":
            access_rules = f"""Access Control (WARD ADMIN — manages a single ward):
- Assigned ward_id: {ward_id}
- On `issues`: filter by  ward_id = '{ward_id}'
- On `users` (workers): filter by  ward_id = '{ward_id}'
- On `announcements`: filter by  ward_id = '{ward_id}'  OR  scope = 'ward'
- Aggregate / cross-ward data is NOT allowed."""
        elif role == "taluka_admin":
            access_rules = f"""Access Control (TALUKA ADMIN — manages all wards in a taluka):
- Assigned taluka_id: {taluka_id}
- On `issues`: JOIN with `wards` to filter  wards.taluka_id = '{taluka_id}'
- On `users`: filter by  taluka_id = '{taluka_id}'
- On `announcements`: filter by  taluka_id = '{taluka_id}'  OR  scope IN ('ward','taluka')
- May aggregate across wards within this taluka only."""
        elif role == "district_admin":
            access_rules = f"""Access Control (DISTRICT ADMIN — manages all talukas in a district):
- Assigned district_id: {district_id}
- On `issues`: JOIN with `wards` → `talukas` to filter  talukas.district_id = '{district_id}'
- On `users`: filter by  district_id = '{district_id}'
- On `announcements`: filter by  district_id = '{district_id}'  OR  scope IN ('ward','taluka','district')
- May aggregate across talukas within this district only."""
        else:  # admin (super-admin / state-level)
            access_rules = """Access Control (SUPER ADMIN — state-level):
- Full read access to all rows in every allowed table with no geographic restriction.
- May run aggregations across the entire state."""

        # ── Agent system prompt ──────────────────────────────────────────
        agent_prompt = f"""You are an expert SQL agent for a civic issue management platform.
Your job is to translate the user's natural-language question into safe SELECT queries,
execute them, and return a clear, human-readable answer.

─── Current User ───
- Role      : {role}
- User ID   : {user_id}
- Ward ID   : {ward_id or 'N/A'}
- Taluka ID : {taluka_id or 'N/A'}
- District ID: {district_id or 'N/A'}

─── Database Schema (allowed tables only) ───

issues
  Core civic complaints. Key columns: id, reporter_id (FK→users), assigned_worker_id (FK→users),
  issue_type (garbage|pothole|streetlight|drain|other), severity (high|medium|low),
  priority (urgent|high|medium|low), status (open|assigned|in_progress|resolved|closed),
  department, description, latitude, longitude, address, ward_id (FK→wards),
  before_photos (JSON), after_photos (JSON), upvote_count, is_duplicate, is_escalated,
  is_blocked, reassignment_count, created_at, resolved_at.

users
  All platform users (citizens, workers, admins). Key columns: id, phone, email, name,
  role (citizen|worker|ward_admin|taluka_admin|district_admin|admin),
  ward_id (FK→wards), taluka_id (FK→talukas), district_id (FK→districts),
  department, language (en|hi|gu), is_active, is_online, is_available,
  latitude, longitude, location_updated_at, created_at.
  ⛔ NEVER select: aadhar_hash, google_id, fcm_token.

issues → issue_comments
  id, issue_id (FK→issues), author_id (FK→users), body, created_at.

issues → issue_votes
  Upvotes on issues. id, issue_id, user_id, created_at. UNIQUE(issue_id, user_id).

issues → issue_flags
  Reports / flags on issues or comments. id, reporter_id, issue_id, comment_id,
  reason (spam|inappropriate|duplicate|false_report|other),
  status (pending|reviewed|dismissed), created_at.

announcements
  Official notices. id, title, body, author_id (FK→users),
  scope (ward|taluka|district|state), ward_id, taluka_id, district_id, expires_at, created_at.

notifications
  Per-user alerts. id, user_id, issue_id, title, body,
  type (status_update|assignment|resolution|system), is_read, created_at.

worker_shifts
  Duty schedule. id, worker_id (FK→users), day_of_week (0=Mon…6=Sun),
  start_time (HH:MM), end_time (HH:MM), is_active. UNIQUE(worker_id, day_of_week).

reward_transactions
  Point ledger. id, user_id, points (+earned / -spent),
  event_type (report_issue|issue_resolved|vote_received|rate_issue|aadhar_verified|
              first_report|resolve_issue|five_star_rating|fast_resolve|weekly_streak|first_resolution),
  reference_id, note, created_at.

user_badges
  Earned badges. id, user_id, badge_key, earned_at.

ward_subscriptions
  Citizens following wards. id, user_id, ward_id, created_at. UNIQUE(user_id, ward_id).

districts
  id, name, state_name (default 'Gujarat'), centroid_lat, centroid_lon.

talukas
  id, name, district_id (FK→districts), centroid_lat, centroid_lon.

wards
  id, name, ward_number, taluka_id (FK→talukas), centroid_lat, centroid_lon.

─── {access_rules} ───

─── Blocked Columns (NEVER select or filter on these) ───
{', '.join(BLOCKED_COLUMNS)}

─── Query Workflow ───
1. Use sql_db_list_tables to confirm available tables.
2. Use sql_db_schema (info_sql_database) to inspect columns of relevant tables.
3. Write a SELECT-only query respecting the access control rules above.
4. Use sql_db_query_checker to validate the query before execution.
5. Execute the validated query with sql_db_query.
6. Present results in a clear, natural-language summary.

─── Safety Rules ───
• ONLY SELECT queries — never INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE.
• NEVER query otps, refresh_tokens, or alembic_version tables.
• NEVER select {', '.join(BLOCKED_COLUMNS)}.
• Always apply the access control filters for the current user's role.
• LIMIT results to 50 rows max unless the user explicitly asks for more.
• Respond in the SAME LANGUAGE as the user's question."""

        # Create ReAct agent using new langchain.agents API
        # Note: new API doesn't accept prompt parameter, we'll add it to messages instead
        agent = create_agent(
            model=llm,
            tools=tools,
        )

        # Execute the agent — uses langchain.agents API with messages-based input
        logger.info(f"Executing SQL Agent [{role}] for: {user_message}")
        logger.info(f"Allowed tables: {langchain_db.get_usable_table_names()}")

        try:
            logger.info("Invoking agent with new langchain.agents API...")
            # Prepend system prompt to messages for the agent
            messages_with_prompt = [
                {"role": "system", "content": agent_prompt},
                {"role": "user", "content": user_message}
            ]
            result = agent.invoke(
                {"messages": messages_with_prompt},
            )
            logger.info(f"Agent invocation completed. Result keys: {result.keys() if isinstance(result, dict) else type(result)}")
        except Exception as invoke_error:
            logger.error(f"Agent invocation failed: {invoke_error}", exc_info=True)
            return f"Agent execution error: {str(invoke_error)[:200]}"

        # Extract final AI message from the messages list
        messages = result.get("messages", [])
        logger.info(f"Extracted {len(messages)} messages from result")
        if messages:
            logger.info(f"Last message type: {type(messages[-1])}, has content: {hasattr(messages[-1], 'content')}")
            content = messages[-1].content
            logger.info(f"Content type: {type(content)}, is list: {isinstance(content, list)}")
            # Handle case where content is a list (multimodal) — convert to string
            if isinstance(content, list):
                output = "\n".join([str(c) if not isinstance(c, dict) else c.get("text", str(c)) for c in content])
            else:
                output = str(content)
        else:
            output = "No results returned"
            logger.warning("No messages in agent result")
        
        logger.info(f"Agent completed with output length: {len(output)}")
        logger.info(f"SQL Agent Output:\n{output[:500]}")  # Log first 500 chars

        return output
        
    except ImportError as ie:
        logger.error(f"LangChain import error: {ie}")
        return f"LangChain dependencies not available: {str(ie)[:100]}"
    except Exception as e:
        logger.error(f"SQL Agent execution error: {e}", exc_info=True)
        return f"Agent error: {str(e)[:200]}"


# ============================================================================
# SQL Agent Tools - Similar to LangChain's SQLDatabaseToolkit
# ============================================================================

def sql_db_list_tables(db) -> str:
    """Get list of all available tables in the database."""
    inspector = inspect(db.get_bind())
    tables = inspector.get_table_names()
    return ", ".join(tables)


def sql_db_schema(db, table_names: str) -> str:
    """Get the schema and sample rows for specified tables."""
    inspector = inspect(db.get_bind())
    tables = [t.strip() for t in table_names.split(",")]
    # Whitelist: only allow tables that actually exist in the database
    valid_tables = set(inspector.get_table_names())
    
    schema_info = []
    for table_name in tables:
        if table_name not in valid_tables:
            schema_info.append(f"Table '{table_name}' does not exist — skipped.")
            continue
        try:
            columns = inspector.get_columns(table_name)
            schema_info.append(f"Table: {table_name}")
            schema_info.append("Columns:")
            for col in columns:
                col_type = str(col["type"])
                schema_info.append(f"  - {col['name']}: {col_type}")
            
            # Get sample rows — use quoted identifier to prevent injection
            try:
                from sqlalchemy import table, column, select as sa_select
                safe_table = table(table_name)
                stmt = sa_select(safe_table).limit(3)
                result = db.execute(stmt)
                rows = result.fetchall()
                if rows:
                    schema_info.append(f"Sample rows ({len(rows)}):")
                    for row in rows:
                        schema_info.append(f"  {dict(row._mapping) if hasattr(row, '_mapping') else row}")
            except Exception as e:
                schema_info.append(f"  (Could not fetch sample rows: {str(e)[:50]})")
            
            schema_info.append("")
        except Exception as e:
            logger.error(f"Error getting schema for {table_name}: {e}")
    
    return "\n".join(schema_info)


def sql_db_query_checker(query: str) -> str:
    """Validate SQL query for common mistakes before execution."""
    # Basic validation checks
    checks = []
    query_upper = query.upper().strip()
    
    # Check for dangerous operations
    dangerous_ops = ["DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "TRUNCATE"]
    for op in dangerous_ops:
        if query_upper.startswith(op):
            return f"❌ INVALID: {op} operations are not allowed. Only SELECT queries are permitted."
    
    # Check if it's a SELECT query
    if not query_upper.startswith("SELECT"):
        return f"❌ INVALID: Query must start with SELECT. Got: {query_upper[:20]}"
    
    # Check for SQL injection patterns (basic)
    if any(pattern in query_upper for pattern in ["--", "/*", "*/"]):
        return f"⚠️ WARNING: Query contains comments. Ensure they are intentional."
    
    return f"✅ VALID: Query looks correct. Ready to execute."


def sql_db_query(db, query: str) -> str:
    """Execute a read-only SQL query and return results."""
    # Hard enforcement — reject anything that isn't a SELECT
    stripped = query.strip().upper()
    if not stripped.startswith("SELECT"):
        return "❌ BLOCKED: Only SELECT queries are allowed."
    # Block dangerous keywords that could appear inside CTEs or subqueries
    for kw in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE", "CREATE"):
        # Match as whole word to avoid false positives (e.g. "selected")
        if re.search(rf"\b{kw}\b", stripped):
            return f"❌ BLOCKED: {kw} operations are not allowed."
    # Block access to sensitive tables
    for tbl in ("otps", "refresh_tokens", "alembic_version"):
        if re.search(rf"\b{tbl}\b", query, re.IGNORECASE):
            return f"❌ BLOCKED: Access to '{tbl}' table is not allowed."
    try:
        result = db.execute(text(query))
        rows = result.fetchall()
        
        if not rows:
            return "Query executed successfully but returned no results."
        
        # Format results
        formatted_rows = []
        for idx, row in enumerate(rows[:20], 1):  # Limit to 20 rows
            if hasattr(row, '_mapping'):
                formatted_rows.append(f"{idx}. {dict(row._mapping)}")
            else:
                formatted_rows.append(f"{idx}. {row}")
        
        result_text = "\n".join(formatted_rows)
        if len(rows) > 20:
            result_text += f"\n\n... and {len(rows) - 20} more rows"
        
        return result_text
    except Exception as e:
        return f"❌ Query Error: {str(e)}"


def _extract_json(text: str) -> dict:
    """Parse JSON from Gemini output, handling markdown code fences.

    Gemini often wraps JSON in ```json ... ``` fences despite being told not to.
    This function strips those fences before parsing.

    Args:
        text: Raw text output from Gemini.

    Returns:
        Parsed dictionary.

    Raises:
        json.JSONDecodeError: If the text cannot be parsed as JSON.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = [ln for ln in lines[1:] if ln.strip() != "```"]
        text = "\n".join(inner).strip()
    return json.loads(text)


def _run_gemini(image_bytes: bytes, mime_type: str, prompt: str) -> Optional[dict]:
    """Send image bytes + prompt to Gemini and return the parsed JSON response.

    Args:
        image_bytes: Raw image bytes.
        mime_type:   MIME type of the image (e.g. ``"image/jpeg"``).
        prompt:      Text prompt to send alongside the image.

    Returns:
        Parsed dict from Gemini's JSON response, or None on any failure.
    """
    client = _get_client()
    if not client:
        logger.warning("GEMINI_API_KEY not set — skipping AI analysis")
        return None

    if not mime_type.startswith("image/"):
        mime_type = "image/jpeg"

    raw_text = ""
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                prompt,
            ],
        )
        raw_text = response.text
        return _extract_json(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"Gemini returned non-JSON response: {e} | raw: {raw_text[:300]}")
        return None
    except Exception as e:
        logger.error(f"Gemini API request failed: {e}", exc_info=True)
        return None


async def classify_issue(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Classify a civic issue from a before-photo using Gemini Vision.

    Analyzes the photo to determine the issue type, severity, confidence score,
    and suggests a description.

    Args:
        image_bytes: Raw image bytes of the before-photo.
        mime_type:   MIME type (default: ``"image/jpeg"``).

    Returns:
        Dict with keys: ``issue_type``, ``severity``, ``confidence``, ``tags``,
        ``suggested_description``. Returns empty defaults if AI is unavailable.
    """
    result = _run_gemini(image_bytes, mime_type, CLASSIFY_ISSUE_PROMPT)
    if not result:
        logger.warning("Issue classification returned empty — AI unavailable or failed")
        return _EMPTY_CLASSIFICATION
    logger.info(f"Issue classified: type={result.get('issue_type')} severity={result.get('severity')} confidence={result.get('confidence')}")
    return result


async def verify_resolution(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Verify whether an after-photo shows a properly resolved issue.

    Analyzes the after-photo to determine if the issue was resolved,
    the quality of the resolution, and any notes.

    Args:
        image_bytes: Raw image bytes of the after-photo.
        mime_type:   MIME type (default: ``"image/jpeg"``).

    Returns:
        Dict with keys: ``is_resolved``, ``confidence``, ``resolution_quality``
        (``"good"``/``"partial"``/``"poor"``), ``notes``.
        Returns empty defaults if AI is unavailable.
    """
    result = _run_gemini(image_bytes, mime_type, VERIFY_RESOLUTION_PROMPT)
    if not result:
        logger.warning("Resolution verification returned empty — AI unavailable or failed")
        return _EMPTY_VERIFICATION
    logger.info(f"Resolution verified: resolved={result.get('is_resolved')} quality={result.get('resolution_quality')}")
    return result


def check_duplicate(issue_type: str, lat: float, lng: float, db) -> Optional[object]:
    """Check if a similar issue exists within 50 meters in the last 48 hours.

    Uses Haversine distance to find nearby issues of the same type
    that are still active (open/assigned/in_progress).

    Args:
        issue_type: Type of issue to check (e.g. ``"pothole"``).
        lat:        Latitude of the new issue.
        lng:        Longitude of the new issue.
        db:         SQLAlchemy database session.

    Returns:
        The existing Issue object if a duplicate is found, None otherwise.
    """
    from app.models.issue import Issue

    try:
        cutoff = datetime.utcnow() - timedelta(hours=48)
        candidates = (
            db.query(Issue)
            .filter(
                Issue.issue_type == issue_type,
                Issue.status.in_(["open", "assigned", "in_progress"]),
                Issue.created_at >= cutoff,
                Issue.is_duplicate == False,
            )
            .all()
        )

        for issue in candidates:
            if _haversine_meters(lat, lng, issue.latitude, issue.longitude) <= 50:
                logger.info(f"Duplicate detected: new issue near existing issue {issue.id}")
                return issue
    except Exception as e:
        logger.error(f"Error during duplicate check: {e}", exc_info=True)

    return None


def build_dynamic_context(user_message: str, current_user, db, base_context: str = "") -> str:
    """Build enriched context using a LangChain-style SQL Agent.

    The agent:
    1. Lists available database tables
    2. Inspects schemas for relevant tables
    3. Generates SQL query
    4. Validates the query before execution
    5. Executes and returns formatted results

    Args:
        user_message: The user's chat message (natural language query).
        current_user: The User object making the request.
        db: SQLAlchemy database session.
        base_context: Optional base context to append to.

    Returns:
        Enriched context string with database query results from the agent.
    """
    context = base_context

    try:
        if not _get_client():
            logger.warning("Cannot build dynamic context - AI client unavailable")
            return context

        # Run the SQL agent
        logger.info(f"Launching SQL Agent for: {user_message[:100]}")
        agent_result = execute_sql_agent(user_message, current_user, db, db_engine)

        if agent_result:
            # Ensure agent_result is a string (handle list case)
            if isinstance(agent_result, list):
                agent_result = "\n".join([str(item) for item in agent_result])
            else:
                agent_result = str(agent_result)
            
            context += f"\n\n--- SQL AGENT RESULTS ---\n"
            context += f"Question: {user_message}\n\n"
            context += agent_result
            logger.info(f"SQL Agent enriched context with {len(agent_result)} chars")
            logger.info(f"SQL Agent Response:\n{agent_result[:500]}")  # Log for debugging
        else:
            logger.info("SQL Agent returned no results")

    except Exception as e:
        logger.error(f"Error building dynamic context with SQL Agent: {e}", exc_info=True)
        # Gracefully fall back to base context

    return context


def chat_response(user_message: str, issue_context: str = "", db=None) -> str:
    """Generate a multilingual civic assistant reply using Groq.

    Automatically detects the user's language and responds in the same language.
    Supports English, Hindi, and Gujarati.

    Now enhanced with dynamic database queries to provide real-time statistics
    and personalized context based on the user's question.

    Args:
        user_message:  The user's chat message.
        issue_context: Optional context about a specific issue (type, status, etc.).
        db:            Optional SQLAlchemy database session for dynamic context queries.

    Returns:
        AI-generated reply string. Returns a fallback message if AI is unavailable.
    """
    logger.info(f"Chat request: {user_message[:100]}")  # Log incoming question
    
    if not settings.GROQ_API_KEY:
        logger.warning("Chat AI unavailable — GROQ_API_KEY not set")
        return "AI service is currently unavailable. Please try again later."
    
    # Use Groq's openai/gpt-oss-120b model for chat responses
    chat_model = ChatGroq(
        model="openai/gpt-oss-120b",
        temperature=0.7,
        api_key=settings.GROQ_API_KEY,
    )

    system = CHATBOT_SYSTEM_PROMPT
    
    # Build final context (static + dynamic)
    if issue_context:
        system += f"\n\nISSUE CONTEXT:\n{issue_context}"

    try:
        # Use Groq to generate chat response
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message}
        ]
        response = chat_model.invoke(messages)
        reply = response.content.strip()
        logger.info(f"Chat response generated ({len(reply)} chars)")
        logger.info(f"AI Response:\n{reply[:500]}")  # Log first 500 chars of response
        return reply
    except Exception as e:
        logger.error(f"Chatbot AI error: {e}", exc_info=True)
        return "Sorry, I could not process your request right now. Please try again."


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate the great-circle distance between two GPS points in meters.

    Args:
        lat1, lng1: Coordinates of the first point.
        lat2, lng2: Coordinates of the second point.

    Returns:
        Distance in meters.
    """
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
