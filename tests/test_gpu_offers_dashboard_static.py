"""Static checks for the GPU offers tab (dashboard-gpu-offers.ts) and its wiring.

These read the source files and the tracked bundle. They run no browser and no server.

* The module never assigns markup strings to the DOM, so provider text (names, ids, data centers, error text)
  can only ever be shown as text.
* The tab is wired: a vtab button, a drawer panel with its root, a loader call, and the tab-group and waffle
  registrations that the rail tests pin.
* The tracked bundle is the build of the current sources. Its content hash matches asset-manifest.json, so a
  source change without a rebuild fails here.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "meridian" / "static"
MODULE = STATIC / "dashboard-gpu-offers.ts"
DASHBOARD = STATIC / "dashboard.ts"
CSS = STATIC / "dashboard.css"
BUNDLE = STATIC / "dashboard.bundle.js"
MANIFEST = STATIC / "asset-manifest.json"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_module_never_assigns_markup_strings_to_the_dom():
    source = _read(MODULE)
    assert "innerHTML" not in source
    assert "outerHTML" not in source
    assert "dangerouslySetInnerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source


def test_the_module_takes_no_url_or_link_from_provider_data():
    source = _read(MODULE)
    assert ".href" not in source
    assert "setAttribute(\"href\"" not in source
    assert "createElement(\"a\")" not in source


def test_the_module_builds_its_path_from_a_fixed_constant_and_the_filters_only():
    source = _read(MODULE)
    assert 'GPU_OFFERS_PATH = "/compute/offers"' in source
    assert "api_key" not in source
    assert "RUNPOD_API_KEY" in source  # only as user-facing text in the not-configured message


def test_the_dashboard_adds_a_vtab_button_for_the_tab():
    source = _read(DASHBOARD)
    assert 'data-vtab="gpu-offers"' in source
    assert "vtab === 'gpu-offers'" in source
    assert "loadGpuOffersTab(project.id)" in source


def test_the_dashboard_has_a_drawer_panel_and_root_matching_the_loader():
    source = _read(DASHBOARD)
    assert 'id="drawer-gpu-offers-${project.id}"' in source
    assert 'id="gpu-offers-root-${project.id}"' in source
    module = _read(MODULE)
    assert 'document.getElementById(`gpu-offers-root-${projectId}`)' in module


def test_the_dashboard_imports_the_loader_from_the_module():
    source = _read(DASHBOARD)
    assert re.search(r'import \{ loadGpuOffersTab \} from "\./dashboard-gpu-offers";', source)


def test_the_tab_is_in_the_work_group_the_rail_tests_pin():
    source = _read(STATIC / "dashboard-tabgroups.ts")
    assert '"gpu-offers"' in source
    assert "work" in source


def test_the_waffle_has_a_tile_and_an_icon_for_the_tab():
    source = _read(STATIC / "dashboard-waffle.ts")
    assert 'id: "gpu-offers"' in source
    assert "gpuoffers" in source
    assert '"gpu-offers", "settings"' in source  # ranked before Settings so Settings stays last


def test_the_stylesheet_has_the_tab_classes_the_module_uses():
    css = _read(CSS)
    module = _read(MODULE)
    for class_name in ("gpu-offers-root", "gpu-offers-table", "gpu-bar-row", "gpu-offers-status--warn"):
        assert class_name in css or class_name in module
    for class_name in ("gpu-offers", "gpu-offers-status", "gpu-bar", "gpu-offers-table", "gpu-offers-showall"):
        assert f".{class_name}" in css, class_name


def test_the_tracked_bundle_matches_its_manifest_hash():
    # build.mjs writes the hash of the bundle bytes into asset-manifest.json. A mismatch means the bundle was not
    # rebuilt after a source change (run `npm run build`).
    manifest = json.loads(_read(MANIFEST))
    digest = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()[:12]
    assert manifest["bundle_hash"] == digest


def test_the_bundle_contains_the_new_tab():
    bundle = _read(BUNDLE)
    assert 'data-vtab="gpu-offers"' in bundle
    assert "gpu-offers-root-" in bundle
    assert "/compute/offers" in bundle
