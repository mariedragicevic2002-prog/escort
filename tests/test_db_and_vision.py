from types import SimpleNamespace

from services import database_service, vision_service


def test_execute_on_connection_converts_tuple_rows():
    class FakeCursor:
        def __init__(self):
            self.description = [SimpleNamespace(name='a'), SimpleNamespace(name='b')]
            self._rows = [(1, 2)]
        def execute(self, query, params=None):
            pass
        def fetchall(self):
            return self._rows
        def close(self):
            pass
        def fetchone(self):
            return None

    class FakeConn:
        def cursor(self, cursor_factory=None):
            return FakeCursor()

    rows = database_service.DatabaseService._execute_on_connection(None, FakeConn(), "select 1", None, True)  # type: ignore[arg-type]
    assert isinstance(rows, list)
    assert rows[0]['a'] == 1 and rows[0]['b'] == 2


def test_validate_deposit_screenshot_accepts_valid():
    vs = vision_service
    # Patch module-level helpers
    vs.HAS_VISION = True
    vs._extract_text_from_image = lambda b: "Payment $100 to 0412345678 Test Name today REF12345"
    vs.get_payid = lambda: "0412345678"
    vs.get_account_name = lambda: "Test Name"

    res = vs.validate_deposit_screenshot_from_bytes(b'fake', '0412345678', required_amount=100, expected_reference='REF12345')
    assert res['valid'] is True


def test_validate_deposit_fail_closed_without_expected_reference():
    vs = vision_service
    vs.HAS_VISION = True
    vs._extract_text_from_image = lambda b: (
        "Payment $100 to 0412345678 Test Name today extra words"
    )
    vs.get_payid = lambda: "0412345678"
    vs.get_account_name = lambda: "Test Name"

    res = vs.validate_deposit_screenshot_from_bytes(
        b'fake', '0412345678', required_amount=100, expected_reference=None
    )
    assert res['valid'] is False
    assert res.get('manual_review_required') is True


def test_validate_deposit_weak_rules_when_reference_not_required():
    """Optional-deposit style callers omit reference; Vision uses amount + corroboration checks."""
    vs = vision_service
    vs.HAS_VISION = True
    vs._extract_text_from_image = lambda b: (
        "Payment $100 to 0412345678 Test Name today extra words"
    )
    vs.get_payid = lambda: "0412345678"
    vs.get_account_name = lambda: "Test Name"

    res = vs.validate_deposit_screenshot_from_bytes(
        b'fake',
        '0412345678',
        required_amount=100,
        expected_reference=None,
        require_payment_reference=False,
    )
    assert res['valid'] is True
    assert res.get('manual_review_required') is not True
