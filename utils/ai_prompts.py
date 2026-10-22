"""Resolve plain-text prompts and legacy per-intent JSON settings."""

import json


def resolve_system_prompt(raw, intent: str, defaults: dict) -> str:
    fallback = defaults[intent]
    if not raw or not isinstance(raw, str) or not raw.strip():
        return fallback
    text = raw.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if isinstance(parsed, dict):
        selected = parsed.get(intent)
        return selected.strip() if isinstance(selected, str) and selected.strip() else fallback
    if isinstance(parsed, str):
        return parsed.strip() or fallback
    return fallback
