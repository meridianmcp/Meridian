// Unit tests for dashboard-settings markup + helpers.
//
// 2ff2ff1f — the Handoff Format textarea (id="ws-handoff-template") must default
// to a tall height (rows="16") and stay vertically resizable.
// f2157803 — the executor-config context_threshold / max_turns controls are
// number inputs (exact value always visible + directly editable), not sliders.
// ca8c0d56 — the Claude Code (rc-watcher) and Codex CLI setup blurbs are terse
// single-liners leading with the copy-paste config.
// 00a1e56a — the settings CARD renderers extracted from loadSettingsTab still
// emit the same canonical DOM ids/structure they produced inline.
import { describe, it, expect, beforeAll, beforeEach, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  _execTurnsNumberInputHtml,
  claudeRcWatcherBlurb,
  codexSetupBlurb,
  _settingsAccountCardHtml,
  _settingsBrowserConnectorCardHtml,
  _settingsNotificationsCardHtml,
  _settingsNotificationPrefsHtml,
  formatTunnelConfigGenerationStatus,
  _renderTunnelConfigGenerationBanner,
} from "./dashboard-settings";

// Vitest runs with cwd = repo root, so resolve the source module from there.
const source = readFileSync(
  resolve(process.cwd(), "meridian/static/dashboard-settings.ts"),
  "utf8",
);

// Isolate the exact <textarea id="ws-handoff-template" ...> opening tag.
const textareaTag =
  source.match(/<textarea id="ws-handoff-template"[^>]*>/)?.[0] ?? "";

// Minimal stand-in for the ambient escapeHtml used at runtime.
const esc = (s: unknown) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string),
  );

const PID = "proj-123";

beforeAll(() => {
  const g = globalThis as any;
  g.escapeHtml = esc;
  (window as any).escapeHtml = esc;
  // Runtime globals the extracted card helpers reference bare (defined at
  // runtime by dashboard-utils / dashboard-core via window).
  g._PLAN_LABELS = { free: "Free", standard: "Standard", admin: "Admin", trial: "Trial" };
  g.displayNotifyTarget = (v: unknown) => String(v || "");
  g.STORAGE_KEY = (k: string) => `meridian.${k}`;
  g.state = { tenantEmail: "", projects: [{ id: PID, name: "My Project" }] };
  (window as any).state = g.state;
  g.isHostedMode = () => false;
});

afterEach(() => {
  const g = globalThis as any;
  g.state = { tenantEmail: "", projects: [{ id: PID, name: "My Project" }] };
  (window as any).state = g.state;
  g.isHostedMode = () => false;
  try {
    localStorage.clear();
  } catch {
    /* jsdom always has localStorage */
  }
});

function parse(html: string): HTMLInputElement {
  const el = document.createElement("div");
  el.innerHTML = html;
  const input = el.querySelector("input");
  if (!input) throw new Error("no input rendered");
  return input as HTMLInputElement;
}

// Strip inline tags so length/word checks measure the visible copy.
const visible = (html: string) => html.replace(/<[^>]+>/g, "");

describe("ws-handoff-template textarea", () => {
  it("exists in the settings source", () => {
    expect(textareaTag).not.toBe("");
  });

  it("defaults to a tall height (rows=16), not the old cramped rows=6", () => {
    expect(textareaTag).toContain('rows="16"');
    expect(textareaTag).not.toContain('rows="6"');
  });

  it("stays vertically resizable", () => {
    expect(textareaTag).toContain("resize:vertical");
  });
});

describe("_execTurnsNumberInputHtml", () => {
  it("renders a number input (not a range slider) so the value is directly editable", () => {
    const input = parse(_execTurnsNumberInputHtml("context_threshold", "p1", 60, 10, 200, 5));
    expect(input.getAttribute("type")).toBe("number");
    expect(input.getAttribute("type")).not.toBe("range");
  });

  it("preserves the exact setting key in the element id (state binding unchanged)", () => {
    const ctx = parse(_execTurnsNumberInputHtml("context_threshold", "proj-42", 60, 10, 200, 5));
    const mt = parse(_execTurnsNumberInputHtml("max_turns", "proj-42", 120, 40, 500, 20));
    expect(ctx.id).toBe("exec-context_threshold-proj-42");
    expect(mt.id).toBe("exec-max_turns-proj-42");
  });

  it("shows the exact current value in the input so it is always visible", () => {
    const input = parse(_execTurnsNumberInputHtml("context_threshold", "p1", 85, 10, 200, 5));
    expect(input.getAttribute("value")).toBe("85");
    expect(input.value).toBe("85");
  });

  it("carries sensible min/max/step bounds matching the clamp on save", () => {
    const ctx = parse(_execTurnsNumberInputHtml("context_threshold", "p1", 60, 10, 200, 5));
    expect(ctx.getAttribute("min")).toBe("10");
    expect(ctx.getAttribute("max")).toBe("200");
    expect(ctx.getAttribute("step")).toBe("5");

    const mt = parse(_execTurnsNumberInputHtml("max_turns", "p1", 120, 40, 500, 20));
    expect(mt.getAttribute("min")).toBe("40");
    expect(mt.getAttribute("max")).toBe("500");
    expect(mt.getAttribute("step")).toBe("20");
  });

  it("advertises a numeric mobile keyboard via inputmode", () => {
    const input = parse(_execTurnsNumberInputHtml("max_turns", "p1", 120, 40, 500, 20));
    expect(input.getAttribute("inputmode")).toBe("numeric");
  });

  it("HTML-escapes the rendered value to avoid attribute injection", () => {
    const html = _execTurnsNumberInputHtml("context_threshold", "p1", '"><img>', 10, 200, 5);
    expect(html).not.toContain('"><img>');
    expect(html).toContain("&quot;&gt;&lt;img&gt;");
  });

  it("is registered on window for the settings template to call", () => {
    expect(typeof (window as any)._execTurnsNumberInputHtml).toBe("function");
  });
});

describe("claudeRcWatcherBlurb", () => {
  const html = claudeRcWatcherBlurb();

  it("still explains the claude --rc / headless hook gap", () => {
    expect(html).toContain("claude --rc");
    expect(html.toLowerCase()).toContain("hook");
  });

  it("is a single terse line, not a multi-sentence paragraph", () => {
    expect(html).not.toContain("\n");
    expect(html).not.toContain("<p");
    expect(visible(html).length).toBeLessThan(180);
    expect((visible(html).match(/\.(\s|$)/g) || []).length).toBeLessThanOrEqual(1);
  });
});

describe("codexSetupBlurb", () => {
  const url = "https://usemeridian.us/mcp";
  const html = codexSetupBlurb(url, esc);

  it("keeps the essential config-file + one-command instruction intact", () => {
    expect(html).toContain("~/.codex/config.toml");
    expect(html).toContain("codex mcp add meridian");
    expect(html).toContain(url);
  });

  it("routes the url through the provided escaper (no unescaped injection)", () => {
    const out = codexSetupBlurb('x"><script>', esc);
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });

  it("is a single terse line, not a multi-sentence paragraph", () => {
    expect(html).not.toContain("\n");
    expect(visible(html).length).toBeLessThan(120);
    expect((visible(html).match(/\.(\s|$)/g) || []).length).toBeLessThanOrEqual(1);
  });
});

describe("_settingsAccountCardHtml", () => {
  it("returns empty string when there is no tenant email (self-host)", () => {
    (globalThis as any).state.tenantEmail = "";
    expect(_settingsAccountCardHtml(PID)).toBe("");
  });

  it("renders the account card with the canonical ids when a tenant is present", () => {
    const g = globalThis as any;
    g.state.tenantEmail = "user@example.com";
    g.state.tenantPlan = "free";
    g.state.tenantHasStripe = false;
    const html = _settingsAccountCardHtml(PID);
    expect(html).toContain(`id="settings-account-card-${PID}"`);
    expect(html).toContain(`id="account-delete-${PID}"`);
    expect(html).toContain("user@example.com");
    expect(html).toContain("Sign out");
    expect(html).toContain('href="/auth/logout"');
  });

  it("shows the billing portal button for Stripe customers", () => {
    const g = globalThis as any;
    g.state.tenantEmail = "paid@example.com";
    g.state.tenantPlan = "standard";
    g.state.tenantHasStripe = true;
    const html = _settingsAccountCardHtml(PID);
    expect(html).toContain(`id="billing-portal-btn-${PID}"`);
    expect(html).toContain("Manage billing");
  });
});

describe("_settingsBrowserConnectorCardHtml", () => {
  it("always renders the browser connector card", () => {
    const html = _settingsBrowserConnectorCardHtml(PID);
    expect(html).toContain("Browser connector");
    expect(html).toContain("docs.usemeridian.us/browser-connector");
    expect(html).toContain("Setup guide");
  });

  it("omits the account-switch note in self-host mode and includes it when hosted", () => {
    (globalThis as any).isHostedMode = () => false;
    const selfHost = _settingsBrowserConnectorCardHtml(PID);
    expect(selfHost).not.toContain("Switch Meridian account");

    (globalThis as any).isHostedMode = () => true;
    const hosted = _settingsBrowserConnectorCardHtml(PID);
    expect(hosted).toContain("Switch Meridian account");
  });
});

describe("_settingsNotificationsCardHtml", () => {
  it("renders the notifications card with all canonical input ids", () => {
    const ntfyResult = {
      status: "fulfilled",
      value: { notify_url: "https://ntfy.sh/my-topic", notify_email: "a@b.com" },
    };
    const html = _settingsNotificationsCardHtml(PID, ntfyResult);
    expect(html).toContain(`id="settings-notifications-card-${PID}"`);
    expect(html).toContain(`id="ntfy-url-${PID}"`);
    expect(html).toContain(`id="ntfy-save-${PID}"`);
    expect(html).toContain(`id="ntfy-test-${PID}"`);
    expect(html).toContain(`id="notify-email-${PID}"`);
    expect(html).toContain(`id="notify-email-save-${PID}"`);
    expect(html).toContain(`id="ntfy-warn-ack-${PID}"`);
  });

  it("disables the ntfy input until the security warning is acknowledged", () => {
    const ntfyResult = { status: "rejected", reason: new Error("no ntfy") };
    const notAck = _settingsNotificationsCardHtml(PID, ntfyResult);
    expect(notAck).toContain(`id="ntfy-save-${PID}" disabled`);

    localStorage.setItem("meridian.ntfy.warn.dismissed", "1");
    const ack = _settingsNotificationsCardHtml(PID, ntfyResult);
    expect(ack).toContain(`id="ntfy-save-${PID}" `);
    expect(ack).not.toContain(`id="ntfy-save-${PID}" disabled`);
  });
});

describe("_settingsNotificationPrefsHtml", () => {
  const PREFS = [
    { key: "hitl", label: "HITL notice" },
    { key: "sprint", label: "Sprint done" },
  ];

  it("renders a checkbox per pref plus the save-status line", () => {
    const prefs = { hitl: true, sprint: false };
    const html = _settingsNotificationPrefsHtml(PID, prefs, PREFS, { base_url: "x" });
    expect(html).toContain('data-pref="hitl"');
    expect(html).toContain('data-pref="sprint"');
    expect(html).toContain('data-pref="hitl" checked');
    expect(html).not.toContain('data-pref="sprint" checked');
    expect(html).toContain(`id="settings-save-status-${PID}"`);
  });

  it("shows the hosted-only notice when prefs are null and mcp is unavailable", () => {
    const html = _settingsNotificationPrefsHtml(PID, null, PREFS, null);
    expect(html).toContain("only available in hosted mode");
    expect(html).not.toContain('data-pref=');
  });

  it("emits nothing when prefs are null but mcp is available (self-host)", () => {
    const html = _settingsNotificationPrefsHtml(PID, null, PREFS, { base_url: "x" });
    expect(html).toBe("");
  });
});

// 02dbd8b4 — runtime configuration generation status (tunnel/executor settings).
describe("formatTunnelConfigGenerationStatus", () => {
  it("reports 'Unknown' when no generation info is present", () => {
    expect(formatTunnelConfigGenerationStatus(undefined).tone).toBe("unknown");
    expect(formatTunnelConfigGenerationStatus(null).tone).toBe("unknown");
    expect(formatTunnelConfigGenerationStatus({}).tone).toBe("unknown");
  });

  it("reports 'warn' + names the generation when restart_required is true", () => {
    const r = formatTunnelConfigGenerationStatus({
      generation: 3, config_hash: "abcdef0123456789", restart_required: true,
    });
    expect(r.tone).toBe("warn");
    expect(r.label).toContain("Restart required");
    expect(r.label).toContain("3");
    expect(r.detail).toContain("meridian --tunnel");
  });

  it("reports 'ok' + names the generation when restart_required is false", () => {
    const r = formatTunnelConfigGenerationStatus({
      generation: 5, config_hash: "abcdef0123456789", restart_required: false,
    });
    expect(r.tone).toBe("ok");
    expect(r.label).toContain("Applied");
    expect(r.label).toContain("5");
  });

  it("truncates a long config hash for display but not identity", () => {
    const full = "abcdef0123456789fedcba9876543210";
    const r = formatTunnelConfigGenerationStatus({ generation: 1, config_hash: full, restart_required: false });
    expect(r.detail).toContain(full.slice(0, 8));
    expect(r.detail).not.toContain(full);
  });
});

describe("_renderTunnelConfigGenerationBanner", () => {
  const HOST_ID = `settings-body-${PID}`;

  beforeEach(() => {
    document.body.innerHTML = `<div id="${HOST_ID}"><div id="tunnel-plugins-section-${PID}"></div></div>`;
  });

  it("inserts a warning banner before the tunnel plugins section when restart_required", async () => {
    (window as any).api = async (_path: string) => ({
      config_generation: { default: { generation: 2, config_hash: "aaaaaaaa1111", restart_required: true } },
    });
    await _renderTunnelConfigGenerationBanner(PID);
    const banner = document.getElementById(`tunnel-config-gen-banner-${PID}`);
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain("Restart required");
    // Banner must precede the tunnel plugins section, not be nested inside it
    // (loadTunnelPluginsSection re-renders that section's innerHTML async and
    // would otherwise wipe anything injected as a child of it).
    const section = document.getElementById(`tunnel-plugins-section-${PID}`);
    expect(banner!.compareDocumentPosition(section!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("renders no banner when the generation is applied (no restart needed)", async () => {
    (window as any).api = async (_path: string) => ({
      config_generation: { default: { generation: 4, config_hash: "bbbbbbbb2222", restart_required: false } },
    });
    await _renderTunnelConfigGenerationBanner(PID);
    expect(document.getElementById(`tunnel-config-gen-banner-${PID}`)).toBeNull();
  });

  it("renders no banner and does not throw when the status fetch fails", async () => {
    (window as any).api = async (_path: string) => { throw new Error("network error"); };
    await expect(_renderTunnelConfigGenerationBanner(PID)).resolves.toBeUndefined();
    expect(document.getElementById(`tunnel-config-gen-banner-${PID}`)).toBeNull();
  });

  it("is idempotent: re-rendering replaces the previous banner instead of duplicating it", async () => {
    (window as any).api = async (_path: string) => ({
      config_generation: { default: { generation: 2, config_hash: "aaaaaaaa1111", restart_required: true } },
    });
    await _renderTunnelConfigGenerationBanner(PID);
    await _renderTunnelConfigGenerationBanner(PID);
    const banners = document.querySelectorAll(`#${HOST_ID} [id="tunnel-config-gen-banner-${PID}"]`);
    expect(banners.length).toBe(1);
  });
});

// ff9d2963 — SECURITY-LAUNCH: the generated Claude/Codex hook snippets used
// to interpolate the user's LIVE bearer token straight into a curl/PowerShell
// command string (`buildHookCurlCommand`/`buildHookPowerShellCommand`, called
// with the raw token from `buildClaudeHookSnippet`/`buildCodexHookSnippet`).
// That command is (a) persisted verbatim into ~/.claude/settings.json — a
// file people routinely paste into bug reports / dotfile-sync repos — and
// (b) re-exposed as a literal argv substring of the hook subprocess on every
// single SessionStart/Stop firing. This is the exact anti-pattern ba31dedf
// already fixed for scripts/meridian_connect.py's generated hooks (curl -K
// <file> reading the header from a local, restrictive-permission config file
// instead of argv/settings.json) — this UI never got the equivalent fix.
//
// `buildHookCurlHeaders`/`buildHookCurlCommand`/`buildHookPowerShellCommand`/
// `buildClaudeHookSnippet`/`buildCodexHookSnippet` are not exported (they are
// closures over `projectId`/`hooksBaseUrl` defined inline in the big settings
// render function), so — matching this file's own established convention for
// asserting on non-exported inline markup (see the `ws-handoff-template`
// textarea check above) — these are source-level regression tests: they pin
// the absence of the literal-token anti-pattern and the presence of its
// env-var-reference replacement.
describe("ff9d2963 — hook snippets never interpolate the live bearer token", () => {
  it("never builds a literal 'Bearer <token>' header from an interpolated token", () => {
    // The old anti-pattern, in both the curl and PowerShell builders:
    //   `-H 'Authorization: Bearer ${token}'`
    //   ` -Headers @{ Authorization = 'Bearer ${token}' }`
    expect(source).not.toMatch(/Authorization:\s*Bearer\s*\$\{token\}/);
    expect(source).not.toMatch(/Authorization\s*=\s*'Bearer\s*\$\{token\}'/);
  });

  it("no longer derives an activeToken/hostedPlaceholderToken to embed in a snippet", () => {
    expect(source).not.toContain("activeToken");
    expect(source).not.toContain("hostedPlaceholderToken");
  });

  it("hook-command builders take a needsAuth boolean, not the raw token", () => {
    expect(source).toMatch(/function buildHookCurlHeaders\(needsAuth: any\)/);
    expect(source).toMatch(/function buildHookCurlCommand\(path: any, needsAuth: any\)/);
    expect(source).toMatch(/function buildHookPowerShellCommand\(path: any, needsAuth: any\)/);
    expect(source).toMatch(/function buildClaudeHookSnippet\(platform: any, needsAuth: any\)/);
    expect(source).toMatch(/function buildCodexHookSnippet\(platform: any, needsAuth: any\)/);
  });

  it("generated curl/PowerShell auth headers reference an env var instead", () => {
    expect(source).toContain("$MERIDIAN_HOOKS_TOKEN");
    expect(source).toContain("$env:MERIDIAN_HOOKS_TOKEN");
  });

  it("the hosted-tier .mcp.json env-block injection (the sanctioned pattern) is untouched", () => {
    // Confirms the fix didn't overreach into the OTHER token-bearing snippet
    // on this page, which is fine as-is: an MCP client's `env` block is read
    // as real environment variables by the child process at startup, not a
    // literal command-line/argv substring re-exposed on every hook firing.
    expect(source).toContain("env:{BEARER_TOKEN:tok.token}");
  });
});
