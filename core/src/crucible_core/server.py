from __future__ import annotations

import argparse
import multiprocessing
from collections.abc import Sequence

import uvicorn

from .core.config import load_settings
from .logging import configure_logging
from .main import app
from .version import VERSION


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI flags; kept separate so tests avoid starting a server."""
    parser = argparse.ArgumentParser(prog="crucible-core")
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the product version and exit.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.version:
        print(VERSION)
        return
    # Required for PyInstaller onedir/multiprocessing on Windows;
    # no-op on a normal interpreter run.
    multiprocessing.freeze_support()
    configure_logging()
    settings = load_settings()
    # Pass the app object (not an import string) so a frozen
    # executable does not need to re-import the package by name.
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":  # pragma: no cover - frozen entry debug
    main()
