"""PyInstaller entry point for the crucible-core onedir runtime.

Importable so the ``.spec`` can reference it by path, and keeps the
frozen startup contract in one place: ``freeze_support()`` first,
then the regular server startup (127.0.0.1:7331, migrations and
recovery via the app lifespan).
"""

from __future__ import annotations

import multiprocessing

from crucible_core.server import main


def run() -> None:
    multiprocessing.freeze_support()
    main()


if __name__ == "__main__":  # pragma: no cover - frozen entry
    run()
