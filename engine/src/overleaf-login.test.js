import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadSavedCookie, status, logout } from "./overleaf-login.js";

// loadSavedCookie()/status()/logout() all take an optional cookieFile path
// (defaulting to the real ~/.meridian-latex/cookie.json) precisely so this
// suite can exercise the real read/parse/delete logic against a throwaway
// temp file, never the real one. (login() itself -- the one function that
// spawns a real, credential-entering browser window -- is explicitly NOT
// unit-tested here or anywhere: it must only ever be run interactively by
// a human. See overleaf-login.js's own header comment.)

function withTempDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-login-test-"));
  try {
    return fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("loadSavedCookie(): a well-formed saved file round-trips", () => {
  withTempDir((dir) => {
    const cookieFile = join(dir, "cookie.json");
    writeFileSync(
      cookieFile,
      JSON.stringify({ baseUrl: "https://www.overleaf.com", cookie: "sharelatex.sid=abc123", savedAt: "2026-09-18T00:00:00.000Z" }),
    );
    assert.equal(loadSavedCookie(cookieFile), "sharelatex.sid=abc123");
  });
});

test("loadSavedCookie(): a missing file returns null, never throws", () => {
  withTempDir((dir) => {
    assert.equal(loadSavedCookie(join(dir, "does-not-exist.json")), null);
  });
});

test("loadSavedCookie(): a corrupted (non-JSON) file returns null, never throws", () => {
  withTempDir((dir) => {
    const cookieFile = join(dir, "cookie.json");
    writeFileSync(cookieFile, "{ not valid json");
    assert.equal(loadSavedCookie(cookieFile), null);
  });
});

test("status(): reports loggedIn true with baseUrl/savedAt for a real saved file", () => {
  withTempDir((dir) => {
    const cookieFile = join(dir, "cookie.json");
    writeFileSync(
      cookieFile,
      JSON.stringify({ baseUrl: "https://www.overleaf.com", cookie: "x", savedAt: "2026-09-18T00:00:00.000Z" }),
    );
    const s = status(cookieFile);
    assert.equal(s.loggedIn, true);
    assert.equal(s.baseUrl, "https://www.overleaf.com");
    assert.equal(s.savedAt, "2026-09-18T00:00:00.000Z");
  });
});

test("status(): reports loggedIn false when no file exists", () => {
  withTempDir((dir) => {
    assert.deepEqual(status(join(dir, "does-not-exist.json")), { loggedIn: false });
  });
});

test("logout(): removes the saved cookie file", () => {
  withTempDir((dir) => {
    const cookieFile = join(dir, "cookie.json");
    writeFileSync(cookieFile, JSON.stringify({ baseUrl: "x", cookie: "y" }));
    assert.equal(existsSync(cookieFile), true);
    logout(cookieFile);
    assert.equal(existsSync(cookieFile), false);
  });
});

test("logout(): a no-op (never throws) when there's nothing to remove", () => {
  withTempDir((dir) => {
    assert.doesNotThrow(() => logout(join(dir, "does-not-exist.json")));
  });
});
