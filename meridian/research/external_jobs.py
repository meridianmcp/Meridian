"""0b7bb873 — the delegated/observed escape hatch: attach a job a researcher
ALREADY submitted through their own Slurm/LSF/PBS/HTCondor/AWS Batch/
Kubernetes/SSH/RunPod script or launcher, without Meridian ever owning the
submission.

THREE TRUTHFUL MODES (only the latter two live in this module —
"managed" is :mod:`meridian.research.scheduler` from the prior item):

* **managed** — Meridian submits through a :class:`~meridian.research.
  providers.base.JobProvider` adapter (item f6627d83).
* **delegated** — the user submits through their own native CLI/runner and
  Meridian attaches the resulting external job id via
  :func:`attach_external_job`. Meridian did not launch it and cannot claim
  control over it beyond what :class:`ExternalJobRef` records.
* **observed/manual** — the user runs arbitrary commands entirely outside
  Meridian and later imports a completed manifest via
  :func:`import_external_manifest`.

REUSE, NOT REINVENTION (same discipline as items 4376e655/f6627d83): this
module has NO table of its own. Every function here reads/writes the SAME
``research_run_attempts`` row via :mod:`meridian.db.experiment_model`'s
``get_attempt``/``transition_attempt`` — an external job reference lives in
the attempt's existing ``provenance_ref`` JSON field under the key
``"external_job"``, exactly like :mod:`meridian.research.scheduler` stores a
managed job's handle under ``"job_handle"``. Nothing hardcodes launcher
specifics into :class:`meridian.research.providers.base.JobSpec` — that
dataclass is untouched by this module entirely; sbatch/bsub/aws batch/sky/
nextflow/snakemake/SSH are represented ONLY as an opaque
:attr:`ExternalJobRef.launcher` enum value plus a sanitized
:attr:`ExternalJobRef.launcher_reference` string.

TRUTHFULNESS GUARANTEES:

* :func:`import_external_manifest` requires an EXPLICIT
  :attr:`ExternalRunReceipt.status` — it never infers success from the mere
  presence of ``output_refs`` (a partial/failed job can still have produced
  some output files; their existence is not a success signal).
* :func:`mark_external_status` (used when polling IS possible, or for a
  human's manual status correction) goes through the SAME validated
  transition table as every other attempt update
  (:func:`meridian.experiment_model.validate_attempt_transition` via
  ``transition_attempt``) — an external system reporting something illegal
  relative to Meridian's already-known state is rejected, not blindly
  trusted.
* No credential or arbitrary command secret ever reaches a stored record:
  :class:`ExternalJobRef`/:class:`ExternalRunReceipt` run every free-text
  field through :func:`meridian.secret_redaction.check_for_secrets` at
  construction time (fail-closed — the same convention
  :mod:`meridian.db.research_graph`/:mod:`meridian.db.experiment_model`
  already use for persisted text).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from meridian.db.experiment_model import get_attempt, transition_attempt
from meridian.experiment_model import ATTEMPT_STATUSES, ATTEMPT_TERMINAL_STATUSES
from meridian.secret_redaction import check_for_secrets

#: Closed vocabulary of recognized native launchers. "custom" is the
#: deliberate escape hatch for anything not in this list (an arbitrary
#: SSH/manual command, a site-specific wrapper) — never a reason to hardcode
#: a new launcher-specific field anywhere in this module.
LAUNCHER_KINDS: frozenset[str] = frozenset(
    {"sbatch", "bsub", "aws_batch", "sky", "nextflow", "snakemake", "ssh", "custom"}
)


def validate_launcher_kind(raw: object) -> str:
    """Return ``raw`` stripped/lowercased if it's one of :data:`LAUNCHER_KINDS`.

    Raises ``ValueError`` naming the full closed set otherwise — mirrors
    ``meridian.experiment_model.validate_attempt_status``'s exact contract.
    """
    value = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if value not in LAUNCHER_KINDS:
        raise ValueError(f"launcher must be one of {sorted(LAUNCHER_KINDS)}, got {raw!r}")
    return value


@dataclass(frozen=True)
class TaskTopology:
    """Multi-node / array-job resource topology. Every field is optional —
    a plain single-process job has ``node_count=1`` and everything else
    ``None``.

    ``array_index``/``array_size`` preserve an array job's task-index
    identity (e.g. Slurm ``--array``); ``node_rank`` preserves a multi-node
    job's per-node identity (e.g. an MPI/NCCL rank). ``parent_launcher_id``
    links a fan-out child task back to the launcher-level submission it came
    from — set on a child :class:`ExternalJobRef`, ``None`` on the parent.
    """

    node_count: int = 1
    node_rank: "int | None" = None
    array_size: "int | None" = None
    array_index: "int | None" = None
    parent_launcher_id: "str | None" = None

    def __post_init__(self) -> None:
        if self.node_count < 1:
            raise ValueError(f"TaskTopology.node_count must be >= 1, got {self.node_count}")
        if (self.array_index is None) != (self.array_size is None):
            raise ValueError("TaskTopology.array_index and array_size must be set together")


@dataclass(frozen=True)
class ExternalJobRef:
    """A reference to a job the USER submitted through their own launcher.
    Meridian records this; it does not — and per the acceptance criteria,
    must not — claim to control the underlying job beyond what's recorded
    here.

    ``launcher_reference`` is the human-readable submission shape (e.g.
    ``"sbatch train.sh --partition=gpu"``) — sanitized (secret-checked) at
    construction, never a place for credentials or secret command
    arguments. ``external_id`` is whatever the external scheduler calls the
    job (a Slurm job id, an AWS Batch job ARN, ...) — opaque to Meridian.
    """

    launcher: str
    external_id: str
    idempotency_key: str
    launcher_reference: "str | None" = None
    account: "str | None" = None
    partition: "str | None" = None
    queue: "str | None" = None
    topology: TaskTopology = field(default_factory=TaskTopology)
    input_refs: "tuple[dict[str, Any], ...]" = ()
    submitted_at: "str | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "launcher", validate_launcher_kind(self.launcher))
        if not (self.external_id or "").strip():
            raise ValueError("ExternalJobRef requires a non-empty external_id")
        if not (self.idempotency_key or "").strip():
            raise ValueError("ExternalJobRef requires a non-empty idempotency_key")
        for field_name in ("launcher_reference", "account", "partition", "queue"):
            value = getattr(self, field_name)
            if value is not None:
                check_for_secrets(value, context=f"ExternalJobRef.{field_name}")

    def to_json(self) -> "dict[str, Any]":
        """Canonical JSON shape stored under ``provenance_ref["external_job"]``."""
        return {
            "launcher": self.launcher,
            "external_id": self.external_id,
            "idempotency_key": self.idempotency_key,
            "launcher_reference": self.launcher_reference,
            "account": self.account,
            "partition": self.partition,
            "queue": self.queue,
            "topology": {
                "node_count": self.topology.node_count,
                "node_rank": self.topology.node_rank,
                "array_size": self.topology.array_size,
                "array_index": self.topology.array_index,
                "parent_launcher_id": self.topology.parent_launcher_id,
            },
            "input_refs": list(self.input_refs),
            "submitted_at": self.submitted_at,
        }


@dataclass(frozen=True)
class ExternalRunReceipt:
    """An explicit, caller-asserted outcome for a delegated/observed job —
    imported after the fact via :func:`import_external_manifest`.

    ``status`` MUST be a TERMINAL attempt status or ``'unknown'`` — a
    receipt describes something that already happened (or that genuinely
    cannot be determined), never a future/in-progress state. This is what
    makes "never infers success from file existence alone" enforceable at
    the type level: a caller MUST say what happened; ``output_refs`` being
    non-empty is never read as an implicit ``'succeeded'``.
    """

    status: str
    failure_class: "str | None" = None
    output_refs: "tuple[dict[str, Any], ...]" = ()
    logs_ref: "dict[str, Any] | None" = None
    finalized_by: "str | None" = None
    detail: "str | None" = None

    def __post_init__(self) -> None:
        status = (self.status or "").strip().lower() if isinstance(self.status, str) else ""
        if status not in ATTEMPT_STATUSES:
            raise ValueError(f"ExternalRunReceipt.status must be one of {sorted(ATTEMPT_STATUSES)}, got {self.status!r}")
        if status not in ATTEMPT_TERMINAL_STATUSES and status != "unknown":
            raise ValueError(
                f"ExternalRunReceipt.status must be terminal ({sorted(ATTEMPT_TERMINAL_STATUSES)}) "
                f"or 'unknown' — a receipt describes something that already happened, got {status!r}"
            )
        object.__setattr__(self, "status", status)
        if status in ("failed", "crashed") and not self.failure_class:
            raise ValueError(f"ExternalRunReceipt.status={status!r} requires a failure_class")
        if status not in ("failed", "crashed") and self.failure_class is not None:
            raise ValueError("ExternalRunReceipt.failure_class is only valid for status 'failed'/'crashed'")
        if self.finalized_by is not None:
            check_for_secrets(self.finalized_by, context="ExternalRunReceipt.finalized_by")
        if self.detail is not None:
            check_for_secrets(self.detail, context="ExternalRunReceipt.detail")


def _external_job_from_provenance(provenance_ref: "dict[str, Any] | None") -> "dict[str, Any] | None":
    if not isinstance(provenance_ref, dict):
        return None
    return provenance_ref.get("external_job")


async def attach_external_job(
    db, project_id: str, attempt_id: str, ref: ExternalJobRef,
) -> dict[str, Any]:
    """Attach ``ref`` (a job the user already submitted) to an existing
    ``queued`` attempt — transitions it to ``'running'`` WITHOUT Meridian
    ever having submitted anything itself.

    Idempotent on ``ref.idempotency_key``: re-attaching with the SAME key
    returns the attempt UNCHANGED (acceptance criterion 1 — "without
    duplicating the run"). Attaching a DIFFERENT key while one is already
    attached raises ``ValueError``, the same "already running under a
    different submission" contract :func:`meridian.research.scheduler.
    submit_run_attempt` uses.
    """
    attempt = await get_attempt(db, project_id, attempt_id)
    if attempt is None:
        raise ValueError(f"run attempt {attempt_id!r} not found in project {project_id!r}")

    if attempt["status"] == "running":
        existing = _external_job_from_provenance(attempt.get("provenance_ref"))
        if existing is not None and existing.get("idempotency_key") == ref.idempotency_key:
            return attempt
        raise ValueError(
            f"run attempt {attempt_id!r} is already 'running' under a different submission"
        )
    if attempt["status"] != "queued":
        raise ValueError(
            f"run attempt {attempt_id!r} must be 'queued' to attach an external job, "
            f"is {attempt['status']!r}"
        )

    return await transition_attempt(
        db, project_id, attempt_id, "running",
        provenance_ref={"external_job": ref.to_json()},
    )


async def mark_external_status(
    db, project_id: str, attempt_id: str, status: str,
    *, failure_class: "str | None" = None, detail: "str | None" = None,
) -> dict[str, Any]:
    """Apply a status observation to an attached external job — used when
    polling IS available for a given launcher, or for a human's manual
    status correction (e.g. reconciling a preemption/requeue the external
    scheduler reported out of band).

    Requires :func:`attach_external_job` to have run first (raises
    ``ValueError`` otherwise). Goes through the SAME validated transition
    table every other attempt update uses — an illegal jump is rejected,
    not blindly trusted from an external source.
    """
    attempt = await get_attempt(db, project_id, attempt_id)
    if attempt is None:
        raise ValueError(f"run attempt {attempt_id!r} not found in project {project_id!r}")
    if _external_job_from_provenance(attempt.get("provenance_ref")) is None:
        raise ValueError(
            f"run attempt {attempt_id!r} has no attached external job — call attach_external_job first"
        )

    kwargs: dict[str, Any] = {}
    if failure_class is not None:
        kwargs["failure_class"] = failure_class
    if detail is not None:
        kwargs["error_message"] = detail
    return await transition_attempt(db, project_id, attempt_id, status, **kwargs)


async def import_external_manifest(
    db, project_id: str, attempt_id: str, receipt: ExternalRunReceipt,
) -> dict[str, Any]:
    """Observed/manual mode: finalize an attempt from a caller-asserted
    :class:`ExternalRunReceipt` imported after the fact (e.g. a user ran a
    script entirely outside Meridian and now reports what happened).

    Unlike :func:`mark_external_status`, this does NOT require a prior
    :func:`attach_external_job` call — "observed" jobs were never attached
    while running, only reported once finished. Refuses (``ValueError``) to
    finalize an attempt that is ALREADY terminal — a receipt describes a
    NEW fact, not a silent overwrite of an existing outcome; call this only
    once per attempt.
    """
    attempt = await get_attempt(db, project_id, attempt_id)
    if attempt is None:
        raise ValueError(f"run attempt {attempt_id!r} not found in project {project_id!r}")
    if attempt["status"] in ATTEMPT_TERMINAL_STATUSES:
        raise ValueError(
            f"run attempt {attempt_id!r} is already terminal ({attempt['status']!r}) — "
            "import_external_manifest finalizes an attempt once, not a repeat overwrite"
        )

    provenance_ref = dict(attempt.get("provenance_ref") or {})
    provenance_ref["external_manifest"] = {
        "finalized_by": receipt.finalized_by,
        "logs_ref": receipt.logs_ref,
    }

    kwargs: dict[str, Any] = {"provenance_ref": provenance_ref}
    if receipt.failure_class is not None:
        kwargs["failure_class"] = receipt.failure_class
    if receipt.detail is not None:
        kwargs["error_message"] = receipt.detail
    if receipt.output_refs:
        kwargs["artifact_refs"] = list(receipt.output_refs)

    # An attempt still 'queued' cannot jump straight to a terminal status
    # (see meridian.experiment_model's transition table) — an observed job
    # that was running outside Meridian is, from Meridian's point of view,
    # passing through 'running' on its way to the receipt's outcome.
    # (ExternalRunReceipt.status is always terminal or 'unknown', never
    # 'queued' itself — see its __post_init__ — so this detour always
    # applies when the attempt hasn't started yet.)
    if attempt["status"] == "queued":
        await transition_attempt(db, project_id, attempt_id, "running")

    return await transition_attempt(db, project_id, attempt_id, receipt.status, **kwargs)
