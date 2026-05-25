"""Admin schedule blueprint; route modules register handlers on `schedule_bp` via side-effect imports."""

from .blueprint import schedule_bp

from . import page_routes as page_routes  # noqa: F401
from . import api_routes as api_routes  # noqa: F401

from .page_routes import _delete_travel_time_blocks

__all__ = ["schedule_bp", "_delete_travel_time_blocks"]
