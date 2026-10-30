"""Safe writer for ``.env`` (the dashboard Settings page edits secrets through this and nothing else).

Guarantees: comments and unrelated lines stay as they are; the file is replaced atomically (temp file in the same
directory, then ``os.replace``); permissions are 0600; exactly one ``.env.bak`` of the previous content is kept; the
new content is parsed back and compared before the swap. Values are never logged or returned.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from aecom.config import ROOT

_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def env_path() -> Path:
    return ROOT / ".env"


def parse(text: str) -> dict[str, str]:
    """Same rules as SecretsProvider._read_dotenv: KEY=value, # comments, optional quotes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _check(key: str, value: str | None) -> None:
    if not _KEY.match(key):
        raise ValueError(f"invalid variable name: {key!r}")
    if value is not None and any(c in value for c in "\n\r\x00"):
        raise ValueError(f"{key}: value must be a single line")


def set_values(updates: dict[str, str | None], path: Path | None = None) -> list[str]:
    """Set (str) or remove (None) variables. Returns the names that changed. Raises ValueError on bad input."""
    path = path or env_path()
    for key, value in updates.items():
        _check(key, value)
    old_text = path.read_text() if path.exists() else ""
    lines = old_text.splitlines()
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if stripped and not stripped.startswith("#") and "=" in stripped else None
        if key in pending:
            value = pending.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
        else:
            out.append(line)
    for key, value in pending.items():
        if value is not None:
            out.append(f"{key}={value}")
    new_text = "\n".join(out).rstrip("\n") + "\n" if out else ""

    expect = parse(old_text)
    for key, value in updates.items():
        if value is None:
            expect.pop(key, None)
        else:
            expect[key] = value.strip().strip('"').strip("'")
    if parse(new_text) != expect:
        raise ValueError("refusing to write: the new .env does not parse back to the expected values")
    changed = [k for k, v in updates.items() if parse(old_text).get(k) != (None if v is None else v.strip().strip('"').strip("'"))]
    if not changed:
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_name(path.name + ".bak")
        bak.write_text(old_text)
        os.chmod(bak, 0o600)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(new_text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return changed
