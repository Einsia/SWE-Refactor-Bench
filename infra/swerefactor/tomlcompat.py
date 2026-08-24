"""TOML reading that works on every interpreter a task image might ship.

``tomllib`` is stdlib from 3.11.  The verifier images are pinned and modern, but
authoring machines are not, so fall back to ``tomli`` when it is available and
say something useful when neither is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:  # Python >= 3.11
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 and older
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise SystemExit(
            "SWERefactorBench needs a TOML reader: run on Python 3.11+ (tomllib) "
            "or `pip install tomli`."
        ) from exc


def loads(text: str) -> dict[str, Any]:
    return _toml.loads(text)


def load(path: str | Path) -> dict[str, Any]:
    """Read one TOML file, reporting the path when it does not parse."""
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            return _toml.load(fh)
    except FileNotFoundError:
        raise
    except Exception as exc:  # TOMLDecodeError differs between backends
        raise ValueError(f"{path}: not valid TOML: {exc}") from exc
