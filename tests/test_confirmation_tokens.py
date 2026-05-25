"""Confirmation-token helper used by booking COLLECTING YES paths."""

import pytest

from utils.confirmation_tokens import CONFIRMATION_WORD_TOKENS, is_confirmation_token


@pytest.mark.parametrize(
    "text",
    sorted(CONFIRMATION_WORD_TOKENS),
)
def test_is_confirmation_token_word(text: str):
    assert is_confirmation_token(text)
    assert is_confirmation_token(f"  {text}. ")
    assert is_confirmation_token(f"please {text} thanks")


@pytest.mark.parametrize(
    "text",
    [
        "maybe tomorrow",
        "no thanks",
        "sounds good to me",
        "",
        "   ",
        "smoke",  # contains ok substring but not token
    ],
)
def test_is_confirmation_token_negative(text: str):
    assert is_confirmation_token(text) is False


def test_is_confirmation_token_none_safe():
    assert is_confirmation_token("") is False
