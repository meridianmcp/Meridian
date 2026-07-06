// codegraph/roles.test.ts — ed5512b6
// The role->color map must be deterministic + total.
import { describe, expect, it } from "vitest";
import { ROLE_COLORS, DEFAULT_ROLE_COLOR, colorForRole } from "./roles";
import type { Role } from "./model";

const ALL_ROLES: Role[] = [
  "folder", "package", "module", "file", "class", "interface",
  "function", "method", "route", "variable", "unknown",
];

describe("role->color map", () => {
  it("is total: every known role has a color", () => {
    for (const role of ALL_ROLES) {
      expect(colorForRole(role)).toBe(ROLE_COLORS[role]);
      expect(typeof colorForRole(role)).toBe("string");
      expect(colorForRole(role)).toMatch(/^#[0-9a-fA-F]{6}$/);
    }
  });

  it("is deterministic: same role → same color across calls", () => {
    for (const role of ALL_ROLES) {
      expect(colorForRole(role)).toBe(colorForRole(role));
    }
  });

  it("unknown / non-string roles → DEFAULT_ROLE_COLOR", () => {
    expect(colorForRole("nope")).toBe(DEFAULT_ROLE_COLOR);
    expect(colorForRole("")).toBe(DEFAULT_ROLE_COLOR);
    expect(colorForRole(undefined)).toBe(DEFAULT_ROLE_COLOR);
    expect(colorForRole(null)).toBe(DEFAULT_ROLE_COLOR);
    expect(colorForRole(123)).toBe(DEFAULT_ROLE_COLOR);
  });

  it("assigns a distinct color to at least the primary structural roles", () => {
    const structural: Role[] = ["folder", "file", "class", "function", "route"];
    const colors = new Set(structural.map((r) => ROLE_COLORS[r]));
    expect(colors.size).toBe(structural.length);
  });
});
