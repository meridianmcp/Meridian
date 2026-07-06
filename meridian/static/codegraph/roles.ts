// codegraph/roles.ts — ed5512b6
//
// Deterministic, TOTAL role -> color map. "color by role" is a core requirement
// of the visualizer, and it must be stable: the same role always maps to the
// same color, and any unknown role maps to a single documented default. Pure —
// no DOM, no globals. Colors are plain hex so this is portable to any host.

import type { Role } from "./model";

/** The canonical, deterministic color for each known role. Values are chosen to
 * be visually distinct on a dark surface but are otherwise arbitrary + stable. */
export const ROLE_COLORS: Record<Role, string> = {
  folder: "#64748b", // slate — containers
  package: "#94a3b8", // lighter slate — top-level containers
  module: "#38bdf8", // sky
  file: "#7dd3fc", // light sky
  class: "#a78bfa", // violet
  interface: "#c084fc", // lighter violet
  function: "#34d399", // emerald
  method: "#4ade80", // green
  route: "#f59e0b", // amber — entry points
  variable: "#fbbf24", // yellow
  unknown: "#94a3b8", // neutral fallback (documented default)
};

/** The single fallback color for any role not present in ROLE_COLORS. Exported
 * so callers/tests can assert the default explicitly. */
export const DEFAULT_ROLE_COLOR = ROLE_COLORS.unknown;

/**
 * Return the deterministic color for a role. TOTAL: every input yields a color;
 * an unrecognized role yields DEFAULT_ROLE_COLOR. Never throws. Pure.
 */
export function colorForRole(role: unknown): string {
  if (typeof role === "string" && role in ROLE_COLORS) {
    return ROLE_COLORS[role as Role];
  }
  return DEFAULT_ROLE_COLOR;
}
