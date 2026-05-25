"""
main_v2/admin_endpoints.py

Admin route and blueprint registration. Called from application.py after app init.
"""
from flask import jsonify

from admin.auth import require_auth


def register_admin_routes(app, router, state_manager):
    """Register /admin/routes and /admin/state routes, then attach all admin blueprints."""
    from admin.blueprints.admin_actions import admin_actions_bp
    from admin.blueprints.analytics import analytics_bp
    from admin.blueprints.booking import booking_bp
    from admin.blueprints.config import config_bp
    from admin.blueprints.database import database_bp
    from admin.blueprints.feedback import feedback_bp, feedback_form, feedback_thanks
    from admin.blueprints.health import health_bp
    from admin.blueprints.location import location_bp
    from admin.blueprints.mobile_api import mobile_api_bp
    from admin.blueprints.rates import rates_bp
    from admin.blueprints.schedule import schedule_bp
    from admin.blueprints.stats import stats_bp
    from admin.routes import admin_bp
    from main_v2.log import logger

    @app.route('/admin/routes', methods=['GET'])
    @require_auth
    def show_routes():
        """Show all registered routes (for debugging)."""
        routes = router.get_all_routes()
        return jsonify({
            "total_routes": len(routes),
            "routes": [{"state": k[0], "intent": k[1], "handler": v} for k, v in routes.items()]
        }), 200

    @app.route('/admin/state/<phone_number>', methods=['GET'])
    @require_auth
    def get_state(phone_number):
        """Get state for a phone number (for debugging)."""
        if not state_manager:
            return jsonify({"error": "State manager not initialized"}), 500
        state = state_manager.get_state(phone_number)
        if state:
            state_copy = dict(state)
            for key, value in state_copy.items():
                if hasattr(value, 'isoformat'):
                    state_copy[key] = value.isoformat()
            return jsonify(state_copy), 200
        return jsonify({"error": "No state found"}), 404

    app.register_blueprint(admin_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(location_bp)
    app.register_blueprint(database_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(schedule_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(booking_bp)
    app.register_blueprint(rates_bp)
    app.register_blueprint(admin_actions_bp)
    app.register_blueprint(feedback_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(mobile_api_bp)
    app.add_url_rule("/feedback", view_func=feedback_form, methods=["GET", "POST"])
    app.add_url_rule("/feedback/thanks", view_func=feedback_thanks, methods=["GET"])
    logger.info("Admin blueprints registered (13 blueprints)")

    # Auto-create bookings table and seed test data on startup
    try:
        from admin.blueprints.mobile_api import _bootstrap_bookings_db
        _bootstrap_bookings_db()
        logger.info("Bookings DB bootstrap complete")
    except Exception as e:
        logger.warning("Bookings DB bootstrap failed (non-fatal): %s", e)
