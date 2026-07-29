from __future__ import annotations

import argparse
from pathlib import Path

from .export import export_contract_schema_files


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export unified platform contract schemas for shared frontend/backend use.",
    )
    parser.add_argument(
        "--output-dir",
        default="safety_auto_research/platform_contracts/generated",
        help="Directory where JSON schema files will be written.",
    )
    args = parser.parse_args()

    manifest = export_contract_schema_files(Path(args.output_dir))
    print(
        f"Exported {len(manifest['objects'])} object schemas and {len(manifest['events'])} event schemas to {args.output_dir}"
    )


if __name__ == "__main__":
    main()