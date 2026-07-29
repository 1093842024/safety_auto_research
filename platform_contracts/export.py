from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .events import ALL_EVENT_MODELS
from .objects import ALL_CONTRACT_MODELS


def export_contract_schemas() -> dict[str, dict[str, dict[str, Any]]]:
    """Export JSON schemas for core objects and event models."""

    return {
        "objects": {
            model.__name__: model.model_json_schema()
            for model in ALL_CONTRACT_MODELS
        },
        "events": {
            model.__name__: model.model_json_schema()
            for model in ALL_EVENT_MODELS
        },
    }


def export_contract_schema_files(output_dir: Path) -> dict[str, dict[str, str]]:
    """Write shared JSON schema files for backend and frontend consumption."""

    schemas = export_contract_schemas()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, str]] = {"objects": {}, "events": {}}

    for group_name, group_schemas in schemas.items():
        group_dir = output_dir / group_name
        group_dir.mkdir(parents=True, exist_ok=True)
        for model_name, schema in group_schemas.items():
            target = group_dir / f"{model_name}.json"
            target.write_text(json.dumps(schema, indent=2, sort_keys=True), encoding="utf-8")
            manifest[group_name][model_name] = str(target.relative_to(output_dir))

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest