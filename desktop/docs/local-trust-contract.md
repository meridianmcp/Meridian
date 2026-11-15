# Local-trust contract: desktop tunnel and runner controls

Status: CONFIRMED by the owner in chat on 2026-10-22, with Amendments A to E at the end of this file.
Amendment F, also at the end, was accepted by the owner in chat on 2026-10-23.
Written by the desktop maintainer at the owner's request (sprint item 84d8fd28 is not in the
repository).

## Rules

1. Start and stop are user actions only, except the three launch-time starts in the Amendment
   (Zotero and the local server, amendments A and B, and the hosted tunnel, amendment G), each off by
   default. Nothing starts on a timer or from any request that arrives over the network.
2. Each action shows a confirmation dialog that names the exact command, the scope and the
   working folder. Cancel changes nothing.
3. The command is fixed: `python -m meridian.local_runner` with the subcommands `start`,
   `stop` and `status`, the `--scope` argument, and `--json` where it is used. It is built as an
   argument list. No shell is used, and no text is interpolated into a command. The scope comes
   from a fixed allowlist whose default is `tunnel`. Any other command or scope is refused.
4. Control is loopback-only. The desktop never accepts control requests from the network and
   never listens for them.
5. Timeouts: `start` is bounded at 30 seconds and `stop` at 15 seconds. On timeout the result is
   "not confirmed", and nothing is retried automatically.
6. The desktop logs each start and stop attempt to a local file in its settings folder: time,
   scope, outcome and the pid if reported. The log holds no secrets.
7. Status is read with `status`, without `--json`, because the core's `status` subcommand takes no
   such flag (rule 3 names `--json` only where it is used, and the core never uses it). A stopped,
   unknown or missing state is never displayed as running.
8. Tunnel tokens and credentials are never read, shown or logged by the desktop.
9. Restart is a stop followed by a start, and each is confirmed separately.
10. The controls stay off until the owner confirms this contract. The switch is the settings
    field `tunnel_control_enabled`. It defaulted to `false` until 2026-10-23, and it defaults to
    `true` from then, by owner decision (Amendment F).

## Owner confirmation

- [x] Read this contract and agree to it, with Amendments A to E.
- Confirmed by the owner in chat on: 2026-10-22
- Amendment F accepted by the owner in chat on: 2026-10-23

Once confirmed, the owner's sprint item 84d8fd28 is recorded done, and the start, stop and
restart controls may be built to this contract.

## Amendment (confirmed by the owner in chat on 2026-10-22)

A. Zotero at launch. The setting `zotero_autostart` (default false) turns this on. When the
   desktop launches with it on, it checks whether Zotero answers on 127.0.0.1:23119. If nothing
   answers, it starts Zotero once, as an argument list with no shell, from the zotero.exe the owner
   confirmed in Settings. The dialog looks for zotero.exe in the standard install folders only (the
   Program Files folders, and Programs\Zotero under %LOCALAPPDATA%). The exact path the dialog named is
   recorded in settings as `confirmed_zotero_exe`. With no confirmed path, the start is skipped and the
   status line says so. The start is logged. The desktop never restarts Zotero on its own.
B. Local server at launch. The setting `local_server_autostart` (default false) turns this on.
   When the desktop launches with it on, it runs the fixed `python -m meridian.local_runner start`
   command for scope `local`, under the same timeouts and log as rule 5 and rule 6. It uses the core
   Python and checkout the owner confirmed, recorded as `confirmed_core_python` and
   `confirmed_core_checkout`, and never the setup saved later. With neither confirmed, the start is
   skipped and the status line says so.
C. Enabling either setting in Settings shows the confirmation dialog from rule 2, naming the exact
   command and scope. Nothing starts until that dialog is confirmed. Confirming it records the exact
   values the dialog showed (the core Python and checkout, or the Zotero path). Saving a core checkout or
   Python path later turns both launch switches off, and the Settings status line says so. The owner
   must confirm them again against the new setup.
D. Tunnel start, stop and restart stay user actions only, and stay behind `tunnel_control_enabled`
   (default true from 2026-10-23, Amendment F), as rule 10 says.
E. Every start in A and B is logged to the same local file as rule 6, with the time, the scope or
   program, the outcome and the pid if reported. The log holds no secrets.

## Amendment F (accepted by the owner in chat on 2026-10-23)

F. Owner decision, 2026-10-23. `tunnel_control_enabled` now defaults to `true`, in place of the
   default `false` of rule 10. The Start, Stop and Restart buttons on the Tunnel tab are on while the
   switch is true and the Meridian core checkout and its Python are set. Each button still asks the
   owner first, every time, in the same dialog from rule 2, and Restart still asks before its start
   (rule 9). Cancel changes nothing. A saved `tunnel_control_enabled: false` turns the buttons off. No
   other part of the contract changes.

## Amendment G (requested by the owner in chat on 2026-10-31; each switch-on needs its own confirmation)

G. Hosted tunnel at launch. The setting `tunnel_autostart` (default false) turns this on. When the desktop
   launches with it on, the desktop starts the tunnel once, through the fixed tunnel start in rule 3 and
   under the same timeouts and log as rules 5, 6 and E, only when all of these hold: the desktop is in
   `hosted` mode, the core checkout and its Python were confirmed in Settings, the tunnel
   buttons are on (`tunnel_control_enabled`, Amendment F), and the runner reports the tunnel scope not
   running. Nothing else starts the tunnel by itself, and a launch that finds the tunnel running starts
   nothing.
   The tunnel gives claude.ai access to the working folder. So the switch shows the same confirmation
   dialog as rule 2, which names the exact tunnel command, the working folder, and says that claude.ai can
   reach that folder every time the desktop opens. Confirming it records the core Python and checkout
   the dialog showed, as `confirmed_core_python` and `confirmed_core_checkout`. Cancel leaves the switch
   off. Turning it off saves at once and asks nothing. Saving a core checkout or Python path later turns
   this switch off, as it does the two in Amendments A and B, and the owner must confirm it again.
   Every launch start in G is logged as rule E says.
