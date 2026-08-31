"""Scoped document-workspace identity and lineage primitives.

This module deliberately has no DOCX, parser, or persistence dependencies.  A
workspace is a small immutable description of one scoped view of a project's
source snapshot; callers can persist the result of :meth:`DocumentWorkspace.to_dict`
or :meth:`DocumentWorkspace.to_json` wherever they choose.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import heapq
import json
import math
import re
from copy import deepcopy
from typing import Any, TypeAlias


JSONPrimitive: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
ProfileValue: TypeAlias = str | dict[str, JSONValue]

_SHA256_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
_WORKSPACE_FIELDS = frozenset(
    {
        "workspace_id",
        "project_id",
        "source_snapshot_sha256",
        "scope",
        "profile",
        "status",
        "parent",
        "supersedes",
    }
)


class DocumentWorkspaceError(ValueError):
    """Base error for malformed workspace values."""


class LineageValidationError(DocumentWorkspaceError):
    """Raised when workspace records do not form a valid deterministic DAG."""


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DocumentWorkspaceError(f"{field_name} must be a non-empty string")
    return value


def _snapshot_hash(value: Any, field_name: str = "source_snapshot_sha256") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise DocumentWorkspaceError(f"{field_name} must be a 64-character SHA-256 hex digest")
    return value.lower()


def _json_value(value: Any, path: str) -> JSONValue:
    """Validate and detach a JSON value so callers cannot mutate the record."""

    if value is None or isinstance(value, (bool, int, str)):
        return deepcopy(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DocumentWorkspaceError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key in value:
            if not isinstance(key, str):
                raise DocumentWorkspaceError(f"{path} has a non-string key")
        for key in sorted(value):
            result[key] = _json_value(value[key], f"{path}.{key}")
        return result
    if isinstance(value, list):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise DocumentWorkspaceError(f"{path} contains a value that is not JSON-safe")


def _profile_value(value: Any) -> ProfileValue:
    if isinstance(value, str):
        return _required_text(value, "profile")
    normalized = _json_value(value, "profile")
    if not isinstance(normalized, dict):
        raise DocumentWorkspaceError("profile must be a non-empty string or JSON object")
    return normalized


def snapshot_sha256(snapshot: bytes | bytearray | memoryview | str) -> str:
    """Return the canonical SHA-256 digest for a byte or UTF-8 text snapshot."""

    if isinstance(snapshot, str):
        snapshot = snapshot.encode("utf-8")
    elif isinstance(snapshot, (bytearray, memoryview)):
        snapshot = bytes(snapshot)
    if not isinstance(snapshot, bytes):
        raise TypeError("snapshot must be text or bytes")
    return hashlib.sha256(snapshot).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class DocumentWorkspace:
    """Immutable identity and scope metadata for a document workspace.

    ``parent`` and ``supersedes`` contain workspace IDs.  The constructor also
    accepts the more explicit ``parent_workspace_id`` and
    ``supersedes_workspace_id`` keyword aliases; serialized form uses the
    concise relation names.
    """

    workspace_id: str
    project_id: str
    source_snapshot_sha256: str
    scope: dict[str, JSONValue]
    profile: ProfileValue
    status: str
    parent: str | None
    supersedes: str | None

    def __init__(
        self,
        workspace_id: str,
        project_id: str,
        source_snapshot_sha256: str,
        scope: Mapping[str, Any] | None = None,
        profile: ProfileValue = "default",
        status: str = "active",
        parent: str | None = None,
        supersedes: str | None = None,
        *,
        parent_workspace_id: str | None = None,
        supersedes_workspace_id: str | None = None,
    ) -> None:
        if parent is not None and parent_workspace_id is not None and parent != parent_workspace_id:
            raise DocumentWorkspaceError("parent and parent_workspace_id disagree")
        if supersedes is not None and supersedes_workspace_id is not None and supersedes != supersedes_workspace_id:
            raise DocumentWorkspaceError("supersedes and supersedes_workspace_id disagree")
        parent = parent if parent is not None else parent_workspace_id
        supersedes = supersedes if supersedes is not None else supersedes_workspace_id

        workspace_id = _required_text(workspace_id, "workspace_id")
        project_id = _required_text(project_id, "project_id")
        status = _required_text(status, "status")
        if parent == workspace_id or supersedes == workspace_id:
            raise DocumentWorkspaceError("a workspace cannot be its own parent or superseded workspace")
        if parent is not None:
            parent = _required_text(parent, "parent")
        if supersedes is not None:
            supersedes = _required_text(supersedes, "supersedes")

        normalized_scope = _json_value({} if scope is None else scope, "scope")
        if not isinstance(normalized_scope, dict):
            raise DocumentWorkspaceError("scope must be a JSON object")

        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "source_snapshot_sha256", _snapshot_hash(source_snapshot_sha256))
        object.__setattr__(self, "scope", normalized_scope)
        object.__setattr__(self, "profile", _profile_value(profile))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "parent", parent)
        object.__setattr__(self, "supersedes", supersedes)

    @property
    def parent_workspace_id(self) -> str | None:
        return self.parent

    @property
    def supersedes_workspace_id(self) -> str | None:
        return self.supersedes

    def is_stale(self, current_source_sha256: str) -> bool:
        """Return whether the workspace was built from a different source hash."""

        return self.source_snapshot_sha256 != _snapshot_hash(
            current_source_sha256, "current_source_sha256"
        )

    def to_dict(self) -> dict[str, JSONValue | None]:
        """Return a detached, JSON-safe representation of this workspace."""

        return deepcopy(
            {
                "workspace_id": self.workspace_id,
                "project_id": self.project_id,
                "source_snapshot_sha256": self.source_snapshot_sha256,
                "scope": self.scope,
                "profile": self.profile,
                "status": self.status,
                "parent": self.parent,
                "supersedes": self.supersedes,
            }
        )

    def to_json(self) -> str:
        """Return canonical JSON suitable for hashing or transport."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DocumentWorkspace":
        if not isinstance(payload, Mapping):
            raise DocumentWorkspaceError("workspace payload must be a JSON object")
        unknown = set(payload) - _WORKSPACE_FIELDS
        if unknown:
            raise DocumentWorkspaceError(f"unknown workspace field(s): {', '.join(sorted(unknown))}")
        missing = _WORKSPACE_FIELDS - set(payload)
        if missing:
            raise DocumentWorkspaceError(f"missing workspace field(s): {', '.join(sorted(missing))}")
        return cls(
            workspace_id=payload["workspace_id"],
            project_id=payload["project_id"],
            source_snapshot_sha256=payload["source_snapshot_sha256"],
            scope=payload["scope"],
            profile=payload["profile"],
            status=payload["status"],
            parent=payload["parent"],
            supersedes=payload["supersedes"],
        )

    @classmethod
    def from_json(cls, payload: str) -> "DocumentWorkspace":
        try:
            decoded = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DocumentWorkspaceError("workspace JSON is invalid") from exc
        return cls.from_dict(decoded)


def validate_lineage(workspaces: Iterable[DocumentWorkspace]) -> tuple[str, ...]:
    """Validate a workspace collection and return a deterministic topological order.

    Both relation types are directed from the referenced workspace to the
    current workspace.  References must exist and stay within the same
    project.  Kahn's algorithm uses a min-heap, so independent branches have
    the same order regardless of input iteration order.
    """

    records = tuple(workspaces)
    if any(not isinstance(record, DocumentWorkspace) for record in records):
        raise LineageValidationError("lineage entries must be DocumentWorkspace values")

    by_id: dict[str, DocumentWorkspace] = {}
    for record in records:
        if record.workspace_id in by_id:
            raise LineageValidationError(f"duplicate workspace_id: {record.workspace_id}")
        by_id[record.workspace_id] = record

    children: dict[str, set[str]] = {workspace_id: set() for workspace_id in by_id}
    indegree = {workspace_id: 0 for workspace_id in by_id}
    for record in sorted(records, key=lambda item: item.workspace_id):
        for relation, referenced_id in (("parent", record.parent), ("supersedes", record.supersedes)):
            if referenced_id is None:
                continue
            referenced = by_id.get(referenced_id)
            if referenced is None:
                raise LineageValidationError(
                    f"{relation} reference from {record.workspace_id} is missing: {referenced_id}"
                )
            if referenced.project_id != record.project_id:
                raise LineageValidationError(
                    f"{relation} reference from {record.workspace_id} crosses projects"
                )
            if record.workspace_id not in children[referenced_id]:
                children[referenced_id].add(record.workspace_id)
                indegree[record.workspace_id] += 1

    ready = [workspace_id for workspace_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        workspace_id = heapq.heappop(ready)
        ordered.append(workspace_id)
        for child_id in sorted(children[workspace_id]):
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                heapq.heappush(ready, child_id)

    if len(ordered) != len(records):
        cycle_nodes = sorted(workspace_id for workspace_id, degree in indegree.items() if degree > 0)
        raise LineageValidationError(
            f"lineage contains a cycle involving: {', '.join(cycle_nodes)}"
        )
    return tuple(ordered)


def is_workspace_stale(workspace: DocumentWorkspace, current_source_sha256: str) -> bool:
    """Functional form of :meth:`DocumentWorkspace.is_stale`."""

    if not isinstance(workspace, DocumentWorkspace):
        raise TypeError("workspace must be a DocumentWorkspace")
    return workspace.is_stale(current_source_sha256)


__all__ = [
    "DocumentWorkspace",
    "DocumentWorkspaceError",
    "JSONPrimitive",
    "JSONValue",
    "LineageValidationError",
    "ProfileValue",
    "is_workspace_stale",
    "snapshot_sha256",
    "validate_lineage",
]
