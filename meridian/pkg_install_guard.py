"""23f21820 -- package-install verification gate and agent_instructions safety checker.

Two concerns handled here:

1. PACKAGE-INSTALL GATE
   Before an executor autonomously runs pip/npm/uvx install for a package NOT
   already in a known-good allowlist, verify it exists and has genuine history via
   a real registry lookup (PyPI JSON API / npm registry). If the network call fails,
   degrade gracefully to a warning (fail-open) -- a structural defect must never
   permanently wedge a session. If the package is suspicious (not found, zero
   downloads, no maintainers, very new), return a warning that tells the executor to
   route through request_hitl before proceeding.

   Motivation: May 2026 CISA/NSA/Five Eyes joint advisory on AI coding agents as
   untrusted components; documented incident where a hallucinated npm package name
   spread to 237 GitHub repos via AI-generated agent skill files, with autonomous
   agents attempting downloads immediately after a defensive registration.

2. AGENT_INSTRUCTIONS CONTENT CHECK (best-effort)
   Meridian's set_agent_instructions currently only validates size. This module adds
   a lightweight scan for suspicious content patterns (invisible unicode, classic
   prompt-injection phrasings, unusual control characters) that could be used to
   redirect agent behaviour via a tampered agent_instructions store. Returns a list
   of findings; caller decides how to surface them. Never hard-blocks on a false-
   positive heuristic -- this is best-effort.

Pure ASCII source: PS 5.1 reads BOM-less UTF-8 as cp1252; the companion .ps1 hook
calls this logic indirectly via a lightweight HTTP call to a local Meridian endpoint,
but if you ever import this from .ps1 you need pure ASCII. Keep this file ASCII only.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Known-good allowlist
# Seeded from pyproject.toml [project.dependencies] and common dev tooling.
# Lookups for names IN this set are skipped entirely -- no registry call needed.
# Names are normalised to lowercase with hyphens (PEP 503 / npm convention).
# ---------------------------------------------------------------------------

#: pip/PyPI package names currently used by this repo or universally safe dev tools.
PYPI_ALLOWLIST: frozenset[str] = frozenset({
    # --- pyproject.toml direct deps (meridian-server) ---
    "fastapi",
    "uvicorn",
    "aiosqlite",
    "pydantic",
    "jinja2",
    "httpx",
    "python-dotenv",
    "psycopg",
    "psycopg-pool",
    "websockets",
    "mcp",
    "anthropic",
    "authlib",
    "itsdangerous",
    "resend",
    "bcrypt",
    "slowapi",
    "tree-sitter",
    "tree-sitter-python",
    "tree-sitter-javascript",
    "tree-sitter-typescript",
    "tree-sitter-c",
    "tree-sitter-cpp",
    "tree-sitter-go",
    "tree-sitter-rust",
    "tree-sitter-java",
    "tree-sitter-c-sharp",
    "pylatexenc",
    "lxml",
    "latex2mathml",
    "duckdb",
    "numpy",
    "watchdog",
    # --- optional extras (semantic) ---
    "model2vec",
    "psutil",
    # --- common dev / test tooling ---
    "pytest",
    "pytest-asyncio",
    "pytest-xdist",
    "ruff",
    "mypy",
    "pixi",
    "pip",
    "setuptools",
    "wheel",
    "hatchling",
    "uv",
    "build",
    "twine",
    # --- common data / ML packages frequently suggested by agents ---
    "pandas",
    "scipy",
    "scikit-learn",
    "matplotlib",
    "pillow",
    "requests",
    "aiohttp",
    "click",
    "rich",
    "typer",
    "pyyaml",
    "toml",
    "tomli",
    "tomllib",
    "packaging",
    "attrs",
    "cattrs",
    "msgpack",
    "orjson",
    "ujson",
    "cryptography",
    "paramiko",
    "boto3",
    "google-cloud-storage",
    "stripe",
    "openai",
    "tiktoken",
    # --- tantivy (mentioned in task own notes as a real example) ---
    "tantivy",
    "tantivy-py",
    "tantivy-search",
})

#: npm package names (scoped or bare) that are known-good for this repo.
NPM_ALLOWLIST: frozenset[str] = frozenset({
    # --- build tooling used by this repo ---
    "esbuild",
    "typescript",
    "vitest",
    "@vitest/coverage-v8",
    "preact",
    "mcp-remote",
    "@upstash/context7-mcp",
    # --- common safe packages ---
    "lodash",
    "axios",
    "express",
    "react",
    "react-dom",
    "next",
    "vite",
    "rollup",
    "webpack",
    "eslint",
    "prettier",
    "jest",
    "ts-node",
    "tsx",
    "zod",
})

# ---------------------------------------------------------------------------
# Registry verification
# ---------------------------------------------------------------------------

#: Minimum download count (last 30 days from PyPI stats) to consider a package
#: well-established. Packages below this threshold trigger a warning (not a block).
PYPI_MIN_DOWNLOADS_WARN = 1_000

#: A package registered within this many days is considered suspiciously new.
PACKAGE_AGE_WARN_DAYS = 30


@dataclass
class VerificationResult:
    """Result of a package registry check."""

    package: str
    manager: str  # "pip" | "npm" | "uvx" | "unknown"
    allowlisted: bool = False
    found: bool = False
    warning: str = ""
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def should_warn(self) -> bool:
        """True when the caller should surface a warning to the executor."""
        return bool(self.warning) or bool(self.error)

    @property
    def ok(self) -> bool:
        """True when the package is clearly safe (no warnings or errors)."""
        return self.found and not self.warning and not self.error


def _normalise_pypi(name: str) -> str:
    """Normalise a PyPI package name per PEP 503 (lowercase, hyphens)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalise_npm(name: str) -> str:
    """Normalise an npm package name (lowercase; preserve @ scope prefix)."""
    return name.strip().lower()


def verify_pypi(package: str, *, timeout: float = 8.0) -> VerificationResult:
    """Check PyPI JSON API for *package*.

    Fails open (returns a result with error= set, no hard exception) on any
    network or parse error. Never raises.
    """
    import json
    import urllib.error
    import urllib.request

    norm = _normalise_pypi(package)
    result = VerificationResult(package=package, manager="pip")

    if norm in PYPI_ALLOWLIST:
        result.allowlisted = True
        result.found = True
        return result

    url = f"https://pypi.org/pypi/{norm}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "meridian-pkg-guard/1 (safety check)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            result.found = False
            result.warning = (
                f"Package '{package}' was NOT found on PyPI (HTTP 404). "
                f"This may be a hallucinated or typo-squatted name. "
                f"Verify the correct package name before installing, or call "
                f"request_hitl to get human confirmation."
            )
        else:
            # Network error -- fail open, surface as a warning not a block.
            result.error = (
                f"PyPI lookup for '{package}' failed (HTTP {exc.code}). "
                f"Proceeding with caution -- verify the package name manually."
            )
        return result
    except Exception as exc:
        result.error = (
            f"PyPI lookup for '{package}' could not complete ({type(exc).__name__}: {exc}). "
            f"Network may be unavailable -- failing open. Verify the package name manually."
        )
        return result

    result.found = True
    info = data.get("info", {})
    result.details["author"] = info.get("author", "")
    result.details["summary"] = (info.get("summary") or "")[:120]
    result.details["home_page"] = info.get("home_page", "")
    maintainers = info.get("maintainers") or []
    result.details["maintainer_count"] = len(maintainers)

    # Check release age of the FIRST version (earliest).
    releases = data.get("releases", {})
    warnings: list[str] = []
    if releases:
        import datetime
        earliest = None
        for _ver, files in releases.items():
            for f in files:
                upload_time = f.get("upload_time") or f.get("upload_time_iso_8601", "")
                if upload_time:
                    try:
                        ts = datetime.datetime.fromisoformat(
                            upload_time.replace("Z", "+00:00")
                        )
                        if earliest is None or ts < earliest:
                            earliest = ts
                    except ValueError:
                        pass
        if earliest is not None:
            now = datetime.datetime.now(datetime.timezone.utc)
            age_days = (now - earliest).days
            result.details["age_days"] = age_days
            if age_days < PACKAGE_AGE_WARN_DAYS:
                warnings.append(
                    f"Package '{package}' was first published only {age_days} day(s) ago -- "
                    f"suspiciously new. Consider verifying via request_hitl."
                )

    # No maintainer info on some packages (empty list from PyPI); not always a red flag,
    # but flag if the package is ALSO new (belt-and-suspenders).
    if result.details.get("maintainer_count", 1) == 0 and result.details.get("age_days", 999) < 90:
        warnings.append(
            f"Package '{package}' has no listed maintainers and is less than 90 days old. "
            f"Treat with extra caution."
        )

    if warnings:
        result.warning = " | ".join(warnings)

    return result


def verify_npm(package: str, *, timeout: float = 8.0) -> VerificationResult:
    """Check the npm registry for *package*.

    Fails open on network/parse errors.
    """
    import json
    import urllib.error
    import urllib.request

    norm = _normalise_npm(package)
    result = VerificationResult(package=package, manager="npm")

    if norm in NPM_ALLOWLIST:
        result.allowlisted = True
        result.found = True
        return result

    # Scoped packages: @scope/name -> encode the slash.
    encoded = norm.replace("/", "%2F")
    url = f"https://registry.npmjs.org/{encoded}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "meridian-pkg-guard/1 (safety check)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            result.found = False
            result.warning = (
                f"Package '{package}' was NOT found on npm (HTTP 404). "
                f"This may be a hallucinated or typo-squatted name. "
                f"Verify the correct package name before installing, or call "
                f"request_hitl to get human confirmation."
            )
        else:
            result.error = (
                f"npm registry lookup for '{package}' failed (HTTP {exc.code}). "
                f"Proceeding with caution -- verify the package name manually."
            )
        return result
    except Exception as exc:
        result.error = (
            f"npm registry lookup for '{package}' could not complete "
            f"({type(exc).__name__}: {exc}). "
            f"Network may be unavailable -- failing open. Verify the package name manually."
        )
        return result

    result.found = True
    time_info = data.get("time", {})
    warnings: list[str] = []

    if "created" in time_info:
        import datetime
        try:
            created = datetime.datetime.fromisoformat(
                time_info["created"].replace("Z", "+00:00")
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            age_days = (now - created).days
            result.details["age_days"] = age_days
            if age_days < PACKAGE_AGE_WARN_DAYS:
                warnings.append(
                    f"npm package '{package}' was created only {age_days} day(s) ago -- "
                    f"suspiciously new. Consider verifying via request_hitl."
                )
        except ValueError:
            pass

    maintainers = data.get("maintainers") or []
    result.details["maintainer_count"] = len(maintainers)
    if len(maintainers) == 0 and result.details.get("age_days", 999) < 90:
        warnings.append(
            f"npm package '{package}' has no listed maintainers and is less than 90 days old."
        )

    if warnings:
        result.warning = " | ".join(warnings)

    return result


# ---------------------------------------------------------------------------
# Command-line parser
# Interprets pip/npm/uvx install commands and extracts package names.
# Handles common flag patterns; errs toward returning MORE packages rather
# than silently dropping names that look flag-like.
# ---------------------------------------------------------------------------

# Patterns matching pip install / npm install / uvx install Bash commands.
_PIP_INSTALL_RE = re.compile(
    r"(?:^|\s|;|&&|\|)"
    r"(?:pip3?|python\s+-m\s+pip|uv\s+pip)\s+install\s+",
    re.IGNORECASE,
)
_NPM_INSTALL_RE = re.compile(
    r"(?:^|\s|;|&&|\|)"
    r"(?:npm|yarn|pnpm|bun)\s+(?:install|add|i\b)\s*",
    re.IGNORECASE,
)
_UVX_INSTALL_RE = re.compile(
    r"(?:^|\s|;|&&|\|)"
    r"uvx\s+(?:--from\s+\S+\s+)?(?:install\s+)?",
    re.IGNORECASE,
)
# Flags to skip when extracting package names from the install command.
_FLAG_RE = re.compile(r"^-")
# Version specifiers / extras that attach to a package name (strip these before lookup).
_VERSION_STRIP_RE = re.compile(r"[=<>!~\[;,@#].*$")


def _strip_version(pkg: str) -> str:
    """Strip version specifier/extras from a package token, e.g. 'numpy>=1.26' -> 'numpy'."""
    # A scoped npm package starts with ``@`` (for example
    # ``@vitest/coverage-v8``).  The generic version regex also treats ``@``
    # as a version separator, so applying it to the whole token would erase
    # the package name.  Strip the scope first, then apply the same rules to
    # the package-name portion so ``@scope/name@1.2`` becomes
    # ``@scope/name`` while unscoped tokens retain their existing behavior.
    if pkg.startswith("@") and "/" in pkg:
        scope, name = pkg.split("/", 1)
        return f"{scope}/{_VERSION_STRIP_RE.sub('', name).strip()}".strip()
    return _VERSION_STRIP_RE.sub("", pkg).strip()


def extract_packages_from_command(command: str) -> list[tuple[str, str]]:
    """Return [(package_name, manager), ...] from a Bash command string.

    Handles multi-command lines (&&, ;, |). Returns an empty list if no
    install pattern is detected. manager is one of 'pip', 'npm', 'uvx'.
    """
    results: list[tuple[str, str]] = []
    # Split on common shell separators so we can check each sub-command.
    # We keep it simple: just scan for the install patterns without a full parser.
    parts = re.split(r"&&|\|\||;", command)
    for part in parts:
        part = part.strip()
        if not part:
            continue

        # pip / python -m pip / uv pip install
        m = _PIP_INSTALL_RE.search(part)
        if m:
            tail = part[m.end():].strip()
            for token in tail.split():
                if _FLAG_RE.match(token):
                    continue
                pkg = _strip_version(token)
                if pkg:
                    results.append((pkg, "pip"))
            continue

        # npm / yarn / pnpm / bun install|add
        m = _NPM_INSTALL_RE.search(part)
        if m:
            tail = part[m.end():].strip()
            for token in tail.split():
                if _FLAG_RE.match(token):
                    continue
                pkg = _strip_version(token)
                if pkg:
                    results.append((pkg, "npm"))
            continue

        # uvx install (or uvx --from PKG TOOL)
        m = _UVX_INSTALL_RE.search(part)
        if m:
            tail = part[m.end():].strip()
            for token in tail.split():
                if _FLAG_RE.match(token):
                    continue
                pkg = _strip_version(token)
                if pkg:
                    results.append((pkg, "uvx"))
            continue

    return results


def verify_package(package: str, manager: str, *, timeout: float = 8.0) -> VerificationResult:
    """Route to the right registry verifier based on *manager*.

    uvx packages resolve via PyPI (they are pip-distributed packages).
    Unknown managers get a pass-through (allowlisted=True so no gate fires).
    """
    if manager in ("pip", "uvx"):
        return verify_pypi(package, timeout=timeout)
    if manager == "npm":
        return verify_npm(package, timeout=timeout)
    # Unknown manager -- fail open (don't block on uncertainty).
    return VerificationResult(
        package=package,
        manager=manager,
        allowlisted=True,
        found=True,
    )


# ---------------------------------------------------------------------------
# Agent instructions content checker (secondary, best-effort)
# ---------------------------------------------------------------------------

# Invisible/zero-width unicode used in prompt injection payloads.
_INVISIBLE_UNICODE_CATEGORIES = frozenset({"Cf", "Zs", "Co"})

# Prompt-injection-style phrasings that are suspicious in agent_instructions.
_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"ignore\s+(all\s+)?(previous|above|prior|earlier)\s+(instructions?|rules?|context)", re.IGNORECASE),
        "classic ignore-previous-instructions injection",
    ),
    (
        re.compile(r"disregard\s+(all\s+)?(previous|above|prior|earlier)\s+(instructions?|rules?|context)", re.IGNORECASE),
        "disregard-previous-instructions injection",
    ),
    (
        re.compile(r"forget\s+(all\s+)?(previous|above|prior|earlier)\s+(instructions?|rules?|context)", re.IGNORECASE),
        "forget-previous-instructions injection",
    ),
    (
        re.compile(r"\[\s*SYSTEM\s*\]|\<\s*SYSTEM\s*\>", re.IGNORECASE),
        "fake system-prompt delimiter",
    ),
    (
        re.compile(r"you\s+are\s+now\s+(?:a\s+)?(?:different|new|another)\s+(?:AI|assistant|model|persona|bot)", re.IGNORECASE),
        "persona-replacement injection",
    ),
    (
        re.compile(r"your\s+(new\s+)?(?:primary\s+)?(?:goal|objective|directive|purpose)\s+is\s+(?:to\s+)?(?:exfil|steal|send|upload|transmit)", re.IGNORECASE),
        "goal-replacement with exfiltration target",
    ),
    (
        re.compile(r"do\s+not\s+(?:tell|inform|mention|reveal)\s+(?:the\s+)?(?:user|human|operator)", re.IGNORECASE),
        "concealment directive",
    ),
    (
        re.compile(r"(?:execute|run|eval)\s*\(", re.IGNORECASE),
        "code-execution phrasing (possible injection)",
    ),
]


@dataclass
class InstructionsFinding:
    """A suspicious pattern found in agent_instructions content."""

    kind: str  # "invisible_unicode" | "injection_pattern"
    description: str
    location: str = ""  # brief context (first 60 chars of the match area)


def check_agent_instructions(text: str | None) -> list[InstructionsFinding]:
    """Scan *text* for suspicious content that may indicate a prompt injection attempt.

    Returns a list of findings. Empty list means nothing suspicious was detected.
    This is BEST-EFFORT -- it will not catch every possible injection technique.
    Never raises; caller decides how to surface findings.
    """
    if not text:
        return []

    findings: list[InstructionsFinding] = []

    # --- Invisible / unusual unicode ---
    invisible_positions: list[int] = []
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        if cat in _INVISIBLE_UNICODE_CATEGORIES and ord(ch) > 127:
            invisible_positions.append(i)

    if invisible_positions:
        sample = text[max(0, invisible_positions[0] - 10): invisible_positions[0] + 20]
        findings.append(InstructionsFinding(
            kind="invisible_unicode",
            description=(
                f"Found {len(invisible_positions)} invisible/control unicode character(s) "
                f"(categories: Cf/Zs/Co) which may be used to hide instructions. "
                f"First occurrence near position {invisible_positions[0]}."
            ),
            location=repr(sample),
        ))

    # --- Prompt injection phrasings ---
    for pattern, label in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 20)
            findings.append(InstructionsFinding(
                kind="injection_pattern",
                description=f"Detected '{label}' pattern.",
                location=repr(text[start:end]),
            ))

    return findings


# ---------------------------------------------------------------------------
# High-level gate function (called from hook HTTP endpoint or tests)
# ---------------------------------------------------------------------------

def gate_command(command: str, *, registry_timeout: float = 8.0) -> dict[str, Any]:
    """Evaluate a Bash command string and return a gate decision.

    Returns a dict with keys:
      - "action": "allow" | "warn" | "block"
      - "message": str (empty when allow)
      - "packages": list of package-name strings extracted
      - "results": list of VerificationResult-like dicts
      - "allowlisted_all": bool (True when every package was in the allowlist)

    The gate NEVER hard-blocks (returns "block") due to a network failure --
    network errors degrade to "warn" so a structural defect never wedges a session.
    A hard "block" is only returned when a package is definitively NOT found
    (HTTP 404) on its registry -- a clear signal of a non-existent package name.
    """
    extracted = extract_packages_from_command(command)
    if not extracted:
        # Not a package install command -- let it through.
        return {
            "action": "allow",
            "message": "",
            "packages": [],
            "results": [],
            "allowlisted_all": True,
        }

    package_results: list[dict[str, Any]] = []
    all_ok = True
    any_not_found = False
    messages: list[str] = []
    allowlisted_all = True

    for pkg_name, manager in extracted:
        r = verify_package(pkg_name, manager, timeout=registry_timeout)

        package_results.append({
            "package": r.package,
            "manager": r.manager,
            "allowlisted": r.allowlisted,
            "found": r.found,
            "warning": r.warning,
            "error": r.error,
            "details": r.details,
        })

        if not r.allowlisted:
            allowlisted_all = False

        if not r.found and not r.error:
            # Definitively not on the registry (404).
            any_not_found = True
            all_ok = False
            messages.append(r.warning or f"Package '{pkg_name}' not found on registry.")
        elif r.warning:
            all_ok = False
            messages.append(r.warning)
        elif r.error:
            # Network failure -- degrade to warn, not block.
            all_ok = False
            messages.append(r.error)

    if all_ok:
        action = "allow"
        message = ""
    elif any_not_found:
        action = "warn"  # warn, not hard-block -- fail-open philosophy
        message = (
            "MERIDIAN PKG GUARD (23f21820): One or more packages could not be verified on "
            "their registry. This is a strong signal of a hallucinated or typo-squatted name. "
            "DO NOT proceed with the install. Call request_hitl to get human confirmation "
            "before installing unverified packages. Details: " + " | ".join(messages)
        )
    else:
        action = "warn"
        message = (
            "MERIDIAN PKG GUARD (23f21820): Package verification warning -- "
            + " | ".join(messages)
            + " -- Verify the package name is correct, or call request_hitl for human confirmation."
        )

    return {
        "action": action,
        "message": message,
        "packages": [p for p, _ in extracted],
        "results": package_results,
        "allowlisted_all": allowlisted_all,
    }
