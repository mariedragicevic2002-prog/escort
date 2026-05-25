"""Flask blueprint for admin schedule routes (no handlers here)."""

from flask import Blueprint

schedule_bp = Blueprint("schedule", __name__, template_folder="../../templates")
