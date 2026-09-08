"""LLM client + auto-labeling tool for the B-flywheel badcase pipeline.

The B 飞轮型's ``collect_badcase`` step uses the baseline's held-out mispredictions
(ground truth already known). For *unlabeled* badcase sources — online eval failures,
user feedback, red-team inputs — we need an LLM to predict the label so the
retraining step has supervised signal. This module is the single bridge from
"row dict" to "predicted label".
"""
from .client import ChatMessage
from .client import ChatResult
from .client import LLMClient
from .client import LLMClientError
from .client import quick_chat
from .providers import DEFAULT_API_KEY_ENV
from .providers import DEFAULT_BASE_URL
from .providers import DEFAULT_MODEL
from .providers import MODEL_CATALOG
from .providers import ProviderConfig
from .providers import find_model
from .providers import list_models

__all__ = [
    "ChatMessage",
    "ChatResult",
    "LLMClient",
    "LLMClientError",
    "ProviderConfig",
    "DEFAULT_BASE_URL",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_MODEL",
    "MODEL_CATALOG",
    "find_model",
    "list_models",
    "quick_chat",
]
