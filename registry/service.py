from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from instance import DEFAULT_ID_FIELDS, InstanceConfig, RaftConfig, get_instance


HEADERS = [
    "uuid", "source", "sample_type", "analyte", "platform", "library_type",
    "species", "sequencing_provider", "sequenced_year", "sample_id",
    "perturbation", "bio_rep_id", "tech_rep_id", "lbg_path", "longleaf_path",
    "labarchives_path", "sample_type_origin", "sequencing_type",
    "sequencing_read_type", "sequencing_instrument", "capture_kit",
    "library_primary_contact_person", "notes", "lims_number",
]
# Earlier header (before lims_number); such files are upgraded in place on first use.
LEGACY_HEADERS = HEADERS[:-1]

REFERENCE_HEADERS = [
    "reference_id", "name", "reference_type", "species", "assembly",
    "version", "provider", "source_url", "lbg_path", "longleaf_path",
    "file_format", "primary_contact_person", "notes",
]

REFERENCE_CONTROLLED_VALUES = {
    "reference_type": ["genome", "transcriptome", "annotation", "index", "panel", "database", "other"],
    "species": ["human", "mouse"],
    "file_format": ["fasta", "fastq", "gtf", "gff", "bed", "vcf", "tsv", "csv", "json", "archive", "other"],
}

WORKFLOW_DEFINITIONS = [
    {
        "workflow_id": "provided_hla", "name": "Externally provided HLA typing", "version": "1",
        "analysis_type": "provided_hla", "compatible_analytes": [], "compatible_library_types": [],
        "description": "HLA alleles supplied by a user or external source during accession.",
    },
    {
        "workflow_id": "raft_hla_typing", "name": "HLA typing", "version": "unconfigured",
        "analysis_type": "hla", "compatible_analytes": ["dna", "rna"], "compatible_library_types": [],
        "description": "Infer HLA alleles and retain caller confidence and provenance.",
    },
    {
        "workflow_id": "raft_gene_expression", "name": "Gene-level expression", "version": "unconfigured",
        "analysis_type": "gene_expression", "compatible_analytes": ["rna"], "compatible_library_types": ["poly-a"],
        "description": "Quantify gene-level TPM and counts.",
    },
    {
        "workflow_id": "raft_transcript_expression", "name": "Transcript-level expression", "version": "unconfigured",
        "analysis_type": "transcript_expression", "compatible_analytes": ["rna"], "compatible_library_types": ["poly-a"],
        "description": "Quantify transcript-level TPM and estimated counts.",
    },
    {
        "workflow_id": "raft_fusion_detection", "name": "Fusion detection", "version": "unconfigured",
        "analysis_type": "fusion", "compatible_analytes": ["rna"], "compatible_library_types": ["poly-a"],
        "description": "Detect and annotate gene fusions with supporting-read evidence.",
    },
]

UUID_FIELDS = list(DEFAULT_ID_FIELDS)  # default; instances may override with [registry] id_fields

WORKFLOW_STATUSES = {"requested", "preparing", "ready", "queued", "running", "completed", "failed", "cancelled"}

CONTROLLED_VALUES = {
    "source": ["1015", "14169", "aml_0001", "balbc", "bbn963", "bl6", "t11", "tc-797", "u87", "u937-a2"],
    "sample_type": ["cell-line", "tumor", "normal-tissue", "normal-blood"],
    "analyte": ["dna", "rna"],
    "platform": ["illumina", "ont", "pacbio", "10x-ont"],
    "library_type": ["poly-a", "wgs", "wes"],
    "species": ["human", "mouse"],
    "sequencing_type": ["bulk", "single-cell"],
    "sequencing_read_type": ["paired-end", "single-end", "ont", "hifi"],
    "sequencing_instrument": ["minion", "sequel ii"],
}


@dataclass(frozen=True)
class RegistryStatus:
    datasets: int
    files: int
    unregistered: tuple[Path, ...]
    missing: tuple[Path, ...]
    reference_datasets: int = 0
    reference_files: int = 0
    unregistered_references: tuple[Path, ...] = ()
    missing_references: tuple[Path, ...] = ()
    requested_workflows: int = 0
    running_workflows: int = 0
    failed_workflows: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


class RegistryService:
    """Deterministic local TSV/SQLite registry used by the Slack interface."""

    def __init__(
        self,
        root: Path,
        *,
        id_prefix: str = "data",
        sequencing_tsv: str = "sequencing.tsv",
        references_tsv: str = "references.tsv",
        database: str = "registry.sqlite3",
        id_fields: tuple[str, ...] | list[str] | None = None,
        controlled_values: dict[str, list[str]] | None = None,
    ):
        self.root = root.resolve()
        self.id_prefix = id_prefix
        self.id_fields = list(id_fields or UUID_FIELDS)
        unknown = set(self.id_fields) - set(HEADERS)
        if unknown:
            raise ValueError(f"Unknown registry id_fields: {sorted(unknown)}")
        self.controlled_values = {
            field: list(values) for field, values in (controlled_values or CONTROLLED_VALUES).items()
        }
        self._overridden_fields: set[str] = set()
        self._raft: RaftConfig | None = None
        self.sequencing_dir = self.root / "sequencing"
        self.references_dir = self.root / "references"
        self.tsv_path = self.root / sequencing_tsv
        self.references_tsv_path = self.root / references_tsv
        self.db_path = self.root / database

    @classmethod
    def from_instance(cls, config: InstanceConfig) -> "RegistryService":
        if config.registry_dir is None:
            where = f" in {config.source_path}" if config.source_path else ""
            raise RuntimeError(
                f"No registry directory configured for {config.name}: set [registry] dir{where} "
                "or DATA_LIBRARIAN_REGISTRY_DIR"
            )
        controlled = {field: list(values) for field, values in CONTROLLED_VALUES.items()}
        overrides = dict(config.vocabulary_overrides)
        for field, values in overrides.items():
            if field not in CONTROLLED_VALUES:
                raise ValueError(
                    f"[vocabulary] {field} is not a controlled field; expected one of {sorted(CONTROLLED_VALUES)}"
                )
            controlled[field] = list(values)
        service = cls(
            config.registry_dir,
            id_prefix=config.id_prefix,
            sequencing_tsv=config.sequencing_tsv,
            references_tsv=config.references_tsv,
            database=config.database,
            id_fields=config.id_fields,
            controlled_values=controlled,
        )
        service._overridden_fields = set(overrides)
        service._raft = config.raft
        return service

    @classmethod
    def from_environment(cls) -> "RegistryService":
        """Build the service for the active instance (DATA_LIBRARIAN_INSTANCE)."""
        return cls.from_instance(get_instance())

    def initialize(self) -> None:
        self.sequencing_dir.mkdir(parents=True, exist_ok=True)
        self.references_dir.mkdir(parents=True, exist_ok=True)
        if not self.tsv_path.exists():
            with self.tsv_path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle, delimiter="\t", lineterminator="\n").writerow(HEADERS)
        else:
            with self.tsv_path.open(newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle, delimiter="\t"), None)
            if header == LEGACY_HEADERS:
                self._upgrade_tsv(self.tsv_path, HEADERS)
            elif header != HEADERS:
                raise RuntimeError(f"Unexpected TSV header in {self.tsv_path}")
        if not self.references_tsv_path.exists():
            with self.references_tsv_path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle, delimiter="\t", lineterminator="\n").writerow(REFERENCE_HEADERS)
        else:
            with self.references_tsv_path.open(newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle, delimiter="\t"), None)
            if header != REFERENCE_HEADERS:
                raise RuntimeError(f"Unexpected TSV header in {self.references_tsv_path}")
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    internal_id TEXT PRIMARY KEY,
                    spreadsheet_uuid TEXT NOT NULL UNIQUE,
                    row_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    dataset_internal_id TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT,
                    accessioned_at TEXT NOT NULL,
                    FOREIGN KEY(dataset_internal_id) REFERENCES datasets(internal_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    dataset_internal_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vocabulary (
                    field_name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(field_name, value)
                );
                CREATE TABLE IF NOT EXISTS reference_datasets (
                    internal_id TEXT PRIMARY KEY,
                    reference_id TEXT NOT NULL UNIQUE,
                    row_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reference_files (
                    path TEXT PRIMARY KEY,
                    reference_internal_id TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT,
                    accessioned_at TEXT NOT NULL,
                    FOREIGN KEY(reference_internal_id) REFERENCES reference_datasets(internal_id)
                );
                CREATE TABLE IF NOT EXISTS workflow_definitions (
                    workflow_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    analysis_type TEXT NOT NULL,
                    compatible_analytes_json TEXT NOT NULL,
                    compatible_library_types_json TEXT NOT NULL,
                    description TEXT NOT NULL,
                    entrypoint TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    dataset_internal_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    scheduler_job_id TEXT,
                    parameters_json TEXT NOT NULL,
                    software_versions_json TEXT NOT NULL,
                    reference_ids_json TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error_message TEXT,
                    FOREIGN KEY(workflow_id) REFERENCES workflow_definitions(workflow_id),
                    FOREIGN KEY(dataset_internal_id) REFERENCES datasets(internal_id)
                );
                CREATE INDEX IF NOT EXISTS workflow_runs_status_idx ON workflow_runs(status);
                CREATE INDEX IF NOT EXISTS workflow_runs_dataset_idx ON workflow_runs(dataset_internal_id);
                CREATE TABLE IF NOT EXISTS output_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    output_type TEXT NOT NULL,
                    path_or_uri TEXT NOT NULL,
                    checksum TEXT,
                    format TEXT,
                    reference_id TEXT,
                    row_count INTEGER,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, output_type, path_or_uri),
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS hla_calls (
                    run_id TEXT NOT NULL,
                    gene TEXT NOT NULL,
                    allele TEXT NOT NULL,
                    resolution TEXT,
                    confidence REAL,
                    caller TEXT,
                    call_rank INTEGER,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, gene, allele, call_rank),
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS gene_expression (
                    run_id TEXT NOT NULL,
                    gene_id TEXT NOT NULL,
                    gene_symbol TEXT,
                    tpm REAL,
                    raw_count REAL,
                    PRIMARY KEY(run_id, gene_id),
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS gene_expression_symbol_idx ON gene_expression(gene_symbol);
                CREATE TABLE IF NOT EXISTS transcript_expression (
                    run_id TEXT NOT NULL,
                    transcript_id TEXT NOT NULL,
                    gene_id TEXT,
                    tpm REAL,
                    estimated_count REAL,
                    PRIMARY KEY(run_id, transcript_id),
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS fusion_calls (
                    fusion_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    gene_5p TEXT NOT NULL,
                    gene_3p TEXT NOT NULL,
                    breakpoint_5p TEXT,
                    breakpoint_3p TEXT,
                    junction_reads INTEGER,
                    spanning_reads INTEGER,
                    confidence REAL,
                    caller TEXT,
                    annotations_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(run_id)
                );
                """
            )
            for field, values in self.controlled_values.items():
                connection.executemany(
                    "INSERT OR IGNORE INTO vocabulary VALUES (?, ?, ?)",
                    [(field, value, _now()) for value in values],
                )
            for field in self._overridden_fields:
                # Drop seeded defaults this instance replaced, keeping any value a dataset uses.
                keep = set(self.controlled_values[field]) | {
                    value for (value,) in connection.execute(
                        "SELECT DISTINCT json_extract(row_json, ?) FROM datasets", (f"$.{field}",)
                    ) if value
                }
                connection.executemany(
                    "DELETE FROM vocabulary WHERE field_name = ? AND value = ?",
                    [(field, value) for value in CONTROLLED_VALUES[field] if value not in keep],
                )
            for field, values in REFERENCE_CONTROLLED_VALUES.items():
                connection.executemany(
                    "INSERT OR IGNORE INTO vocabulary VALUES (?, ?, ?)",
                    [(f"reference.{field}", value, _now()) for value in values],
                )
            for workflow in WORKFLOW_DEFINITIONS:
                connection.execute(
                    """INSERT OR IGNORE INTO workflow_definitions
                       (workflow_id, name, version, analysis_type, compatible_analytes_json,
                        compatible_library_types_json, description, entrypoint, active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)""",
                    (workflow["workflow_id"], workflow["name"], workflow["version"], workflow["analysis_type"],
                     json.dumps(workflow["compatible_analytes"]), json.dumps(workflow["compatible_library_types"]),
                     workflow["description"], _now(), _now()),
                )
            if self._raft:
                for raft_workflow in self._raft.workflows:
                    connection.execute(
                        """INSERT INTO workflow_definitions
                           (workflow_id, name, version, analysis_type, compatible_analytes_json,
                            compatible_library_types_json, description, entrypoint, active, created_at, updated_at)
                           VALUES (?, ?, ?, 'raft', ?, ?, ?, 'raft', ?, ?, ?)
                           ON CONFLICT(workflow_id) DO UPDATE SET
                             name = excluded.name, version = excluded.version,
                             compatible_analytes_json = excluded.compatible_analytes_json,
                             compatible_library_types_json = excluded.compatible_library_types_json,
                             description = excluded.description, active = excluded.active,
                             updated_at = excluded.updated_at""",
                        (raft_workflow.workflow_id, raft_workflow.name, raft_workflow.version or "unset",
                         json.dumps(list(raft_workflow.compatible_analytes)),
                         json.dumps(list(raft_workflow.compatible_library_types)),
                         f"RAFT {raft_workflow.workflow} ({raft_workflow.input_data})",
                         int(raft_workflow.enabled), _now(), _now()),
                    )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @property
    def drop_dir(self) -> Path:
        """Compatibility alias; the entire sequencing directory is monitored."""
        return self.sequencing_dir

    def _drop_files(self) -> list[Path]:
        return sorted(
            path.resolve() for path in self.sequencing_dir.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        )

    def _reference_drop_files(self) -> list[Path]:
        return sorted(
            path.resolve() for path in self.references_dir.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        )

    def status(self) -> RegistryStatus:
        self.initialize()
        with self._connect() as connection:
            known = {row["path"] for row in connection.execute("SELECT path FROM files")}
            datasets = connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
            known_references = {row["path"] for row in connection.execute("SELECT path FROM reference_files")}
            reference_datasets = connection.execute("SELECT COUNT(*) FROM reference_datasets").fetchone()[0]
            workflow_counts = dict(connection.execute(
                "SELECT status, COUNT(*) FROM workflow_runs GROUP BY status"
            ).fetchall())
        observed = self._drop_files()
        observed_references = self._reference_drop_files()
        return RegistryStatus(
            datasets=datasets,
            files=len(observed),
            unregistered=tuple(path for path in observed if str(path) not in known),
            missing=tuple(Path(path) for path in sorted(known) if not Path(path).exists()),
            reference_datasets=reference_datasets,
            reference_files=len(observed_references),
            unregistered_references=tuple(path for path in observed_references if str(path) not in known_references),
            missing_references=tuple(Path(path) for path in sorted(known_references) if not Path(path).exists()),
            requested_workflows=sum(
                workflow_counts.get(key, 0) for key in ("requested", "preparing", "ready", "queued")
            ),
            running_workflows=workflow_counts.get("running", 0),
            failed_workflows=workflow_counts.get("failed", 0),
        )

    def vocabularies(self) -> dict[str, list[str]]:
        self.initialize()
        result = {field: [] for field in self.controlled_values}
        with self._connect() as connection:
            for row in connection.execute(
                "SELECT field_name, value FROM vocabulary WHERE field_name NOT LIKE 'reference.%' "
                "ORDER BY field_name, value"
            ):
                result.setdefault(row["field_name"], []).append(row["value"])
        return result

    def reference_vocabularies(self) -> dict[str, list[str]]:
        self.initialize()
        result = {field: [] for field in REFERENCE_CONTROLLED_VALUES}
        with self._connect() as connection:
            for row in connection.execute(
                "SELECT field_name, value FROM vocabulary WHERE field_name LIKE 'reference.%' ORDER BY field_name, value"
            ):
                result[row["field_name"].split(".", 1)[1]].append(row["value"])
        return result

    def workflow_definitions(self) -> list[dict]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM workflow_definitions
                   WHERE active = 1 AND analysis_type != 'provided_hla'
                     AND COALESCE(entrypoint, '') != 'raft' ORDER BY name"""
            ).fetchall()
        return [
            {
                **dict(row),
                "compatible_analytes": json.loads(row["compatible_analytes_json"]),
                "compatible_library_types": json.loads(row["compatible_library_types_json"]),
            }
            for row in rows
        ]

    def validate_workflows(self, workflow_ids: list[str], row: dict[str, str]) -> list[dict]:
        definitions = {item["workflow_id"]: item for item in self.workflow_definitions()}
        selected = []
        for workflow_id in dict.fromkeys(workflow_ids):
            workflow = definitions.get(workflow_id)
            if not workflow:
                raise ValueError(f"Unknown or inactive workflow: {workflow_id}")
            analytes = workflow["compatible_analytes"]
            libraries = workflow["compatible_library_types"]
            if analytes and row.get("analyte") not in analytes:
                raise ValueError(f"{workflow['name']} is not compatible with analyte {row.get('analyte') or '(blank)' }.")
            if libraries and row.get("library_type") not in libraries:
                raise ValueError(f"{workflow['name']} is not compatible with library type {row.get('library_type') or '(blank)' }.")
            selected.append(workflow)
        return selected

    def parse_provided_hla(self, value: str | None) -> list[dict]:
        """Validate and normalize comma-, whitespace-, or newline-delimited HLA alleles."""
        tokens = re.split(r"[\s,;]+", (value or "").strip())
        calls = []
        seen = set()
        pattern = re.compile(r"^(?:HLA-)?([A-Z0-9]+)\*([0-9]{2,3}(?::[0-9A-Z]{2,3}){0,3}[A-Z]?)$", re.I)
        for token in filter(None, tokens):
            match = pattern.fullmatch(token)
            if not match:
                raise ValueError(
                    f"Invalid HLA allele `{token}`. Use forms such as HLA-A*02:01 or B*07:02."
                )
            gene = f"HLA-{match.group(1).upper()}"
            allele = f"{match.group(1).upper()}*{match.group(2).upper()}"
            key = (gene, allele)
            if key not in seen:
                calls.append({
                    "gene": gene, "allele": allele,
                    "resolution": f"{match.group(2).count(':') + 1}-field",
                    "confidence": None, "caller": "user-provided",
                    "call_rank": len(calls) + 1, "metadata_json": {},
                })
                seen.add(key)
        return calls

    def propose_reference(self, values: dict[str, str], selected_files: list[str]) -> tuple[dict[str, str], tuple[Path, ...], list[str]]:
        status = self.status()
        available = set(status.unregistered_references)
        files = tuple(Path(path).resolve() for path in selected_files)
        if not files:
            raise ValueError("Select at least one reference file to accession.")
        if any(path not in available for path in files):
            raise ValueError("The reference file selection is stale; reopen the accession form.")
        row = {header: (values.get(header) or "").strip() for header in REFERENCE_HEADERS}
        if not row["name"] or not row["reference_type"]:
            raise ValueError("Name and reference type are required.")
        row["lbg_path"] = row["lbg_path"] or (str(files[0]) if len(files) == 1 else os.path.commonpath(files))
        identity = [row["name"], row["species"], row["assembly"], row["version"], row["reference_type"]]
        row["reference_id"] = f"{self.id_prefix}_ref_" + "_".join(_slug(value) for value in identity if value)
        return row, files, []

    def commit_reference(self, row: dict[str, str], files: list[str], slack_user_id: str) -> str:
        self.initialize()
        paths = [Path(path).resolve() for path in files]
        allowed = set(self.status().unregistered_references)
        if not paths or any(path not in allowed for path in paths):
            raise ValueError("The proposed reference file set is stale; run accession again.")
        internal_id, created_at = str(uuid.uuid4()), _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO reference_datasets VALUES (?, ?, ?, ?)",
                               (internal_id, row["reference_id"], json.dumps(row, sort_keys=True), created_at))
            for path in paths:
                stat = path.stat()
                connection.execute("INSERT INTO reference_files VALUES (?, ?, ?, ?, ?, ?)",
                                   (str(path), internal_id, stat.st_size, stat.st_mtime_ns, None, created_at))
            for field in REFERENCE_CONTROLLED_VALUES:
                if row.get(field):
                    connection.execute("INSERT OR IGNORE INTO vocabulary VALUES (?, ?, ?)",
                                       (f"reference.{field}", row[field], created_at))
            connection.execute(
                "INSERT INTO events(event_type, dataset_internal_id, details_json, created_at) VALUES (?, ?, ?, ?)",
                ("slack_reference_accession", internal_id, json.dumps({"slack_user_id": slack_user_id, "row": row, "files": files}), created_at),
            )
            self._append_rows(self.references_tsv_path, REFERENCE_HEADERS, row)
        return internal_id

    def propose(
        self, values: dict[str, str], selected_files: list[str] | None = None
    ) -> tuple[dict[str, str], tuple[Path, ...], list[str]]:
        status = self.status()
        if not status.unregistered:
            raise ValueError("There are no unregistered files in the drop directory.")
        available = set(status.unregistered)
        files = (
            tuple(Path(path).resolve() for path in selected_files)
            if selected_files is not None
            else status.unregistered
        )
        if not files:
            raise ValueError("Select at least one file to accession.")
        if any(path not in available for path in files):
            raise ValueError("The file selection is stale; reopen the accession form.")
        row = {
            header: (values.get(header) or "").strip()
            for header in HEADERS
        }
        row["lbg_path"] = row["lbg_path"] or (
            str(files[0]) if len(files) == 1
            else os.path.commonpath(files)
        )
        row["uuid"] = "_".join(
            [self.id_prefix] + [_slug(row[field]) for field in self.id_fields if row[field]]
        )
        warnings: list[str] = []
        if row["sequencing_read_type"] == "paired-end":
            names = [path.name for path in files]
            has_r1 = any(re.search(r"(?:^|[_\.])R?1(?:[_\.]|$)", name, re.I) for name in names)
            has_r2 = any(re.search(r"(?:^|[_\.])R?2(?:[_\.]|$)", name, re.I) for name in names)
            if not (has_r1 and has_r2):
                warnings.append("Paired-end files do not clearly contain both R1 and R2.")
        return row, files, warnings

    def commit(
        self, row: dict[str, str], files: list[str], slack_user_id: str,
        workflow_ids: list[str] | None = None, provided_hla_calls: list[dict] | None = None,
    ) -> str:
        self.initialize()
        selected_workflows = self.validate_workflows(workflow_ids or [], row)
        paths = [Path(path).resolve() for path in files]
        allowed = set(self.status().unregistered)
        if not paths or any(path not in allowed for path in paths):
            raise ValueError("The proposed file set is stale; run accession again.")
        internal_id = str(uuid.uuid4())
        created_at = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO datasets VALUES (?, ?, ?, ?)",
                (internal_id, row["uuid"], json.dumps(row, sort_keys=True), created_at),
            )
            for path in paths:
                stat = path.stat()
                connection.execute(
                    "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
                    (str(path), internal_id, stat.st_size, stat.st_mtime_ns, None, created_at),
                )
            for field in self.controlled_values:
                if row.get(field):
                    connection.execute(
                        "INSERT OR IGNORE INTO vocabulary VALUES (?, ?, ?)",
                        (field, row[field], created_at),
                    )
            connection.execute(
                "INSERT INTO events(event_type, dataset_internal_id, details_json, created_at) VALUES (?, ?, ?, ?)",
                ("slack_accession", internal_id, json.dumps({"slack_user_id": slack_user_id, "row": row, "files": files}), created_at),
            )
            for workflow in selected_workflows:
                run_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO workflow_runs
                       (run_id, workflow_id, dataset_internal_id, status, requested_by,
                        scheduler_job_id, parameters_json, software_versions_json,
                        reference_ids_json, requested_at, started_at, completed_at, error_message)
                       VALUES (?, ?, ?, 'requested', ?, NULL, '{}', '{}', '[]', ?, NULL, NULL, NULL)""",
                    (run_id, workflow["workflow_id"], internal_id, slack_user_id, created_at),
                )
                connection.execute(
                    "INSERT INTO events(event_type, dataset_internal_id, details_json, created_at) VALUES (?, ?, ?, ?)",
                    ("workflow_requested", internal_id, json.dumps({"run_id": run_id, "workflow_id": workflow["workflow_id"]}), created_at),
                )
            if provided_hla_calls:
                run_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO workflow_runs
                       (run_id, workflow_id, dataset_internal_id, status, requested_by,
                        scheduler_job_id, parameters_json, software_versions_json,
                        reference_ids_json, requested_at, started_at, completed_at, error_message)
                       VALUES (?, 'provided_hla', ?, 'completed', ?, NULL, ?, '{}', '[]', ?, ?, ?, NULL)""",
                    (run_id, internal_id, slack_user_id,
                     json.dumps({"provenance": "provided during Slack accession"}),
                     created_at, created_at, created_at),
                )
                connection.executemany(
                    """INSERT INTO hla_calls
                       (run_id, gene, allele, resolution, confidence, caller, call_rank, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(run_id, call["gene"], call["allele"], call.get("resolution"),
                      call.get("confidence"), call.get("caller", "user-provided"),
                      call.get("call_rank"), json.dumps(call.get("metadata_json", {}), sort_keys=True))
                     for call in provided_hla_calls],
                )
                connection.execute(
                    "INSERT INTO events(event_type, dataset_internal_id, details_json, created_at) VALUES (?, ?, ?, ?)",
                    ("provided_hla_recorded", internal_id,
                     json.dumps({"run_id": run_id, "alleles": [call["allele"] for call in provided_hla_calls]}),
                     created_at),
                )
            self._append_tsv(row)
        return internal_id

    def workflow_runs(self, dataset_internal_id: str | None = None) -> list[dict]:
        self.initialize()
        query = """SELECT r.*, d.name AS workflow_name, d.analysis_type
                   FROM workflow_runs r JOIN workflow_definitions d USING(workflow_id)"""
        parameters: tuple = ()
        if dataset_internal_id:
            query += " WHERE r.dataset_internal_id = ?"
            parameters = (dataset_internal_id,)
        query += " ORDER BY r.requested_at, r.run_id"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def datasets(self) -> list[dict]:
        """Registered datasets, newest first, each with its row and file paths."""
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM datasets ORDER BY created_at DESC, internal_id").fetchall()
            files = connection.execute("SELECT path, dataset_internal_id FROM files ORDER BY path").fetchall()
        by_dataset: dict[str, list[str]] = {}
        for item in files:
            by_dataset.setdefault(item["dataset_internal_id"], []).append(item["path"])
        return [
            {"internal_id": row["internal_id"], "dataset_id": row["spreadsheet_uuid"],
             "row": json.loads(row["row_json"]), "files": by_dataset.get(row["internal_id"], []),
             "created_at": row["created_at"]}
            for row in rows
        ]

    def dataset(self, internal_id: str) -> dict:
        for item in self.datasets():
            if item["internal_id"] == internal_id:
                return item
        raise ValueError(f"Unknown dataset: {internal_id}")

    def create_workflow_run(
        self, workflow_id: str, dataset_internal_id: str, requested_by: str, parameters: dict
    ) -> str:
        self.initialize()
        run_id, created_at = str(uuid.uuid4()), _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO workflow_runs
                   (run_id, workflow_id, dataset_internal_id, status, requested_by,
                    scheduler_job_id, parameters_json, software_versions_json,
                    reference_ids_json, requested_at, started_at, completed_at, error_message)
                   VALUES (?, ?, ?, 'requested', ?, NULL, ?, '{}', '[]', ?, NULL, NULL, NULL)""",
                (run_id, workflow_id, dataset_internal_id, requested_by,
                 json.dumps(parameters, sort_keys=True), created_at),
            )
            connection.execute(
                "INSERT INTO events(event_type, dataset_internal_id, details_json, created_at) VALUES (?, ?, ?, ?)",
                ("workflow_requested", dataset_internal_id,
                 json.dumps({"run_id": run_id, "workflow_id": workflow_id, "requested_by": requested_by}),
                 created_at),
            )
        return run_id

    def workflow_run(self, run_id: str) -> dict:
        for run in self.workflow_runs():
            if run["run_id"] == run_id:
                return {**run, "parameters": json.loads(run["parameters_json"] or "{}")}
        raise ValueError(f"Unknown workflow run: {run_id}")

    def record_event(self, event_type: str, dataset_internal_id: str | None, details: dict) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO events(event_type, dataset_internal_id, details_json, created_at) VALUES (?, ?, ?, ?)",
                (event_type, dataset_internal_id, json.dumps(details, sort_keys=True), _now()),
            )

    def update_workflow_run(self, run_id: str, status: str, **details) -> None:
        allowed = WORKFLOW_STATUSES
        if status not in allowed:
            raise ValueError(f"Invalid workflow status: {status}")
        self.initialize()
        fields = ["status = ?"]
        values = [status]
        for field in ("scheduler_job_id", "parameters_json", "software_versions_json", "reference_ids_json", "error_message"):
            if field in details:
                fields.append(f"{field} = ?")
                value = details[field]
                values.append(json.dumps(value, sort_keys=True) if field.endswith("_json") and not isinstance(value, str) else value)
        if status == "running":
            fields.append("started_at = COALESCE(started_at, ?)")
            values.append(_now())
        if status in {"completed", "failed", "cancelled"}:
            fields.append("completed_at = ?")
            values.append(_now())
        values.append(run_id)
        with self._connect() as connection:
            cursor = connection.execute(f"UPDATE workflow_runs SET {', '.join(fields)} WHERE run_id = ?", values)
            if not cursor.rowcount:
                raise ValueError(f"Unknown workflow run: {run_id}")

    def register_output_artifact(self, run_id: str, output_type: str, path_or_uri: str, **metadata) -> str:
        self.initialize()
        artifact_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO output_artifacts
                   (artifact_id, run_id, output_type, path_or_uri, checksum, format,
                    reference_id, row_count, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, run_id, output_type, path_or_uri, metadata.get("checksum"),
                 metadata.get("format"), metadata.get("reference_id"), metadata.get("row_count"),
                 json.dumps(metadata.get("metadata", {}), sort_keys=True), _now()),
            )
        return artifact_id

    def replace_result_rows(self, run_id: str, result_type: str, rows: list[dict]) -> None:
        """Atomically replace normalized results produced by one workflow run."""
        specifications = {
            "hla": ("hla_calls", ["gene", "allele", "resolution", "confidence", "caller", "call_rank", "metadata_json"]),
            "gene_expression": ("gene_expression", ["gene_id", "gene_symbol", "tpm", "raw_count"]),
            "transcript_expression": ("transcript_expression", ["transcript_id", "gene_id", "tpm", "estimated_count"]),
            "fusion": ("fusion_calls", ["fusion_id", "gene_5p", "gene_3p", "breakpoint_5p", "breakpoint_3p", "junction_reads", "spanning_reads", "confidence", "caller", "annotations_json"]),
        }
        if result_type not in specifications:
            raise ValueError(f"Unsupported result type: {result_type}")
        table, columns = specifications[result_type]
        prepared = []
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column)
                if column in {"metadata_json", "annotations_json"} and not isinstance(value, str):
                    value = json.dumps(value or {}, sort_keys=True)
                if column == "fusion_id" and not value:
                    value = str(uuid.uuid4())
                values.append(value)
            prepared.append([run_id, *values])
        placeholders = ", ".join("?" for _ in range(len(columns) + 1))
        with self._connect() as connection:
            if not connection.execute("SELECT 1 FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone():
                raise ValueError(f"Unknown workflow run: {run_id}")
            connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            connection.executemany(
                f"INSERT INTO {table} (run_id, {', '.join(columns)}) VALUES ({placeholders})",
                prepared,
            )

    def _upgrade_tsv(self, tsv_path: Path, headers: list[str]) -> None:
        """Rewrite a TSV with the current headers (new columns left empty)."""
        with tsv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self._write_rows(tsv_path, headers, rows)

    def _append_tsv(self, row: dict[str, str]) -> None:
        self._append_rows(self.tsv_path, HEADERS, row)

    def _append_rows(self, tsv_path: Path, headers: list[str], row: dict[str, str]) -> None:
        with tsv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        rows.append(row)
        self._write_rows(tsv_path, headers, rows)

    def _write_rows(self, tsv_path: Path, headers: list[str], rows: list[dict]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{tsv_path.name}.", dir=self.root)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=headers, delimiter="\t", lineterminator="\n", restval=""
                )
                writer.writeheader()
                writer.writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, tsv_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
