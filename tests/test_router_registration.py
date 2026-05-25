from __future__ import annotations

from core.router import Router
from handlers import new_conversation
from main_v2.router_registration import register_router_handlers


def test_router_registers_new_cancel_booking_handler():
    router = Router()
    register_router_handlers(router)
    assert router.dispatch_table[("NEW", "cancel_booking")] is new_conversation.handle_cancel_booking_new


def test_router_registers_new_other_handler():
    router = Router()
    register_router_handlers(router)
    assert router.dispatch_table[("NEW", "other")] is new_conversation.handle_new_ambiguous


def test_router_registers_new_pricing_inquiry_handler():
    router = Router()
    register_router_handlers(router)
    assert router.dispatch_table[("NEW", "pricing_inquiry")] is new_conversation.handle_ask_rates
