from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    os.chdir(ROOT)

    if _as_bool(os.getenv("RUN_MIGRATIONS_ON_STARTUP"), default=True):
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True)

    os.execv(sys.executable, [sys.executable, "run.py"])


if __name__ == "__main__":
    main()
