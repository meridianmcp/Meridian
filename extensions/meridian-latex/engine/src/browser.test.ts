import { test } from "node:test";
import assert from "node:assert/strict";
import type { Options as ChromeLaunchOptions } from "chrome-launcher";
import { launchOverleafBrowser, type LaunchedChromeLike } from "./browser.js";
import { CHROME_PROFILE_DIR } from "./overleaf-login.js";

function fakeChromeNotInstalledError(): Error {
  const err = new Error("No Chrome installations found.");
  Object.defineProperty(err, "constructor", { value: { name: "ChromeNotInstalledError" } });
  return err;
}

test("launchOverleafBrowser: creates the config dir before launching (same ENOENT bug class as login.js)", async () => {
  let ensureCalled = false;
  const launchImpl = async (): Promise<LaunchedChromeLike> => ({ kill: async () => {} });
  await launchOverleafBrowser({
    ensureConfigDirImpl: () => {
      ensureCalled = true;
    },
    launchImpl,
  });
  assert.equal(ensureCalled, true);
});

test("launchOverleafBrowser: passes the extension load flags, reuses the login profile dir, and sets startingUrl", async () => {
  let seenOpts: ChromeLaunchOptions | undefined;
  const launchImpl = async (opts: ChromeLaunchOptions): Promise<LaunchedChromeLike> => {
    seenOpts = opts;
    return { kill: async () => {} };
  };
  const result = await launchOverleafBrowser({ ensureConfigDirImpl: () => {}, launchImpl });

  assert.equal(result.error, null);
  assert.ok(seenOpts);
  assert.equal(seenOpts.userDataDir, CHROME_PROFILE_DIR, "must reuse login.js's profile so a saved session carries over");
  assert.equal(seenOpts.startingUrl, "https://www.overleaf.com/project");
  const chromeFlags = seenOpts.chromeFlags ?? [];
  assert.ok(
    chromeFlags.some((f) => f.startsWith("--load-extension=")),
    "must load the extension",
  );
  assert.ok(
    chromeFlags.some((f) => f.startsWith("--disable-extensions-except=")),
    "must not load unrelated extensions from the profile alongside it",
  );
  assert.ok(!chromeFlags.includes("--headless"), "must NEVER be headless -- a human must be able to see and use this window");
});

test("launchOverleafBrowser: a custom projectUrl/extensionPath/profileDir override the defaults", async () => {
  let seenOpts: ChromeLaunchOptions | undefined;
  const launchImpl = async (opts: ChromeLaunchOptions): Promise<LaunchedChromeLike> => {
    seenOpts = opts;
    return { kill: async () => {} };
  };
  await launchOverleafBrowser({
    ensureConfigDirImpl: () => {},
    launchImpl,
    projectUrl: "https://www.overleaf.com/project/abc123",
    extensionPath: "/custom/extension/path",
    profileDir: "/custom/profile/dir",
  });

  assert.ok(seenOpts);
  assert.equal(seenOpts.startingUrl, "https://www.overleaf.com/project/abc123");
  assert.equal(seenOpts.userDataDir, "/custom/profile/dir");
  assert.ok((seenOpts.chromeFlags ?? []).some((f) => f === "--load-extension=/custom/extension/path"));
});

test("launchOverleafBrowser: Chrome not installed -> clear, actionable error, never throws", async () => {
  const launchImpl = async (): Promise<LaunchedChromeLike> => {
    throw fakeChromeNotInstalledError();
  };
  const result = await launchOverleafBrowser({ ensureConfigDirImpl: () => {}, launchImpl });

  assert.equal(result.chrome, null);
  assert.match(result.error ?? "", /Chrome is not installed/);
  assert.match(result.error ?? "", /CHROME_PATH/);
});

test("launchOverleafBrowser: some other launch failure -> generic clear error, never throws", async () => {
  const launchImpl = async (): Promise<LaunchedChromeLike> => {
    throw new Error("spawn EACCES");
  };
  const result = await launchOverleafBrowser({ ensureConfigDirImpl: () => {}, launchImpl });

  assert.equal(result.chrome, null);
  assert.match(result.error ?? "", /Failed to launch the Overleaf automation browser/);
  assert.match(result.error ?? "", /EACCES/);
});

test("launchOverleafBrowser: a failing ensureConfigDirImpl itself is surfaced as a clear error, not thrown", async () => {
  const launchImpl = async (): Promise<LaunchedChromeLike> => ({ kill: async () => {} });
  const result = await launchOverleafBrowser({
    ensureConfigDirImpl: () => {
      throw new Error("EACCES: permission denied, mkdir");
    },
    launchImpl,
  });

  assert.equal(result.chrome, null);
  assert.match(result.error ?? "", /Failed to launch the Overleaf automation browser/);
});
