"""Per-instance configuration for data-librarian.

The same code base backs several Slack apps (for example "IGI Data Librarian" and
"PIRL Data Librarian"). Each app runs with its own TOML file, selected with the
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
# "run" (launch a RAFT workflow) exists only when the instance has a [raft] section.
COMMANDS = ("status", "sequencing-accession", "reference-accession", "help", "run")
OPTIONAL_COMMANDS = ("run",)

# Registry fields slugged into a dataset ID, in order (see [registry] id_fields).
DEFAULT_ID_FIELDS = (
    "source", "sample_id", "sample_type", "analyte", "platform", "library_type",
    "species", "sequenced_year", "perturbation", "bio_rep_id", "tech_rep_id",
)
RAFT_INPUT_TYPES = ("fastqs", "bams")
RAFT_LAUNCHERS = ("sbatch", "local")

_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_COMMAND_RE = re.compile(r"^/[a-z0-9][a-z0-9_-]*$")
_MAX_COMMAND_LENGTH = 32  # Slack's limit, including the leading slash.

# Chat assistant defaults (used only when an Anthropic credential is configured).
DEFAULT_ASSISTANT_MODEL = "claude-opus-5-5"
DEFAULT_ASSISTANT_EFFORT = "medium"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class LimsConfig:
    """Connection to a LIMS used only to catalog accessioned data (never for open queries)."""

    url: str
    read_token_env: str
    write_token_env: str
    timeout_seconds: float = 10.0
    sample_label: str = "Sample ID or LIMS number"

    @property
    def read_token(self) -> str:
        return os.environ.get(self.read_token_env, "").strip()

    @property
    def write_token(self) -> str:
        return os.environ.get(self.write_token_env, "").strip()


@dataclass(frozen=True)
class RaftWorkflow:
    """One RAFT off-the-shelf workflow the bot may set up and launch."""

    id: str  # bot-side key, e.g. "qc"
    name: str  # shown in Slack
    workflow: str  # RAFT workflow name (raft run --workflow)
    version: str  # RAFT workflow version (raft run --version)
    species: str = "human"
    input_data: str = "fastqs"  # fastqs | bams
    workflow_profile: str = ""  # optional raft run --workflow-profile
    enabled: bool = True
    compatible_analytes: tuple[str, ...] = ()
    compatible_library_types: tuple[str, ...] = ()

    @property
    def workflow_id(self) -> str:
        """Key in the registry's workflow_definitions table."""
        return f"raft:{self.id}"


@dataclass(frozen=True)
class RaftConfig:
    workspace: Path  # directory holding this bot's own .raft.cfg
    executable: str = "raft"
    launcher: str = "sbatch"  # sbatch | local
    nextflow_profile: str = ""  # raft run --profile at launch (empty = Nextflow default)
    sbatch_args: tuple[str, ...] = ()
    setup_timeout_seconds: int = 1800
    poll_seconds: int = 300
    workflows: tuple[RaftWorkflow, ...] = ()

    @property
    def enabled_workflows(self) -> tuple[RaftWorkflow, ...]:
        return tuple(w for w in self.workflows if w.enabled)

    def workflow(self, workflow_id: str) -> RaftWorkflow:
        for workflow in self.workflows:
            if workflow.id == workflow_id or workflow.workflow_id == workflow_id:
                return workflow
        raise KeyError(f"Unknown RAFT workflow {workflow_id!r}")


@dataclass(frozen=True)
class InstanceConfig:
    """Settings that distinguish one librarian instance from another."""

    name: str  # Slack app name, e.g. "IGI Data Librarian"
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
    assistant_model: str = DEFAULT_ASSISTANT_MODEL
    assistant_effort: str = DEFAULT_ASSISTANT_EFFORT
    assistant_fallbacks: bool = True  # retry declined requests on Anthropic's recommended fallback model
    # When true, channel messages never contain file names, paths, sample IDs or dataset IDs;
    # those go only to the requesting user (modal or DM). See [privacy] in instances/igi.toml.
    protect_phi: bool = False
    approvers: tuple[str, ...] = ()  # Slack user IDs allowed to write to the LIMS and launch RAFT
    id_fields: tuple[str, ...] = DEFAULT_ID_FIELDS
    vocabulary_overrides: tuple[tuple[str, tuple[str, ...]], ...] = ()
    lims: LimsConfig | None = None
    raft: RaftConfig | None = None
    source_path: Path | None = None

    def command(self, key: str) -> str:
        """Return the slash command for ``key`` (one of ``COMMANDS``)."""
        if key not in COMMANDS:
            raise KeyError(f"Unknown command key {key!r}; expected one of {COMMANDS}")
        return dict(self.command_overrides).get(key) or f"/{self.command_prefix}-{key}"

    def command_enabled(self, key: str) -> bool:
        return key not in OPTIONAL_COMMANDS or (key == "run" and self.raft is not None)

    @property
    def commands(self) -> dict[str, str]:
        """Slash commands this instance registers (optional ones only when configured)."""
        return {key: self.command(key) for key in COMMANDS if self.command_enabled(key)}

    def is_approver(self, user_id: str | None) -> bool:
        return bool(user_id) and user_id in self.approvers


def _resolve(path: str, base: Path) -> Path:
    candidate = Path(os.path.expandvars(path)).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


def _str_tuple(value, what: str, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{what} must be a list of strings{where}")
    return tuple(item.strip() for item in value if item.strip())


def _parse_lims(lims: dict, where: str) -> LimsConfig | None:
    if not lims:
        return None
    url = str(lims.get("url", "")).strip().rstrip("/")
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        raise ValueError(f"[lims] url must be an https:// URL{where}; got {url!r}")
    return LimsConfig(
        url=url,
        read_token_env=str(lims.get("read_token_env") or "LIMS_READ_TOKEN"),
        write_token_env=str(lims.get("write_token_env") or "LIMS_WRITE_TOKEN"),
        timeout_seconds=float(lims.get("timeout_seconds", 10)),
        sample_label=str(lims.get("sample_label") or "Sample ID or LIMS number"),
    )


def _parse_raft(raft: dict, base: Path, where: str) -> RaftConfig | None:
    if not raft:
        return None
    workspace = raft.get("workspace")
    if not workspace:
        raise ValueError(f"[raft] workspace is required{where}")
    launcher = str(raft.get("launcher", "sbatch"))
    if launcher not in RAFT_LAUNCHERS:
        raise ValueError(f"[raft] launcher must be one of {RAFT_LAUNCHERS}{where}; got {launcher!r}")
    workflows = []
    for item in raft.get("workflows", []) or []:
        workflow_key = str(item.get("id", "")).strip()
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", workflow_key):
            raise ValueError(f"[[raft.workflows]] id must be lowercase letters, digits, '-' or '_'{where}")
        input_data = str(item.get("input_data", "fastqs"))
        if input_data not in RAFT_INPUT_TYPES:
            raise ValueError(f"[[raft.workflows]] input_data must be one of {RAFT_INPUT_TYPES}{where}")
        enabled = item.get("enabled", True)
        version = str(item.get("version", "")).strip()
        if enabled and not version:
            raise ValueError(f"RAFT workflow {workflow_key!r} is enabled but has no version{where}")
        workflows.append(RaftWorkflow(
            id=workflow_key,
            name=str(item.get("name") or workflow_key),
            workflow=str(item.get("workflow") or workflow_key),
            version=version,
            species=str(item.get("species", "human")),
            input_data=input_data,
            workflow_profile=str(item.get("workflow_profile", "")),
            enabled=bool(enabled),
            compatible_analytes=_str_tuple(item.get("compatible_analytes"), "compatible_analytes", where),
            compatible_library_types=_str_tuple(item.get("compatible_library_types"), "compatible_library_types", where),
        ))
    if len({w.id for w in workflows}) != len(workflows):
        raise ValueError(f"[[raft.workflows]] ids must be distinct{where}")
    return RaftConfig(
        workspace=_resolve(str(workspace), base),
        executable=str(raft.get("executable", "raft")),
        launcher=launcher,
        nextflow_profile=str(raft.get("nextflow_profile", "")),
        sbatch_args=_str_tuple(raft.get("sbatch_args"), "[raft] sbatch_args", where),
        setup_timeout_seconds=int(raft.get("setup_timeout_seconds", 1800)),
        poll_seconds=max(30, int(raft.get("poll_seconds", 300))),
        workflows=tuple(workflows),
    )


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

    assistant = data.get("assistant", {}) or {}
    assistant_model = str(assistant.get("model") or DEFAULT_ASSISTANT_MODEL).strip()
    assistant_effort = str(assistant.get("effort") or DEFAULT_ASSISTANT_EFFORT).strip()
    if assistant_effort not in EFFORT_LEVELS:
        raise ValueError(f"[assistant] effort must be one of {EFFORT_LEVELS}{where}; got {assistant_effort!r}")
    assistant_fallbacks = assistant.get("fallbacks", True)
    if not isinstance(assistant_fallbacks, bool):
        raise ValueError(f"[assistant] fallbacks must be true or false{where}")

    privacy = data.get("privacy", {}) or {}
    protect_phi = privacy.get("protect_phi", False)
    if not isinstance(protect_phi, bool):
        raise ValueError(f"[privacy] protect_phi must be true or false{where}")
    approvers = _str_tuple((data.get("permissions", {}) or {}).get("approvers"), "[permissions] approvers", where)

    id_fields = _str_tuple(registry.get("id_fields"), "[registry] id_fields", where) or DEFAULT_ID_FIELDS
    vocabulary = data.get("vocabulary", {}) or {}
    vocabulary_overrides = tuple(sorted(
        (str(field), _str_tuple(values, f"[vocabulary] {field}", where)) for field, values in vocabulary.items()
    ))

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
        assistant_model=assistant_model,
        assistant_effort=assistant_effort,
        assistant_fallbacks=assistant_fallbacks,
        protect_phi=protect_phi,
        approvers=approvers,
        id_fields=id_fields,
        vocabulary_overrides=vocabulary_overrides,
        lims=_parse_lims(data.get("lims", {}) or {}, where),
        raft=_parse_raft(data.get("raft", {}) or {}, base, where),
        source_path=source_path,
    )

    for command in config.commands.values():
        if not _COMMAND_RE.match(command) or len(command) > _MAX_COMMAND_LENGTH:
            raise ValueError(
                f"Invalid slash command {command!r}{where}: must start with '/', use lowercase "
                f"letters, digits, '-' or '_', and be at most {_MAX_COMMAND_LENGTH} characters"
            )
    if len(set(config.commands.values())) != len(config.commands):
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
