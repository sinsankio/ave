"""Claude client factory. Every agent gets its client from here so tracing is applied once."""

from __future__ import annotations

import os
from pathlib import Path

import anthropic

from ave.observability import Tracer


def anthropic_credentials_source() -> str | None:
    """Name the credential source the SDK would use, without reading the secret."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            return var
    if os.environ.get("ANTHROPIC_PROFILE"):
        return "ANTHROPIC_PROFILE"
    if (Path.home() / ".config" / "anthropic").is_dir():
        return "ant profile (~/.config/anthropic)"
    return None


def make_anthropic_client(tracer: Tracer, **kwargs) -> anthropic.Anthropic:
    return tracer.wrap_anthropic(anthropic.Anthropic(**kwargs))
