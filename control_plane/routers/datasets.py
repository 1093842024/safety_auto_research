"""Dataset management endpoints (数据集管理页).

* ``GET    /datasets``            — list registered datasets (newest first)
* ``POST   /datasets``            — register a local dataset (path-based import;
                                    validates the path, computes live stats)
* ``GET    /datasets/{id}``       — one dataset re-analyzed live + sample rows
* ``DELETE /datasets/{id}``       — remove the registration (data on disk is untouched)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status as http_status
from pydantic import BaseModel

from .. import datasets


class DatasetRegisterRequest(BaseModel):
    """Body for POST /datasets (module-level so FastAPI can build its schema)."""

    name: str
    modality: str  # text | image | audio
    task_kind: str  # classification | llm_generation
    data_path: str
    label_file: str = ""
    label_field: str = ""
    content_field: str = ""
    notes: str = ""


def build_datasets_router(deps) -> APIRouter:
    router = APIRouter()

    @router.get("/datasets", summary="List registered datasets (数据集管理)")
    def list_datasets() -> list[dict]:
        return datasets.list_datasets()

    @router.post(
        "/datasets",
        status_code=http_status.HTTP_201_CREATED,
        summary="Register a local dataset (path-based import; validates + computes stats)",
    )
    def register_dataset(body: DatasetRegisterRequest) -> dict:
        try:
            return datasets.register_dataset(
                name=body.name,
                modality=body.modality,
                task_kind=body.task_kind,
                data_path=body.data_path,
                label_file=body.label_file,
                label_field=body.label_field,
                content_field=body.content_field,
                notes=body.notes,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )

    @router.get("/datasets/{dataset_id}", summary="One dataset, re-analyzed live")
    def get_dataset(dataset_id: str) -> dict:
        rec = datasets.refresh_dataset(dataset_id)
        if rec is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"dataset {dataset_id} not found",
            )
        return rec

    @router.delete("/datasets/{dataset_id}", summary="Remove a dataset registration")
    def delete_dataset(dataset_id: str) -> dict:
        if not datasets.delete_dataset(dataset_id):
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"dataset {dataset_id} not found",
            )
        return {"deleted": dataset_id}

    return router
