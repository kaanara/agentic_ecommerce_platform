"""Secrets never enter prompts. Tools ask the SecretsProvider at call time.

Today: environment / `.env`. Later: swap for a vault-backed provider without touching tools.
"""

from __future__ import annotations

import os

from aecom import envfile


class SecretsProvider:
    def __init__(self) -> None:
        self._dotenv = self._read_dotenv()

    @staticmethod
    def _read_dotenv() -> dict[str, str]:
        path = envfile.env_path()
        values: dict[str, str] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def get(self, name: str) -> str | None:
        return os.environ.get(name) or self._dotenv.get(name)

    def reload(self) -> None:
        """Re-read .env (after the Settings page changed it). A variable set in the process environment still wins."""
        self._dotenv = self._read_dotenv()

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def export_prefixed(self, prefix: str) -> None:
        """Expose non-secret-handling library config (e.g. LANGSMITH_*) from .env to os.environ."""
        for key, value in self._dotenv.items():
            if key.startswith(prefix):
                os.environ.setdefault(key, value)


secrets = SecretsProvider()
