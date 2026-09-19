import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
import { loadSavedCookie, status, logout } from "./overleaf-login.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

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

// --- CLI entry-point guard: must actually run as a real subprocess -------
//
// Real, live bug found 2026-09-19 (Adam ran `node src/overleaf-login.js
// login` on his real Windows machine and got silent zero output): the
// bottom-of-file `if (import.meta.url === ...)` guard used to hand-build
// its comparison string, which never matched import.meta.url's REAL format
// on Windows (three slashes -- "file:///C:/...", the scheme's empty host
// plus the path -- vs. the guard's own two-slash "file://C:/..."). This
// meant `login()`/`status()`/`logout()` were NEVER invoked from the CLI on
// Windows, silently. Fixed via node:url's pathToFileURL(), the documented,
// cross-platform-correct ESM replacement for CommonJS's
// `require.main === module`.
//
// This class of bug is invisible to a normal `import { status } from
// "./overleaf-login.js"` test (as used everywhere else in this file) --
// import.meta.url in that case is the TEST FILE's own url, and
// process.argv[1] is the test runner's entry point, so the guard correctly
// stays false regardless of whether the comparison logic itself is broken.
// The only real way to exercise this exact bug is to actually spawn this
// file as its own process, exactly how a human running it from a terminal
// does -- which is what these tests do.

test("CLI: `node overleaf-login.js status` actually runs (the exact command that was silently broken)", () => {
  const scriptPath = join(__dirname, "overleaf-login.js");
  const output = execFileSync(process.execPath, [scriptPath, "status"], { encoding: "utf-8" });
  const parsed = JSON.parse(output);
  assert.equal(typeof parsed.loggedIn, "boolean", "status must actually run and print real JSON, not silently produce nothing");
});

test("CLI: an unrecognized subcommand actually runs and exits non-zero with a clear message", () => {
  const scriptPath = join(__dirname, "overleaf-login.js");
  assert.throws(
    () => execFileSync(process.execPath, [scriptPath, "not-a-real-command"], { encoding: "utf-8", stdio: "pipe" }),
    (err) => {
      assert.equal(err.status, 1);
      assert.match(err.stderr, /Unknown command/);
      return true;
    },
  );
});

// Real regression found 2026-09-19, caught immediately after fixing the
// Windows guard above, while verifying this module still imports cleanly
// as part of index.js (the package's new npm "main"): pathToFileURL(argv[1])
// THROWS if argv[1] is undefined -- a real regression versus the old,
// wrong-but-non-throwing `argv[1]?.replace(...)`. `node -e` is exactly a
// context where argv[1] is undefined; this test reproduces that directly
// rather than relying on index.js's own happy path to catch it.
test("importing this module in a context with no real argv[1] (e.g. node -e) never throws", () => {
  const scriptPath = join(__dirname, "overleaf-login.js");
  // import() needs a real URL, not a bare Windows path (a raw "C:\..."
  // path is misparsed as a URL with scheme "c:") -- pathToFileURLString
  // below is the same fix this exact test is regression-testing, applied
  // to the test's own dynamic import of the module under test.
  const scriptUrl = pathToFileURL(scriptPath).href;
  const output = execFileSync(
    process.execPath,
    ["-e", `import(${JSON.stringify(scriptUrl)}).then(() => console.log("ok")).catch((e) => { console.error(e); process.exit(1); })`],
    { encoding: "utf-8" },
  );
  assert.match(output, /ok/);
});
