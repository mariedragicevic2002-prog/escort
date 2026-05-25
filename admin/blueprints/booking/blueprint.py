"""Flask blueprint for public booking webform routes (no route handlers here)."""

from flask import Blueprint

booking_bp = Blueprint("booking", __name__, template_folder="../../templates")
