"""

Location management routes.

Endpoints:
- /location : Admin location update form
- /location/planned-tours : CRUD for planned tour list
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import json
import logging

from flask import Blueprint, jsonify, render_template, request, session

from admin.auth import login_user, verify_password
from core.settings_manager import clear_cache, get_setting, set_setting

logger = logging.getLogger("escort_chatbot.admin.location")

location_bp = Blueprint('location', __name__, template_folder='../templates')

# City to timezone mapping
CITY_TIMEZONES = {
    'Adelaide': 'Australia/Adelaide',
    'Brisbane': 'Australia/Brisbane',
    'Canberra': 'Australia/Sydney',
    'Darwin': 'Australia/Darwin',
    'Gold Coast': 'Australia/Brisbane',
    'Hobart': 'Australia/Hobart',
    'Melbourne': 'Australia/Sydney',
    'Perth': 'Australia/Perth',
    'Sydney': 'Australia/Sydney',
}


def _get_timezone_for_city(city):
    """Get timezone for a given city."""
    return CITY_TIMEZONES.get(city, 'Australia/Sydney')


def _get_current_incall_location():
    """Get current incall location details."""
    return {
        'city': get_setting('city', ''),
        'hotel_name': get_setting('hotel_name', ''),
        'address': get_setting('address', ''),
    }


def _get_touring_australia():
    """Get touring Australia details."""
    return {
        'is_touring': get_setting('is_touring', '0') == '1',
        'tour_start_date': get_setting('tour_start_date', ''),
        'tour_end_date': get_setting('tour_end_date', ''),
        'tour_city': get_setting('tour_city', ''),
        'tour_hotel_name': get_setting('tour_hotel_name', ''),
        'tour_address': get_setting('tour_address', ''),
    }


def _update_touring_australia(is_touring, start_date, end_date, tour_city, tour_hotel_name, tour_address):
    """Update touring Australia details."""
    try:
        set_setting('is_touring', '1' if is_touring else '0')
        set_setting('tour_start_date', start_date or '')
        set_setting('tour_end_date', end_date or '')
        set_setting('tour_city', tour_city or '')
        set_setting('tour_hotel_name', tour_hotel_name or '')
        set_setting('tour_address', tour_address or '')
        return True
    except Exception as e:
        logger.error(f"Error updating touring details: {e}")
        return False


def _update_current_location(city, hotel_name, address):
    """Update current location. Returns True only if all DB writes succeeded."""
    try:
        _failed = []
        if not set_setting('city', city):
            _failed.append('city')
        if not set_setting('hotel_name', hotel_name):
            _failed.append('hotel name')
        if not set_setting('address', address):
            _failed.append('address')

        # Auto-set timezone based on city
        timezone = _get_timezone_for_city(city)
        if not set_setting('timezone', timezone):
            _failed.append('timezone')

        if _failed:
            logger.error("_update_current_location: failed to save fields: %s", _failed)
            return False

        # Clear entire cache so other WSGI workers see fresh values on next read
        clear_cache()

        return True
    except Exception as e:
        logger.error("Error updating location: %s", e)
        return False


@location_bp.route("/location", methods=["GET", "POST"])
def location_update():
    """Admin web form for updating location."""
    error = None
    success = None
    # Check if already authenticated as admin OR location
    authenticated = session.get("location_authenticated", False) or session.get("admin_authenticated", False)

    # Handle login
    if request.method == "POST" and request.form.get("action") == "login":
        password = request.form.get("password")
        if verify_password(password or ""):
            login_user()  # Use proper session initialization
            session["location_authenticated"] = True
            authenticated = True
            logger.info("Successful location login")
        else:
            error = "Invalid password"
            logger.warning("Failed location login attempt")

    # Handle location update (only if authenticated)
    elif request.method == "POST" and request.form.get("action") == "update" and authenticated:
        return _handle_form_location_update(request)

    # Handle touring update (only if authenticated)
    elif request.method == "POST" and request.form.get("action") == "update_touring" and authenticated:
        return _handle_touring_update(request)

    # Handle JSON request (from JavaScript)
    elif request.method == "POST" and request.is_json:
        if not authenticated:
            return jsonify({"success": False, "error": "Not authenticated"}), 401
        return _handle_json_location_update(request)

    # GET request: Show location form
    return _render_location_page(error, success, authenticated)


def _handle_form_location_update(request):
    """Handle form-based location update."""
    city = request.form.get("city")
    hotel_name = request.form.get("hotel_name", "").strip()
    address = request.form.get("address")
    intercom = request.form.get("intercom", "").strip()
    parking = request.form.get("parking", "").strip()
    timezone = request.form.get("timezone", "").strip()

    # Check if this is an AJAX request
    is_ajax = request.headers.get('X-CSRFToken') is not None

    if not city:
        if is_ajax:
            return jsonify({"success": False, "error": "City is required"}), 400
        return _render_location_page(error="City is required", success=None, authenticated=True)

    try:
        # Keep hotel name blank if not provided - address will be shown instead
        hotel_display = hotel_name if hotel_name else ""

        # Store intercom and parking in settings
        set_setting("location_intercom", intercom if intercom else "")
        set_setting("location_parking", parking if parking else "")

        success_result = _update_current_location(city, hotel_display, address or "")
        
        # Override timezone if manually selected (takes priority over auto-set)
        if timezone:
            set_setting("timezone", timezone)
            set_setting("location_timezone", timezone)

        if success_result:
            success_msg = "Location updated! Clients will receive access details 1 hour before booking."
            logger.info(f"Location updated to {city} - {hotel_display}")
            if is_ajax:
                return jsonify({"success": True, "message": success_msg})
            return _render_location_page(error=None, success=success_msg, authenticated=True)
        else:
            if is_ajax:
                return jsonify({"success": False, "error": "Failed to update location in database"}), 500
            return _render_location_page(error="Failed to update location in database", success=None, authenticated=True)

    except Exception as e:
        error_msg = f"Error: {str(e)}"
        logger.error(f"Location update failed: {e}")
        if is_ajax:
            return jsonify({"success": False, "error": str(e)}), 500
        return _render_location_page(error=error_msg, success=None, authenticated=True)


def _handle_json_location_update(request):
    """Handle JSON-based location update."""
    data = request.get_json()
    city = data.get("city", "").strip()
    hotel_name = data.get("hotel_name", "").strip()
    address = data.get("address", "").strip()
    intercom = data.get("intercom", "").strip()
    parking = data.get("parking", "").strip()

    if not city:
        return jsonify({"success": False, "error": "City is required"}), 400

    try:
        # Keep hotel name blank if not provided - address will be shown instead
        hotel_display = hotel_name if hotel_name else ""

        # Store intercom and parking in settings
        set_setting("location_intercom", intercom if intercom else "")
        set_setting("location_parking", parking if parking else "")

        success_result = _update_current_location(city, hotel_display, address or "")

        if success_result:
            new_timezone = get_setting('timezone', 'Australia/Sydney')
            logger.info(f"Location updated to {city} - {hotel_display}")
            return jsonify({
                "success": True,
                "message": "Location updated! Access details will be sent to clients 1 hour before booking.",
                "timezone": new_timezone
            })
        else:
            return jsonify({"success": False, "error": "Failed to update location"}), 500

    except Exception as e:
        logger.error(f"JSON location update failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _handle_touring_update(request):
    """Handle touring Australia update."""
    is_ajax = request.headers.get('X-CSRFToken') is not None

    is_touring = request.form.get("is_touring", "0") == "1"
    start_date = request.form.get("tour_start_date", "").strip()
    end_date = request.form.get("tour_end_date", "").strip()
    tour_city = request.form.get("tour_city", "").strip()
    tour_hotel_name = request.form.get("tour_hotel_name", "").strip()
    tour_address = request.form.get("tour_address", "").strip()

    try:
        success_result = _update_touring_australia(is_touring, start_date, end_date, tour_city, tour_hotel_name, tour_address)

        if success_result:
            status_msg = "Touring enabled" if is_touring else "Touring disabled"
            success_msg = f"{status_msg}! Clients will see your touring schedule." if is_touring else "Touring disabled. Your home location is now active."
            logger.info(f"Touring details updated: {status_msg}")
            if is_ajax:
                return jsonify({"success": True, "message": success_msg})
            return _render_location_page(error=None, success=success_msg, authenticated=True)
        else:
            if is_ajax:
                return jsonify({"success": False, "error": "Failed to update touring details"}), 500
            return _render_location_page(error="Failed to update touring details", success=None, authenticated=True)

    except Exception as e:
        logger.error(f"Touring update failed: {e}")
        if is_ajax:
            return jsonify({"success": False, "error": str(e)}), 500
        return _render_location_page(error=f"Error: {str(e)}", success=None, authenticated=True)


def _render_location_page(error, success, authenticated):
    """Render the location management page."""
    location = _get_current_incall_location()
    current_intercom = get_setting("location_intercom", "")
    current_parking = get_setting("location_parking", "")
    touring = _get_touring_australia()

    # Use configured cities, sorted for a nicer dropdown
    cities = sorted([city.title() for city in CITY_TIMEZONES.keys()])

    # Get current timezone - prefer saved setting, fallback to city-based
    saved_timezone = get_setting("location_timezone")
    if saved_timezone:
        current_timezone = saved_timezone
    else:
        # Get current city - use dynamic value, no hardcoded fallback
        current_city = (location or {}).get('city', '') or get_setting('city', '')
        current_timezone = _get_timezone_for_city(current_city)

    return render_template(
        "location.html",
        current_location=location,
        cities=cities,
        current_intercom=current_intercom,
        current_parking=current_parking,
        current_timezone=current_timezone,
        touring=touring,
        planned_tours=_get_planned_tours(),
        error=error,
        success=success,
        authenticated=authenticated,
    )


def _get_planned_tours():
    """Return list of planned tours combining planned_tours_json and current touring settings."""
    raw = get_setting("planned_tours_json", "")
    try:
        tours = json.loads(raw) if raw else []
        if not isinstance(tours, list):
            tours = []
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        tours = []

    # Also include the current touring settings if they have a city and start date
    tour_city = get_setting("tour_city", "")
    tour_start = get_setting("tour_start_date", "")
    if tour_city and tour_start:
        current = {
            "city": tour_city,
            "start_date": tour_start,
            "end_date": get_setting("tour_end_date", ""),
            "hotel": get_setting("tour_hotel_name", ""),
            "notes": "",
        }
        # Only add if not already in the list (match on city + start_date)
        already = any(
            t.get("city") == current["city"] and t.get("start_date") == current["start_date"]
            for t in tours
        )
        if not already:
            tours.append(current)

    tours.sort(key=lambda t: t.get("start_date", ""))
    return tours


def _save_planned_tours(tours):
    """Persist planned tours list to settings."""
    set_setting("planned_tours_json", json.dumps(tours))


@location_bp.route('/location/planned-tours', methods=['GET'])
def get_planned_tours():
    """Return all planned tours as JSON."""
    if not (session.get("location_authenticated") or session.get("admin_authenticated")):
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    return jsonify({"success": True, "tours": _get_planned_tours()})


@location_bp.route('/location/planned-tours', methods=['POST'])
def add_planned_tour():
    """Add a new planned tour."""
    if not (session.get("location_authenticated") or session.get("admin_authenticated")):
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    data = request.get_json() or {}
    city = (data.get("city") or "").strip()
    start_date = (data.get("start_date") or "").strip()
    end_date = (data.get("end_date") or "").strip()
    hotel = (data.get("hotel") or "").strip()
    notes = (data.get("notes") or "").strip()
    if not city or not start_date:
        return jsonify({"success": False, "error": "City and start date are required"}), 400
    tours = _get_planned_tours()
    tours.append({"city": city, "start_date": start_date, "end_date": end_date, "hotel": hotel, "notes": notes})
    # Sort by start date
    tours.sort(key=lambda t: t.get("start_date", ""))
    _save_planned_tours(tours)
    logger.info(f"Planned tour added: {city} {start_date}")
    return jsonify({"success": True, "tours": tours})


@location_bp.route('/location/planned-tours/delete', methods=['POST'])
def delete_planned_tour():
    """Delete a planned tour by index (POST to avoid proxy issues with DELETE)."""
    if not (session.get("location_authenticated") or session.get("admin_authenticated")):
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    data = request.get_json() or {}
    index = data.get("index")
    if index is None:
        return jsonify({"success": False, "error": "Index is required"}), 400
    try:
        index = int(index)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid index"}), 400
    tours = _get_planned_tours()
    if index < 0 or index >= len(tours):
        return jsonify({"success": False, "error": "Invalid tour index"}), 400
    removed = tours.pop(index)
    _save_planned_tours(tours)
    logger.info(f"Planned tour removed: {removed.get('city')} {removed.get('start_date')}")
    return jsonify({"success": True, "tours": tours})


@location_bp.route('/location/logout', methods=['GET', 'POST'])
def location_logout():
    """Logout from location page."""
    session.pop("location_authenticated", None)
    from flask import redirect, url_for
    return redirect(url_for('location.location_update'))
