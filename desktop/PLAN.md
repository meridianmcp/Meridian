# Meridian Desktop — plan

Private repo. Local coordination only. The hosted site (usemeridian.us) already
provides the web UI, so this app does not reproduce it.

## Status (as of 2026-10-14)

| Item | State |
|---|---|
| Repo | Branch `dev`, commits `7c1aa0c` and `ddfca52`. Working tree clean. |
| Skeleton build (workflow `wsil64j56`) | Stopped by the lead while still running. It had written a root-layout package, packaging scripts, and `LICENSES/`. Partial. See "Incidents". |
| Autodesk and auto-test research (workflow `wwcwaw6tn`) | Finished. See "Research findings". |
| Maintenance, adopters, LGPL-in-the-wild research (`wud5uwacx`) | Finished. See "Research findings". |
| Wave 1 foundation | Committed on `dev` (`7c1aa0c`). 64 tests pass under pixi. Boundary ok. The packaged bundle passes the verifier (2026-10-14) and its `--smoke` exits 0. See "Qt DLL policy". |
| Environment | pixi only: Python 3.12.15, PySide6 6.12.0. The old `.venv` (3.10) was deleted on 2026-10-14 after a reference check. |

## Scope

Desktop features are local coordination only:
Projects, Writing, Zotero, Tunnel, Services, Code, Logs, Settings.
Each tab is built in a numbered wave (see "Waves").

## Layout and interpreter

- Root package `meridian_desktop/` at the repo root, not `src/`.
  `packaging/verify_bundle.py` assumes this layout.
- Python 3.12.15 through pixi (`pixi.toml`, `pixi.lock`). The Meridian core
  supports 3.11 to 3.12, so the desktop and the core share one interpreter line.
  No system Python was changed.

## Licensing rules

- **PySide6 / Qt:** we elect LGPL-3.0-only. PySide6's own metadata reads
  `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` and points to a commercial Qt
  license file. We do not use the GPL or commercial options. The election is
  recorded in `packaging/lgpl_manifest.toml` and `NOTICE.md`.
  - Ship Qt as dynamically linked, separately replaceable DLLs.
  - Ship the LGPL text, notices, a written source offer, and an EULA that permits
    modification and reverse engineering for debugging.
  - Folder-mode PyInstaller only, not one-file.
- **meridian-docs** (`extensions/meridian-docs`): MIT. Bundling is allowed.
- **meridian-latex** (the meridian-latex directory in the Meridian repo, including its Overleaf Chrome
  extension and engine): **Meridian Source License 1.0 (MSL-1.0)**. This is not an
  OSI open-source license.
  - Section 3(a) bars Competing Use: offering it as a hosted or managed service.
  - Section 3(e) bars sublicensing or reselling it as a standalone commercial
    product for third parties.
  - **Rule:** the desktop never vendors, copies, or bundles any meridian-latex
    code. The desktop connects to a user-installed engine over loopback
    (`http://127.0.0.1:8471`). If no engine is running, the LaTeX section shows
    "not connected" and nothing else changes.
  - Whether a *connecting* desktop build raises a 3(e) issue for a commercial
    release is an owner decision. This is not legal advice. Get a read before any
    paid release.
- The repo's own license is MSL-1.0, the same text as the core. `LICENSE` in this
  repo is that text, unchanged. Its "Software" definition covers "the Meridian source
  code", and the desktop is Meridian source code. Chosen on the owner's delegation,
  2026-10-14. Wording review: owner.

Boundary enforcement: `tools/check_boundary.py` (Wave 1) fails when Qt is
imported outside `meridian_desktop/ui/` (with `tray.py` listed as a UI exception)
or when any file names the meridian-latex directory. The verifier's
`check_source_leak` scans the built bundle.

## Writing stack

### DOCX — meridian-docs (MIT)

- Package `meridian-docs-mcp` (stdlib-only OOXML parser). The MCP surface has
  about 65 tools. Examples: `audit_document`, `document_outline`,
  `check_render_capability`, `scan_citation_keys`, `search_document`,
  `write_section`, `insert_citation`, `insert_image`, `insert_equation`,
  `insert_figure_block`, `sync_bibliography`, `merge_docx_draft`.
- 29 journal style presets (28 publishers plus `default`) in
  `meridian_docs/journal_style_presets/`.
- **Connection (planned):**
  - Read-only calls (audit, outline, render capability, citation scan) run
    in-process. The package is stdlib-only, so there is no Qt conflict.
  - Writes go through the MCP server, never a direct file edit. The OOXML
    integrity checks and render gate then run on every write.
  - Figure and image inserts pass the rights policy first (`rights_policy.py`:
    STORE_IMAGE / STORE_UNMODIFIED / LINK_ONLY). In `commercialized` use mode, a
    gated image is refused, not inserted.
- Desktop shows: document list, audit findings, render-gate result, citation
  consistency, and a diff of the last write.

### LaTeX / Overleaf — meridian-latex (MSL-1.0, connect only)

- Engine HTTP server on `127.0.0.1:8471`. Routes:
  `GET /health`, `GET /extension-version`, `POST /outline`, `POST /claim`,
  `POST /lease`, `POST /release`, `GET|POST /provenance`,
  `GET /zotero-lookup`, `POST /open-overleaf`, `GET /claims`.
- MCP surface: about 23 tools, for example `outline_tex`, `lint_tex`,
  `lease_document`, `claim_node`, `record_provenance`, `overleaf_login_status`.
- Chrome MV3 extension for `https://www.overleaf.com/project/*` only. Host
  permissions are Overleaf and `127.0.0.1:8471`.
- **Desktop connection (planned):**
  - Probe `/health`, then `/extension-version`. Show "connected" only when both
    answer. Show the engine version and the extension version side by side.
  - Show live claims (`/claims`) and provenance (`/provenance`). Release a claim
    only after confirmation (`/release`).
  - "Open in Overleaf" calls `/open-overleaf` after confirmation.
  - Zotero lookup via `/zotero-lookup`, shared with the Zotero tab.
  - **Overleaf login is human-only.** The desktop shows login status from
    `overleaf_login_status` and tells the owner to run
    `node dist/overleaf-login.js login` in a terminal. The desktop never runs it,
    never reads a session cookie, and never stores one.
  - **Cloud-hosted Overleaf (the owner's long-term preference) conflicts with that
    rule.** A hosted engine would need an Overleaf session on a server. Decide that
    before building anything, and check Overleaf's terms (not done yet). Hosting the
    engine for third parties is Competing Use under MSL 3(a).
- Known risks from the engine README:
  - Its CORS once accepted any installed browser extension. It is now pinned to
    the extension id via the manifest `key`. The extension must be reloaded
    after that change. The desktop must not treat CORS as authentication.
  - The write-back spec (`docs/write-back-spec.md` in the meridian-latex directory) is
    marked "design, not yet implemented". Confirm which claim and lease routes
    are real before relying on them.
- The spec requires meridian-latex to stay self-contained. It must not depend on
  `meridian-docs` or the Meridian server, and the desktop must not change that.

### Google Docs — no extension exists yet

- **Finding:** the repo has no Google Docs extension or integration. Searching
  tracked files for Google Docs or gdocs returned nothing. "Google" appears only
  in comments and in the note that Chrome is required.
- **Google Drive connector:** a Google Drive connector is attached to this
  session (tools such as `search_files`, `read_file_content`, `update_file`,
  `share_file`). I have not called it. I have not confirmed whether it covers
  Google Docs editing.
- **Options (owner decision needed before Wave 5):**
  - **A. Chrome MV3 extension for `docs.google.com`,** mirroring the Overleaf
    one. Talks to the same loopback engine pattern. No OAuth. DOM-driven, so it
    breaks when Google changes its UI.
  - **B. Google Docs API with OAuth.** Needs a Google Cloud project and a
    consent screen created by the owner. Structured `batchUpdate` edits. Tokens
    kept in the OS keychain. This is the recommended write path.
  - **C. Drive connector for read and export only,** as a first step. Checks
    value before committing to A or B.
- **Decision (owner, 2026-10-14):** OAuth. The local path is option B.
- **Local path (desktop, no cloud):** loopback OAuth with PKCE, then the Docs REST
  API `documents.batchUpdate`. It is the only documented write path. Comments and
  suggestions are GA per the 2026-09-30 release notes. Suggestion mode rejects
  named ranges and several structural edits. Scope: `drive.file` (non-sensitive),
  plus `documents` (sensitive) if needed. **Untested:** whether a `drive.file` token
  can batchUpdate a Doc chosen through the Picker.
- **Testing-status limit:** refresh tokens expire after 7 days for any scope except
  basic profile. That includes `drive.file`. Owner-only use needs weekly re-consent,
  or the consent screen must leave Testing.
- **Add-ons and extensions are not the local path.** Apps Script runs on Google's
  servers and has no documented route to `127.0.0.1`. Chrome's Local Network Access
  gate blocks public-to-loopback requests without a permission. Neither is no-install.
- **Cloud (hosted) path, the owner's long-term preference.** A Docs add-on or Chrome
  Web Store extension calling usemeridian.us would let users index and work through
  comments without the tunnel. Constraints:
  - Google's Limited Use policy forbids building databases and permanent copies of
    Google user data. It also forbids using that data to train or improve AI
    models, and requires encryption at rest. Persisting Doc content in Neon, or
    sending it to a model provider, needs a policy read before anything is built.
  - `drive` and `drive.readonly` are restricted and trigger a security assessment.
    `documents` and `drive.file` are not restricted.
  - Open: Google's Docs MCP server (Developer Preview, 2026-10-01) is not
    evaluated. Whether Chrome content scripts can edit the Docs editor is unknown
    and needs a DevTools spike.

## Qt DLL policy (verified by a real build, 2026-10-14)

- **Build:** PyInstaller 6.22.3, onedir, `packaging/build.py`. PyInstaller collects
  Qt plugins and their dependencies by hook. The first raw build had 53 verifier
  problems: forbidden Qt DLLs (Qt6Network, Qt6OpenGL, Qt6Pdf, the Qml family,
  Qt6Quick, Qt6Svg, Qt6VirtualKeyboard), plugin categories outside the allowlist
  (imageformats, iconengines, generic, tls, networkinformation,
  platforminputcontexts), and `opengl32sw.dll`.
- **Fix:** prune after collection, driven by the manifest. `prune_forbidden` removed
  31 files. The verifier then checks what is left. The plugin allowlist is now
  per file: `qwindows` (Windows platform), `qoffscreen` (headless, used only by
  `--smoke`, imports Qt6Core and Qt6Gui only), `qmodernwindowsstyle`.
- **Allowed Qt6 DLLs:** Qt6Core, Qt6Gui, Qt6Widgets (the import closure).
- **Forbidden Qt6 DLLs:** 149, by name.
- **Non-Qt DLLs:** allowed are the MSVC runtime (`msvcp140`, `msvcp140_1`,
  `msvcp140_2`, `vcruntime140`, `vcruntime140_1`) and `pyside6.abi3`. Forbidden are
  FFmpeg (`avcodec`, `avformat`, `avutil`, `swresample`, `swscale`), `opengl32sw`,
  `pyside6qml.abi3`, `concrt140`, `vcamp140`, `vccorlib140`, `vcomp140`, and
  `msvcp140_codecvt_ids`.
- **Result:** the verifier passes on the pruned bundle, with 10 checks. The first
  verified build recorded 6 sha256 hashes. A second run without writes matches
  them. The packaged `--smoke` exits 0.
- **Still open:**
  - The verifier does not enforce the non-Qt lists yet. Wave 7 adds that.
  - `shiboken6.abi3.dll` is in the bundle and is not classified. Its folder is
    outside `PySide6/`, so the classification test does not cover it.
  - `--smoke` uses the offscreen platform, so the real Windows GUI path is not yet
    exercised by the bundle test.
  - The MSVC runtime licence is unverified.
  - `Qt6Network` is forbidden, so the app cannot use QtNetwork. If Wave 3 needs it,
    reclassify through the closure check.

## Waves

Each wave ends with: tests green, `tools/check_boundary.py` clean, the verifier
run with a stated result, and a report of exactly what ran.

1. **Foundation (in progress).** Qt shell with the eight placeholder tabs,
   settings store (`local`/`hosted` mode), boundary checker, LGPL manifest, static
   bundle verifier, draft LICENSES/NOTICE/OFFER-SOURCE/EULA, smoke and unit tests.
   Placeholders make no network calls.
2. **Projects and settings.** Project list and detail, folders, locality, the
   commercialized use-mode toggle, Hosted/Local switch. Client setup for Claude
   Desktop and Codex (from `meridian/client_setup.py`, uncommitted on
   `mcp-client-setup`, so commit that first by path). Then Cursor, Gemini CLI,
   VS Code Copilot, Windsurf (after a path check), and Zed (UI only). Grok and
   ChatGPT get hosted-connector instructions only.
3. **Services, tunnel, logs.** Health for local MCP servers, server status, the
   six tunnel slots, served folders, logs, diagnostics. Tunnel start and stop stay
   locked until item `84d8fd28` (local-trust contract) is done.
4. **Zotero.** Status for the local API on `127.0.0.1:23119` (last check: not
   responding), launch with confirmation, library search, citation lookup.
   Autostart needs the owner's explicit `--yes`.
5. **Writing.** DOCX panel (reads in-process, writes via MCP, rights gate on
   images), LaTeX panel (engine probe, claims, provenance, Overleaf open, login
   status), Google Docs panel (per the owner's option choice).
6. **Research, outputs, code.** Research runs and findings (scheduler dry-run
   only; the simulator is blocked), outputs registry, provenance, staleness,
   BM25 code search (`meridian/code_search.py`, uncommitted on `mcp-client-setup`),
   codebase index status, Serena activation per project. `meridian-outputs` needs
   authorization through `/mcp` before it can be used.
7. **Release.** PyInstaller folder build. `verify_bundle.py --dist` against the
   real output. Confirm no meridian-latex or MSL file is present. Ship LICENSES,
   NOTICE, OFFER-SOURCE, EULA.
8. **Regression.** Per-tab offscreen smoke tests (pytest-qt), boundary check in
   CI, leak mutation tests (plant a forbidden import and confirm the check fails),
   a scan of the built folder for forbidden files.

## Research findings

From `wud5uwacx` (finished):
- **PySide6 cadence:** steady, tied to Qt 6.12 LTS. 6.12.0 shipped Oct 8, 2026.
- **wxPython:** 4.3.2 shipped Oct 13, 2026. Small core team, long gaps between
  releases, and it builds on the wxWidgets 3.3.x line that its own notes call
  API/ABI unstable. Higher stall risk than PySide6.
- **Commercial PySide6 adopters:** five confirmed or strongly indicated, all in
  VFX, 3D, and reverse-engineering tools. None found in medical, industrial HMI,
  CAD, or lab instruments.
- **Commercial wxPython adopters:** few, and mostly dated (Dropbox 2009 client,
  Transana, Vintech RCAM-Pro, Digsby). None confirmed for 2025–2026.
- **Gaps:** Qt LTS end dates unconfirmed (qt.io pages returned 404). PySide 6.12.0's
  bundled Qt version unconfirmed. wxPython licensing not checked.

From `wwcwaw6tn` (finished), checked against Autodesk and GitHub pages:
- **Maya 2022:** Autodesk published Qt 5.15.2 and PySide2 5.15.2 as forks under
  `github.com/autodesk-forks` (tag `Maya2022`). The release notes name no
  license. The fork repos contain LGPLv3, GPLv2, GPLv3, and a Qt commercial file.
  Which of those the tag carries is not verified.
- **Maya 2025 and 2026:** the DEVHELP pages list Qt 6.5.3 and PySide6 6.5.3 with no
  link, "available upon request", and no license named.
- **Autodesk's 2014 Inventor page** offered Qt 4.7.2 under LGPL-2.1, with a source
  offer valid three years from purchase. That is the only Autodesk page found
  that names an LGPL offer term. The three-year term is Autodesk's, not Qt's.
- **Maya DevKit** is distributed under the Autodesk License and Services
  Agreement, not as open source. Maya core is described as proprietary. The
  Autodesk EULA text itself was not retrieved.
- **What this means:** Autodesk publishes Qt/PySide source for some versions and
  offers it on request for others. No Autodesk statement of LGPL practice for
  current Maya was found. Do not cite Autodesk as a model without a stronger
  primary source.

Automated LGPL, license, and leak testing (from each tool's own page; from the
narrow research run):
- **License scanning:** the verifier reads licence metadata from the standard
  library (`importlib.metadata`). `pip-licenses` was dropped: the dependency guard
  blocked it, and the stdlib read covers the need. Other candidates, not adopted:
  `LicenseCheck` (MIT), `liccheck` (Apache-2.0), ScanCode toolkit (Apache-2.0 code,
  CC-BY-4.0 rules).
- **Linkage:** PyInstaller's `pyi-bindepend` lists DLL dependencies. Its output
  format is not yet confirmed. PyInstaller is GPL-2.0 with a bundling exception.
  The verifier reads PE import tables with `pefile` (already installed).
- **Leak and boundary:** `import-linter` (BSD-2-Clause) for forbidden-import
  contracts. `gitleaks` (MIT) for secrets. `check-wheel-contents` (MIT) for
  wheel-level rules. None of these detects proprietary source content in a public
  tree, so we need a custom CI check. `tools/check_boundary.py` and the verifier's
  source-leak scan cover part of it.
- **Maya test harnesses** (`mayatest`, AL_USDMaya googletest, maya-usd) are not
  relevant here. This app does not run inside Maya.
- **Gap:** no Qt-specific LGPL checker was found. The Qt LGPL obligations page
  gives no offer duration.

## Decisions answered on 2026-10-14

- **Hosted Docs may store a database and a live copy for indexing.** The owner
  says indexing needs a DB, or at least a live copy. Google's Limited Use policy
  still applies. A policy read is required before anything is built. Encrypt at
  rest.
- **Overleaf may store the session server-side** if needed. This supersedes the
  "desktop never stores a cookie" rule for the cloud design. The local-engine
  rule stays for the local path. The MSL-1.0 Competing Use point for third-party
  hosting still needs a read. Encrypt at rest.
- **Qt DLL decisions accepted** (see "Qt DLL policy").
- **`.venv` deleted** on 2026-10-14. pixi is the only environment. No references
  remain except the verifier's forbidden-path names and the checker's skip list.
- **Review-comment ledger approved.**
- **Distribution (Chrome Web Store and similar) under consideration.** See
  "Distribution options".
- **Next:** a megasprint with an executor handoff, after prospecting.
- **Zotero autostart: approved** (owner, 2026-10-14). Not blocked any more. It is
  still gated in the UI by an explicit enable.
- **Repo licence: MSL-1.0**, delegated to me, applied (see above).
- **MSL-1.0 amendment: not needed for the desktop as designed.** The desktop never
  bundles the engine and connects over loopback. The point to check is whether
  the Overleaf extension is ever published to a store, because that is
  distribution of MSL code. Candidate clause, not applied, for the owner to
  decide:
  > Connection exception. Software distributed under this licence may connect
  > over a local network interface to a separately installed copy of meridian-latex
  > that the user runs on their own machine. That connection does not make the
  > connecting software a Competing Use or a sublicence, and it may be distributed
  > with, or alongside, a separately licensed client.
- **MCP approvals: no `/mcp` step.** See "MCP control panel".

## MCP control panel (owner requirement, 2026-10-14)

- **Requirement:** the desktop is the external control panel. Users should not
  have to authorize Meridian's MCP servers through `/mcp` in each client.
- **What is true now:** the servers that ask for authorization (`meridian-outputs`,
  `meridian-code`, `meridian-debug`, `meridian-extractor`, `meridian-fs`) are
  claude.ai connectors, so they're authorized in claude.ai connector settings. Local
  stdio servers need no OAuth. The main repo's `.mcp.json` uses a bearer header for
  the hosted server. Verified by reading the config files (names and transport only,
  no values).
- **Design:**
  1. Meridian-owned servers run as local stdio launchers, written by the desktop's
     client setup. No OAuth.
  2. The hosted server's bearer token lives in the OS keychain. The desktop writes
     it into client config, so the user approves once, in the desktop.
  3. Remote servers that do require OAuth: the desktop runs the authorization-code
     flow itself, using loopback PKCE. It stores tokens in the keychain and writes
     the client config. **Unverified:** whether Claude Code and Claude Desktop accept
     externally stored tokens for remote servers. Verify before relying on it.
  4. Consent is explicit and per server. The consent screen lists the scopes. There
     is no silent auto-approval, because that would remove the protection MCP
     authorization exists to provide. The desktop automates the flow, not the
     consent. Revocation and an audit log live in the desktop.

## Distribution options (under consideration, not decided)

- **A. Desktop only** (LGPL, pixi and PyInstaller bundle, local engines). It keeps
  files local, and the tunnel and Zotero stay local. Users who don't want local
  tools get no indexing of Docs or Overleaf without the tunnel.
- **B. Google Docs add-on from the Workspace Marketplace.** Google-native. It
  installs from the Docs add-on store and needs a hosted backend, since Apps Script
  runs on Google's servers. It also needs Marketplace review and OAuth verification.
- **C. Chrome Web Store extension** for docs.google.com and overleaf.com. It
  installs from the store, which is a trust signal. It must call the hosted backend,
  because loopback needs a Chrome permission. Store review, single purpose, minimal
  permissions (activeTab is the lightest), and the 2026 disclosure rule all apply.
  The existing Overleaf extension is MSL-1.0 and would need its licence reviewed
  before any reuse.
- **D. Hosted web surface only** (usemeridian.us). No install. OAuth and the Picker
  run in the browser.
- **Working recommendation:** build the hosted backend first, since B, C, and D all
  need it. Then add C as the in-page surface for Docs and Overleaf. Keep A for local
  folders, the tunnel, and Zotero. Trust messaging must say which parts are hosted.

## Owner decisions still open (before the answers above)

1. **Google Docs approach:** A, B, or C above. Recommended: test C, then build B.
2. **MSL-1.0 and a commercial desktop build:** confirm that connecting to a
   user-installed engine (never bundling it) is the rule you want. Get a read
   before any paid release.
3. **Python 3.12:** the core needs it. Approve installing 3.12 on this machine, or
   keep the desktop on 3.10 until the core import is needed.
4. **Repo license:** currently `TBD-owner`.
5. **Zotero autostart:** needs your explicit `--yes`.
6. **meridian-outputs:** needs `/mcp` authorization before the Outputs panel can
   use it.
7. **Overleaf login:** you run `node dist/overleaf-login.js login` yourself. The
   desktop never does.

## Incidents

- **Workflow `wsil64j56` stopped early (2026-10-14, 04:19).** The lead stopped it
  because its output file was 0 bytes and that looked like a dead run. It was
  still running. It had written a root-layout package, packaging scripts, and
  `LICENSES/`. Its partial work is kept and is being finished on that layout.
  Lesson: a 0-byte workflow output file does not mean the workflow has stopped.
- **Earlier wrong report.** The lead said the skeleton build had produced no
  files. That was wrong. Files existed when the check ran.
- **Wave 1 builder blocked.** The first brief said "no network downloads" and
  also allowed installing pytest-qt. That contradiction blocked the builder, which
  stopped with no deliverables. Corrected: installing declared dev dependencies
  is approved. No other downloads.
- **Duplicate research.** A narrow research run was relaunched while
  `wwcwaw6tn` was still running. Both finished. Their results are merged above.

## Not done / not verified (say so until it is)

- Wave 1: gates pass, not committed. The verifier's real checks have not run on a
  real bundle, because no PyInstaller build has been made.
- Wave 1 exe path: the embedded CArchive path is untested. Only PYZ files written by
  PyInstaller's own writer were tested.
- Google Docs: no extension exists. The Drive connector has not been tested.
- Autodesk: partly verified (see "Research findings"). No current Maya LGPL
  statement.
- Automated test tools: named from their own pages. Only `pytest-qt` is installed
  (Wave 1).
- Tunnel start and stop: locked by `84d8fd28`.
- Zotero local API: last check did not respond.
- Repo has no commits.
