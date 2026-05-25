from core.prompt_registry import (
    DEFAULT_SAFETY_LAYER,
    get_layered_prompt,
    get_runtime_persona_prompt,
)


def test_get_layered_prompt_includes_safety_and_sections():
    prompt = get_layered_prompt(
        "fallback",
        persona_prompt="Persona: warm",
        state_prompt="State: CONFIRMED",
        safety_prompt="Safety: no policy decisions.",
        include_default_safety=True,
    )
    assert "Persona: warm" in prompt
    assert "State: CONFIRMED" in prompt
    assert "Safety: no policy decisions." in prompt
    assert DEFAULT_SAFETY_LAYER in prompt


def test_runtime_persona_prompt_uses_admin_settings(monkeypatch):
    vals = {
        "ai_personality_name": "Professional",
        "ai_custom_personality": "Avoid slang.",
        "ai_use_emojis": "false",
        "ai_max_chars": "140",
        "ai_personality_tone": "2",
        "ai_response_length": "2",
    }
    monkeypatch.setattr("core.settings_manager.get_setting", lambda key, default=None: vals.get(key, default))

    prompt = get_runtime_persona_prompt()

    assert "business-like tone" in prompt
    assert "Avoid slang." in prompt
    assert "Do not use emojis." in prompt
    assert "under 140 characters" in prompt
