"""Wire all state/intent handlers onto the Router."""

from handlers import (
    availability_check,
    booking_collection,
    confirmed_booking,
    deposit_flow,
    link_handlers,
    post_booking,
    safety,
    touring_inquiry,
)
from handlers import new_conv as new_conversation  # direct import — shim removed
from handlers import ai_fallback as ai_fallback_handler
from handlers.booking_coll.handle_provide_field_v2 import handle_provide_field_v2
from handlers.new_conv.greeting_v2 import handle_greeting_v2

from .log import logger
from .v2_tuple_wrappers import legacy_handler_to_v2_tuple, register_v2_tuple_wrappers_for_migration_states


def register_router_handlers(router):
    """Register all state/intent handlers (same order as legacy main_v2)."""

    router.register("NEW", "greeting", new_conversation.handle_greeting)
    router.register("NEW", "book_appointment", new_conversation.handle_book_appointment)
    router.register("NEW", "quick_booking", booking_collection.handle_quick_booking)
    router.register("NEW", "ask_availability", new_conversation.handle_ask_availability)
    router.register("NEW", "provide_field", new_conversation.handle_ask_availability)
    router.register("NEW", "available_now", new_conversation.handle_available_now)
    router.register("NEW", "ask_rates", new_conversation.handle_ask_rates)
    router.register("NEW", "pricing_inquiry", new_conversation.handle_ask_rates)
    router.register("NEW", "doubles_enquiry", new_conversation.handle_doubles_enquiry)
    router.register("NEW", "couples_booking", new_conversation.handle_couples_enquiry)
    router.register("NEW", "overnight_enquiry", new_conversation.handle_overnight_enquiry)
    router.register("NEW", "request_outcall", new_conversation.handle_request_outcall)
    router.register("NEW", "dinner_date_enquiry", new_conversation.handle_dinner_date_enquiry)
    router.register("NEW", "msog_enquiry", new_conversation.handle_msog_enquiry)
    router.register("NEW", "location_enquiry", new_conversation.handle_location_enquiry)
    router.register("NEW", "rate_negotiation", new_conversation.handle_rate_negotiation)
    router.register("NEW", "service_inquiry", new_conversation.handle_service_inquiry)
    router.register("NEW", "touring_inquiry", touring_inquiry.handle_touring_inquiry)
    # YES / lock-in while DB state is still NEW (v2 mapping bug recovery) — must be before (NEW, *).
    router.register("NEW", "confirm_booking", availability_check.handle_check_availability)
    router.register("NEW", "cancel_booking", new_conversation.handle_cancel_booking_new)
    router.register("NEW", "other", new_conversation.handle_new_ambiguous)
    router.register("NEW", "reschedule", new_conversation.handle_modify_booking_new)
    router.register("NEW", "modify_booking", new_conversation.handle_modify_booking_new)

    router.register("COLLECTING", "provide_field", booking_collection.handle_provide_field)
    router.register("COLLECTING", "book_appointment", booking_collection.handle_provide_field)
    # Same intro + collecting pipeline as NEW — do not fall through to (COLLECTING, *).
    router.register("COLLECTING", "dinner_date_enquiry", new_conversation.handle_dinner_date_enquiry)
    router.register("COLLECTING", "couples_booking", new_conversation.handle_couples_enquiry)
    router.register("COLLECTING", "doubles_enquiry", new_conversation.handle_doubles_enquiry)
    router.register("COLLECTING", "ask_availability", booking_collection.handle_provide_field)
    router.register("COLLECTING", "greeting", booking_collection.handle_provide_field)
    router.register("COLLECTING", "confirm_booking", booking_collection.handle_provide_field)
    router.register("COLLECTING", "quick_booking", booking_collection.handle_quick_booking)
    router.register("COLLECTING", "available_now", new_conversation.handle_available_now)
    router.register("COLLECTING", "cancel_booking", booking_collection.handle_cancel_booking)
    router.register("COLLECTING", "goodbye", booking_collection.handle_goodbye)
    router.register("COLLECTING", "ask_rates", booking_collection.handle_ask_rates)
    router.register("COLLECTING", "pricing_inquiry", booking_collection.handle_ask_rates)
    router.register("COLLECTING", "overnight_enquiry", new_conversation.handle_overnight_enquiry)
    router.register("COLLECTING", "request_outcall", new_conversation.handle_request_outcall)
    router.register("COLLECTING", "msog_enquiry", new_conversation.handle_msog_enquiry)
    router.register("COLLECTING", "location_enquiry", new_conversation.handle_location_enquiry)
    router.register("COLLECTING", "rate_negotiation", new_conversation.handle_rate_negotiation)
    router.register("COLLECTING", "service_inquiry", new_conversation.handle_service_inquiry)
    router.register("COLLECTING", "touring_inquiry", touring_inquiry.handle_touring_inquiry)
    router.register("COLLECTING", "resend_link", link_handlers.handle_resend_link)
    router.register("COLLECTING", "reschedule", booking_collection.handle_provide_field)

    router.register("CHECKING_AVAILABILITY", "confirm_booking", availability_check.handle_check_availability)
    router.register("CHECKING_AVAILABILITY", "provide_field", availability_check.handle_check_availability)
    router.register("CHECKING_AVAILABILITY", "reschedule", availability_check.handle_check_availability)
    router.register("CHECKING_AVAILABILITY", "modify_booking", availability_check.handle_check_availability)
    router.register("CHECKING_AVAILABILITY", "cancel_booking", availability_check.handle_check_availability)
    router.register("CHECKING_AVAILABILITY", "greeting", availability_check.handle_check_availability)
    router.register("CHECKING_AVAILABILITY", "ask_availability", availability_check.handle_check_availability)
    router.register("CHECKING_AVAILABILITY", "book_appointment", availability_check.handle_check_availability)
    router.register("CHECKING_AVAILABILITY", "request_outcall", availability_check.handle_check_availability)

    router.register("DEPOSIT_REQUIRED", "deposit_screenshot", deposit_flow.handle_deposit_screenshot)
    router.register("DEPOSIT_REQUIRED", "deposit_query", deposit_flow.handle_deposit_query)
    router.register("DEPOSIT_REQUIRED", "refuse_deposit", deposit_flow.handle_refuse_deposit)
    router.register("DEPOSIT_REQUIRED", "available_now", new_conversation.handle_available_now)
    router.register("DEPOSIT_REQUIRED", "cancel_booking", deposit_flow.handle_cancel_booking)
    router.register("DEPOSIT_REQUIRED", "provide_field", deposit_flow.handle_provide_field)
    router.register("DEPOSIT_REQUIRED", "book_appointment", deposit_flow.handle_provide_field)
    router.register("DEPOSIT_REQUIRED", "ask_availability", deposit_flow.handle_provide_field)
    router.register("DEPOSIT_REQUIRED", "reschedule", deposit_flow.handle_provide_field)
    router.register("DEPOSIT_REQUIRED", "modify_booking", deposit_flow.handle_provide_field)
    router.register("DEPOSIT_REQUIRED", "resend_link", link_handlers.handle_resend_link)
    router.register("DEPOSIT_REQUIRED", "goodbye", deposit_flow.handle_goodbye)
    router.register("DEPOSIT_REQUIRED", "*", deposit_flow.handle_deposit_query)

    # After confirmation, casual greetings should get AI/conversation replies — not the static
    # booking recap (which felt robotic and drove clients back toward slot-picking flows).
    router.register("CONFIRMED", "goodbye", confirmed_booking.handle_goodbye)
    router.register("CONFIRMED", "greeting", confirmed_booking.handle_provide_field)
    router.register("CONFIRMED", "reschedule", confirmed_booking.handle_reschedule)
    router.register("CONFIRMED", "modify_booking", confirmed_booking.handle_modify_booking)
    router.register("CONFIRMED", "cancel_booking", confirmed_booking.handle_cancel_booking)
    router.register("CONFIRMED", "available_now", new_conversation.handle_available_now)
    router.register("CONFIRMED", "ask_rates", confirmed_booking.handle_ask_rates)
    router.register("CONFIRMED", "pricing_inquiry", confirmed_booking.handle_ask_rates)
    router.register("CONFIRMED", "rate_negotiation", confirmed_booking.handle_rate_negotiation)
    router.register("CONFIRMED", "service_inquiry", confirmed_booking.handle_service_inquiry)
    router.register("CONFIRMED", "doubles_enquiry", confirmed_booking.handle_doubles_enquiry)
    router.register("CONFIRMED", "couples_booking", new_conversation.handle_couples_enquiry)
    router.register("CONFIRMED", "touring_inquiry", touring_inquiry.handle_touring_inquiry)
    router.register("CONFIRMED", "deposit_screenshot", confirmed_booking.handle_optional_deposit_screenshot)
    router.register("CONFIRMED", "provide_field", confirmed_booking.handle_provide_field)
    router.register("CONFIRMED", "book_appointment", confirmed_booking.handle_provide_field)
    router.register("CONFIRMED", "ask_availability", confirmed_booking.handle_provide_field)
    router.register("CONFIRMED", "refuse_deposit", confirmed_booking.handle_provide_field)
    router.register("CONFIRMED", "resend_link", link_handlers.handle_resend_link)
    router.register("CONFIRMED", "*", confirmed_booking.handle_provide_field)

    router.register("POST_BOOKING", "greeting", post_booking.handle_greeting)
    router.register("POST_BOOKING", "book_appointment", post_booking.handle_book_appointment)
    router.register("POST_BOOKING", "available_now", new_conversation.handle_available_now)
    router.register("POST_BOOKING", "ask_rates", post_booking.handle_ask_rates)
    router.register("POST_BOOKING", "pricing_inquiry", post_booking.handle_ask_rates)
    router.register("POST_BOOKING", "ask_availability", post_booking.handle_ask_availability)
    router.register("POST_BOOKING", "touring_inquiry", touring_inquiry.handle_touring_inquiry)
    router.register("POST_BOOKING", "service_inquiry", post_booking.handle_service_inquiry)
    router.register("POST_BOOKING", "provide_field", post_booking.handle_provide_field)
    router.register("POST_BOOKING", "goodbye", post_booking.handle_goodbye)
    router.register("POST_BOOKING", "*", post_booking.handle_greeting)

    router.register("*", "unsafe_request", safety.handle_unsafe_request)
    router.register("*", "rude_abusive", safety.handle_rude_abusive)
    router.register("*", "flirt", new_conversation.handle_flirt)
    router.register("*", "wrong_number_opt_out", new_conversation.handle_wrong_number_opt_out)
    router.register("*", "enquiry_keyword", new_conversation.handle_enquiry_keyword)
    router.register("*", "touring_subscribe", touring_inquiry.handle_touring_subscribe)
    router.register("*", "touring_inquiry", touring_inquiry.handle_touring_inquiry)

    router.register("NEW", "*", new_conversation.handle_greeting)
    router.register("COLLECTING", "*", booking_collection.handle_provide_field)
    router.register("CHECKING_AVAILABILITY", "*", availability_check.handle_unknown_in_checking)
    router.register("MANUAL_REVIEW_PENDING", "*", availability_check.handle_manual_review_pending)

    # Same as COLLECTING: explicit dinner route must come before (EXTENDED_ENQUIRY, *) or
    # every intent hits AI fallback and clients see the generic ENQUIRY template.
    # Availability intents MUST use the real calendar-backed handlers — never AI fallback,
    # which would fabricate specific times (e.g. "Yes I'm free at 2pm Saturday").
    router.register("EXTENDED_ENQUIRY", "dinner_date_enquiry", new_conversation.handle_dinner_date_enquiry)
    router.register("EXTENDED_ENQUIRY", "ask_availability", new_conversation.handle_ask_availability)
    router.register("EXTENDED_ENQUIRY", "available_now", new_conversation.handle_available_now)
    router.register("EXTENDED_ENQUIRY", "*", ai_fallback_handler.handle_fallback_with_ai)

    router.register("*", "*", ai_fallback_handler.handle_fallback_with_ai)

    logger.info("Router configured with all handlers")

    # ------------------------------------------------------------------
    # V2 event-driven handlers (BookingContext, return (event, response))
    # Only called when conversation row has flow_version = 'v2'.
    # ------------------------------------------------------------------
    router.register_v2("COLLECTING", "provide_field", handle_provide_field_v2)
    router.register_v2("COLLECTING", "book_appointment", handle_provide_field_v2)
    router.register_v2("COLLECTING", "ask_availability", handle_provide_field_v2)
    router.register_v2("COLLECTING", "confirm_booking", handle_provide_field_v2)
    router.register_v2("COLLECTING", "greeting", handle_provide_field_v2)
    router.register_v2(
        "COLLECTING",
        "couples_booking",
        legacy_handler_to_v2_tuple(new_conversation.handle_couples_enquiry),
    )
    router.register_v2(
        "COLLECTING",
        "doubles_enquiry",
        legacy_handler_to_v2_tuple(new_conversation.handle_doubles_enquiry),
    )
    router.register_v2(
        "COLLECTING",
        "overnight_enquiry",
        legacy_handler_to_v2_tuple(new_conversation.handle_overnight_enquiry),
    )
    router.register_v2(
        "COLLECTING",
        "request_outcall",
        legacy_handler_to_v2_tuple(new_conversation.handle_request_outcall),
    )
    router.register_v2(
        "COLLECTING",
        "msog_enquiry",
        legacy_handler_to_v2_tuple(new_conversation.handle_msog_enquiry),
    )
    router.register_v2(
        "COLLECTING",
        "location_enquiry",
        legacy_handler_to_v2_tuple(new_conversation.handle_location_enquiry),
    )
    router.register_v2(
        "COLLECTING",
        "rate_negotiation",
        legacy_handler_to_v2_tuple(new_conversation.handle_rate_negotiation),
    )
    router.register_v2(
        "COLLECTING",
        "service_inquiry",
        legacy_handler_to_v2_tuple(new_conversation.handle_service_inquiry),
    )
    router.register_v2(
        "COLLECTING",
        "dinner_date_enquiry",
        legacy_handler_to_v2_tuple(new_conversation.handle_dinner_date_enquiry),
    )
    router.register_v2(
        "COLLECTING",
        "touring_inquiry",
        legacy_handler_to_v2_tuple(touring_inquiry.handle_touring_inquiry),
    )
    router.register_v2(
        "COLLECTING",
        "ask_rates",
        legacy_handler_to_v2_tuple(booking_collection.handle_ask_rates),
    )
    router.register_v2(
        "COLLECTING",
        "pricing_inquiry",
        legacy_handler_to_v2_tuple(booking_collection.handle_ask_rates),
    )
    router.register_v2("COLLECTING", "*", handle_provide_field_v2)

    # NEW / CHECKING / DEPOSIT: native (event, response) for all v1 routes (migration).
    register_v2_tuple_wrappers_for_migration_states(router)

    # Override the wrapped v1 greeting with the native v2 AI-powered handler.
    # Must come AFTER register_v2_tuple_wrappers_for_migration_states so the
    # exact (NEW, greeting) entry takes precedence over the wrapped fallback.
    router.register_v2("NEW", "greeting", handle_greeting_v2)

    logger.info("V2 router handlers registered")
