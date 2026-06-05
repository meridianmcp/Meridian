"""Scan source/docs for potentially sensitive hardcoded data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = ["meridian", "docs", "templates", "scripts"]
TEXT_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".sqlite", ".db", ".pyc", ".woff", ".woff2", ".ttf", ".eot",
}
PUBLIC_HINTS = ("docs", "templates", "static")
AUTH_HINTS = ("auth", "oauth", "login", "github", "microsoft")
EXAMPLE_MARKERS = ("...", "<", "user:pass@host", "your_", "example", "localhost")


@dataclass
class Finding:
    severity: str
    category: str
    path: Path
    line_no: int
    snippet: str
    reason: str


def existing_targets() -> list[Path]:
    targets: list[Path] = []
    for raw in DEFAULT_TARGETS:
        target = ROOT / raw
        if target.exists():
            targets.append(target)
    repo_templates = ROOT / "meridian" / "templates"
    if repo_templates.exists() and repo_templates not in targets:
        targets.append(repo_templates)
    return targets


def iter_files() -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for target in existing_targets():
        for path in target.rglob("*"):
            if path.is_dir():
                continue
            if path.suffix.lower() in TEXT_SKIP_SUFFIXES:
                continue
            if path in seen:
                continue
            seen.add(path)
            files.append(path)
    return sorted(files)


def is_public_facing(path: Path) -> bool:
    lowered = path.as_posix().lower()
    return any(hint in lowered for hint in PUBLIC_HINTS)


def is_auth_related(path: Path, line: str) -> bool:
    lowered = f"{path.as_posix()} {line}".lower()
    return any(hint in lowered for hint in AUTH_HINTS)


def looks_like_example(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in EXAMPLE_MARKERS)


def mask(match: str) -> str:
    if len(match) <= 12:
        return match
    return f"{match[:6]}...{match[-4:]}"


def classify_connection(match: str) -> tuple[str, str]:
    if looks_like_example(match):
        return "info", "example connection string"
    return "high", "live-looking connection string"


def classify_email(match: str, path: Path, line: str) -> tuple[str, str]:
    domain = match.split("@", 1)[1].lower()
    if domain == "usemeridian.us":
        return "info", "company-owned contact or auth email"
    return "high", "personal email in source"


PATTERNS = [
    ("real_email", re.compile(r"\b[A-Za-z0-9._%+-]+@(?:gmail\.com|umsystem\.edu|usemeridian\.us)\b")),
    ("project_uuid", re.compile(r"5787cc92(?:-[0-9a-fA-F-]{0,36})?")),
    ("internal_host", re.compile(r"\b(?:autumn-pine|mute-hall|mute-bread)\b", re.IGNORECASE)),
    ("public_name", re.compile(r"\b(?:acqui-hire|Chinampa|Churchill)\b", re.IGNORECASE)),
    ("api_key", re.compile(r"\b(?:sk_(?:live|test)_[A-Za-z0-9]{12,}|sk_meridian_[A-Za-z0-9_-]{20,}|napi_[A-Za-z0-9]{16,})\b")),
    ("connection", re.compile(r"postgresql://[^\s\"'<>`]+")),
]


def scan_file(path: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for category, pattern in PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(0)

                if category == "public_name" and not is_public_facing(path):
                    continue
                if category == "connection":
                    severity, reason = classify_connection(value)
                elif category == "real_email":
                    severity, reason = classify_email(value, path, line)
                elif category == "api_key":
                    severity = "high"
                    reason = "live-looking API key"
                elif category == "project_uuid":
                    severity = "high"
                    reason = "hardcoded Meridian project ID"
                elif category == "internal_host":
                    severity = "high"
                    reason = "internal Neon host codename"
                else:
                    severity = "high"
                    reason = "internal codename in public-facing file"

                findings.append(
                    Finding(
                        severity=severity,
                        category=category,
                        path=path,
                        line_no=line_no,
                        snippet=mask(value),
                        reason=reason,
                    )
                )
    return findings


def main() -> int:
    findings: list[Finding] = []
    for path in iter_files():
        if path.name == "scan_sensitive.py":
            continue
        findings.extend(scan_file(path))

    findings.sort(key=lambda f: (f.severity, str(f.path), f.line_no, f.category))

    counts = {"high": 0, "medium": 0, "info": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
        rel = finding.path.relative_to(ROOT).as_posix()
        print(
            f"[{finding.severity.upper()}] {finding.category} "
            f"{rel}:{finding.line_no} {finding.snippet} - {finding.reason}"
        )

    print(
        f"\nSummary: high={counts['high']} medium={counts['medium']} info={counts['info']} "
        f"across {len(findings)} finding(s)"
    )
    return 1 if counts["high"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
