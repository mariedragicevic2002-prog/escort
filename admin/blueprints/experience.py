"""
Experience page blueprint - Shows available services and pricing.
"""

import logging

from flask import Blueprint, render_template

from utils.log_sanitize import sanitize_log_value

logger = logging.getLogger("escort_chatbot.admin.experience")

experience_bp = Blueprint('experience', __name__, template_folder='../templates')


@experience_bp.route("/experience", methods=["GET"])
def experience_page():
    """Experience/services guide page."""
    logger.info("Experience page accessed")
    return render_template("experience.html")


@experience_bp.route("/e/<short_code>", methods=["GET"])
def experience_short_url(short_code):
    """Experience page via short URL (e.g., /e/ABC123)."""
    logger.info("Experience page accessed via short URL: %s", sanitize_log_value(short_code))
    # Short URLs can also serve the same experience page
    # In the future, could track which client accessed it via the short_code
    return render_template("experience.html")
