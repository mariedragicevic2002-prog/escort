"""
State Manager - Single source of truth for conversation state.
Uses optimistic locking to prevent race conditions.
"""

import json
import logging
import random
import hashlib
import os
import time as _time
from datetime import date, datetime, time
from typing import Any
from psycopg2 import sql as psy_sql

from services.conversation_event_service import record_conversation_event
from utils.log_sanitize import sanitize_log_value
from utils.structured_logging import log_quality_metric

try:
    from services.state_cache import get_cached_state, invalidate_cached_state, set_cached_state
except Exception:
    def get_cached_state(phone_number: str) -> dict[str, Any] | None:  # type: ignore[misc]
        return None

    def set_cached_state(phone_number: str, state: dict[str, Any]) -> bool:  # type: ignore[misc]
        return False

    def invalidate_cached_state(phone_number: str) -> bool:  # type: ignore[misc]
        return False

logger = logging.getLogger("adella_chatbot.state_manager")


_STATE_JSON_FIELDS = ("missing_fields", "offered_slot_hours", "offered_slot_minutes", "offered_slot_dates")


def _is_schema_or_programming_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(
        token in msg
        for token in (
            "undefined column",
            "column does not exist",
            "undefined table",
            "relation does not exist",
            "syntax error at or near",
            "invalid input syntax",
            "cannot cast",
            "missing from-clause entry",
        )
    ):
        return True
    name = type(exc).__name__.lower()
    return name in {
        "programmingerror",
        "undefinedcolumn",
        "undefinedtable",
        "syntaxerror",
        "invalidtextrepresentation",
    }


def _raise_if_schema_error(exc: BaseException, op: str) -> None:
    if _is_schema_or_programming_error(exc):
        logger.critical("State manager %s failed due to schema/query error: %s", op, exc)
        raise exc


def _hydrate_state_record(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    hydrated = dict(state)
    if "current_state" not in hydrated:
        hydrated["current_state"] = "NEW"
    for json_field in _STATE_JSON_FIELDS:
        raw_value = hydrated.get(json_field)
        if raw_value and isinstance(raw_value, str):
            try:
                hydrated[json_field] = json.loads(raw_value)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning(
                    "Corrupt %s JSON for %s: %r (raw=%r) — defaulting to []",
                    json_field,
                    sanitize_log_value(str(hydrated.get("phone_number") or "")),
                    exc,
                    raw_value,
                )
                hydrated[json_field] = []
    raw_date = hydrated.get("date")
    if isinstance(raw_date, str):
        try:
            hydrated["date"] = date.fromisoformat(raw_date[:10])
        except ValueError:
            pass
    raw_time = hydrated.get("time")
    if isinstance(raw_time, str):
        try:
            hydrated["time"] = time.fromisoformat(raw_time.split("+")[0].strip())
        except ValueError:
            pass
    datetime_fields = {
        "created_at",
        "updated_at",
        "last_message_at",
        "deposit_requested_at",
        "confirmed_at",
        "optional_deposit_paid_at",
        "tour_subscribed_at",
        "awaiting_yes_set_at_ts",
    }
    for key in datetime_fields:
        value = hydrated.get(key)
        if not isinstance(value, str):
            continue
        try:
            hydrated[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return hydrated


def _merge_state_for_cache(current: dict[str, Any], *, new_state: str | None = None, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(current or {})
    if new_state is not None:
        merged["current_state"] = new_state
    for field, value in (updates or {}).items():
        merged[field] = value
    if merged.get("version") is not None:
        try:
            merged["version"] = int(merged["version"]) + 1
        except (TypeError, ValueError):
            pass
    return _hydrate_state_record(merged) or merged


def _sync_state_cache(phone_number: str, state: dict[str, Any] | None = None, *, conn=None) -> None:
    if conn is not None or not isinstance(state, dict):
        invalidate_cached_state(phone_number)
        return
    set_cached_state(phone_number, state)


# Derived from core.state_machine.STATE_TRANSITIONS — single source of truth.
# NEW intentionally excluded from DEPOSIT_REQUIRED targets: a pending-deposit
# client must not be silently reclassified to NEW by the dispatcher. The only
# legitimate reset path is StateManager.clear_booking (force=True).
def _build_valid_transitions() -> dict[str, frozenset[str]]:
    from core.state_machine import STATE_TRANSITIONS
    return {
        state: frozenset(targets.values())
        for state, targets in STATE_TRANSITIONS.items()
    }

VALID_STATE_TRANSITIONS: dict[str, frozenset[str]] = _build_valid_transitions()

# Columns in conversation_states that can be updated (avoids invalid SET and ensures client_name is persisted)
ALLOWED_STATE_UPDATE_FIELDS = frozenset({
    'current_state', 'date', 'time', 'duration', 'experience_type', 'incall_outcall',
    'outcall_address', 'client_name', 'missing_fields', 'first_contact_sent',
    'available_now_requested', 'arrival_time_minutes',
    'deposit_required', 'deposit_amount', 'deposit_reason', 'deposit_requested_at',
    'deposit_payment_reference',
    'outcall_awaiting_yes',
    'incall_awaiting_yes',
    'awaiting_yes_set_at',
    'awaiting_name',
    'deposit_screenshot_attempts', 'deposit_paid',
    'peacock_event_id', 'graphite_event_id', 'confirmed_event_id', 'travel_outbound_event_id', 'travel_return_event_id',
    'confirmed_at', 'confirmation_token', 'total_booking_cost', 'post_booking_messages', 'room_detail_reminder_scheduled', 'room_detail_reminder_sent',
    'forward_incall_replies_to_escort',
    'peacock_created_at',
    'optional_deposit_requested', 'optional_deposit_paid',
    'optional_deposit_amount', 'optional_deposit_paid_at',
    'outcall_travel_notification_scheduled', 'outcall_travel_notification_sent',
    'confirmation_30min_scheduled', 'confirmation_30min_sent',
    'feedback_request_sent',
    'manual_review_required',
    'awaiting_refund_details',
    'profanity_count',
    'profanity_detected',
    'unsafe_service_requested',
    'tour_sms_subscription',
    'tour_subscription_city',
    'tour_subscribed_at',
    'last_touring_inquiry_city',
    'offered_slot_hours',
    'offered_slot_minutes',
    'offered_slot_date',
    'offered_slot_dates',
    'frustration_reply_sent',
    '_consecutive_same_response_count',
    'message_count',
    'booking_status',
    'bump_deposit_amount',
    'confirmed_ai_reply_count',
    # Outcall "available now" follow-up: auto-picked earliest slot after address (prevents re-sending slot list)
    'earliest_slot_auto_selected',
    'booking_type',
    'doubles_type',
    'flow_version',
    'escort_supply_confirmed',
    'escort_supply_source',
    'dinner_restaurant',
    'dinner_after_preference',
    'dinner_client_address',
    'dinner_client_outside_15km',
    '_verified_address',
    '_verified_distance_km',
    'calendar_yes_degraded',
    'mmf_exploration_tags',
    'mmf_exploration_prompt_sent',
    'mmf_male_sourcing_escort_notified',
    'awaiting_booking_change_cancel_choice',
    'awaiting_yes_set_at_ts',
})

# JSONB columns: psycopg2 adapts Python list → PostgreSQL text[] unless we send JSON.
_JSON_STRING_FIELDS_FOR_JSONB = frozenset({
    'missing_fields',
    'offered_slot_hours',
    'offered_slot_minutes',
    'offered_slot_dates',
})


def _normalize_value_for_db(field: str, value: Any) -> Any:
    """
    Convert Python booking field values to DB-compatible types.
    PostgreSQL DATE expects date; TIME expects time or string.
    """
    if value is None:
        return None
    if field == 'date':
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return datetime.strptime(value[:10], '%Y-%m-%d').date()
            except ValueError:
                return value
        return value
    if field == 'time':
        # Store as (hour, minute) tuple -> convert to time object for PostgreSQL TIME column
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                h, m = int(value[0]), int(value[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    return time(hour=h, minute=m)
            except (ValueError, TypeError):
                pass
        if isinstance(value, int):
            # Integer hour (e.g. 11 for 11:00) -> convert to time object
            try:
                if 0 <= value <= 23:
                    return time(hour=value, minute=0)
            except (ValueError, TypeError):
                pass
        if isinstance(value, time):
            return value
        return value
    if field == 'available_now_requested':
        return bool(value) if value is not None else False
    if field == 'earliest_slot_auto_selected':
        return bool(value) if value is not None else False
    if field == 'dinner_client_outside_15km':
        return bool(value) if value is not None else False
    if field == 'forward_incall_replies_to_escort':
        return bool(value) if value is not None else False
    if field == 'escort_supply_confirmed':
        return bool(value) if value is not None else False
    if field == 'manual_review_required':
        return bool(value) if value is not None else False
    if field == 'arrival_time_minutes':
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if field == '_verified_distance_km':
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return value


def _stable_rollout_bucket(phone_number: str) -> int:
    """Deterministic 0-99 bucket for a phone number (used for v2 rollout)."""
    key = (phone_number or "").strip() or "unknown"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _parse_rollout_percent(raw: Any) -> int | None:
    """Parse rollout percent in [0, 100]; return None when invalid."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        value = int(float(s))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, value))


class StateManager:
    """Manages conversation state with optimistic locking."""

    def __init__(self, db_service=None):
        """
        Initialize state manager.

        Args:
            db_service: Database service instance (optional for testing)
        """
        self.db = db_service

    def get_state(self, phone_number: str, conn=None) -> dict[str, Any] | None:
        """
        Get current state for a phone number.

        Args:
            phone_number: Client's phone number
            conn: Optional open connection (same transaction as other writes)

        Returns:
            Dict with state data, or None if no state exists
        """
        if not self.db:
            # For testing: return a mock state
            return {
                'phone_number': phone_number,
                'current_state': 'NEW',
                'version': 1,
                'last_message_at': None
            }
        
        if conn is None:
            cached = get_cached_state(phone_number)
            if isinstance(cached, dict):
                return _hydrate_state_record(cached)

        try:
            result = self.db.execute_query(
                """SELECT * FROM conversation_states WHERE phone_number = %s""",
                (phone_number,),
                fetch=True,
                conn=conn,
            )

            if result:
                state = _hydrate_state_record(result[0])
                if conn is None and state is not None:
                    set_cached_state(phone_number, state)
                return state
            if conn is None:
                invalidate_cached_state(phone_number)
            return None

        except Exception as e:
            logger.error("Failed to get state for %s: %s", sanitize_log_value(phone_number), e)
            _raise_if_schema_error(e, "get_state")
            return None

    def create_state(self, phone_number: str, initial_state: str = "NEW", conn=None) -> bool:
        """
        Create new conversation state.

        Args:
            phone_number: Client's phone number
            initial_state: Initial state (default: NEW)
            conn: Optional open connection (same transaction as other writes)

        Returns:
            True if created successfully, False otherwise
        """
        if not self.db:
            # For testing: just return True
            return True

        flow_version = self._resolve_new_conversation_flow_version(phone_number)
        try:
            try:
                # New schema path: persist rollout-selected flow_version on row creation.
                self.db.execute_query(
                    """INSERT INTO conversation_states (phone_number, current_state, missing_fields, flow_version)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (phone_number) DO NOTHING""",
                    (phone_number, initial_state, json.dumps(["date", "time", "duration"]), flow_version),
                    fetch=False,
                    conn=conn,
                )
            except Exception as e:
                # Backward compatibility for older DBs without the flow_version column.
                msg = str(e).lower()
                if "flow_version" not in msg or ("does not exist" not in msg and "undefined column" not in msg):
                    raise
                logger.warning(
                    "conversation_states.flow_version column missing; creating state without flow_version for %s",
                    sanitize_log_value(phone_number),
                )
                self.db.execute_query(
                    """INSERT INTO conversation_states (phone_number, current_state, missing_fields)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (phone_number) DO NOTHING""",
                    (phone_number, initial_state, json.dumps(["date", "time", "duration"])),
                    fetch=False,
                    conn=conn,
                )
            invalidate_cached_state(phone_number)
            return True
        except Exception as e:
            logger.error("Failed to create state for %s: %s", sanitize_log_value(phone_number), e)
            return False

    def _resolve_new_conversation_flow_version(self, phone_number: str) -> str:
        """
        Resolve flow version for newly-created conversation rows.

        Priority:
        1) FLOW_VERSION_DEFAULT env var ("v1"|"v2")
        2) admin_settings.flow_version_default ("v1"|"v2")
        3) FLOW_VERSION_V2_ROLLOUT_PERCENT env var (0-100)
        4) admin_settings.flow_version_v2_rollout_percent (0-100)
        5) fallback "v1"
        """
        env_default = (os.environ.get("FLOW_VERSION_DEFAULT") or "").strip().lower()
        if env_default in {"v1", "v2"}:
            return env_default

        db_default = ""
        try:
            from core.settings_manager import get_setting

            db_default = (get_setting("flow_version_default") or "").strip().lower()
        except Exception as e:
            logger.warning("Could not read flow_version_default from settings: %s", e)
        if db_default in {"v1", "v2"}:
            return db_default

        env_rollout = _parse_rollout_percent(os.environ.get("FLOW_VERSION_V2_ROLLOUT_PERCENT"))
        if env_rollout is None:
            try:
                from core.settings_manager import get_setting

                env_rollout = _parse_rollout_percent(get_setting("flow_version_v2_rollout_percent"))
            except Exception as e:
                logger.warning("Could not read flow_version_v2_rollout_percent from settings: %s", e)
                env_rollout = None

        if env_rollout is None:
            return "v1"
        if env_rollout >= 100:
            return "v2"
        if env_rollout <= 0:
            return "v1"

        return "v2" if _stable_rollout_bucket(phone_number) < env_rollout else "v1"

    def get_or_create_context(self, phone_number: str) -> dict[str, Any]:
        """Get existing state or create a new one if missing."""
        state = self.get_state(phone_number)
        if state:
            return state
        # Create default state if none exists
        self.create_state(phone_number)
        return self.get_state(phone_number) or {
            'phone_number': phone_number,
            'current_state': 'NEW',
            'last_message_at': None
        }

    def transition(
        self,
        phone_number: str,
        new_state: str,
        updates: dict[str, Any] | None = None,
        conn=None,
        force: bool = False,
    ) -> bool:
        """
        Transition to a new state with optimistic locking.

        When called outside a caller-managed transaction (``conn is None``), a
        version-conflict retries internally with jittered backoff so concurrent
        SMS on the same number don't drop a state update on the floor. When
        ``conn`` is supplied, retrying would just re-read the same snapshot
        inside the caller's transaction, so we attempt only once.

        Args:
            phone_number: Client's phone number
            new_state: Target state
            updates: Optional field updates to apply

        Returns:
            True if transition succeeded, False otherwise.
        """
        max_attempts = 1 if conn is not None else 4
        backoffs = [0.02, 0.05, 0.5]  # ~570ms total worst case
        last_conflict = False

        for attempt in range(max_attempts):
            outcome = self._transition_once(
                phone_number, new_state, updates, conn=conn, force=force
            )
            if outcome == "ok":
                return True
            if outcome != "conflict":
                # Non-retryable: missing state, invalid transition, or DB error.
                return False
            last_conflict = True
            if attempt < max_attempts - 1:
                base = backoffs[min(attempt, len(backoffs) - 1)]
                _time.sleep(base + random.uniform(0, base))

        if last_conflict:
            logger.error(
                "State transition for %s gave up after %d version conflicts",
                sanitize_log_value(phone_number),
                max_attempts,
            )
            log_quality_metric(
                "state_transition_conflict_giveup",
                phone_number=phone_number,
                attempts=max_attempts,
            )
        return False

    def _transition_once(
        self,
        phone_number: str,
        new_state: str,
        updates: dict[str, Any] | None,
        conn,
        force: bool,
    ) -> str:
        """Single attempt. Returns 'ok', 'conflict', or 'fail'."""
        if self.db is None:
            return "fail"
        try:
            # Get current version
            current = self.get_state(phone_number, conn=conn)
            if not current:
                logger.error("Cannot transition - no state exists for %s", sanitize_log_value(phone_number))
                return "fail"

            old_state = current.get("current_state", "NEW")
            allowed = VALID_STATE_TRANSITIONS.get(old_state)
            if not force and allowed is not None and new_state not in allowed:
                logger.warning(
                    "Rejected invalid state transition for %s: %s -> %s",
                    sanitize_log_value(phone_number),
                    sanitize_log_value(old_state),
                    sanitize_log_value(new_state),
                )
                log_quality_metric(
                    "state_transition_rejected",
                    phone_number=phone_number,
                    from_state=old_state,
                    to_state=new_state,
                    reason="invalid_transition",
                )
                return "fail"

            current_version = current['version']

            # Build update query
            set_clauses: list[Any] = [
                psy_sql.SQL("current_state = %s"),
                psy_sql.SQL("version = version + 1"),
                psy_sql.SQL("updated_at = CURRENT_TIMESTAMP"),
            ]
            params = [new_state]

            cache_updates: dict[str, Any] = {}
            if updates:
                for field, value in updates.items():
                    if field not in ALLOWED_STATE_UPDATE_FIELDS:
                        logger.debug("Skipping unknown state field: %s", sanitize_log_value(field))
                        continue
                    cache_updates[field] = value
                    if field in _JSON_STRING_FIELDS_FOR_JSONB:
                        set_clauses.append(psy_sql.SQL("{} = %s").format(psy_sql.Identifier(field)))
                        params.append(json.dumps(value))
                    else:
                        set_clauses.append(psy_sql.SQL("{} = %s").format(psy_sql.Identifier(field)))
                        params.append(_normalize_value_for_db(field, value))
                    if field == 'client_name' and value:
                        logger.info(f"Persisting client_name for {phone_number}: {value!r}")

            # Add WHERE clause for optimistic locking
            params.extend([phone_number, current_version])

            # Execute update with RETURNING version so we know exactly whether this
            # specific UPDATE landed (rowcount=0 means a concurrent write won the race).
            returning_query = psy_sql.SQL(
                "UPDATE conversation_states SET {} WHERE phone_number = %s AND version = %s RETURNING version"
            ).format(psy_sql.SQL(", ").join(set_clauses))

            rows = self.db.execute_query(returning_query, tuple(params), fetch=True, conn=conn)
            if not rows:
                logger.warning(
                    "Version conflict during transition for %s — UPDATE matched 0 rows",
                    sanitize_log_value(phone_number),
                )
                log_quality_metric(
                    "state_transition_conflict",
                    phone_number=phone_number,
                    from_state=old_state,
                    to_state=new_state,
                )
                return "conflict"

            logger.info(
                "State transition: %s -> %s",
                sanitize_log_value(phone_number),
                sanitize_log_value(new_state),
            )
            record_conversation_event(
                self.db,
                phone_number=phone_number,
                event_type="state_transition",
                from_state=old_state,
                to_state=new_state,
                metadata={"updates_applied": bool(updates)},
            )
            _sync_state_cache(
                phone_number,
                _merge_state_for_cache(current, new_state=new_state, updates=cache_updates),
                conn=conn,
            )
            return "ok"

        except Exception as e:
            logger.error("Failed to transition state for %s: %s", sanitize_log_value(phone_number), e)
            _raise_if_schema_error(e, "transition")
            log_quality_metric(
                "state_transition_error",
                phone_number=phone_number,
                to_state=new_state,
                error=type(e).__name__,
            )
            return "fail"

    def update_fields(
        self,
        phone_number: str,
        updates: dict[str, Any],
        conn=None,
    ) -> bool:
        """
        Update fields without changing state.

        Args:
            phone_number: Client's phone number
            updates: Fields to update

        Returns:
            True if update succeeded, False if version conflict
        """
        if self.db is None:
            return False
        max_attempts = 1 if conn is not None else 2
        for attempt in range(max_attempts):
            try:
                current = self.get_state(phone_number, conn=conn)
                if not current:
                    return False

                current_version = current['version']

                # Build update query (only allowed columns so client_name and others are always valid)
                set_clauses: list[Any] = [
                    psy_sql.SQL("version = version + 1"),
                    psy_sql.SQL("updated_at = CURRENT_TIMESTAMP"),
                ]
                params = []
                valid_update_applied = False
                cache_updates: dict[str, Any] = {}

                for field, value in updates.items():
                    if field not in ALLOWED_STATE_UPDATE_FIELDS:
                        logger.debug("Skipping unknown state field: %s", sanitize_log_value(field))
                        continue
                    valid_update_applied = True
                    cache_updates[field] = value
                    if field in _JSON_STRING_FIELDS_FOR_JSONB:
                        set_clauses.append(psy_sql.SQL("{} = %s").format(psy_sql.Identifier(field)))
                        params.append(json.dumps(value))
                    else:
                        set_clauses.append(psy_sql.SQL("{} = %s").format(psy_sql.Identifier(field)))
                        params.append(_normalize_value_for_db(field, value))
                    if field == 'client_name' and value:
                        logger.info(f"Persisting client_name for {phone_number}: {value!r}")

                # No valid fields to write: treat as success and avoid version churn.
                if not valid_update_applied:
                    return True

                params.extend([phone_number, current_version])

                query = psy_sql.SQL(
                    "UPDATE conversation_states SET {} WHERE phone_number = %s AND version = %s RETURNING version"
                ).format(psy_sql.SQL(", ").join(set_clauses))

                rows = self.db.execute_query(query, tuple(params), fetch=True, conn=conn)
                if rows:
                    _sync_state_cache(phone_number, _merge_state_for_cache(current, updates=cache_updates), conn=conn)
                    return True

                if attempt == 0 and conn is None:
                    logger.warning("Version conflict for %s, retrying...", sanitize_log_value(phone_number))
                    continue
                return False

            except Exception as e:
                logger.error("Failed to update fields for %s: %s", sanitize_log_value(phone_number), e)
                _raise_if_schema_error(e, "update_fields")
                return False
        return False

    def mark_awaiting_confirmation(
        self,
        phone_number: str,
        *,
        is_outcall: bool,
        deposit_required: bool,
        deposit_amount,
        deposit_reason,
        extra: dict[str, Any] | None = None,
        conn=None,
    ) -> bool:
        """Persist the canonical 'awaiting YES' flags + deposit state.

        Centralises the field set previously duplicated in
        ``_provide_field_stages_finish.py`` and
        ``availability_parts/main_flow.py`` — mutually-exclusive outcall/
        incall awaiting flags plus the deposit triple. Pass ``extra`` for
        call-site additions like ``client_name`` or
        ``auto_confirm_without_experience``.
        """
        if isinstance(deposit_reason, str):
            reason = deposit_reason.strip()
        elif deposit_reason is None:
            reason = ""
        else:
            reason = str(deposit_reason).strip()
        from utils.timezone import get_current_datetime
        _now = get_current_datetime()
        updates: dict[str, Any] = {
            "outcall_awaiting_yes": bool(is_outcall),
            "incall_awaiting_yes": not bool(is_outcall),
            "awaiting_yes_set_at": _now.isoformat(),
            "awaiting_yes_set_at_ts": _now,
            "deposit_required": bool(deposit_required),
            "deposit_amount": deposit_amount,
            "deposit_reason": reason,
        }
        if extra:
            updates.update(extra)
        return self.update_fields(phone_number, updates, conn=conn)

    def set_awaiting_yes_flags(
        self,
        phone_number: str,
        *,
        is_outcall: bool,
        extra_updates: dict[str, Any] | None = None,
        conn=None,
    ) -> bool:
        """Set canonical awaiting-YES flags with timestamp in one call."""
        from utils.timezone import get_current_datetime
        _now = get_current_datetime()
        updates: dict[str, Any] = {
            "outcall_awaiting_yes": bool(is_outcall),
            "incall_awaiting_yes": not bool(is_outcall),
            "awaiting_yes_set_at": _now.isoformat(),
            "awaiting_yes_set_at_ts": _now,
        }
        if extra_updates:
            updates.update(extra_updates)
        return self.update_fields(phone_number, updates, conn=conn)

    def _build_recent_history_for_summary(self, phone_number: str, limit: int = 12) -> list[dict[str, str]]:
        """Return recent message history formatted for the conversation summarizer."""
        if not self.db or not getattr(self.db, "database_url", None):
            return []
        try:
            rows = self.db.execute_query(
                """
                SELECT direction, message_body
                FROM message_history
                WHERE phone_number = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (phone_number, max(1, min(int(limit or 12), 50))),
                fetch=True,
            ) or []
            history: list[dict[str, str]] = []
            for row in reversed(rows):
                content = str((row or {}).get("message_body") or "").strip()
                if not content:
                    continue
                direction = str((row or {}).get("direction") or "").strip().lower()
                history.append({
                    "role": "assistant" if direction == "outbound" else "user",
                    "content": content,
                })
            return history
        except Exception as e:
            logger.warning("recent history fetch failed for %s: %s", phone_number, e)
            return []

    def append_booking_history(self, phone_number: str, booking_fields: dict, *, confirmed_at=None, deposit_paid: bool = False, total_cost: int | None = None) -> bool:
        """Append an immutable row to booking_history for analytics / returning-client context.

        Uses ON CONFLICT DO NOTHING so it is safe to call from multiple confirmation
        code paths without risk of duplicates (phone_number + confirmed_at is UNIQUE).
        """
        if not self.db:
            return False
        from datetime import timezone as _tz
        try:
            _at = confirmed_at
            if _at is None:
                from utils.timezone import get_current_datetime
                _at = get_current_datetime()
            if isinstance(_at, datetime) and _at.tzinfo is None:
                _at = _at.replace(tzinfo=_tz.utc)
            # Parse date/time fields tolerantly.
            _date = booking_fields.get('date')
            _time = booking_fields.get('time')
            if isinstance(_date, str) and _date:
                from datetime import date as _date_cls
                try:
                    _date = _date_cls.fromisoformat(_date)
                except ValueError:
                    _date = None
            if isinstance(_time, str) and _time:
                from datetime import time as _time_cls
                try:
                    _time = _time_cls.fromisoformat(_time.split('+')[0].strip())
                except ValueError:
                    _time = None
            self.db.execute_query(
                """
                INSERT INTO booking_history
                    (phone_number, confirmed_at, booking_date, booking_time,
                     duration, experience_type, incall_outcall, booking_type,
                     deposit_required, deposit_amount, deposit_paid,
                     total_booking_cost, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'chatbot')
                ON CONFLICT ON CONSTRAINT booking_history_unique_confirmation DO NOTHING
                """,
                (
                    phone_number,
                    _at,
                    _date or None,
                    _time or None,
                    booking_fields.get('duration'),
                    booking_fields.get('experience_type'),
                    booking_fields.get('incall_outcall'),
                    booking_fields.get('booking_type'),
                    bool(booking_fields.get('deposit_required') or booking_fields.get('deposit_amount')),
                    booking_fields.get('deposit_amount'),
                    deposit_paid,
                    total_cost,
                ),
            )
            if getattr(self.db, "database_url", None):
                try:
                    from services.ai_task_queue import enqueue_ai_task

                    enqueue_ai_task(
                        self.db,
                        task_type="extract_booking_memory",
                        payload={"phone_number": phone_number, "booking_data": booking_fields or {}},
                    )
                    history = self._build_recent_history_for_summary(phone_number)
                    if history:
                        enqueue_ai_task(
                            self.db,
                            task_type="summarize_conversation",
                            payload={"phone_number": phone_number, "history": history},
                        )
                except Exception as queue_err:
                    logger.warning("post-booking AI task enqueue failed for %s: %s", phone_number, queue_err)
            return True
        except Exception as e:
            logger.warning("append_booking_history failed for %s: %s", phone_number, e)
            return False

    def claim_confirmation_token_status(self, phone_number: str, token: str) -> str:
        """Atomically claim a confirmation token.

        Returns one of:
        - ``"claimed"``   → token was newly claimed
        - ``"duplicate"`` → token already claimed
        - ``"error"``     → DB failure while attempting to claim
        """
        if not self.db:
            return "claimed"
        try:
            rows = self.db.execute_query(
                """
                UPDATE conversation_states
                   SET confirmation_token = %s
                 WHERE phone_number = %s
                   AND confirmation_token IS NULL
                RETURNING phone_number
                """,
                (token, phone_number),
                fetch=True,
            )
            if rows:
                invalidate_cached_state(phone_number)
                return "claimed"
            invalidate_cached_state(phone_number)
            logger.warning(
                "Duplicate confirmation attempt for %s — token already claimed",
                phone_number,
            )
            return "duplicate"
        except Exception as e:
            logger.error(
                "claim_confirmation_token DB error for %s — blocking to prevent duplicate: %s",
                phone_number, e,
            )
            return "error"

    def claim_confirmation_token(self, phone_number: str, token: str) -> bool:
        """Backward-compatible bool wrapper around claim_confirmation_token_status()."""
        return self.claim_confirmation_token_status(phone_number, token) == "claimed"

    def release_confirmation_token(self, phone_number: str, token: str) -> bool:
        """Release a previously-claimed confirmation token when finalization fails."""
        if self.db is None:
            return False
        try:
            rows = self.db.execute_query(
                """
                UPDATE conversation_states
                   SET confirmation_token = NULL
                 WHERE phone_number = %s
                   AND confirmation_token = %s
                RETURNING phone_number
                """,
                (phone_number, token),
                fetch=True,
            )
            released = bool(rows)
            if released:
                invalidate_cached_state(phone_number)
            if not released:
                logger.warning(
                    "release_confirmation_token no-op for %s (token mismatch or missing row)",
                    phone_number,
                )
            return released
        except Exception as e:
            logger.error(
                "release_confirmation_token DB error for %s: %s",
                phone_number,
                e,
            )
            return False

    def touch(self, phone_number: str, conn=None) -> bool:
        """
        Update last_message_at timestamp.

        Args:
            phone_number: Client's phone number
            conn: Optional open connection (same transaction as other writes)

        Returns:
            True if successful
        """
        if self.db is None:
            return False
        try:
            self.db.execute_query(
                """UPDATE conversation_states
                   SET last_message_at = CURRENT_TIMESTAMP
                   WHERE phone_number = %s""",
                (phone_number,),
                fetch=False,
                conn=conn,
            )
            invalidate_cached_state(phone_number)
            return True
        except Exception as e:
            logger.error("Failed to touch state for %s: %s", sanitize_log_value(phone_number), e)
            _raise_if_schema_error(e, "touch")
            return False

    def is_blocked(self, phone_number: str) -> bool:
        """
        Check if phone number is blocked.

        Args:
            phone_number: Client's phone number

        Returns:
            True if blocked
        """
        if self.db is None:
            return False
        try:
            result = self.db.execute_query(
                """SELECT 1 FROM blocked_clients WHERE phone_number = %s""",
                (phone_number,),
                fetch=True
            )
            return bool(result)
        except Exception as e:
            logger.error(f"Failed to check blocked status: {e}")
            return False

    def block_client(self, phone_number: str, reason: str, notes: str = "") -> bool:
        """
        Block a client.

        Args:
            phone_number: Client's phone number
            reason: Reason for blocking
            notes: Additional notes

        Returns:
            True if successful
        """
        if self.db is None:
            return False
        try:
            self.db.execute_query(
                """INSERT INTO blocked_clients (phone_number, reason, notes)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (phone_number) DO UPDATE
                   SET reason = EXCLUDED.reason, notes = EXCLUDED.notes""",
                (phone_number, reason, notes)
            )
            logger.warning(
                "Blocked client %s: %s",
                sanitize_log_value(phone_number),
                sanitize_log_value(reason),
            )
            return True
        except Exception as e:
            logger.error("Failed to block client %s: %s", sanitize_log_value(phone_number), e)
            return False

    def log_message(
        self,
        phone_number: str,
        direction: str,
        message_body: str,
        media_urls: list[str] | None = None,
        intent: str | None = None,
        conn=None,
    ) -> bool:
        """
        Log message to history.

        Args:
            phone_number: Client's phone number
            direction: 'inbound' or 'outbound'
            message_body: Message text
            media_urls: List of media URLs
            intent: Classified intent

        Returns:
            True if successful
        """
        if self.db is None:
            return True
        try:
            # Ensure state exists before logging (foreign key constraint)
            state = self.get_state(phone_number, conn=conn)
            if not state:
                # Create state if it doesn't exist
                self.create_state(phone_number, "NEW", conn=conn)
                state = self.get_state(phone_number, conn=conn)
            
            current_state = state['current_state'] if state else 'NEW'

            self.db.execute_query(
                """INSERT INTO message_history
                   (phone_number, direction, message_body, media_urls, state_at_time, intent_classified)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (phone_number, direction, message_body, media_urls or [], current_state, intent),
                fetch=False,
                conn=conn,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to log message: {e}")
            _raise_if_schema_error(e, "log_message")
            return False

    def log_inbound_and_touch(
        self,
        phone_number: str,
        message_body: str,
        media_urls: list[str] | None = None,
        intent: str | None = None,
    ) -> bool:
        """
        Log inbound SMS and bump last_message_at in a single DB transaction.
        """
        if not self.db:
            try:
                self.log_message(phone_number, "inbound", message_body, media_urls, intent=intent)
                self.touch(phone_number)
                return True
            except Exception as e:
                logger.error("log_inbound_and_touch (no db): %s", e)
                return False
        try:
            with self.db.transaction() as conn:
                if not self.log_message(
                    phone_number, "inbound", message_body, media_urls, intent=intent, conn=conn
                ):
                    return False
                return self.touch(phone_number, conn=conn)
        except Exception as e:
            logger.error("log_inbound_and_touch failed for %s: %s", sanitize_log_value(phone_number), e)
            _raise_if_schema_error(e, "log_inbound_and_touch")
            return False

    def get_booking_fields(self, phone_number: str) -> dict[str, Any]:
        """
        Get booking fields from state.

        Args:
            phone_number: Client's phone number

        Returns:
            Dict with booking fields
        """
        state = self.get_state(phone_number)
        if not state:
            return {}

        return {
            'date': state.get('date'),
            'time': state.get('time'),
            'duration': state.get('duration'),
            'experience_type': state.get('experience_type'),
            'incall_outcall': state.get('incall_outcall'),
            'outcall_address': state.get('outcall_address'),
            'client_name': state.get('client_name'),
            'booking_type': state.get('booking_type'),
            'booking_status': state.get('booking_status'),
            'escort_supply_source': state.get('escort_supply_source'),
            'mmf_exploration_tags': state.get('mmf_exploration_tags'),
            'mmf_exploration_prompt_sent': state.get('mmf_exploration_prompt_sent'),
            'dinner_restaurant': state.get('dinner_restaurant'),
            'dinner_after_preference': state.get('dinner_after_preference'),
            'dinner_client_address': state.get('dinner_client_address'),
            'dinner_client_outside_15km': state.get('dinner_client_outside_15km'),
            '_verified_address': state.get('_verified_address'),
            '_verified_distance_km': state.get('_verified_distance_km'),
        }

    def clear_booking(self, phone_number: str) -> bool:
        """
        Clear booking fields and reset to NEW state.

        Args:
            phone_number: Client's phone number

        Returns:
            True if successful
        """
        try:
            # Administrative reset — always allowed (refuse deposit, cancel, 3-failed-attempts).
            # The DEPOSIT_REQUIRED->NEW path in VALID_STATE_TRANSITIONS is deliberately disallowed
            # so the dispatcher can't silently reclassify a pending-deposit client as NEW.
            return self.transition(
                phone_number,
                "NEW",
                force=True,
                updates={
                    'date': None,
                    'time': None,
                    'duration': None,
                    'experience_type': None,
                    'incall_outcall': None,
                    'outcall_address': None,
                    'client_name': None,
                    'missing_fields': ["date", "time", "duration"],
                    'deposit_required': False,
                    'deposit_amount': None,
                    'deposit_reason': None,
                    'deposit_requested_at': None,
                    'deposit_payment_reference': None,
                    'deposit_screenshot_attempts': 0,
                    'deposit_paid': False,
                    'profanity_count': 0,
                    'profanity_detected': False,
                    'unsafe_service_requested': False,
                    'peacock_event_id': None,
                    'graphite_event_id': None,
                    'confirmed_event_id': None,
                    'travel_outbound_event_id': None,
                    'travel_return_event_id': None,
                    'confirmed_at': None,
                    'total_booking_cost': None,
                    'post_booking_messages': 0,
                    'first_contact_sent': False,
                    'feedback_request_sent': False,
                    'available_now_requested': False,
                    'arrival_time_minutes': None,
                    'outcall_awaiting_yes': False,
                    'incall_awaiting_yes': False,
                    'awaiting_booking_change_cancel_choice': False,
                    'awaiting_yes_set_at': None,
                    'awaiting_name': False,
                    '_consecutive_same_response_count': 0,
                    'message_count': 0,
                    'booking_status': None,
                    'room_detail_reminder_scheduled': False,
                    'room_detail_reminder_sent': False,
                    'forward_incall_replies_to_escort': False,
                    'peacock_created_at': None,
                    'optional_deposit_requested': False,
                    'optional_deposit_paid': False,
                    'optional_deposit_amount': None,
                    'optional_deposit_paid_at': None,
                    'outcall_travel_notification_scheduled': False,
                    'outcall_travel_notification_sent': False,
                    'confirmation_30min_scheduled': False,
                    'confirmation_30min_sent': False,
                    'offered_slot_hours': None,
                    'offered_slot_minutes': None,
                    'offered_slot_date': None,
                    'offered_slot_dates': None,
                    'confirmation_token': None,
                    'booking_type': None,
                    'escort_supply_confirmed': False,
                    'escort_supply_source': None,
                    'dinner_restaurant': None,
                    'dinner_after_preference': None,
                    'dinner_client_address': None,
                    'dinner_client_outside_15km': False,
                    '_verified_address': None,
                    '_verified_distance_km': None,
                    'awaiting_refund_details': False,
                    'manual_review_required': False,
                    'bump_deposit_amount': None,
                    'calendar_yes_degraded': False,
                    'mmf_exploration_tags': None,
                    'mmf_exploration_prompt_sent': False,
                    'mmf_male_sourcing_escort_notified': False,
                    'awaiting_booking_change_cancel_choice': False,
                }
            )
        except Exception as e:
            logger.error("Failed to clear booking for %s: %s", sanitize_log_value(phone_number), e)
            _raise_if_schema_error(e, "clear_booking")
            return False

    def set_field(self, phone_number: str, field: str, value: Any) -> bool:
        """
        Set a single field (test compatibility method).
        
        Args:
            phone_number: Client's phone number
            field: Field name to set
            value: Value to set
        
        Returns:
            True if successful
        """
        return self.update_fields(phone_number, {field: value})

    def save_context(self, phone_number: str, context: dict[str, Any]) -> bool:
        """
        Save conversation context (test compatibility method).
        
        Args:
            phone_number: Client's phone number
            context: Context dict to save
        
        Returns:
            True if successful
        """
        return self.update_fields(phone_number, context)
