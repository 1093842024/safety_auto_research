"""tmeoa gateway integration for the OSS benchmark validation harness.

Every OSS repo that originally needed local weights or a vendor API is redirected
through :mod:`tmeoa.client`. :mod:`tmeoa.anthropic_shim` additionally translates
the Anthropic Messages wire format, which is what ARA's ``seal`` pipeline speaks.
"""
from .client import (  # noqa: F401
    ALL_MODELS,
    DEFAULT_TEXT_MODEL,
    DEFAULT_VISION_MODEL,
    TEXT_MODELS,
    VISION_MODELS,
    TmeoaError,
    TokenScore,
    USAGE,
    chat,
    chat_text,
    chat_vision,
    image_to_data_uri,
    probe_models,
    score_first_token,
)

__all__ = [
    "ALL_MODELS",
    "DEFAULT_TEXT_MODEL",
    "DEFAULT_VISION_MODEL",
    "TEXT_MODELS",
    "VISION_MODELS",
    "TmeoaError",
    "TokenScore",
    "USAGE",
    "chat",
    "chat_text",
    "chat_vision",
    "image_to_data_uri",
    "probe_models",
    "score_first_token",
]
