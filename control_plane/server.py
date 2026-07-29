from __future__ import annotations

import os

import uvicorn

from .api import create_app


def main() -> None:
    """Run the unified control-plane API with uvicorn."""

    app = create_app()
    host = os.environ.get("CONTROL_HOST", "127.0.0.1")
    port = int(os.environ.get("CONTROL_PORT", "8000"))
    log_level = os.environ.get("CONTROL_LOG_LEVEL", "info")
    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
