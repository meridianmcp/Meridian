import { test } from "node:test";
import assert from "node:assert/strict";
import { fetchAllTags, findKeyTag, lookupCitationKey, launchZotero, type FetchResponseLike } from "./zotero.js";

function jsonResponse(body: unknown, ok = true, status = 200): FetchResponseLike {
  return { ok, status, json: async () => body };
}

// --- findKeyTag (pure, no network) -----------------------------------------

test("findKeyTag: matches a tag ending in exactly :key:<citekey>, any prefix", () => {
  const tags = ["P1", "P1:key:margulies2005454", "P1:needs-evidence-card"];
  assert.equal(findKeyTag(tags, "margulies2005454"), "P1:key:margulies2005454");
});

test("findKeyTag: a different prefix still matches -- the prefix is not hardcoded", () => {
  const tags = ["ProjectX:key:smith2020"];
  assert.equal(findKeyTag(tags, "smith2020"), "ProjectX:key:smith2020");
});

test("findKeyTag: a truncated/misspelled key must NOT match (exact suffix, not substring)", () => {
  const tags = ["P1:key:smith2020"];
  assert.equal(findKeyTag(tags, "smith20"), null);
  assert.equal(findKeyTag(tags, "smith2020extra"), null);
});

test("findKeyTag: returns null for an empty tag list or empty key", () => {
  assert.equal(findKeyTag([], "smith2020"), null);
  assert.equal(findKeyTag(["P1:key:smith2020"], ""), null);
  assert.equal(findKeyTag(["P1:key:smith2020"], null), null);
});

// --- fetchAllTags (paginated) -----------------------------------------------

test("fetchAllTags: pages through multiple pages until a short page ends it", async () => {
  const calls: string[] = [];
  const fetchImpl = async (url: string): Promise<FetchResponseLike> => {
    calls.push(url);
    if (url.includes("start=0")) {
      return jsonResponse(Array.from({ length: 100 }, (_, i) => ({ tag: `tag${i}` })));
    }
    if (url.includes("start=100")) {
      return jsonResponse([{ tag: "tag100" }, { tag: "tag101" }]);
    }
    throw new Error(`unexpected page requested: ${url}`);
  };
  const tags = await fetchAllTags({ fetchImpl });
  assert.equal(tags.length, 102);
  assert.equal(tags[0], "tag0");
  assert.equal(tags[101], "tag101");
  assert.equal(calls.length, 2, "must stop after the short (< page size) page, not request a third");
});

test("fetchAllTags: a single short page (the common small-library case) makes exactly one request", async () => {
  const fetchImpl = async (): Promise<FetchResponseLike> => jsonResponse([{ tag: "a" }, { tag: "b" }]);
  const tags = await fetchAllTags({ fetchImpl });
  assert.deepEqual(tags, ["a", "b"]);
});

test("fetchAllTags: an empty library returns []", async () => {
  const fetchImpl = async (): Promise<FetchResponseLike> => jsonResponse([]);
  assert.deepEqual(await fetchAllTags({ fetchImpl }), []);
});

test("fetchAllTags: a non-ok response throws (caller decides how to handle it)", async () => {
  const fetchImpl = async (): Promise<FetchResponseLike> => jsonResponse(null, false, 500);
  await assert.rejects(() => fetchAllTags({ fetchImpl }));
});

// --- lookupCitationKey (the real end-to-end function) -----------------------

test("lookupCitationKey: a key with a matching :key: tag resolves true, with the item's title", () => {
  const fetchImpl = async (url: string): Promise<FetchResponseLike> => {
    if (url.includes("/tags")) return jsonResponse([{ tag: "P1:key:margulies2005454" }]);
    if (url.includes("/items")) return jsonResponse([{ data: { title: "Genome sequencing..." } }]);
    throw new Error(`unexpected url: ${url}`);
  };
  return lookupCitationKey("margulies2005454", { fetchImpl }).then((result) => {
    assert.deepEqual(result, { resolved: true, tag: "P1:key:margulies2005454", title: "Genome sequencing..." });
  });
});

test("lookupCitationKey: a key with no matching tag resolves false", async () => {
  const fetchImpl = async (url: string): Promise<FetchResponseLike> => {
    if (url.includes("/tags")) return jsonResponse([{ tag: "P1:key:someoneelse2020" }]);
    throw new Error(`unexpected url: ${url}`);
  };
  const result = await lookupCitationKey("jimenez2023swebench", { fetchImpl });
  assert.deepEqual(result, { resolved: false });
});

test("lookupCitationKey: Zotero unreachable resolves null with a human-readable reason, never throws", async () => {
  const fetchImpl = async (): Promise<FetchResponseLike> => {
    throw new Error("ECONNREFUSED");
  };
  const result = await lookupCitationKey("smith2020", { fetchImpl });
  assert.equal(result.resolved, null);
  assert.match((result as { reason: string }).reason, /Zotero unreachable/);
  assert.match((result as { reason: string }).reason, /ECONNREFUSED/);
});

test("lookupCitationKey: an empty/missing citation key resolves false without any network call", async () => {
  let called = false;
  const fetchImpl = async (): Promise<FetchResponseLike> => {
    called = true;
    return jsonResponse([]);
  };
  const result = await lookupCitationKey("", { fetchImpl });
  assert.deepEqual(result, { resolved: false });
  assert.equal(called, false);
});

test("lookupCitationKey: tag matched, but fetching the item's title fails -- still resolved:true, title:null (the tag match IS the answer)", async () => {
  const fetchImpl = async (url: string): Promise<FetchResponseLike> => {
    if (url.includes("/tags")) return jsonResponse([{ tag: "P1:key:margulies2005454" }]);
    if (url.includes("/items")) return jsonResponse(null, false, 500);
    throw new Error(`unexpected url: ${url}`);
  };
  const result = await lookupCitationKey("margulies2005454", { fetchImpl });
  assert.deepEqual(result, { resolved: true, tag: "P1:key:margulies2005454", title: null });
});

test("lookupCitationKey: tag matched, but the items lookup throws -- still resolved:true, title:null", async () => {
  const fetchImpl = async (url: string): Promise<FetchResponseLike> => {
    if (url.includes("/tags")) return jsonResponse([{ tag: "P1:key:margulies2005454" }]);
    if (url.includes("/items")) throw new Error("network blip");
    throw new Error(`unexpected url: ${url}`);
  };
  const result = await lookupCitationKey("margulies2005454", { fetchImpl });
  assert.deepEqual(result, { resolved: true, tag: "P1:key:margulies2005454", title: null });
});

// --- launchZotero (pure command dispatch, no real process spawned) --------

test("launchZotero: dispatches the platform-appropriate zotero:// open command on win32", async () => {
  const calls: string[] = [];
  const execImpl = async (command: string): Promise<boolean> => {
    calls.push(command);
    return true;
  };
  const ok = await launchZotero({ execImpl, platform: "win32" });
  assert.equal(ok, true);
  assert.equal(calls.length, 1);
  assert.match(calls[0], /start.*"zotero:\/\/"/);
});

test("launchZotero: dispatches `open` on darwin", async () => {
  const calls: string[] = [];
  const execImpl = async (command: string): Promise<boolean> => {
    calls.push(command);
    return true;
  };
  await launchZotero({ execImpl, platform: "darwin" });
  assert.match(calls[0], /^open "zotero:\/\/"$/);
});

test("launchZotero: dispatches `xdg-open` on linux", async () => {
  const calls: string[] = [];
  const execImpl = async (command: string): Promise<boolean> => {
    calls.push(command);
    return true;
  };
  await launchZotero({ execImpl, platform: "linux" });
  assert.match(calls[0], /^xdg-open "zotero:\/\/"$/);
});

test("launchZotero: a failing execImpl resolves false, never throws", async () => {
  const execImpl = async (): Promise<boolean> => {
    throw new Error("no handler registered for zotero://");
  };
  const ok = await launchZotero({ execImpl, platform: "linux" });
  assert.equal(ok, false);
});

// --- lookupCitationKey auto-start (Adam's ask, 2026-09-24: "auto startup
// zotero if it's off") ------------------------------------------------------

test("lookupCitationKey auto-start: Zotero not running -> launched -> comes up on the first poll -> lookup succeeds", async () => {
  let attempt = 0;
  const fetchImpl = async (url: string): Promise<FetchResponseLike> => {
    if (url.includes("/tags")) {
      attempt += 1;
      if (attempt === 1) throw new Error("ECONNREFUSED"); // first call: Zotero not up yet
      return jsonResponse([{ tag: "P1:key:margulies2005454" }]); // second call (after launch+poll): up
    }
    if (url.includes("/items")) return jsonResponse([{ data: { title: "Genome sequencing..." } }]);
    throw new Error(`unexpected url: ${url}`);
  };
  const launchCalls: string[] = [];
  const execImpl = async (command: string): Promise<boolean> => {
    launchCalls.push(command);
    return true;
  };
  const sleeps: number[] = [];
  const sleepImpl = async (ms: number): Promise<void> => {
    sleeps.push(ms);
  }; // no real waiting in tests

  const result = await lookupCitationKey("margulies2005454", {
    fetchImpl,
    execImpl,
    sleepImpl,
    autoStart: true,
    platform: "linux",
    autoStartMaxWaitMs: 5000,
    autoStartPollIntervalMs: 1000,
  });

  assert.deepEqual(result, { resolved: true, tag: "P1:key:margulies2005454", title: "Genome sequencing..." });
  assert.equal(launchCalls.length, 1, "must attempt to launch Zotero exactly once");
  assert.equal(sleeps.length, 1, "must poll once before the retry that succeeds");
});

test("lookupCitationKey auto-start: launch command itself fails -> clear reason, no retry loop entered", async () => {
  const fetchImpl = async (): Promise<FetchResponseLike> => {
    throw new Error("ECONNREFUSED");
  };
  const execImpl = async (): Promise<boolean> => {
    throw new Error("xdg-open: command not found");
  };
  let sleepCalled = false;
  const sleepImpl = async (): Promise<void> => {
    sleepCalled = true;
  };

  const result = await lookupCitationKey("smith2020", { fetchImpl, execImpl, sleepImpl, autoStart: true, platform: "linux" });

  assert.equal(result.resolved, null);
  assert.match((result as { reason: string }).reason, /could not be auto-started/);
  assert.equal(sleepCalled, false, "must not poll at all if the launch attempt itself failed");
});

test("lookupCitationKey auto-start: launched but API never comes up within the wait window -> clear reason", async () => {
  const fetchImpl = async (): Promise<FetchResponseLike> => {
    throw new Error("ECONNREFUSED"); // never recovers, every call fails
  };
  const execImpl = async (): Promise<boolean> => true;
  const sleeps: number[] = [];
  const sleepImpl = async (ms: number): Promise<void> => {
    sleeps.push(ms);
  };

  const result = await lookupCitationKey("smith2020", {
    fetchImpl,
    execImpl,
    sleepImpl,
    autoStart: true,
    platform: "linux",
    autoStartMaxWaitMs: 3000,
    autoStartPollIntervalMs: 1000,
  });

  assert.equal(result.resolved, null);
  assert.match((result as { reason: string }).reason, /auto-started but its local API never came up/);
  assert.equal(sleeps.length, 3, "must poll exactly ceil(maxWait/interval) times, not loop forever");
});

test("lookupCitationKey auto-start: disabled via autoStart:false preserves the old fail-fast behavior", async () => {
  const fetchImpl = async (): Promise<FetchResponseLike> => {
    throw new Error("ECONNREFUSED");
  };
  let execCalled = false;
  const execImpl = async (): Promise<boolean> => {
    execCalled = true;
    return true;
  };

  const result = await lookupCitationKey("smith2020", { fetchImpl, execImpl, autoStart: false });

  assert.equal(result.resolved, null);
  assert.match((result as { reason: string }).reason, /^Zotero unreachable:/);
  assert.equal(execCalled, false, "autoStart:false must never attempt to launch anything");
});

test("lookupCitationKey auto-start: Zotero IS running but erroring (non-ok response) -> no launch attempt, unchanged reason", async () => {
  const fetchImpl = async (): Promise<FetchResponseLike> => jsonResponse(null, false, 500);
  let execCalled = false;
  const execImpl = async (): Promise<boolean> => {
    execCalled = true;
    return true;
  };

  const result = await lookupCitationKey("smith2020", { fetchImpl, execImpl });

  assert.equal(result.resolved, null);
  assert.match((result as { reason: string }).reason, /Zotero local API returned 500/);
  assert.equal(execCalled, false, "a reachable-but-erroring Zotero must never trigger an auto-launch attempt");
});
