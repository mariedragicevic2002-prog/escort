# Backward-compatibility shim — retained only for test patches that reference
# ``handlers.new_conversation.*``.  All production code in refactor2 imports
# directly from ``handlers.new_conv``.  Do not add logic here.
import warnings as _warnings
_warnings.warn(
    "handlers.new_conversation is deprecated in refactor2. "
    "Import from handlers.new_conv directly.",
    DeprecationWarning,
    stacklevel=2,
)
from handlers.new_conv import *  # noqa: F401,F403,E402
from handlers.new_conv.enquiries_simple import handle_enquiry_keyword, handle_wrong_number_opt_out, handle_flirt  # noqa: F401,E402
from handlers.new_conv.availability_stages import _handle_ask_availability_impl  # noqa: F401,E402
from handlers.new_conv.booking import (  # noqa: F401,E402
    _handle_book_appointment_impl,
    handle_cancel_booking_new,
    handle_modify_booking_new,
)
