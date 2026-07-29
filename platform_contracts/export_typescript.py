from __future__ import annotations

import argparse
import annotated_types as _at
import datetime as _dt
import enum as _enum
import types
from pathlib import Path
from typing import Any
from typing import get_args
from typing import get_origin
from typing import Union

from . import enums as _enums_mod
from .events import ALL_EVENT_MODELS
from .objects import ALL_CONTRACT_MODELS


def _collect_enums() -> list[type[_enum.Enum]]:
    enums = [
        v
        for v in vars(_enums_mod).values()
        if isinstance(v, type) and issubclass(v, _enum.Enum) and v is not _enum.Enum
    ]
    return sorted(enums, key=lambda e: e.__name__)


def _is_enum(ann: Any) -> bool:
    return isinstance(ann, type) and issubclass(ann, _enum.Enum)


def _scalar_core(ann: Any) -> str:
    """Map a non-union Python annotation to a zod expression."""

    if _is_enum(ann):
        return ann.__name__

    origin = get_origin(ann)
    if origin in (list, set, tuple):
        return f"z.array({_scalar_core(get_args(ann)[0])})"

    if origin is dict:
        _key, value = get_args(ann)
        return f"z.record(z.string(), {_scalar_core(value)})"

    if ann is _dt.datetime:
        return "z.string()"
    if ann is str:
        return "z.string()"
    if ann is int:
        return "z.number().int()"
    if ann is float:
        return "z.number()"
    if ann is bool:
        return "z.boolean()"
    if ann is Any:
        return "z.any()"
    return "z.any()"


def _resolve(ann: Any) -> tuple[str, bool]:
    """Return (zod_expr, is_optional) for an annotation, unwrapping ``X | None`` unions."""

    origin = get_origin(ann)
    if origin in (Union, getattr(types, "UnionType", None)):
        args = get_args(ann)
        non_none = [a for a in args if a is not type(None)]
        if type(None) in args:
            if len(non_none) == 1:
                inner_expr, _inner_optional = _resolve(non_none[0])
                return inner_expr, True
            union_expr = "z.union([" + ", ".join(_resolve(a)[0] for a in non_none) + "])"
            return union_expr, True
        return "z.union([" + ", ".join(_resolve(a)[0] for a in non_none) + "])", False
    return _scalar_core(ann), False


def _apply_constraints(expr: str, scalar_ann: Any, fi: Any) -> str:
    # pydantic v2 stores constraints inside fi.metadata as annotated_types objects.
    for meta in getattr(fi, "metadata", ()) or ():
        if isinstance(meta, _at.Interval):
            if meta.ge is not None:
                expr = f"{expr}.min({meta.ge})"
            if meta.gt is not None:
                expr = f"{expr}.gt({meta.gt})"
            if meta.le is not None:
                expr = f"{expr}.max({meta.le})"
            if meta.lt is not None:
                expr = f"{expr}.lt({meta.lt})"
        elif isinstance(meta, _at.MinLen):
            expr = f"{expr}.min({meta.min_length})"
        elif isinstance(meta, _at.MaxLen):
            expr = f"{expr}.max({meta.max_length})"
        elif isinstance(meta, _at.Len):
            if meta.min_length is not None:
                expr = f"{expr}.min({meta.min_length})"
            if meta.max_length is not None:
                expr = f"{expr}.max({meta.max_length})"
        elif isinstance(meta, _at.Ge):
            expr = f"{expr}.min({meta.ge})"
        elif isinstance(meta, _at.Le):
            expr = f"{expr}.max({meta.le})"
        elif isinstance(meta, _at.Gt):
            expr = f"{expr}.gt({meta.gt})"
        elif isinstance(meta, _at.Lt):
            expr = f"{expr}.lt({meta.lt})"

    # Fallback: some pydantic builds also expose constraints directly on FieldInfo.
    for op, attr in (("min", "ge"), ("max", "le"), ("gt", "gt"), ("lt", "lt")):
        val = getattr(fi, attr, None)
        if val is not None and f".{op}(" not in expr:
            expr = f"{expr}.{op}({val})"
    if getattr(fi, "min_length", None) is not None and ".min(" not in expr:
        expr = f"{expr}.min({fi.min_length})"
    if getattr(fi, "max_length", None) is not None and ".max(" not in expr:
        expr = f"{expr}.max({fi.max_length})"
    return expr


def _build_field_expr(fi: Any) -> str:
    ann = fi.annotation
    expr, optional = _resolve(ann)

    # Constraints apply to the scalar (non-union) type, so unwrap X | None.
    scalar_ann = ann
    origin = get_origin(ann)
    if origin in (Union, getattr(types, "UnionType", None)):
        non_none = [a for a in get_args(ann) if a is not type(None)]
        scalar_ann = non_none[0] if len(non_none) == 1 else None

    expr = _apply_constraints(expr, scalar_ann, fi)
    if not fi.is_required():
        expr = f"{expr}.optional()"
    return expr


def _gen_enum(enum_cls: type[_enum.Enum]) -> str:
    name = enum_cls.__name__
    values = ", ".join(repr(e.value) for e in enum_cls)
    return (
        f"export const {name} = z.enum([{values}] as const);\n"
        f"export type {name} = z.infer<typeof {name}>;\n"
    )


def _gen_model(model: Any) -> str:
    name = model.__name__
    lines = [f"export const {name}Schema = z.object({{"]
    for fname, fi in model.model_fields.items():
        expr = _build_field_expr(fi)
        lines.append(f"  {fname}: {expr},")
    lines.append("});")
    lines.append(f"export type {name} = z.infer<typeof {name}Schema>;")
    return "\n".join(lines)


def generate_typescript() -> str:
    parts: list[str] = []
    parts.append('import { z } from "zod";')
    parts.append("")
    parts.append("// =============================================================================")
    parts.append("// Auto-generated unified platform contracts (objects + events + enums).")
    parts.append("// Source of truth: safety_auto_research/platform_contracts.")
    parts.append("// Do NOT edit by hand; regenerate with export_typescript.py.")
    parts.append("// =============================================================================")
    parts.append("")

    parts.append("// ----- Enums -----")
    for enum_cls in _collect_enums():
        parts.append(_gen_enum(enum_cls))
        parts.append("")

    parts.append("// ----- Core objects -----")
    for model in ALL_CONTRACT_MODELS:
        parts.append(_gen_model(model))
        parts.append("")

    parts.append("// ----- Event models -----")
    for model in ALL_EVENT_MODELS:
        parts.append(_gen_model(model))
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate frontend TypeScript types + zod form constraints from the unified contract schemas.",
    )
    parser.add_argument(
        "--output",
        default="safety_auto_research/platform_contracts/generated/typescript/contracts.ts",
        help="Target .ts file (zod schemas + inferred types).",
    )
    args = parser.parse_args()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generate_typescript(), encoding="utf-8")
    print(f"Wrote TypeScript contracts ({len(_collect_enums())} enums, "
          f"{len(ALL_CONTRACT_MODELS)} objects, {len(ALL_EVENT_MODELS)} events) to {out}")


if __name__ == "__main__":
    main()
