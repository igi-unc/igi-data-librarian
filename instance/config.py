"""Per-instance configuration for data-librarian.

The same code base backs several Slack apps (for example "IGI Librarian" and
"PIRL Librarian"). Each app runs with its own TOML file, selected with the
``DATA_LIBRARIAN_INSTANCE`` environment variable. See ``instances/`` for examples.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_ENV = "DATA_LIBRARIAN_INSTANCE"
REGISTRY_DIR_ENV = "DATA_LIBRARIAN_REGISTRY_DIR"

# Command keys and their default suffix after the instance's command prefix.
COMMANDS = ("status", "sequencing-accession", "reference-accession", "help")

_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_COMMAND_RE = re.compile(r"^/[a-z0-9][a-z0-9_-]*$")
_MAX_COMMAND_LENGTH = 32  # Slack's limit, including the leading slash.


@dataclass(frozen=True)
class InstanceConfig:
    """Settings that distinguish one librarian instance from another."""

    name: str  # Slack app name, e.g. "IGI Librarian"
    label: str  # Short group name used in messages, e.g. "IGI"
    command_prefix: str  # e.g. "igi" -> /igi-status
    id_prefix: str  # e.g. "igi" -> igi_<fields>, igi_ref_<fields>
    description: str
    bot_display_name: str
    registry_dir: Path | None = None
    sequencing_tsv: str = "sequencing.tsv"
    references_tsv: str = "references.tsv"
    database: str = "registry.sqlite3"
    command_overrides: tuple[tuple[str, str], ...] = ()
    source_path: Path | None = None

    def command(self, key: str) -> str:
        """Return the slash command for ``key`` (one of ``COMMANDS``)."""
        if key not in COMMANDS:
            raise KeyError(f"Unknown command key {key!r}; expected one of {COMMANDS}")
        return dict(self.command_overrides).get(key) or f"/{self.command_prefix}-{key}"

    @property
    def commands(self) -> dict[str, str]:
        return {key: self.command(key) for key in COMMANDS}


def _resolve(path: str, base: Path) -> Path:
    candidate = Path(os.path.expandvars(path)).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


def parse_instance(data: dict, source_path: Path | None = None) -> InstanceConfig:
    """Build and validate an ``InstanceConfig`` from parsed TOML data."""
    where = f" in {source_path}" if source_path else ""

    name = str(data.get("name", "")).strip()
    prefix = str(data.get("command_prefix", "")).strip()
    if not name:
        raise ValueError(f"'name' is required{where}")
    if not _PREFIX_RE.match(prefix):
        raise ValueError(
            f"'command_prefix' must be lowercase letters, digits, '-' or '_'{where}; got {prefix!r}"
        )

    label = str(data.get("label") or prefix.upper()).strip()
    id_prefix = str(data.get("id_prefix") or prefix.replace("-", "_")).strip()
    if not re.match(r"^[a-z0-9_]+$", id_prefix):
        raise ValueError(f"'id_prefix' must be lowercase letters, digits or '_'{where}; got {id_prefix!r}")

    registry = data.get("registry", {}) or {}
    base = source_path.parent if source_path else PROJECT_ROOT
    registry_dir_value = os.environ.get(REGISTRY_DIR_ENV) or registry.get("dir")
    registry_dir = _resolve(registry_dir_value, base) if registry_dir_value else None

    overrides = {str(k): str(v) for k, v in (data.get("commands", {}) or {}).items()}
    unknown = set(overrides) - set(COMMANDS)
    if unknown:
        raise ValueError(f"Unknown [commands] keys{where}: {sorted(unknown)}; expected {COMMANDS}")

    config = InstanceConfig(
        name=name,
        label=label,
        command_prefix=prefix,
        id_prefix=id_prefix,
        description=str(
            data.get("description") or f"Accession and monitor {label} sequencing datasets and references"
        ),
        bot_display_name=str(data.get("bot_display_name") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")),
        registry_dir=registry_dir,
        sequencing_tsv=str(registry.get("sequencing_tsv", "sequencing.tsv")),
        references_tsv=str(registry.get("references_tsv", "references.tsv")),
        database=str(registry.get("database", "registry.sqlite3")),
        command_overrides=tuple(sorted(overrides.items())),
        source_path=source_path,
    )

    for command in config.commands.values():
        if not _COMMAND_RE.match(command) or len(command) > _MAX_COMMAND_LENGTH:
            raise ValueError(
                f"Invalid slash command {command!r}{where}: must start with '/', use lowercase "
                f"letters, digits, '-' or '_', and be at most {_MAX_COMMAND_LENGTH} characters"
            )
    if len(set(config.commands.values())) != len(COMMANDS):
        raise ValueError(f"Slash commands must be distinct{where}: {config.commands}")
    return config


def load_instance(path: str | Path) -> InstanceConfig:
    """Load an instance config from a TOML file (relative paths are from the project root)."""
    resolved = _resolve(str(path), PROJECT_ROOT)
    if not resolved.is_file():
        raise FileNotFoundError(f"Instance config not found: {resolved}")
    with resolved.open("rb") as handle:
        return parse_instance(tomllib.load(handle), source_path=resolved)


@lru_cache(maxsize=None)
def _load_cached(path: str) -> InstanceConfig:
    return load_instance(path)


def get_instance() -> InstanceConfig:
    """Return the active instance, selected by ``DATA_LIBRARIAN_INSTANCE``."""
    path = os.environ.get(INSTANCE_ENV)
    if not path:
        raise RuntimeError(
            f"{INSTANCE_ENV} is not set; point it at an instance file such as instances/igi.toml"
        )
    return _load_cached(path)


def clear_cache() -> None:
    _load_cached.cache_clear()
