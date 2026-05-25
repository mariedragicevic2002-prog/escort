"""Rates management blueprint."""

import json
import logging

from flask import Blueprint, jsonify, render_template, request, session

from admin.auth import login_user, require_auth, verify_password
from core.rates_from_config import _load_pricing, get_default_pricing
from core.settings_manager import get_setting, set_setting

logger = logging.getLogger("escort_chatbot.admin.rates")

rates_bp = Blueprint('rates', __name__, template_folder='../templates')


def _is_rates_authenticated():
    """Check if user is authenticated for rates access."""
    return session.get("admin_authenticated", False) or session.get("rates_authenticated", False)


@rates_bp.route('/rates', methods=['GET', 'POST'])
def rates_dashboard():
    """Rates management dashboard."""
    authenticated = _is_rates_authenticated()
    error = None

    # Handle authentication
    if request.method == 'POST' and not authenticated:
        password = request.form.get('password')
        if verify_password(password or ""):
            login_user()  # Use proper session initialization
            session["rates_authenticated"] = True
            authenticated = True
        else:
            error = 'Invalid password'

    if not authenticated:
        return render_template('rates.html', authenticated=False, error=error)

    # Load merged pricing (includes legacy key migration)
    try:
        pricing = _load_pricing()
    except Exception as e:
        logger.warning("Failed to load pricing config, using defaults: %s", e)
        pricing = _default_pricing()
    return render_template('rates.html', authenticated=True, pricing=pricing)


@rates_bp.route('/rates/update', methods=['POST'])
@require_auth
def update_rates():
    """Update pricing configuration."""
    try:
        data = request.get_json()

        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        # Validate pricing data structure (incall, outcall, surcharge, deposit amounts)
        pricing_config = {
            "incall": data.get("incall", {}),
            "outcall": data.get("outcall", {}),
            "surcharge": int(data.get("surcharge", 100)),
            "deposit_outcall": int(data.get("deposit_outcall", 100)),
            "deposit_incall": int(data.get("deposit_incall", 50)),
            "deposit_mff_pair": int(data.get("deposit_mff_pair", 200)),
            "deposit_overnight": int(data.get("deposit_overnight", 200)),
            "deposit_dinner_date_outcall": int(data.get("deposit_dinner_date_outcall", 100)),
            "deposit_extended_experience_outcall": int(
                data.get("deposit_extended_experience_outcall", 200)
            ),
            "surcharge_doubles_escort_supplied_outcall": int(
                data.get("surcharge_doubles_escort_supplied_outcall", 200)
            ),
        }

        # Save to database
        set_setting("pricing_config", json.dumps(pricing_config))
        logger.info("Pricing configuration updated successfully")

        return jsonify({"success": True, "message": "Rates updated successfully"})
    except Exception as e:
        logger.exception("Failed to update rates")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


def _default_pricing():
    """Return default pricing structure (used when none saved or load fails)."""
    return get_default_pricing()


def _load_pricing_config():
    """Load pricing configuration from database or return defaults."""
    try:
        saved_pricing = get_setting("pricing_config")
    except Exception as e:
        logger.warning("get_setting(pricing_config) failed: %s", e)
        return _default_pricing()
    if saved_pricing:
        try:
            return json.loads(saved_pricing)
        except json.JSONDecodeError:
            logger.warning("Failed to parse saved pricing config, using defaults")
    return _default_pricing()


def validate_rate_data(data: dict) -> bool:
    """
    Validate rate update data structure and values.

    Args:
        data: Dictionary containing rate data to validate

    Returns:
        True if data is valid, False otherwise
    """
    if not isinstance(data, dict):
        return False

    # Check required fields exist and are correct types
    required_fields = {
        'incall': dict,
        'outcall': dict,
        'surcharge': int,
        'deposit_incall': int,
        'deposit_outcall': int
    }

    for field, expected_type in required_fields.items():
        if field not in data:
            continue  # Optional field
        if not isinstance(data[field], expected_type):
            return False

    # Validate numeric values are reasonable
    if 'surcharge' in data:
        if not (0 <= data['surcharge'] <= 1000):
            return False

    if 'surcharge_doubles_escort_supplied_outcall' in data:
        if not (0 <= data['surcharge_doubles_escort_supplied_outcall'] <= 1000):
            return False

    for deposit_field in [
        'deposit_incall',
        'deposit_outcall',
        'deposit_mff_pair',
        'deposit_overnight',
        'deposit_dinner_date_outcall',
        'deposit_extended_experience_outcall',
    ]:
        if deposit_field in data:
            if not (0 <= data[deposit_field] <= 1000):
                return False

    # Validate rate dictionaries contain valid rate values
    for rate_type in ['incall', 'outcall']:
        if rate_type in data:
            rates = data[rate_type]
            if not isinstance(rates, dict):
                return False
            for _service, price in rates.items():
                if not isinstance(price, (int, float)) or price < 0 or price > 10000:
                    return False

    return True
