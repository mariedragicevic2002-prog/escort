-- ============================================================================
-- NEWBOT COMPLETE DATABASE SCHEMA
-- ============================================================================
-- Single consolidated schema; numbered migration files were merged into this file.
-- Safe to re-run on existing databases (CREATE IF NOT EXISTS + ADD COLUMN IF NOT EXISTS).
-- ============================================================================

-- ============================================================================
-- CONVERSATION_STATES — single source of truth (001 + 002 + 007–022)
-- ============================================================================

CREATE TABLE IF NOT EXISTS conversation_states (
    phone_number VARCHAR(20) PRIMARY KEY,
    current_state VARCHAR(25) NOT NULL CHECK (current_state IN
        ('NEW', 'COLLECTING', 'CHECKING_AVAILABILITY', 'DEPOSIT_REQUIRED', 'CONFIRMED', 'POST_BOOKING', 'EXTENDED_ENQUIRY', 'MANUAL_REVIEW_PENDING')),

    -- Booking fields (denormalized)
    date DATE,
    time TIME,
    duration INTEGER CHECK (duration IS NULL OR duration > 0),
    experience_type VARCHAR(50),
    incall_outcall VARCHAR(10) CHECK (incall_outcall IN ('incall', 'outcall')),
    outcall_address TEXT,
    client_name VARCHAR(100),

    missing_fields JSONB,
    first_contact_sent BOOLEAN DEFAULT FALSE,
    available_now_requested BOOLEAN DEFAULT FALSE,
    arrival_time_minutes INTEGER,
    message_count INTEGER DEFAULT 0,

    booking_status VARCHAR(50),
    booking_type VARCHAR(50),
    bump_deposit_amount DECIMAL(10,2),
    profanity_count INTEGER DEFAULT 0,
    profanity_detected BOOLEAN DEFAULT FALSE,
    unsafe_service_requested BOOLEAN DEFAULT FALSE,
    incall_awaiting_yes BOOLEAN DEFAULT FALSE,
    outcall_awaiting_yes BOOLEAN DEFAULT FALSE,
    awaiting_name BOOLEAN DEFAULT FALSE,
    awaiting_refund_details BOOLEAN DEFAULT FALSE,
    manual_review_required BOOLEAN DEFAULT FALSE,
    optional_deposit_requested BOOLEAN DEFAULT FALSE,
    optional_deposit_paid BOOLEAN DEFAULT FALSE,
    optional_deposit_paid_at TIMESTAMP,

    deposit_required BOOLEAN DEFAULT FALSE,
    deposit_amount DECIMAL(10,2),
    deposit_reason VARCHAR(100),
    deposit_payment_reference VARCHAR(20),
    deposit_requested_at TIMESTAMP,
    deposit_screenshot_attempts INTEGER DEFAULT 0,
    deposit_paid BOOLEAN DEFAULT FALSE,
    optional_deposit_amount INTEGER,

    peacock_event_id VARCHAR(100),
    confirmed_event_id VARCHAR(100),
    travel_outbound_event_id VARCHAR(100),
    travel_return_event_id VARCHAR(100),
    graphite_event_id VARCHAR(100),
    confirmed_at TIMESTAMP,
    confirmation_token TEXT,

    post_booking_messages INTEGER DEFAULT 0,

    room_detail_reminder_scheduled TIMESTAMP,
    room_detail_reminder_sent BOOLEAN DEFAULT FALSE,
    forward_incall_replies_to_escort BOOLEAN DEFAULT FALSE,

    tour_sms_subscription BOOLEAN DEFAULT FALSE,
    tour_subscription_city VARCHAR(50),
    tour_subscribed_at TIMESTAMP,
    last_touring_inquiry_city VARCHAR(50),

    offered_slot_hours JSONB,
    offered_slot_minutes JSONB,
    offered_slot_date DATE,

    dinner_restaurant TEXT,
    dinner_after_preference VARCHAR(20),
    dinner_client_address TEXT,
    dinner_client_outside_15km BOOLEAN DEFAULT FALSE,
    _verified_address TEXT,
    _verified_distance_km DOUBLE PRECISION,

    reminder_24h_scheduled TIMESTAMP,
    reminder_2h_scheduled TIMESTAMP,
    reminder_24h_sent BOOLEAN DEFAULT FALSE,
    reminder_2h_sent BOOLEAN DEFAULT FALSE,
    outcall_travel_notification_scheduled TIMESTAMP,
    outcall_travel_notification_sent BOOLEAN DEFAULT FALSE,
    client_notes TEXT,
    peacock_created_at TIMESTAMP,
    confirmation_30min_scheduled TIMESTAMP,
    confirmation_30min_sent BOOLEAN DEFAULT FALSE,
    total_booking_cost INTEGER,
    feedback_request_sent BOOLEAN DEFAULT FALSE,
    earliest_slot_auto_selected BOOLEAN DEFAULT FALSE,
    price INTEGER,
    special_requests TEXT,

    confirmed_ai_reply_count INTEGER DEFAULT 0,
    calendar_yes_degraded BOOLEAN DEFAULT FALSE,

    escort_supply_source VARCHAR(20),
    escort_supply_confirmed BOOLEAN DEFAULT FALSE,
    offered_slot_dates JSONB,

    mmf_exploration_tags TEXT,
    mmf_exploration_prompt_sent BOOLEAN DEFAULT FALSE,
    mmf_male_sourcing_escort_notified BOOLEAN DEFAULT FALSE,

    -- Router / UX tracking (see core.state_manager.ALLOWED_STATE_UPDATE_FIELDS)
    awaiting_yes_set_at TEXT,
    frustration_reply_sent BOOLEAN DEFAULT FALSE,
    _consecutive_same_response_count INTEGER DEFAULT 0,
    flow_version VARCHAR(10) DEFAULT 'v1',
    doubles_type VARCHAR(10),

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Notification tracking for deposit_followup_service and checkin_service
    deposit_followup_sent_at TIMESTAMPTZ DEFAULT NULL,
    checkin_sms_sent_at      TIMESTAMPTZ DEFAULT NULL,

    version INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_conversation_state ON conversation_states(current_state);
CREATE INDEX IF NOT EXISTS idx_conversation_updated ON conversation_states(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_last_message ON conversation_states(last_message_at DESC);

CREATE INDEX IF NOT EXISTS idx_conversation_tour_subscription
ON conversation_states(tour_sms_subscription, tour_subscription_city)
WHERE tour_sms_subscription = TRUE;

-- ============================================================================
-- MESSAGE_HISTORY TABLE - Audit trail
-- ============================================================================

CREATE TABLE IF NOT EXISTS message_history (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    direction VARCHAR(10) NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    message_body TEXT NOT NULL,
    media_urls TEXT[],
    state_at_time VARCHAR(25),
    intent_classified VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (phone_number) REFERENCES conversation_states(phone_number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_message_history_phone ON message_history(phone_number, created_at DESC);

-- ============================================================================
-- BLOCKED_CLIENTS TABLE - Client blocking
-- ============================================================================

CREATE TABLE IF NOT EXISTS blocked_clients (
    phone_number VARCHAR(20) PRIMARY KEY,
    reason VARCHAR(50) NOT NULL,
    blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_blocked_clients_reason ON blocked_clients(reason);

-- ============================================================================
-- SAFETY_SCREENING TABLES - Flagged client number screening
-- ============================================================================

CREATE TABLE IF NOT EXISTS safety_screening_watchlist (
    id SERIAL PRIMARY KEY,
    normalized_phone VARCHAR(20) NOT NULL UNIQUE,
    raw_phone VARCHAR(40),
    source_label VARCHAR(64) DEFAULT 'config_excel_upload',
    is_active BOOLEAN DEFAULT TRUE,
    warning_recency_rank INTEGER,
    report_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_safety_watchlist_active
ON safety_screening_watchlist (is_active, normalized_phone);

CREATE TABLE IF NOT EXISTS safety_screening_match_log (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20),
    normalized_phone VARCHAR(20),
    matched BOOLEAN DEFAULT FALSE,
    action_taken VARCHAR(20) DEFAULT 'warn_only',
    escort_notified BOOLEAN DEFAULT FALSE,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_safety_match_log_phone
ON safety_screening_match_log (normalized_phone, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_safety_match_log_notified
ON safety_screening_match_log (escort_notified, created_at DESC);

-- ============================================================================
-- INCALL_LOCATIONS TABLE - Current location info
-- ============================================================================

CREATE TABLE IF NOT EXISTS incall_locations (
    id SERIAL PRIMARY KEY,
    city TEXT NOT NULL,
    address TEXT NOT NULL,
    intercom_number TEXT,
    timezone TEXT NOT NULL DEFAULT 'Australia/Adelaide',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_locations_updated ON incall_locations(updated_at DESC);

-- ============================================================================
-- ADMIN_SETTINGS TABLE - Admin configuration
-- ============================================================================

CREATE TABLE IF NOT EXISTS admin_settings (
    id SERIAL PRIMARY KEY,
    setting_key VARCHAR(50) NOT NULL UNIQUE,
    setting_value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_settings_key ON admin_settings(setting_key);



-- ============================================================================
-- 004_webform_tokens.sql
-- ============================================================================

-- Migration 004: Webform tokens for secure booking links
-- Creates tables for booking form token security

-- Webform tokens for booking form access
CREATE TABLE IF NOT EXISTS webform_tokens (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    short_code VARCHAR(6) UNIQUE,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    use_count INTEGER DEFAULT 0
);

-- Experience tokens for experience guide page
CREATE TABLE IF NOT EXISTS experience_tokens (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    short_code VARCHAR(6) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL
);

-- Add indexes for performance
CREATE INDEX IF NOT EXISTS idx_webform_tokens_phone ON webform_tokens(phone_number);
CREATE INDEX IF NOT EXISTS idx_webform_tokens_expires ON webform_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_experience_tokens_expires ON experience_tokens(expires_at);



-- ============================================================================
-- 005_admin_features.sql
-- ============================================================================

-- Migration: Add admin feature tables
-- Date: 2025-02-08
-- Description: Add tables for admin phone numbers and activity logging

-- Admin phone numbers table
CREATE TABLE IF NOT EXISTS admin_phones (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Admin activity logs table
CREATE TABLE IF NOT EXISTS admin_activity_logs (
    id SERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    details TEXT,
    success BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Create indices for performance
CREATE INDEX IF NOT EXISTS idx_admin_activity_logs_created_at ON admin_activity_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_phones_phone_number ON admin_phones(phone_number);



-- ============================================================================
-- 006_restore_features.sql
-- ============================================================================

-- Migration 006: Restore old chatbot features
-- Date: 2026-02-10
-- Description: Add tables for deposit uploads, client notes, and reschedule tracking

-- Upload tokens for deposit screenshot uploads
CREATE TABLE IF NOT EXISTS upload_tokens (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    short_code VARCHAR(6) NOT NULL UNIQUE,
    token_hash VARCHAR(64) NOT NULL DEFAULT '',
    deposit_amount INTEGER NOT NULL DEFAULT 100,
    payment_reference VARCHAR(20),
    created_at TIMESTAMP DEFAULT NOW(),
    used BOOLEAN DEFAULT FALSE,
    used_at TIMESTAMP,
    upload_attempts INTEGER DEFAULT 0
);

-- Client notes for admin database page
CREATE TABLE IF NOT EXISTS client_notes (
    phone_number VARCHAR(20) PRIMARY KEY,
    notes TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Pending reschedules for schedule management
CREATE TABLE IF NOT EXISTS pending_reschedules (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(255) NOT NULL,
    phone_number VARCHAR(20) NOT NULL,
    original_time TEXT NOT NULL,
    new_date DATE NOT NULL,
    new_time TIME NOT NULL,
    requested_at TIMESTAMP DEFAULT NOW(),
    confirmed BOOLEAN DEFAULT FALSE,
    confirmed_at TIMESTAMP
);

-- Add indexes for performance
CREATE INDEX IF NOT EXISTS idx_upload_tokens_short_code ON upload_tokens(short_code);
CREATE INDEX IF NOT EXISTS idx_upload_tokens_phone_number ON upload_tokens(phone_number);
CREATE INDEX IF NOT EXISTS idx_upload_tokens_created ON upload_tokens(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_upload_tokens_payment_reference ON upload_tokens(payment_reference);
CREATE INDEX IF NOT EXISTS idx_client_notes_updated ON client_notes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pending_reschedules_requested ON pending_reschedules(requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_pending_reschedules_phone ON pending_reschedules(phone_number);
CREATE INDEX IF NOT EXISTS idx_pending_reschedules_event_id ON pending_reschedules(event_id);
CREATE INDEX IF NOT EXISTS idx_pending_reschedules_confirmed ON pending_reschedules(confirmed);

-- Add upload_attempts column to upload_tokens if it doesn't exist (for backwards compatibility)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='upload_tokens' AND column_name='upload_attempts'
    ) THEN
        ALTER TABLE upload_tokens ADD COLUMN upload_attempts INTEGER DEFAULT 0;
    END IF;
END $$;

-- Required by core.deposit_upload_tokens INSERT (matches main_v2/database startup migration)
ALTER TABLE upload_tokens
    ADD COLUMN IF NOT EXISTS token_hash VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE upload_tokens
    ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(20);


-- ============================================================================
-- Alerts + admin audit (utils/alerts.py, utils/admin_audit.py) — not gated on RUN_STARTUP_DB_MIGRATIONS
-- ============================================================================

CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    component VARCHAR(50) NOT NULL,
    message TEXT,
    severity VARCHAR(20) DEFAULT 'warning',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id SERIAL PRIMARY KEY,
    action VARCHAR(100) NOT NULL,
    details TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_log_created ON admin_audit_log(created_at DESC);


-- ============================================================================
-- 007_improvements.sql — tables, indexes, functions (conversation_states cols merged above)
-- ============================================================================

-- Migration 007: Improvements and Enhancements

CREATE TABLE IF NOT EXISTS client_feedback (
    id SERIAL PRIMARY KEY,
    client_phone_number VARCHAR(20) NOT NULL,
    client_name VARCHAR(100),
    booking_date DATE,
    booking_time TIME,
    duration INTEGER,
    experience_type VARCHAR(50),
    incall_outcall VARCHAR(10),
    arrived_on_time BOOLEAN,
    was_respectful BOOLEAN,
    would_see_again BOOLEAN,
    star_rating SMALLINT,
    comments TEXT,
    feedback_received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feedback_pending (
    id SERIAL PRIMARY KEY,
    client_phone_number VARCHAR(20) NOT NULL,
    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_client_feedback_phone ON client_feedback(client_phone_number);
CREATE INDEX IF NOT EXISTS idx_client_feedback_received ON client_feedback(feedback_received_at DESC);

-- Add indexes for reminder queries
CREATE INDEX IF NOT EXISTS idx_conversation_reminders 
ON conversation_states(reminder_24h_scheduled, reminder_2h_scheduled) 
WHERE current_state = 'CONFIRMED';

CREATE INDEX IF NOT EXISTS idx_conversation_notes 
ON conversation_states(phone_number) 
WHERE client_notes IS NOT NULL;

-- Add index for message history intent analysis
CREATE INDEX IF NOT EXISTS idx_message_history_intent 
ON message_history(intent_classified) 
WHERE intent_classified IS NOT NULL;

-- Create client preferences table (optional, can use JSONB in conversation_states)
CREATE TABLE IF NOT EXISTS client_preferences (
    phone_number VARCHAR(20) PRIMARY KEY,
    preferred_duration INTEGER,
    preferred_experience VARCHAR(10),
    preferred_location VARCHAR(10),
    total_bookings INTEGER DEFAULT 0,
    last_booking_date DATE,
    vip_status BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (phone_number) REFERENCES conversation_states(phone_number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_client_preferences_vip 
ON client_preferences(vip_status) 
WHERE vip_status = TRUE;

-- Add rate limiting tracking table
CREATE TABLE IF NOT EXISTS rate_limit_tracking (
    phone_number VARCHAR(20) PRIMARY KEY,
    message_count INTEGER DEFAULT 0,
    last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    warnings INTEGER DEFAULT 0,
    cooldown_until TIMESTAMP,
    FOREIGN KEY (phone_number) REFERENCES conversation_states(phone_number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_cooldown 
ON rate_limit_tracking(cooldown_until) 
WHERE cooldown_until IS NOT NULL;

-- Add analytics tracking table
CREATE TABLE IF NOT EXISTS booking_analytics (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20),
    event_type VARCHAR(50) NOT NULL,  -- 'booking_started', 'booking_completed', 'booking_cancelled', etc.
    state_from VARCHAR(25),
    state_to VARCHAR(25),
    booking_fields JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (phone_number) REFERENCES conversation_states(phone_number) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_analytics_event_type 
ON booking_analytics(event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_analytics_phone 
ON booking_analytics(phone_number, created_at DESC);

-- Add function to update client preferences automatically
CREATE OR REPLACE FUNCTION update_client_preferences()
RETURNS TRIGGER AS $$
BEGIN
    -- Update preferences when booking is confirmed
    IF NEW.current_state = 'CONFIRMED' AND OLD.current_state != 'CONFIRMED' THEN
        INSERT INTO client_preferences (
            phone_number,
            preferred_duration,
            preferred_experience,
            preferred_location,
            total_bookings,
            last_booking_date,
            updated_at
        )
        VALUES (
            NEW.phone_number,
            NEW.duration,
            NEW.experience_type,
            NEW.incall_outcall,
            1,
            NEW.date,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (phone_number) DO UPDATE SET
            preferred_duration = COALESCE(client_preferences.preferred_duration, NEW.duration),
            preferred_experience = COALESCE(client_preferences.preferred_experience, NEW.experience_type),
            preferred_location = COALESCE(client_preferences.preferred_location, NEW.incall_outcall),
            total_bookings = client_preferences.total_bookings + 1,
            last_booking_date = NEW.date,
            updated_at = CURRENT_TIMESTAMP;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger for automatic preference updates
DROP TRIGGER IF EXISTS trigger_update_client_preferences ON conversation_states;
CREATE TRIGGER trigger_update_client_preferences
    AFTER UPDATE ON conversation_states
    FOR EACH ROW
    EXECUTE FUNCTION update_client_preferences();

-- Add function to clean old rate limit data
CREATE OR REPLACE FUNCTION clean_old_rate_limit_data()
RETURNS void AS $$
BEGIN
    DELETE FROM rate_limit_tracking
    WHERE cooldown_until IS NOT NULL 
      AND cooldown_until < CURRENT_TIMESTAMP - INTERVAL '24 hours'
      AND warnings = 0;
END;
$$ LANGUAGE plpgsql;

-- Add function to clean old analytics data (keep last 90 days)
CREATE OR REPLACE FUNCTION clean_old_analytics()
RETURNS void AS $$
BEGIN
    DELETE FROM booking_analytics
    WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '90 days';
END;
$$ LANGUAGE plpgsql;

-- Clean old message history rows (default retention 90 days)
CREATE OR REPLACE FUNCTION clean_old_message_history(keep_days INTEGER DEFAULT 90)
RETURNS void AS $$
BEGIN
    DELETE FROM message_history
    WHERE created_at < CURRENT_TIMESTAMP - (GREATEST(1, keep_days)::text || ' days')::interval;
END;
$$ LANGUAGE plpgsql;



-- ============================================================================
-- 008_analytics_indexes.sql
-- ============================================================================

-- Migration 008: Analytics Indexes
-- Add indexes for better query performance on analytics queries

-- Index for state transitions analytics
CREATE INDEX IF NOT EXISTS idx_conversation_state_updated 
ON conversation_states(updated_at DESC, current_state);

-- Index for confirmed bookings by confirmed_at only (time-series)
CREATE INDEX IF NOT EXISTS idx_conversation_confirmed_at_time
ON conversation_states(confirmed_at DESC)
WHERE confirmed_at IS NOT NULL;

-- Index for deposit analytics
CREATE INDEX IF NOT EXISTS idx_conversation_deposit 
ON conversation_states(deposit_required, deposit_paid, updated_at DESC)
WHERE deposit_required = TRUE;

-- Index for client analytics (phone number + confirmed date)
CREATE INDEX IF NOT EXISTS idx_conversation_phone_confirmed 
ON conversation_states(phone_number, confirmed_at DESC)
WHERE confirmed_at IS NOT NULL;

-- Index for message history analytics
CREATE INDEX IF NOT EXISTS idx_message_history_created 
ON message_history(created_at DESC, intent_classified);

-- ============================================================================
-- 014_booking_date_index.sql
-- ============================================================================
-- Lookups by booking calendar date (schedule merge, financial fallback, admin)
CREATE INDEX IF NOT EXISTS idx_conversation_booking_date
ON conversation_states (date ASC)
WHERE date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_client_feedback_booking_date
ON client_feedback (booking_date ASC)
WHERE booking_date IS NOT NULL;


-- ============================================================================
-- 015_link_tokens.sql
-- ============================================================================
-- HMAC link tokens for one-time-use tracking

CREATE TABLE IF NOT EXISTS link_tokens (
    id SERIAL PRIMARY KEY,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    gateway VARCHAR(32) NOT NULL,
    used BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_link_tokens_hash ON link_tokens (token_hash);
CREATE INDEX IF NOT EXISTS idx_link_tokens_cleanup ON link_tokens (created_at) WHERE used = TRUE;


-- ============================================================================
-- 009_error_handling.sql
-- ============================================================================

-- Migration 009: Error Handling & Circuit Breaker Tracking
-- Add tables for tracking errors and circuit breaker states

-- Error log table
CREATE TABLE IF NOT EXISTS error_log (
    id SERIAL PRIMARY KEY,
    service_name VARCHAR(100) NOT NULL,
    error_type VARCHAR(100) NOT NULL,
    error_message TEXT,
    context JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_error_log_service ON error_log(service_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_error_log_unresolved ON error_log(resolved, created_at DESC) WHERE resolved = FALSE;

-- Circuit breaker state tracking
CREATE TABLE IF NOT EXISTS circuit_breaker_states (
    id SERIAL PRIMARY KEY,
    circuit_name VARCHAR(100) UNIQUE NOT NULL,
    state VARCHAR(20) NOT NULL,  -- closed, open, half_open
    failure_count INTEGER DEFAULT 0,
    last_failure_time TIMESTAMP,
    last_state_change TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    recovery_timeout INTEGER DEFAULT 60,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_circuit_breaker_name ON circuit_breaker_states(circuit_name);
CREATE INDEX IF NOT EXISTS idx_circuit_breaker_state ON circuit_breaker_states(state, updated_at DESC);

-- Dead letter queue for failed operations
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id SERIAL PRIMARY KEY,
    operation_type VARCHAR(100) NOT NULL,
    operation_data JSONB NOT NULL,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_retry_at TIMESTAMP,
    processed BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_dlq_unprocessed ON dead_letter_queue(processed, created_at DESC) WHERE processed = FALSE;
CREATE INDEX IF NOT EXISTS idx_dlq_operation_type ON dead_letter_queue(operation_type, created_at DESC);

-- AI task queue for deferred non-critical AI workloads
CREATE TABLE IF NOT EXISTS ai_task_queue (
    id BIGSERIAL PRIMARY KEY,
    task_type VARCHAR(80) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_task_queue_status_created
ON ai_task_queue(status, created_at ASC);

-- Semantic memory storage (structured + embedding-ready payloads)
CREATE TABLE IF NOT EXISTS semantic_memory (
    id BIGSERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    memory_type VARCHAR(50) NOT NULL,
    memory_text TEXT NOT NULL,
    embedding_payload JSONB,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_semantic_memory_phone_created
ON semantic_memory(phone_number, created_at DESC);

-- Append-only conversation telemetry for event-sourced analytics
CREATE TABLE IF NOT EXISTS conversation_events (
    id BIGSERIAL PRIMARY KEY,
    phone_number VARCHAR(20),
    event_type VARCHAR(80) NOT NULL,
    from_state VARCHAR(40),
    to_state VARCHAR(40),
    intent VARCHAR(80),
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversation_events_phone_created
ON conversation_events(phone_number, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_events_type_created
ON conversation_events(event_type, created_at DESC);

-- Refactor runtime transactional outbox for worker-driven side effects
CREATE TABLE IF NOT EXISTS refactor_outbox_events (
    event_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_type VARCHAR(120) NOT NULL,
    aggregate_type VARCHAR(80) NOT NULL,
    aggregate_id VARCHAR(80) NOT NULL,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'failed', 'dead_letter', 'published')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5 CHECK (max_retries >= 0),
    next_retry_at TIMESTAMPTZ,
    processing_started_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_refactor_outbox_status_retry
ON refactor_outbox_events(status, next_retry_at, created_at);
CREATE INDEX IF NOT EXISTS idx_refactor_outbox_aggregate
ON refactor_outbox_events(aggregate_type, aggregate_id, created_at DESC);

-- Refactor inbound durable queue for queue-first ingress
CREATE TABLE IF NOT EXISTS refactor_inbound_queue_messages (
    message_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'retry', 'dead', 'sent')),
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),
    next_attempt_at TIMESTAMPTZ,
    processing_started_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_refactor_inbound_queue_status_retry
ON refactor_inbound_queue_messages(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_refactor_inbound_queue_dedup_created
ON refactor_inbound_queue_messages(dedup_key, created_at DESC);

CREATE TABLE IF NOT EXISTS refactor_inbound_worker_guard (
    message_id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_refactor_inbound_worker_guard_processed
ON refactor_inbound_worker_guard(processed_at DESC);

-- httpSMS inbound idempotency (INSERT ... ON CONFLICT)
CREATE TABLE IF NOT EXISTS httpsms_message_dedup (
    message_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_httpsms_message_dedup_created ON httpsms_message_dedup (created_at DESC);


-- ============================================================================
-- Legacy column upgrades (conversation_states)
-- For databases created before columns were merged into CREATE TABLE above.
-- ============================================================================

ALTER TABLE conversation_states
    ADD COLUMN IF NOT EXISTS available_now_requested BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS arrival_time_minutes INTEGER,
    ADD COLUMN IF NOT EXISTS message_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS booking_status VARCHAR(50),
    ADD COLUMN IF NOT EXISTS booking_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS bump_deposit_amount DECIMAL(10,2),
    ADD COLUMN IF NOT EXISTS profanity_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS profanity_detected BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS unsafe_service_requested BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS incall_awaiting_yes BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS outcall_awaiting_yes BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS awaiting_name BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS awaiting_refund_details BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS manual_review_required BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS optional_deposit_requested BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS optional_deposit_paid BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS optional_deposit_paid_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS deposit_payment_reference VARCHAR(20),
    ADD COLUMN IF NOT EXISTS graphite_event_id VARCHAR(100),
    ADD COLUMN IF NOT EXISTS confirmation_token TEXT,
    ADD COLUMN IF NOT EXISTS optional_deposit_amount INTEGER,
    ADD COLUMN IF NOT EXISTS tour_sms_subscription BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS tour_subscription_city VARCHAR(50),
    ADD COLUMN IF NOT EXISTS tour_subscribed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_touring_inquiry_city VARCHAR(50),
    ADD COLUMN IF NOT EXISTS offered_slot_hours JSONB,
    ADD COLUMN IF NOT EXISTS offered_slot_minutes JSONB,
    ADD COLUMN IF NOT EXISTS offered_slot_date DATE,
    ADD COLUMN IF NOT EXISTS dinner_restaurant TEXT,
    ADD COLUMN IF NOT EXISTS dinner_after_preference VARCHAR(20),
    ADD COLUMN IF NOT EXISTS dinner_client_address TEXT,
    ADD COLUMN IF NOT EXISTS dinner_client_outside_15km BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS _verified_address TEXT,
    ADD COLUMN IF NOT EXISTS _verified_distance_km DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS reminder_24h_scheduled TIMESTAMP,
    ADD COLUMN IF NOT EXISTS reminder_2h_scheduled TIMESTAMP,
    ADD COLUMN IF NOT EXISTS reminder_24h_sent BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS reminder_2h_sent BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS outcall_travel_notification_scheduled TIMESTAMP,
    ADD COLUMN IF NOT EXISTS outcall_travel_notification_sent BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS client_notes TEXT,
    ADD COLUMN IF NOT EXISTS peacock_created_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS confirmation_30min_scheduled TIMESTAMP,
    ADD COLUMN IF NOT EXISTS confirmation_30min_sent BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS total_booking_cost INTEGER,
    ADD COLUMN IF NOT EXISTS feedback_request_sent BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS forward_incall_replies_to_escort BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS earliest_slot_auto_selected BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS price INTEGER,
    ADD COLUMN IF NOT EXISTS special_requests TEXT,
    ADD COLUMN IF NOT EXISTS confirmed_ai_reply_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS calendar_yes_degraded BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS escort_supply_source VARCHAR(20),
    ADD COLUMN IF NOT EXISTS escort_supply_confirmed BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS offered_slot_dates JSONB,
    ADD COLUMN IF NOT EXISTS mmf_exploration_tags TEXT,
    ADD COLUMN IF NOT EXISTS mmf_exploration_prompt_sent BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS mmf_male_sourcing_escort_notified BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS awaiting_yes_set_at TEXT,
    ADD COLUMN IF NOT EXISTS frustration_reply_sent BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS _consecutive_same_response_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS flow_version VARCHAR(10) DEFAULT 'v1',
    ADD COLUMN IF NOT EXISTS doubles_type VARCHAR(10);

-- ============================================================================
-- current_state CHECK alignment (FSM parity)
-- Ensure DB accepts all runtime FSM states, including EXTENDED_ENQUIRY and
-- MANUAL_REVIEW_PENDING.
-- ============================================================================

DO $$
DECLARE
    _con record;
BEGIN
    FOR _con IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid
        WHERE c.contype = 'c'
          AND t.relname = 'conversation_states'
          AND n.nspname = current_schema()
          AND a.attname = 'current_state'
          AND a.attnum = ANY (c.conkey)
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.%I DROP CONSTRAINT IF EXISTS %I',
            current_schema(),
            'conversation_states',
            _con.conname
        );
    END LOOP;
END $$;

ALTER TABLE conversation_states
    ADD CONSTRAINT chk_conversation_states_current_state
    CHECK (
        current_state IN (
            'NEW',
            'COLLECTING',
            'CHECKING_AVAILABILITY',
            'DEPOSIT_REQUIRED',
            'CONFIRMED',
            'POST_BOOKING',
            'EXTENDED_ENQUIRY',
            'MANUAL_REVIEW_PENDING'
        )
    );


-- ============================================================================
-- Duration CHECK (021) — normalize legacy zero durations + named constraint
-- ============================================================================

UPDATE conversation_states SET duration = NULL WHERE duration = 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_conversation_states_duration_positive'
    ) THEN
        ALTER TABLE conversation_states
        ADD CONSTRAINT chk_conversation_states_duration_positive
        CHECK (duration IS NULL OR duration > 0);
    END IF;
END $$;


-- ============================================================================
-- Confirmation token uniqueness (022) — run after column exists on legacy DBs
-- ============================================================================

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_states_confirmation_token
ON conversation_states (confirmation_token)
WHERE confirmation_token IS NOT NULL;


-- ============================================================================
-- Booking history — durable append-only confirmation log (023)
-- Every confirmed booking writes one immutable row here so returning-client
-- personalisation and analytics have a reliable history source independent
-- of the mutable conversation_states row.
-- ============================================================================

CREATE TABLE IF NOT EXISTS booking_history (
    id BIGSERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    confirmed_at TIMESTAMPTZ NOT NULL,
    booking_date DATE,
    booking_time TIME,
    duration INTEGER,
    experience_type VARCHAR(50),
    incall_outcall VARCHAR(10),
    booking_type VARCHAR(30),
    deposit_required BOOLEAN DEFAULT FALSE,
    deposit_amount INTEGER,
    deposit_paid BOOLEAN DEFAULT FALSE,
    total_booking_cost INTEGER,
    source VARCHAR(20) DEFAULT 'chatbot',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT booking_history_unique_confirmation UNIQUE (phone_number, confirmed_at)
);

CREATE INDEX IF NOT EXISTS idx_booking_history_phone
    ON booking_history (phone_number, confirmed_at DESC);

CREATE INDEX IF NOT EXISTS idx_booking_history_confirmed
    ON booking_history (confirmed_at DESC);


-- ============================================================================
-- awaiting_yes_set_at typed column (024) — dual-write safe migration
-- Adds TIMESTAMPTZ companion and backfills from the legacy TEXT column.
-- The TEXT column is kept for backwards compatibility; code writes both and
-- reads the typed column first.
-- ============================================================================

ALTER TABLE conversation_states
    ADD COLUMN IF NOT EXISTS awaiting_yes_set_at_ts TIMESTAMPTZ;

-- Backfill from TEXT column, skipping rows with non-parseable values.
UPDATE conversation_states
   SET awaiting_yes_set_at_ts = awaiting_yes_set_at::TIMESTAMPTZ
 WHERE awaiting_yes_set_at IS NOT NULL
   AND awaiting_yes_set_at ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}'
   AND awaiting_yes_set_at_ts IS NULL;

-- ============================================================================
-- BOOKINGS TABLE — primary booking store for the mobile APK (DB-backed)
-- Replaces Google Calendar as the source of truth for bookings.
-- ============================================================================

CREATE TABLE IF NOT EXISTS bookings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    client_name VARCHAR(200) NOT NULL DEFAULT '',
    phone VARCHAR(20) NOT NULL DEFAULT '',
    duration VARCHAR(50) NOT NULL DEFAULT '',
    type VARCHAR(20) NOT NULL DEFAULT 'incall',
    experience VARCHAR(100) NOT NULL DEFAULT '',
    preferences TEXT[] DEFAULT '{}',
    deposit_status VARCHAR(30) DEFAULT 'not_required',
    deposit_amount NUMERIC(10,2) DEFAULT 0,
    deposit_reference VARCHAR(200) DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'reserved',
    special_requests TEXT,
    organise_other_escort BOOLEAN DEFAULT FALSE,
    notes TEXT,
    price_total NUMERIC(10,2),
    remaining_amount NUMERIC(10,2),
    outcall_address TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ADD COLUMN IF NOT EXISTS guards — safe to re-run on legacy tables that were created
-- from an older schema (e.g. phone_number/date/time columns instead of start_time/end_time).
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS start_time TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS end_time TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS client_name VARCHAR(200) NOT NULL DEFAULT '';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS phone VARCHAR(20) NOT NULL DEFAULT '';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS duration VARCHAR(50) NOT NULL DEFAULT '';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS type VARCHAR(20) NOT NULL DEFAULT 'incall';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS experience VARCHAR(100) NOT NULL DEFAULT '';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS preferences TEXT[] DEFAULT '{}';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS deposit_status VARCHAR(30) DEFAULT 'not_required';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS deposit_amount NUMERIC(10,2) DEFAULT 0;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS deposit_reference VARCHAR(200) DEFAULT '';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS status VARCHAR(30) NOT NULL DEFAULT 'reserved';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS special_requests TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS organise_other_escort BOOLEAN DEFAULT FALSE;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS price_total NUMERIC(10,2);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS remaining_amount NUMERIC(10,2);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS outcall_address TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_bookings_start_time ON bookings(start_time);
CREATE INDEX IF NOT EXISTS idx_bookings_phone ON bookings(phone);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);

-- Drop NOT NULL constraints on legacy columns that may still exist from the old schema
-- (old bookings table used phone_number/date/time TEXT columns; new code uses phone/start_time/end_time)
ALTER TABLE bookings ALTER COLUMN phone_number DROP NOT NULL;
ALTER TABLE bookings ALTER COLUMN date DROP NOT NULL;
ALTER TABLE bookings ALTER COLUMN time DROP NOT NULL;


-- ============================================================================
-- Financial fields backfill and cleanup
-- ============================================================================

-- Backfill price_total for bookings with deposit but no total
UPDATE bookings
SET price_total = deposit_amount * 2
WHERE price_total IS NULL
  AND deposit_amount IS NOT NULL
  AND deposit_amount > 0;

-- Backfill remaining_amount for bookings with price_total and deposit
UPDATE bookings
SET remaining_amount = price_total - deposit_amount
WHERE remaining_amount IS NULL
  AND price_total IS NOT NULL
  AND deposit_amount IS NOT NULL;

-- Set deposit_reference to empty string if NULL
UPDATE bookings
SET deposit_reference = ''
WHERE deposit_reference IS NULL;

-- Set default values for bookings with no deposit (reserved/peacock)
UPDATE bookings
SET price_total = 600,
    remaining_amount = 600
WHERE price_total IS NULL
  AND (deposit_status = 'not_required' OR deposit_amount IS NULL OR deposit_amount = 0)
  AND status IN ('reserved', 'confirmed', 'reschedule-confirmed');

-- Fix reserved bookings: set deposit_amount to 0 where deposit_status is not_required
UPDATE bookings
SET deposit_amount = 0
WHERE status = 'reserved'
  AND deposit_status = 'not_required'
  AND deposit_amount > 0;

-- Convert decimal values to integers for cleaner display
UPDATE bookings
SET price_total = ROUND(price_total)
WHERE price_total IS NOT NULL;

UPDATE bookings
SET remaining_amount = ROUND(remaining_amount)
WHERE remaining_amount IS NOT NULL;

UPDATE bookings
SET deposit_amount = ROUND(deposit_amount)
WHERE deposit_amount IS NOT NULL;

-- ============================================================================
-- MOBILE PUSH TOKENS / DELIVERY DEDUPE (FCM / EXPO)
-- ============================================================================

CREATE TABLE IF NOT EXISTS push_device_tokens (
    id BIGSERIAL PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    token_type VARCHAR(10) NOT NULL CHECK (token_type IN ('expo', 'fcm')),
    platform VARCHAR(20) NOT NULL DEFAULT 'android',
    provider VARCHAR(20) NOT NULL DEFAULT 'fcm',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_push_device_tokens_active
ON push_device_tokens(active, token_type);

CREATE TABLE IF NOT EXISTS push_delivery_log (
    id BIGSERIAL PRIMARY KEY,
    booking_id TEXT NOT NULL,
    notification_type VARCHAR(32) NOT NULL,
    reminder_minutes INTEGER,
    token_hash VARCHAR(64) NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_push_delivery_dedupe
ON push_delivery_log (booking_id, notification_type, COALESCE(reminder_minutes, -1), token_hash);

-- ============================================================================
-- MIGRATION: deposit follow-up + pre-booking check-in tracking columns
-- ADD COLUMN IF NOT EXISTS is safe to re-run on existing databases.
-- ============================================================================
ALTER TABLE conversation_states
    ADD COLUMN IF NOT EXISTS deposit_followup_sent_at TIMESTAMPTZ DEFAULT NULL;

ALTER TABLE conversation_states
    ADD COLUMN IF NOT EXISTS checkin_sms_sent_at TIMESTAMPTZ DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_conv_deposit_followup
    ON conversation_states (current_state, deposit_followup_sent_at, deposit_requested_at)
    WHERE deposit_followup_sent_at IS NULL AND deposit_paid = FALSE;

CREATE INDEX IF NOT EXISTS idx_conv_checkin_sms
    ON conversation_states (current_state, checkin_sms_sent_at, reminder_2h_scheduled)
    WHERE checkin_sms_sent_at IS NULL;

