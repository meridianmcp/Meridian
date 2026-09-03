"""Bounded, tunnel-independent single-file XML/JSON inspector (item 2ffd763d).

Implements the "Bounded file-shape inspector" contract from
``docs/meridian-storage-and-file-inspector-contract-2026-08-31.md``: a
read-only inspection facade over ONE file that returns a deterministic,
capped structural summary -- never a second parser/index/database, never a
directory walk, never a write, never a network call.

Scope (Wave 0, this item): raw XML and JSON only. CSV/XLSX/DOCX are
explicitly out of scope here -- see the design doc's "Scope and delegation"
section (DOCX/OOXML delegates to ``meridian-docs``; tabular formats are a
separate planned "Wave 1" item; output indexing/search stays in
``meridian-outputs``).

Security posture:
  - No directory walk, shell, network, imports/includes, writes, or
    database/cache persistence -- this module touches disk only to
    ``os.stat``/open-and-read the ONE requested path, nothing else.
  - Secret-named files are never opened at all (:func:`is_secret_path`,
    patterns ported from
    ``extensions/meridian-outputs/meridian_outputs/outputs_local.py``'s
    ``is_secret_path`` -- see that module's docstring for the exhaustive
    list and rationale; this package cannot import core ``meridian`` or the
    ``meridian_outputs`` extension package, per the standalone-uvx-
    installable isolation constraint documented in both packages'
    pyproject.toml, so the pattern list is intentionally duplicated here
    rather than imported).
  - XML hardening lives in :mod:`meridian_file_inspection.xml_safe` --- see
    that module's docstring for the full XXE/DTD/entity threat model and
    defense layers.
  - ``source_ref`` in every response is a redacted, portable reference
    (basename + up to two parent directory names) -- never the raw
    machine-local absolute path -- per the design doc's "Volatile
    timestamps and machine-local absolute paths are excluded" requirement.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
from typing import Any

import lxml.etree as LET

from . import xml_safe

SCHEMA_VERSION = "1.0.0"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_MAX_DEPTH = 100
DEFAULT_MAX_ITEMS = 50_000
DEFAULT_PREVIEW_CHARS = 2000
DEFAULT_TIMEOUT_SECONDS = 5.0

#: Stable error-code vocabulary (design doc "Common preflight limits").
#: Never raise an uncaught exception across the ``inspect_file`` boundary --
#: every failure mode maps to one of these codes instead.
ERROR_CODES = frozenset({"unsupported", "limit_exceeded", "malformed", "denied", "timeout", "partial"})

STATE_COMPLETE = "complete"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"

_SUPPORTED_FORMATS = frozenset({"xml", "json"})

# ---------------------------------------------------------------------------
# Secret-path exclusion -- ported from
# extensions/meridian-outputs/meridian_outputs/outputs_local.py::is_secret_path
# (same pattern list; duplicated rather than imported per the standalone-uvx
# isolation constraint -- see this module's docstring).
# ---------------------------------------------------------------------------
_SECRET_BASENAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.crt",
    "*.cer",
    "*.der",
    "id_rsa",
    "id_rsa.*",
    "id_dsa",
    "id_dsa.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*secret*",
    "*secrets*",
    "*credential*",
    "*credentials*",
    "*password*",
    "*passwd*",
    "token",
    "token.*",
    "*_token",
    "*_token.*",
    "*api*token*",
    "*auth*token*",
    "*access*token*",
    "*bearer*token*",
    "*refresh*token*",
    "*apikey*",
    "*api_key*",
    "*auth_key*",
    "*access_key*",
    "*private_key*",
    "*.htpasswd",
    "*.netrc",
    "netrc",
    ".netrc",
    "config.ini",
    "config.cfg",
    "config.conf",
    "config.yaml",
    "config.yml",
    "config.toml",
    "config.json",
    "settings.ini",
    "settings.cfg",
    "settings.conf",
    "settings.yaml",
    "settings.yml",
    "settings.toml",
    "settings.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.toml",
    "secrets.json",
    "*.tfvars",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "*.vault",
    "vault.yaml",
    "vault.yml",
)
_SECRET_PATTERNS_LOWER: tuple[str, ...] = tuple(p.lower() for p in _SECRET_BASENAME_PATTERNS)
_SECRET_PATTERN_RE = re.compile(
    "|".join(fnmatch.translate(p) for p in _SECRET_PATTERNS_LOWER), re.IGNORECASE,
)


def is_secret_path(path: str) -> bool:
    """Return True if ``path``'s basename matches a secret-file exclusion
    pattern. See module docstring -- ported verbatim from
    ``meridian_outputs.outputs_local.is_secret_path``."""
    return _SECRET_PATTERN_RE.match(os.path.basename(path)) is not None


def _err(code: str, reason: str, detail: str | None = None) -> dict[str, Any]:
    assert code in ERROR_CODES, f"unknown error code {code!r}"
    out: dict[str, Any] = {"code": code, "reason": reason}
    if detail:
        out["detail"] = detail
    return out


def _redact_source_ref(path: str) -> str:
    """Portable, redacted reference: basename plus up to two parent
    directory names, with no drive letter / UNC / home-directory prefix and
    no full absolute path. Never round-trippable back to the real machine-
    local path -- purely a human-readable hint (design doc: "redacted-
    portable-reference")."""
    norm = path.replace("\\", "/")
    parts = [p for p in norm.split("/") if p not in ("", ".", "..")]
    tail = parts[-3:] if len(parts) > 3 else parts
    return "/".join(tail) if tail else os.path.basename(path)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sniff_format(head: bytes, declared: str) -> str | None:
    """Sniff format from magic bytes, never from extension alone (design doc:
    "extension plus magic/signature check, never extension alone").

    ``declared`` is the caller/extension-suggested format ("auto", "xml", or
    "json"); an explicit non-"auto" value is honoured only when the magic
    bytes are at least plausible for it (first significant byte matches),
    otherwise sniffing wins -- callers should not be able to force a binary
    blob to be parsed as XML just by naming it ``.xml``.
    """
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    first = stripped[:1]
    sniffed: str | None = None
    if first == b"<":
        sniffed = "xml"
    elif first in (b"{", b"["):
        sniffed = "json"

    if declared in _SUPPORTED_FORMATS:
        return declared if sniffed is None or sniffed == declared else sniffed
    return sniffed


def _resolve_path_policy(
    path: str,
    *,
    allowed_root: str | None,
    allow_symlinks: bool,
) -> dict[str, Any] | None:
    """Preflight path-policy checks. Returns an error dict on any violation,
    or ``None`` when the path is safe to open. Never raises."""
    if not path or not isinstance(path, str):
        return _err("denied", "invalid_path", "path must be a non-empty string")

    if is_secret_path(path):
        return _err("denied", "secret_path_excluded")

    if not allow_symlinks and os.path.islink(path):
        return _err("denied", "symlink_not_allowed")

    if allowed_root is not None:
        try:
            real_root = os.path.realpath(allowed_root)
            real_path = os.path.realpath(path)
        except OSError as exc:
            return _err("denied", "path_resolution_failed", str(exc))
        if os.path.commonpath([real_root, real_path]) != real_root:
            return _err("denied", "outside_allowed_root")

    if not os.path.exists(path):
        return _err("denied", "not_found")

    if os.path.isdir(path):
        return _err("denied", "is_a_directory")

    if not os.path.isfile(path):
        return _err("denied", "not_a_regular_file")

    if not os.access(path, os.R_OK):
        return _err("denied", "unreadable")

    return None


def _json_structure_scan(text: str, *, max_depth: int, max_items: int) -> dict[str, Any] | None:
    """Single-pass bracket scan (string/escape aware) bounding nesting depth
    and container count BEFORE any real JSON parser ever sees the text.

    This exists because Python's stdlib ``json`` module parses recursively:
    a pathologically deep (but syntactically tiny) input can exhaust the
    interpreter's recursion limit or, for sufficiently deep input, the
    underlying C stack -- a real crash risk, not merely a slow response.
    Bounding depth/item-count with a flat iterative scan first means
    ``json.loads`` is only ever invoked on input already proven to be within
    bounds.

    Returns an error dict (limit_exceeded) if a bound is exceeded, else
    ``None``.
    """
    depth = 0
    max_seen = 0
    items = 0
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            items += 1
            max_seen = max(max_seen, depth)
            if depth > max_depth:
                return _err("limit_exceeded", "max_depth_exceeded", f"depth {depth} > max_depth {max_depth}")
            if items > max_items:
                return _err("limit_exceeded", "max_items_exceeded", f"items {items} > max_items {max_items}")
        elif ch in "}]":
            depth -= 1
    return None


def _summarize_json(value: Any, *, max_items: int, preview_chars: int) -> dict[str, Any]:
    """Bounded, deterministic structural summary of an already-parsed (and
    already bound-checked) JSON value. Never includes full content -- only
    counts, a capped key/sample listing, and a capped text preview."""
    if isinstance(value, dict):
        root_kind = "object"
        keys = list(value.keys())
        shown = keys[:max_items]
        shape = {
            "root_kind": root_kind,
            "key_count": len(keys),
            "keys": shown,
            "truncated_keys": len(keys) > len(shown),
        }
    elif isinstance(value, list):
        root_kind = "array"
        shape = {
            "root_kind": root_kind,
            "length": len(value),
            "sample_types": [_json_type(v) for v in value[: min(max_items, 50)]],
            "truncated_sample": len(value) > min(max_items, 50),
        }
    else:
        root_kind = _json_type(value)
        shape = {"root_kind": root_kind}

    preview = _canonical_json(value)
    truncated_preview = len(preview) > preview_chars
    shape["preview"] = preview[:preview_chars]
    shape["truncated_preview"] = truncated_preview
    return shape


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _select_json(value: Any, selector: str) -> tuple[Any, dict[str, Any] | None]:
    """Resolve a bounded, safe dotted/bracket selector (e.g. ``a.b.0.c``)
    against an already-parsed JSON value. Read-only pure traversal -- no
    code execution, no wildcard/expression language. Returns
    ``(sub_value, error_or_None)``."""
    node = value
    for raw_part in selector.split("."):
        part = raw_part.strip()
        if part == "":
            return None, _err("malformed", "invalid_selector", f"empty segment in {selector!r}")
        if isinstance(node, dict):
            if part not in node:
                return None, _err("denied", "selector_not_found", f"key {part!r} not found")
            node = node[part]
        elif isinstance(node, list):
            if not part.lstrip("-").isdigit():
                return None, _err("malformed", "invalid_selector", f"expected index, got {part!r}")
            idx = int(part)
            if idx < 0 or idx >= len(node):
                return None, _err("denied", "selector_not_found", f"index {idx} out of range")
            node = node[idx]
        else:
            return None, _err("denied", "selector_not_found", f"cannot descend into scalar at {part!r}")
    return node, None


def inspect_file(
    path: str,
    format: str = "auto",
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_items: int = DEFAULT_MAX_ITEMS,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    allowed_root: str | None = None,
    allow_symlinks: bool = False,
    selector: str | None = None,
) -> dict[str, Any]:
    """Inspect exactly ONE local XML or JSON file and return a bounded,
    deterministic structural summary. Never raises -- every failure mode is
    reported in the returned envelope's ``errors``/``state`` fields.

    See the module docstring and
    ``docs/meridian-storage-and-file-inspector-contract-2026-08-31.md`` for
    the full contract this implements.
    """
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_ref": _redact_source_ref(path),
        "format": None,
        "mime": None,
        "size_bytes": None,
        "source_sha256": None,
        "parser_id": None,
        "parser_version": None,
        "result_hash": None,
        "state": STATE_FAILED,
        "shape": {},
        "bounds": {
            "max_bytes": max_bytes,
            "max_depth": max_depth,
            "max_items": max_items,
            "preview_chars": preview_chars,
            "timeout_seconds": timeout_seconds,
        },
        "warnings": [],
        "errors": [],
        "provenance_ref": None,
    }

    policy_error = _resolve_path_policy(path, allowed_root=allowed_root, allow_symlinks=allow_symlinks)
    if policy_error is not None:
        envelope["errors"].append(policy_error)
        return envelope

    try:
        size_bytes = os.path.getsize(path)
    except OSError as exc:
        envelope["errors"].append(_err("denied", "unreadable", str(exc)))
        return envelope

    envelope["size_bytes"] = size_bytes
    if size_bytes > max_bytes:
        envelope["errors"].append(
            _err("limit_exceeded", "max_bytes_exceeded", f"{size_bytes} > {max_bytes}")
        )
        return envelope

    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        envelope["errors"].append(_err("denied", "unreadable", str(exc)))
        return envelope

    if len(data) > max_bytes:
        envelope["errors"].append(
            _err("limit_exceeded", "max_bytes_exceeded", f">= {len(data)} > {max_bytes}")
        )
        return envelope

    envelope["source_sha256"] = _sha256_bytes(data)

    resolved_format = sniff_format(data[:4096], format if format in _SUPPORTED_FORMATS or format == "auto" else "auto")
    if resolved_format is None:
        envelope["errors"].append(_err("unsupported", "format_not_recognized"))
        return envelope
    envelope["format"] = resolved_format
    envelope["mime"] = "application/xml" if resolved_format == "xml" else "application/json"

    if resolved_format == "xml":
        return _inspect_xml(envelope, data, max_depth=max_depth, max_items=max_items,
                             preview_chars=preview_chars, timeout_seconds=timeout_seconds)
    return _inspect_json(envelope, data, max_depth=max_depth, max_items=max_items,
                          preview_chars=preview_chars, timeout_seconds=timeout_seconds,
                          selector=selector)


def _inspect_xml(
    envelope: dict[str, Any], data: bytes, *, max_depth: int, max_items: int,
    preview_chars: float, timeout_seconds: float,
) -> dict[str, Any]:
    envelope["parser_id"] = "lxml-xml-secure"
    envelope["parser_version"] = xml_safe.parser_version()
    try:
        shape, partial, warnings = xml_safe.parse_secure(
            data, max_depth=max_depth, max_items=max_items,
            preview_chars=preview_chars, timeout_seconds=timeout_seconds,
        )
    except xml_safe.XmlSecurityError as exc:
        envelope["errors"].append(_err(exc.code, exc.reason, exc.detail))
        return envelope
    except LET.XMLSyntaxError as exc:
        envelope["errors"].append(_err("malformed", "xml_syntax_error", str(exc)))
        return envelope

    envelope["shape"] = shape.to_dict()
    envelope["warnings"].extend(warnings)
    envelope["state"] = STATE_PARTIAL if partial else STATE_COMPLETE
    envelope["result_hash"] = _sha256_bytes(_canonical_json(envelope["shape"]).encode("utf-8"))
    return envelope


def _inspect_json(
    envelope: dict[str, Any], data: bytes, *, max_depth: int, max_items: int,
    preview_chars: int, timeout_seconds: float, selector: str | None,
) -> dict[str, Any]:
    envelope["parser_id"] = "stdlib-json-bounded"
    envelope["parser_version"] = "json/1 (bounded prescan + json.loads)"

    start = time.monotonic()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        envelope["errors"].append(_err("malformed", "invalid_utf8", str(exc)))
        return envelope

    scan_error = _json_structure_scan(text, max_depth=max_depth, max_items=max_items)
    if scan_error is not None:
        envelope["errors"].append(scan_error)
        return envelope

    if (time.monotonic() - start) > timeout_seconds:
        envelope["errors"].append(_err("timeout", "wall_clock_budget_exceeded_prescan"))
        return envelope

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        envelope["errors"].append(_err("malformed", "json_decode_error", str(exc)))
        return envelope

    if (time.monotonic() - start) > timeout_seconds:
        envelope["warnings"].append(_err("timeout", "wall_clock_budget_exceeded_parse"))
        envelope["state"] = STATE_PARTIAL
    else:
        envelope["state"] = STATE_COMPLETE

    shape = _summarize_json(value, max_items=max_items, preview_chars=preview_chars)

    if selector:
        selected_value, sel_error = _select_json(value, selector)
        if sel_error is not None:
            envelope["warnings"].append(sel_error)
            shape["selected"] = None
        else:
            shape["selected"] = _summarize_json(selected_value, max_items=max_items, preview_chars=preview_chars)

    envelope["shape"] = shape
    envelope["result_hash"] = _sha256_bytes(_canonical_json(shape).encode("utf-8"))
    return envelope
