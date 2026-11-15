"""Sprint febd27ec: the desktop app lives in this repo under desktop/, and must not reach the server.

These pin the boundary the move depends on:

- the hosted server image leaves desktop/ out (.dockerignore);
- no core workflow can fire on a desktop-v tag, because every core tag trigger starts with a literal "v".
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"


def test_dockerignore_leaves_the_desktop_folder_out_of_the_server_image():
    lines = [line.strip() for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    assert "desktop/" in lines


def _triggers(workflow: Path) -> dict:
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1), so accept either spelling. `on` may also be a
    # single event name or a list of them, which carry no filters.
    raw = data.get(True, data.get("on"))
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {event: None for event in raw}
    return raw


def test_no_core_workflow_fires_on_a_desktop_v_tag():
    # A desktop-v tag is a tag push. A workflow fires on it only if it listens for tag pushes or for release or create
    # events. Each push trigger must filter tags to v-patterns, or filter branches so tags never match. A push
    # trigger with no tag or branch filter fires on every tag, desktop-v included.
    for path in sorted(WORKFLOWS.glob("*.yml")):
        triggers = _triggers(path)
        assert "release" not in triggers and "create" not in triggers, f"{path.name} listens for release or create"
        if "push" not in triggers:
            continue
        push = triggers["push"] or {}
        assert isinstance(push, dict), f"{path.name}: a bare push trigger fires on every tag"
        if "tags" in push:
            patterns = [str(tag) for tag in (push["tags"] or [])]
            assert patterns, f"{path.name}: an empty tag filter"
            for pattern in patterns:
                assert pattern.startswith("v"), f"{path.name} triggers on {pattern!r}, which a desktop-v tag could match"
        else:
            assert "branches" in push or "branches-ignore" in push, (
                f"{path.name}: a push trigger with no tag or branch filter fires on every tag"
            )


def test_the_source_distribution_leaves_the_desktop_folder_out():
    # Core publishes an sdist to PyPI on v-tags. hatchling includes the whole repo in an sdist unless told otherwise.
    import tomllib

    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "/desktop" in config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]


def test_the_desktop_workflow_runs_only_on_desktop_changes():
    triggers = _triggers(WORKFLOWS / "desktop-test.yml")
    for event in ("push", "pull_request"):
        paths = triggers[event]["paths"]
        assert paths, f"{event} must be limited to desktop changes"
        assert all(p.startswith(("desktop/", ".github/workflows/desktop-test")) for p in paths), paths


def test_the_desktop_workflow_is_not_one_the_promotion_gate_watches():
    # deploy.yml auto-promotes dev to main only when the run named in its workflow_run gate succeeds. The desktop
    # workflow must not be in that list, or a desktop test failure would block a server deploy.
    name = yaml.safe_load((WORKFLOWS / "desktop-test.yml").read_text(encoding="utf-8"))["name"]
    gate = _triggers(WORKFLOWS / "deploy.yml")["workflow_run"]["workflows"]
    assert name not in gate
    assert name != "Test (dev branch)"
