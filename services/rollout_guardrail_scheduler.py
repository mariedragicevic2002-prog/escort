"""
Automated rollout guardrail enforcement scheduler.
Periodically checks v1/v2 performance and applies guardrail actions if needed.
Logs all auto-actions to admin_audit_log.
"""
import threading
import time
from datetime import datetime
from core.settings_manager import set_setting
from admin.blueprints.stats import _collect_all_stats
from utils.admin_audit import log_admin_audit

# Configurable interval and cooldown (seconds)
CHECK_INTERVAL = 3600  # 1 hour
COOLDOWN_PERIOD = 6 * 3600  # 6 hours

_last_action_time = None


def rollout_guardrail_job():
    global _last_action_time
    while True:
        now = datetime.utcnow()
        if _last_action_time and (now - _last_action_time).total_seconds() < COOLDOWN_PERIOD:
            time.sleep(CHECK_INTERVAL)
            continue
        try:
            # Use default filters (30 days, all locations/experiences)
            stats = _collect_all_stats(days=30, location_filter="all", experience_filter="all")
            guardrail = stats.get("rollout_guardrail") or {}
            action = str(guardrail.get("recommended_action") or "none").strip().lower()
            details = f"Auto-guardrail: action={action} | stats={guardrail}"
            if action == "none":
                log_admin_audit("auto_guardrail_check", details)
            elif action == "force_v1":
                set_setting("flow_version_default", "v1")
                set_setting("flow_version_v2_rollout_percent", "0")
                log_admin_audit("auto_guardrail_force_v1", details)
                _last_action_time = now
            elif action == "reduce_v2":
                percent = int(guardrail.get("suggested_rollout_percent") or 0)
                set_setting("flow_version_default", "rollout")
                set_setting("flow_version_v2_rollout_percent", str(percent))
                log_admin_audit("auto_guardrail_reduce_v2", details)
                _last_action_time = now
            else:
                log_admin_audit("auto_guardrail_no_action", details)
        except Exception as e:
            log_admin_audit("auto_guardrail_error", f"{e}")
        time.sleep(CHECK_INTERVAL)

def start_rollout_guardrail_scheduler():
    t = threading.Thread(target=rollout_guardrail_job, daemon=True)
    t.start()
