// Unit tests for the reference-manager (Zotero) status row (sprint item f5e1ed49).
//
// 39c117b1 made `zotero` a first-class bundled tunnel slot, so GET /tunnel/status
// now reports `zotero_active` (+ an optional `slot_status.zotero` diagnostic).
// _renderZoteroStatusRow maps that payload to the same three-state status badge
// the other slot rows use. These tests exercise the status mapping directly.
//
// dashboard-plugins.ts is a *script* module (its symbols live on `window`, not
// ES exports — the same legacy pattern the other dashboard-*.ts files use), so
// we import it for its side effects and read the function off the global. A
// stand-in escapeHtml is installed first because the module's helpers call the
// bare global at render time.
import { describe, it, expect } from "vitest";

const _escapeHtml = (s: unknown) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string),
  );
(globalThis as any).escapeHtml = _escapeHtml;
(globalThis as any).window = (globalThis as any).window || globalThis;
(globalThis as any).window.escapeHtml = _escapeHtml;

// dashboard-plugins.ts is a legacy *script* module (symbols land on `window`,
// not ES exports). A side-effect import runs its top-level assignments so the
// function under test becomes reachable off the global.
import "./dashboard-plugins";

const _renderZoteroStatusRow: (status: any) => string =
  (globalThis as any)._renderZoteroStatusRow ||
  (globalThis as any).window._renderZoteroStatusRow;

describe("_renderZoteroStatusRow", () => {
  it("is exposed on the global after the module loads", () => {
    expect(typeof _renderZoteroStatusRow).toBe("function");
  });

  it("renders the reference-manager label and the /zotero slot tag", () => {
    const html = _renderZoteroStatusRow({ zotero_active: true });
    expect(html).toContain("Reference manager");
    expect(html).toContain("/zotero");
    expect(html).toContain("zotero-mcp");
  });

  it("maps zotero_active=true (no health issue) to the active state", () => {
    const html = _renderZoteroStatusRow({ zotero_active: true });
    expect(html).toContain('data-zotero-status="active"');
    expect(html).toContain(">active<");
    // Green success dot, mirroring the plugin rows' active badge.
    expect(html).toContain("var(--success");
  });

  it("maps zotero_active=false to installed_inactive (bundled but not connected)", () => {
    const html = _renderZoteroStatusRow({ zotero_active: false });
    expect(html).toContain('data-zotero-status="installed_inactive"');
    expect(html).toContain(">inactive<");
    // Amber inactive dot + the shared "start tunnel to activate" hint.
    expect(html).toContain("#f59e0b");
    expect(html).toContain("start tunnel to activate");
  });

  it("treats a connected-but-unhealthy slot as unhealthy, with the reason badge", () => {
    const html = _renderZoteroStatusRow({
      zotero_active: true,
      slot_status: {
        zotero: { reason: "preflight_failed", detail: "Zotero not running on 127.0.0.1:23119" },
      },
    });
    expect(html).toContain('data-zotero-status="unhealthy"');
    expect(html).toContain(">unhealthy<");
    // The actionable health warning surfaces the reason + escaped detail.
    expect(html).toContain('data-slot-warning="zotero"');
    expect(html).toContain("preflight failed");
    expect(html).toContain("Zotero not running on 127.0.0.1:23119");
  });

  it("ignores an unrelated slot's health entry (only reads slot_status.zotero)", () => {
    const html = _renderZoteroStatusRow({
      zotero_active: true,
      slot_status: { word: { reason: "access_denied" } },
    });
    // A health issue on `word` must not degrade the zotero row.
    expect(html).toContain('data-zotero-status="active"');
    expect(html).not.toContain("data-slot-warning");
  });

  it("defaults to not-detected when given an empty / missing payload", () => {
    expect(_renderZoteroStatusRow(undefined)).toContain('data-zotero-status="installed_inactive"');
    expect(_renderZoteroStatusRow({})).toContain('data-zotero-status="installed_inactive"');
  });
});
