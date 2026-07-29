from __future__ import annotations

from pydantic import BaseModel
from pydantic import ConfigDict


class ContractModel(BaseModel):
    """Base model for canonical platform contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)