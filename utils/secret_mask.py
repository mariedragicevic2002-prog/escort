"""UI-only helpers for showing secrets as masked fingerprints (never log these)."""


def mask_secret_value(value: str | None) -> str:
    """
    Return a non-reversible fingerprint: all ``*`` except the last four characters.
    Empty or whitespace-only input returns an empty string.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if len(s) <= 4:
        return "*" * len(s)
    return ("*" * (len(s) - 4)) + s[-4:]
