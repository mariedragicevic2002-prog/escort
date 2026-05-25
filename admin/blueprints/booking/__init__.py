"""Public booking blueprint; route modules register handlers on `booking_bp` via side-effect imports."""

from .blueprint import booking_bp

from . import experience_routes as experience_routes  # noqa: F401
from . import webform_routes as webform_routes  # noqa: F401
from . import short_link_routes as short_link_routes  # noqa: F401
from . import api_booked_times as api_booked_times  # noqa: F401
from . import deposit_routes as deposit_routes  # noqa: F401
from . import confirmation_routes as confirmation_routes  # noqa: F401

__all__ = ["booking_bp"]
