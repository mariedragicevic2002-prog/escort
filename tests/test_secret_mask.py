import pytest

from utils.secret_mask import mask_secret_value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        (None, ""),
        ("   ", ""),
        ("ab", "**"),
        ("abcd", "****"),
        ("abcdefghij", "******ghij"),
        ("sk-ant-api03-xyz1", "*************xyz1"),
    ],
)
def test_mask_secret_value(raw, expected):
    assert mask_secret_value(raw) == expected
