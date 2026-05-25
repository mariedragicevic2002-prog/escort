from handlers.booking_coll import _shared_dinner_doubles as doubles_patterns
from handlers.booking_coll._provide_field_context import CollectingCtx
from handlers.booking_coll._provide_field_stages_finish import _stage_apply_extracted_updates_and_name
from templates import greetings


def test_how_about_phrase_is_not_accepted_as_client_name():
    assert not greetings.is_valid_client_name("How About")
    assert greetings.extract_client_name("How about tomorrow night?") == ""


def test_day_prefixed_time_messages_do_not_yield_client_name():
    assert greetings.extract_client_name("Wed 1am") == ""
    assert greetings.extract_client_name("Mon 7pm") == ""


def test_mmf_exploration_keywords_are_not_valid_client_names():
    assert not greetings.is_valid_client_name("Bisexual")
    assert not greetings.is_valid_client_name("Humiliation")
    assert not greetings.is_valid_client_name("Voyeurism")


def test_mmf_exploration_reply_does_not_get_saved_as_client_name():
    store = {}

    class _FakeSM:
        def update_fields(self, _pn, updates):
            store.update(updates)

        def get_booking_fields(self, _pn):
            return dict(store)

    class _FakeFC:
        def get_missing_fields(self, _fields):
            return []

    class _FakeFV:
        _last_verified_hotel_info = None

    ctx = CollectingCtx(
        phone_number="+61400000000",
        message="Bisexual and humiliation",
        raw_context={},
        state_manager=_FakeSM(),
        field_collector=_FakeFC(),
        field_validator=_FakeFV(),
        ai_service=None,
        db_service=None,
    )
    ctx.state = {}
    ctx.current_fields = {}
    ctx.extracted = {}

    _stage_apply_extracted_updates_and_name(ctx)
    assert "client_name" not in store


def test_existing_client_name_not_overwritten_when_message_starts_with_weekday():
    store = {"client_name": "Harry"}

    class _FakeSM:
        def update_fields(self, _pn, updates):
            store.update(updates)

        def get_booking_fields(self, _pn):
            return dict(store)

    class _FakeFC:
        def get_missing_fields(self, _fields):
            return []

    class _FakeFV:
        _last_verified_hotel_info = None

    ctx = CollectingCtx(
        phone_number="+61400000000",
        message="Wed 1am",
        raw_context={},
        state_manager=_FakeSM(),
        field_collector=_FakeFC(),
        field_validator=_FakeFV(),
        ai_service=None,
        db_service=None,
    )
    ctx.state = {"client_name": "Harry"}
    ctx.current_fields = {"client_name": "Harry"}
    ctx.extracted = {}

    _stage_apply_extracted_updates_and_name(ctx)
    assert store["client_name"] == "Harry"


def test_doubles_supply_gate_accepts_need_u_to_organise():
    assert doubles_patterns._ESCORT_SUPPLIES_PATTERNS.search("Need u to organise it")
    assert doubles_patterns._ESCORT_SUPPLIES_PATTERNS.search("I need u to")


def test_doubles_supply_gate_accepts_can_you_provide_other_girl():
    """Natural reply after MFF gate — 'provide' was missing from verb list (only organise/find/etc.)."""
    msg = "Can you provide the other girl?"
    assert doubles_patterns._ESCORT_SUPPLIES_PATTERNS.search(msg)
    assert not doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search(msg)


def test_doubles_supply_gate_accepts_organise_other_girl_typos_and_politeness():
    """Broader MFF wording: organise, ok-if-your (typo), hoping + csn/can typo."""
    cases = [
        "can you organise the other girl",
        "is it ok if your bring the other escort",
        "im was hoping you csn organise her for me",
    ]
    for msg in cases:
        assert doubles_patterns._ESCORT_SUPPLIES_PATTERNS.search(msg), msg
        assert not doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search(msg), msg


def test_doubles_supply_gate_accepts_suss_out_and_hook_up():
    """Informal AU/NZ/UK phrasing for sourcing the other person."""
    cases = [
        "can you suss out another escort",
        "could u suss out the other bloke",
        "can you hook up the other girl",
        "could you hook up with another guy",
    ]
    for msg in cases:
        assert doubles_patterns._ESCORT_SUPPLIES_PATTERNS.search(msg), msg
        assert not doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search(msg), msg


def test_doubles_supply_gate_accepts_providing_a_friend():
    # Regression: "MMF with me and a friend which ill be providing" was not detected as client-supplies
    assert doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search("MMF with me and a friend which ill be providing")
    assert doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search("ill be providing")
    assert doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search("i'll be providing")
    assert doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search("im providing a mate")
    assert doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search("a friend which ill be providing")


def test_implicit_escort_supplies_when_you_and_another_bloke_mmf():
    msg = (
        "Hi im keen to book you and another bloke for a doubles MMF booking "
        "are you free tomorrow at 10pm?"
    )
    assert doubles_patterns.implicit_escort_supplies_other_person(msg)
    assert not doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search(msg)


def test_implicit_escort_supplies_when_you_and_another_chick_mff():
    msg = "Keen to book you and another chick for MFF doubles tonight"
    assert doubles_patterns.implicit_escort_supplies_other_person(msg)
    assert not doubles_patterns._CLIENT_SUPPLIES_PATTERNS.search(msg)


def test_implicit_escort_supplies_not_when_client_brings_mate():
    msg = "MMF tomorrow I'll bring my mate"
    assert not doubles_patterns.implicit_escort_supplies_other_person(msg)
