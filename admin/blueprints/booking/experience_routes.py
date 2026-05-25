"""Experience guide and short experience link routes."""

from datetime import datetime

from flask import render_template

from config import get_current_incall_location, get_escort_name
from core.rates_from_config import get_incall_pricing, get_outcall_pricing
from core.webform_security import get_experience_token_from_short_code

from .blueprint import booking_bp


@booking_bp.route("/experience", methods=["GET"])
def experience_page():
    """Experience guide page - publicly viewable."""
    location = get_current_incall_location()
    return render_template(
        "experience.html",
        rates=get_incall_pricing(),
        outcall_rates=get_outcall_pricing(),
        current_city=location.get("city", "") if isinstance(location, dict) else "",
    )


@booking_bp.route("/e/<short_code>")
def short_experience_link(short_code):
    """Short URL for experience guide page."""
    token_data = get_experience_token_from_short_code(short_code)

    if not token_data:
        return render_template(
            "booking_error.html",
            error_title="Link Not Found",
            error_message="This experience guide link is invalid or has expired.",
        ), 404

    # Check expiry
    if token_data.get("expires_at") and datetime.now() > token_data["expires_at"]:
        return render_template(
            "booking_error.html",
            error_title="Link Expired",
            error_message=f"This experience guide link has expired. Please text {get_escort_name()} to request a new one.",
        ), 400

    location = get_current_incall_location()
    return render_template(
        "experience.html",
        rates=get_incall_pricing(),
        outcall_rates=get_outcall_pricing(),
        current_city=location.get("city", "") if isinstance(location, dict) else "",
    )
