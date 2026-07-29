from __future__ import annotations

import uvicorn

from .api import create_app


def main() -> None:
    """Run the unified control-plane API with uvicorn."""

    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
