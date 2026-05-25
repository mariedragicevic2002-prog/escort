"""
Compact client profile builder for AI context injection.
"""

from typing import Any


def _compute_avg_message_length(history: list[Any] | None) -> float | None:
    """Compute average non-empty message length from recent history."""
    if not history:
        return None
    lengths: list[int] = []
    for item in history:
        if isinstance(item, dict):
            content = item.get("content")
            if content is None:
                content = item.get("message_body")
        else:
            content = item
        if content is None:
            continue
        text = str(content).strip()
        if text:
            lengths.append(len(text))
    if not lengths:
        return None
    return round(sum(lengths) / len(lengths), 1)


def build_client_profile(
    state: dict[str, Any] | None,
    client_context: dict[str, Any] | None,
    history: list[Any] | None = None,
) -> dict[str, Any]:
    """Build a compact, stable profile from live state + historical context."""
    s = state or {}
    cc = client_context or {}
    total = cc.get("total_bookings")
    profile = {
        "phone_number": (s.get("phone_number") or cc.get("phone_number") or "").strip() or None,
        "client_name": (s.get("client_name") or "").strip() or None,
        "is_returning_client": bool(total and int(total) > 0),
        "total_bookings": int(total or 0),
        "preferred_duration": cc.get("preferred_duration"),
        "preferred_experience": cc.get("preferred_experience"),
        "preferred_location": cc.get("preferred_location"),
        "last_booking_date": cc.get("last_booking_date"),
        "active_state": s.get("current_state") or "NEW",
        "avg_message_length": _compute_avg_message_length(history),
    }
    return profile


def build_client_profile_with_memory(
    state: dict | None,
    client_context: dict | None,
    client_memory_service=None,
    phone_number: str | None = None,
    history: list[Any] | None = None,
) -> dict:
    """Build profile + inject long-term memories if available."""
    profile = build_client_profile(state, client_context, history=history)
    if phone_number:
        profile["phone_number"] = (phone_number or "").strip() or profile.get("phone_number")
    if client_memory_service and phone_number:
        try:
            memories = client_memory_service.get_memories(phone_number, limit=5)
            profile["long_term_memories"] = memories
            profile["memory_prompt_snippet"] = client_memory_service.format_for_prompt(phone_number)
        except Exception:
            pass
    return profile


def profile_to_prompt_snippet(profile: dict[str, Any] | None) -> str:
    """Serialize compact profile into short prompt-friendly prose."""
    p = profile if isinstance(profile, dict) else {}
    parts: list[str] = []
    name = p.get("client_name")
    if name:
        parts.append(f"Client name: {name}.")
    total = p.get("total_bookings")
    total_i = int(total) if total is not None else 0
    if total_i <= 0:
        parts.append("Client is a new client.")
    else:
        label = "booking" if total_i == 1 else "bookings"
        parts.append(f"Client is a returning client ({total_i} past {label}).")
        # Returning client personalisation: skip re-explaining basics
        parts.append(
            "This client already knows how the booking process works — skip re-explaining basics "
            "and go straight to confirming details."
        )
    prefs = [p.get("preferred_duration"), p.get("preferred_experience"), p.get("preferred_location")]
    prefs = [str(x) for x in prefs if x not in (None, "", [])]
    if prefs and total_i > 0:
        # For returning clients, refer to their usual preferences by name
        parts.append(
            f"Their usual preferences are: {', '.join(prefs)} — "
            "you can reference these directly (e.g. 'would you like the same as last time?')."
        )
    elif prefs:
        parts.append(f"Typical preferences: {', '.join(prefs)}.")
    last_date = p.get("last_booking_date")
    if last_date:
        parts.append(f"Last booking: {last_date}.")
    avg_message_length = p.get("avg_message_length")
    try:
        avg_message_length = float(avg_message_length) if avg_message_length is not None else None
    except (TypeError, ValueError):
        avg_message_length = None
    if avg_message_length is not None and avg_message_length < 15:
        parts.append("This client sends brief messages — keep your response under 80 characters.")
    elif avg_message_length is not None and avg_message_length > 100:
        parts.append("This client is detailed — you may give fuller responses (up to 200 characters).")
    snippet = p.get("memory_prompt_snippet")
    if snippet:
        parts.append(str(snippet).strip())
    episodic = p.get("episodic_prompt_snippet")
    if episodic:
        parts.append(str(episodic).strip())
    return " ".join(parts)
