# Meridian local runner and tunnel architecture investigation

Date: 2026-08-31

Status: planning record for `meridian-build`; no product version change.

## Executive conclusion

Meridian should be local-first for development and local desktop use. The
tunnel should remain an optional authenticated transport adapter for browser
clients, hosted collaboration, and remote access. It should not be the only
way to reach local code, documents, outputs, or scripts.

The current complaint is architectural rather than a single broken socket:
the tunnel process is simultaneously a relay client, plugin registry, child
process supervisor, cold package installer, readiness probe, reconnect loop,
port owner, idle reaper, and resource watchdog. That makes failures opaque,
expensive to diagnose, and capable of taking down too much of the local tool
surface at once.

## Evidence from the current repository

- `meridian/__main__.py` exposes separate `--mcp` and `--tunnel` modes, but
  `--tunnel` immediately performs stale-port cleanup and then enters the large
  `tunnel_client.run_tunnel()` orchestration path.
- `meridian/tunnel_client.py` currently contains the multi-slot supervisor.
  The indexed source reports `run_tunnel()` at roughly 775 lines, with many
  loops and subprocess/reconnect branches. `SlotProxy` lazily starts one
  child per slot and has useful state, port, process-group, idle-kill, and
  restart logic, but those mechanisms still share one parent runtime.
- `meridian/tunnel_plugins.py` resolves a broad set of filesystem, code,
  extractor, Office, Desktop Commander, Docs, Outputs, debugger, and custom
  slots. Several defaults invoke `uvx` or `npx`; local-path packages can
  trigger cold environment construction, and stale overrides are possible.
- `meridian/tunnel_lifecycle.py` already provides a bounded lifecycle state
  machine and a small transition ring buffer. This is valuable groundwork,
  but the module itself documents that live diagnostics wiring is follow-up
  work.
- `meridian/tunnel_preflight.py` provides bounded child preflight and an
  advisory quarantine tracker. Its own contract says it is not yet wired into
  the live slot-advertisement path.
- The audit found a Windows event-loop mismatch risk: the slim tunnel
  entrypoint can force a selector loop while tunnel command handling uses
  asyncio subprocess APIs that require a Proactor-compatible loop on Windows.
- The normal `run_tunnel()` slot construction does not consistently opt every
  child into the existing owned-process lifecycle and budget watchdog. The
  budget machinery exists, but existence is not enforcement; this is why a
  single runaway indexer can dominate the host.
- The CLI startup path performs a broad stale-port sweep before the more
  careful claim-file/identity checks. That can terminate an unrelated process
  or a concurrent tunnel instance and is unsafe as an application startup
  primitive.
- Several timeout/error paths still need review for bounded cleanup: plugin
  installation can return after a timeout without killing/reaping the child,
  and diagnostic probes can collect large stdout/stderr streams before
  truncating them.
- The WebSocket client has a path using `max_size=None`, so the transport lacks
  a hard frame-size guard. This is both a memory risk and a missing failure
  contract.
- The custom-plugin watchdog primarily observes launcher liveness. A wrapper
  can remain alive while the inner MCP service is unusable unless readiness is
  probed through the same contract as built-in slots.
- The current process shape on Windows shows multiple independent
  `codebase-memory-mcp`, Docs, Outputs, `uvx`, Node, and Meridian processes.
  That is evidence of a real process-ownership problem: the user sees a
  collection of launchers rather than one application with one authoritative
  status surface.
- Existing local stdio launchers are viable and materially faster for direct
  iteration. Hosted tunnel slot availability is a separate runtime fact and
  must not be presented as equivalent to local launcher availability.

## Serena recovery and generic structured-file gap

The local tunnel was started for this audit with a repository-scoped
`--no-kill` launch, so it did not run the legacy broad port sweep. A direct
MCP probe of the generated code-extractor endpoint returned a successful
`initialize`, and `tools/list` returned Serena 1.27.0 with 20 tools, including
`find_symbol`. A bounded `find_symbol` call resolved `run_tunnel` to
`meridian/tunnel_client.py` at lines 6850–7633. This proves the Serena
endpoint and launcher can work; it does not prove that every already-open
client has refreshed its connector/tool registry. The current planning
connector still reported no active Serena slot through its normal fallback
path, so tunnel-level health and client-level discovery remain distinct
states that the runner must expose explicitly.

The same probe confirmed a separate missing capability: there is no generic,
tunnel-independent MCP operation for inspecting one arbitrary local XML or
JSON file. `meridian-docs` is DOCX/LaTeX-oriented, `meridian-outputs` is
output-tree/index/provenance-oriented, and Desktop Commander is raw file
access. Existing DOCX parsing also loads complete XML members into an
in-memory tree without universal byte, expansion-ratio, depth, or item
budgets. This is why the planner staged the separate
`local-file-inspection__inspect_file` capability instead of expanding either
specialist package.

The local-only inspection contract is recorded in Meridian insight
`e277e4cf-577f-4b93-824d-ea202957b80f` and sprint item
`2ffd763d-9d1d-4928-82f7-ff4fb67a5113`. It must remain independent of Serena:
Serena inactivity may degrade symbol intelligence, but must not block safe
single-file XML/JSON inspection.

The live Windows process audit found multiple Serena/proxy chains for the
same repository and an existing process already owning the standard 8810
listener. The new scoped tunnel process was deliberately started with
`--no-kill`, so no unrelated process was terminated. This is the concrete
reason the permanent runner must reconcile instance leases and process
identity before binding a port; “Serena is running” and “this runner owns the
Serena slot” are different facts. A direct local probe still passed
`initialize` and `find_symbol`, so the immediate capability is available while
the ownership/readiness cleanup remains a Wave 0/1 implementation item.

The temporary tunnel process used for this probe was then stopped by its
explicit process IDs; the pre-existing Serena proxy tree was left untouched.
A second local `find_symbol` probe still returned HTTP 200 and the expected
`run_tunnel` location. This is the desired recovery behavior: remove only an
identified duplicate owner and preserve the working capability, rather than
killing processes by executable name.

## Failure matrix

| Failure | Why it hurts | Correct boundary | Permanent direction |
| --- | --- | --- | --- |
| Cold `uvx`/`npx` install | First use looks hung and consumes time/memory in the tunnel process | Installation/preparation | Preflight and cache dependencies before serving; never install on a request path |
| One child crashes | A shared supervisor/relay can make the whole surface appear unavailable | Per-slot process group | Isolate children, expose slot-level state, restart only the failed slot |
| Detached Windows launcher | Parent PID can exit while the real server remains alive | Process ownership | Record process-group/child identity and use a single-instance lease, not only a port probe |
| Duplicate tunnels | Duplicate slots contend for ports, caches, and index databases | Machine-local instance | One runner instance per scope; explicit attach/reuse or fail with a diagnostic |
| Tunnel socket opens then dies | “Connected” is shown before the connection is usable | Readiness | Require authenticated, usable readiness and surface `never_ready`/backoff state |
| Memory pressure | Multiple Python/Node/indexer processes compound; tests and indexing can exhaust RAM | Host-local budget | Sample RSS/CPU per child, quiesce, then terminate only the offender; keep bounded history |
| Windows loop mismatch | Selector-based tunnel entrypoint cannot spawn subprocesses reliably | Runner process model | Keep tunnel subprocess work on a Proactor-compatible loop or isolate it in a managed child |
| Broad port sweep | Startup can kill an unrelated or competing process | Ownership | Replace port-only cleanup with a scoped lease plus identity-checked process tree |
| Unbounded frame/output | A peer or child can inflate runner RSS before truncation | Resource boundary | Set WebSocket/message limits and tail-bounded stdout/stderr readers |
| Wrapper alive, service dead | Watchdog sees a PID but not a working MCP service | Readiness | Probe the service, not merely its launcher |
| Stale plugin override | A saved command can point at an old local path or package entry point | Config resolution | Show resolved command, freshness, and repair action before launch |
| Tunnel unavailable | Browser/hosted clients lose access even though local tools work | Transport adapter | Capability-based local fallback; make degraded mode explicit |
| Debugging requires a tunnel | A small code/config bug becomes a multi-hour remote round trip | Developer escape hatch | `doctor` plus a direct script runner that talks to the same contracts locally |

## Proposed operating model

```text
Desktop app / CLI
        |
        v
Local Runner Supervisor  <---- direct scripts / doctor / JSON status
        |
        +-- Meridian core stdio (`--mcp`)
        +-- optional local specialist children (Docs, Outputs, CodeIndex, Serena, DC)
        +-- optional hosted tunnel adapter
                                  |
                                  +-- browser clients
                                  +-- hosted/team control plane
```

The supervisor owns process lifecycle and diagnostics. It does not own the
authority of Meridian project data; Postgres remains the hosted authority and
the existing local-resilience layer remains the local degraded-mode authority.
The tunnel is an adapter that exposes selected local capabilities to a remote
client, not the source of truth for local work.

### Local modes

1. `meridian --mcp`: direct stdio server, no tunnel and no network dependency
   unless a specific tool explicitly requests hosted state.
2. `meridian local` (proposed): a bounded local supervisor with a machine-
   readable status endpoint/JSON file, one-instance protection, per-slot
   controls, logs, and `doctor` commands.
3. `meridian --tunnel`: an explicit bridge mode launched by the local
   supervisor, with a clear “local child healthy / remote socket healthy” split.

The exact CLI spelling is a design detail; the invariant is that direct local
execution must not be routed through the tunnel merely because the tunnel
exists.

## Product architecture decisions

### 1. Make the fast path scriptable

Every important local action should be callable through a stable, documented
JSON contract: start/stop/restart a slot, inspect status, run a preflight,
tail bounded logs, and execute a trusted diagnostic script. A script should
be able to reproduce a bug against the real module in minutes without needing
the UI, a tunnel, or a remote deployment.

Scripts must be explicitly scoped to a repo/project, use an allowlisted
command or declared executable, write receipts, and never receive secrets
through a shared Meridian record. The script registry and artifact receipt
work already identified in the board should become part of this contract.

### 2. Separate control plane from transport

The local supervisor should expose one status model with independent states
for child process, local MCP readiness, and remote tunnel readiness. A green
local slot must remain green when the hosted socket is down; a green socket
must not mask a dead child.

### 3. Stop hiding preparation inside first use

`uvx`/`npx` resolution and package installation should be an explicit
preflight step with a bounded timeout, cache location, version/fingerprint,
and a repair action. The first actual tool call should not trigger an
unbounded dependency fetch.

### 4. Make resource ownership visible

The runner should persist a small machine-local manifest containing runner
instance ID, parent PID, child PID/process-group IDs, repo scope, slot,
resolved command fingerprint, start time, health state, and last bounded log
tail. It should be recoverable after a crash and safe to delete/rebuild.

### 5. Package a real app only after the runtime contract is stable

The first implementation should be a CLI/service-quality local runner and
diagnostic contract. A thin desktop shell can then provide tray status,
start/stop/restart, logs, plugin toggles, and “open diagnostics”. Tauri is a
credible long-term shell because its official documentation supports bundling
external binaries as sidecars, system-tray UI, and autostart across Windows,
macOS, and Linux. See the [Tauri sidecar guide](https://v2.tauri.app/develop/sidecar/),
[Tauri system-tray guide](https://v2.tauri.app/learn/system-tray/), and
[Tauri autostart guide](https://v2.tauri.app/plugin/autostart/).

Electron is technically viable and has a first-class utility-process model
for child services, but it brings a Chromium/Node desktop runtime to a project
whose core is Python. Its utility process and sandbox APIs are useful
references, not a reason to introduce Electron before the runner contract is
proven. See [Electron utilityProcess](https://www.electronjs.org/docs/latest/api/utility-process)
and [Electron sandboxing](https://www.electronjs.org/docs/latest/tutorial/sandbox).

For Windows distribution, Microsoft documents MSIX as the modern packaging
path with clean install/update/uninstall behavior, while also documenting
limitations and elevation requirements when packaging services. The first
developer build should therefore prove the runner with a direct signed
installer/portable build; MSIX should follow once background execution and
package-identity requirements are understood. See Microsoft's
[Windows packaging overview](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/packaging/)
and [MSIX service limitations](https://learn.microsoft.com/en-us/windows/msix/packaging-tool/convert-an-installer-with-services).

## Proposed execution waves

### Wave 0 — local runner contract and diagnostics

- Define a version-neutral local runner status schema.
- Add a single-instance lease and scope-aware process manifest.
- Expose `doctor`, `status`, `preflight`, `restart`, and bounded log-tail
  operations without a tunnel.
- Make every runner-owned child use the existing process-group/Job Object
  lifecycle and resource budget path; add a test that a budget breach kills
  only the owning slot's tree.
- Remove broad port-only cleanup from the normal startup path and replace it
  with identity-aware stale-instance recovery.
- Add bounded frame, response, stdout, stderr, and log retention limits.
- Make local `--mcp` reachability and hosted tunnel reachability distinct
  capability checks.
- Add focused tests for duplicate launch, child crash, stale PID, cold-start
  timeout, malformed command, and bounded output.

### Wave 1 — isolate and simplify the existing tunnel

- Refactor slot supervision behind the runner contract without changing the
  existing plugin commands.
- Wire preflight/quarantine into advertisement and startup so unhealthy slots
  are not reported as usable.
- Resolve the Windows event-loop contract for tunnel subprocesses and keep
  stdio MCP protocol traffic isolated from supervisor logs.
- Ensure one child failure cannot restart unrelated slots.
- Make memory/CPU budgets configurable, observable, and enforced per child.
- Add a bounded local script escape hatch using the same diagnostics and
  provenance contract.

### Wave 2 — local/hosted capability routing

- Route direct desktop calls locally by default.
- Use the tunnel only when a remote client explicitly needs a local resource
  or when the user chooses hosted execution.
- Publish a handshake that reports local readiness, remote readiness,
  available fallbacks, and the authority for each data type.
- Keep Meridian Docs, Outputs, CodeIndex, Serena, and Desktop Commander
  independently restartable and independently visible.

### Wave 3 — desktop packaging

- Build a thin Tauri shell around the stable runner contract.
- Windows first: tray app, no-terminal onboarding, signed portable/installer
  artifact, repair/diagnostics action, and optional autostart.
- macOS: `.app` plus LaunchAgent-backed autostart and signed distribution.
- Linux: AppImage/deb plus user-session autostart; retain the CLI for servers.
- Only then evaluate MSIX/Store packaging and enterprise service installation.

## Not part of the first runner sprint

- Tigris/S3 is not required to make the local runner useful. It may later hold
  large logs, caches, or portable diagnostic bundles, but control state and
  authoritative provenance must not depend on object storage being available.
- A tunnel rewrite is not the first step. The local contract, diagnostics, and
  process ownership model must land first so tunnel behavior can be tested as
  one adapter instead of the entire user experience.
- Do not bundle every specialist into one always-on process. Idle and on-demand
  slots are appropriate, but they need independent state and clear ownership.

## Exit criteria for starting implementation

The implementation sprint is ready when each item names its exact write scope,
local fallback, focused test file, and expected status receipt. The first
executor wave should not require a production deploy or a live tunnel. It must
be able to prove the local path on Windows with small, serial tests and a
bounded synthetic child process. Production tunnel verification is a later
integration gate, not a prerequisite for improving local iteration.
