"""
Analytics API Blueprint
Provides JSON API endpoints for analytics data.
"""

import logging

from flask import Blueprint, jsonify, request

import config
from admin.auth import require_auth
from services.analytics_service import AnalyticsService
from services.database_service import get_shared_db

logger = logging.getLogger("escort_chatbot.admin.analytics")

analytics_bp = Blueprint('analytics', __name__)


@analytics_bp.route('/api/analytics/funnel', methods=['GET'])
@require_auth
def get_funnel_analytics():
    """Get booking funnel analytics."""
    try:
        days = max(1, min(365, int(request.args.get('days', 30))))
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        analytics = AnalyticsService(db)
        
        data = analytics.get_booking_funnel_analytics(days=days)
        return jsonify(data), 200
    except Exception as e:
        logger.exception("Error getting funnel analytics")
        return jsonify({"error": "An internal error occurred"}), 500


@analytics_bp.route('/api/analytics/revenue', methods=['GET'])
@require_auth
def get_revenue_analytics():
    """Get revenue analytics."""
    try:
        days = max(1, min(365, int(request.args.get('days', 30))))
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        analytics = AnalyticsService(db)
        
        data = analytics.get_revenue_analytics(days=days)
        return jsonify(data), 200
    except Exception as e:
        logger.exception("Error getting revenue analytics")
        return jsonify({"error": "An internal error occurred"}), 500


@analytics_bp.route('/api/analytics/clients', methods=['GET'])
@require_auth
def get_client_analytics():
    """Get client analytics."""
    try:
        days = max(1, min(365, int(request.args.get('days', 30))))
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        analytics = AnalyticsService(db)
        
        data = analytics.get_client_analytics(days=days)
        return jsonify(data), 200
    except Exception as e:
        logger.exception("Error getting client analytics")
        return jsonify({"error": "An internal error occurred"}), 500


@analytics_bp.route('/api/analytics/operational', methods=['GET'])
@require_auth
def get_operational_metrics():
    """Get operational metrics."""
    try:
        days = max(1, min(365, int(request.args.get('days', 7))))
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        analytics = AnalyticsService(db)
        
        data = analytics.get_operational_metrics(days=days)
        return jsonify(data), 200
    except Exception as e:
        logger.exception("Error getting operational metrics")
        return jsonify({"error": "An internal error occurred"}), 500


@analytics_bp.route('/api/analytics/ai-cost', methods=['GET'])
@require_auth
def get_ai_cost_analytics():
    """Get AI token usage and cost analytics."""
    try:
        days = max(1, min(90, int(request.args.get('days', 7))))
        db = get_shared_db(config.DATABASE_URL)
        from services.ai_call_log_service import AICallLogService

        svc = AICallLogService(db)
        daily = svc.get_daily_cost_by_day(days=days)
        summary = svc.get_daily_cost(days=days)
        return jsonify({
            "summary": summary,
            "by_day": daily,
            "days": days,
        }), 200
    except Exception as e:
        logger.exception("Error getting AI cost analytics")
        return jsonify({"error": "An internal error occurred"}), 500


@analytics_bp.route('/api/analytics/summary', methods=['GET'])
@require_auth
def get_analytics_summary():
    """Get all analytics in one response."""
    try:
        days = max(1, min(365, int(request.args.get('days', 30))))
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        analytics = AnalyticsService(db)
        
        return jsonify({
            'funnel': analytics.get_booking_funnel_analytics(days=days),
            'revenue': analytics.get_revenue_analytics(days=days),
            'clients': analytics.get_client_analytics(days=days),
            'operational': analytics.get_operational_metrics(days=7)
        }), 200
    except Exception as e:
        logger.exception("Error getting analytics summary")
        return jsonify({"error": "An internal error occurred"}), 500
