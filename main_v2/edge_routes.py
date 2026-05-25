"""Small HTTP routes: public healthcheck."""

from __future__ import annotations


import logging
import os
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from utils.structured_logging import get_logger

structured_logger = get_logger("escort_chatbot.main")
logger = logging.getLogger("escort_chatbot.main")

_INTERNAL_REPORT_SECRET_KEYS = (
    "REFACTOR_INTERNAL_REPORT_SECRET",
    "INTERNAL_API_SECRET",
)
_INTERNAL_REPORT_NEXT_SECRET_KEYS = (
    "REFACTOR_INTERNAL_REPORT_SECRET_NEXT",
    "INTERNAL_API_SECRET_NEXT",
)
_INTERNAL_REPORT_DEPRECATED_SECRET_KEYS = (
    "REFACTOR_INTERNAL_REPORT_SECRET_DEPRECATED",
    "INTERNAL_API_SECRET_DEPRECATED",
)
_INTERNAL_REPORT_CUTOVER_STATE_KEYS = (
    "REFACTOR_INTERNAL_REPORT_SECRET_CUTOVER_STATE",
    "INTERNAL_API_SECRET_CUTOVER_STATE",
)


def _first_env_secret(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _internal_report_secret() -> str:
    return _first_env_secret(_INTERNAL_REPORT_SECRET_KEYS)


def _is_internal_report_authorized(*, headers, remote_addr: str | None) -> bool:
    try:
        from app.security.auth import SharedSecretVerifier  # noqa: PLC0415
    except ImportError:
        logger.error("production readiness auth unavailable: missing app.security.auth")
        return False

    verifier = SharedSecretVerifier(
        secret_provider=_internal_report_secret,
        next_secret_provider=lambda: _first_env_secret(_INTERNAL_REPORT_NEXT_SECRET_KEYS),
        deprecated_secret_provider=lambda: _first_env_secret(_INTERNAL_REPORT_DEPRECATED_SECRET_KEYS),
        cutover_state_provider=lambda: _first_env_secret(_INTERNAL_REPORT_CUTOVER_STATE_KEYS),
        header_name="X-Internal-Secret",
        allow_loopback_without_secret=False,
    )
    result = verifier.verify(headers=headers, remote_addr=remote_addr)
    if result.authorized:
        return True
    logger.warning(
        "production readiness internal auth rejected: %s key_version=%s cutover_state=%s",
        result.reason,
        result.key_version,
        result.cutover_state,
    )
    return False


def _build_production_readiness_service():
    from core.settings_manager import get_setting  # noqa: PLC0415
    from main_v2 import runtime  # noqa: PLC0415
    try:
        from app.ops.production_readiness_service import ProductionReadinessReportService  # noqa: PLC0415
        from app.queue.providers import DatabaseQueueProvider  # noqa: PLC0415
        from app.workers.supervision.lease import DatabaseWorkerLeaseStore  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("production_readiness_unavailable") from exc

    db_service = getattr(runtime, "db_service", None)
    inbound_provider = None
    outbound_provider = None
    lease_store = None
    if db_service is not None and hasattr(db_service, "execute_query"):
        try:
            queue_provider = DatabaseQueueProvider(db_service=db_service)
            inbound_provider = queue_provider.inbound()
            outbound_provider = queue_provider.outbound()
        except Exception as exc:
            logger.warning("production readiness queue providers unavailable: %s", type(exc).__name__)
        try:
            lease_store = DatabaseWorkerLeaseStore(db_service=db_service)
        except Exception as exc:
            logger.warning("production readiness lease store unavailable: %s", type(exc).__name__)
    return ProductionReadinessReportService(
        inbound_provider=inbound_provider,
        outbound_provider=outbound_provider,
        lease_store=lease_store,
        setting_getter=get_setting,
        sample_window=50,
        stale_claim_window=20,
        guardrail_action_window=5,
    )


def register_edge_routes(app: Flask) -> None:
    """Register /healthcheck on the given app."""

    @app.route("/healthcheck", methods=["GET"])
    def health_check():
        """Public liveness check — returns status only, no internal details."""
        from main_v2 import runtime
        try:
            db_service = runtime.db_service
            db_ok = False
            if db_service:
                try:
                    db_service.execute_query("SELECT 1", fetch=True)
                    db_ok = True
                except Exception as e:
                    logger.warning("Healthcheck DB ping failed: %s", e)
            status = "ok" if db_ok else "degraded"
            return jsonify({
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), 200 if db_ok else 503
        except Exception as e:
            structured_logger.error("health_check_error", error=str(e))
            return jsonify({"status": "error"}), 500

    @app.route("/internal/refactor/production-readiness", methods=["GET"])
    def production_readiness_report():
        if not _is_internal_report_authorized(
            headers=dict(request.headers.items()),
            remote_addr=request.remote_addr,
        ):
            return jsonify({"status": "error", "error": "Unauthorized"}), 401
        try:
            report_service = _build_production_readiness_service()
            return jsonify(report_service.build_scrubbed_report()), 200
        except RuntimeError as e:
            if str(e) == "production_readiness_unavailable":
                return jsonify({"status": "error", "error": "service_unavailable"}), 503
            raise
        except Exception as e:
            structured_logger.error("production_readiness_error", error=str(e))
            logger.exception("production readiness report failed")
            return jsonify({"status": "error", "error": "internal_error"}), 500
