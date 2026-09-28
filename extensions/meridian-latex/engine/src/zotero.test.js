import { test } from "node:test";
import assert from "node:assert/strict";
import { fetchAllTags, findKeyTag, lookupCitationKey } from "./zotero.js";

function jsonResponse(body, ok = true, status = 200) {
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
  const calls = [];
  const fetchImpl = async (url) => {
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
  const fetchImpl = async () => jsonResponse([{ tag: "a" }, { tag: "b" }]);
  const tags = await fetchAllTags({ fetchImpl });
  assert.deepEqual(tags, ["a", "b"]);
});

test("fetchAllTags: an empty library returns []", async () => {
  const fetchImpl = async () => jsonResponse([]);
  assert.deepEqual(await fetchAllTags({ fetchImpl }), []);
});

test("fetchAllTags: a non-ok response throws (caller decides how to handle it)", async () => {
  const fetchImpl = async () => jsonResponse(null, false, 500);
  await assert.rejects(() => fetchAllTags({ fetchImpl }));
});

// --- lookupCitationKey (the real end-to-end function) -----------------------

test("lookupCitationKey: a key with a matching :key: tag resolves true, with the item's title", () => {
  const fetchImpl = async (url) => {
    if (url.includes("/tags")) return jsonResponse([{ tag: "P1:key:margulies2005454" }]);
    if (url.includes("/items")) return jsonResponse([{ data: { title: "Genome sequencing..." } }]);
    throw new Error(`unexpected url: ${url}`);
  };
  return lookupCitationKey("margulies2005454", { fetchImpl }).then((result) => {
    assert.deepEqual(result, { resolved: true, tag: "P1:key:margulies2005454", title: "Genome sequencing..." });
  });
});

test("lookupCitationKey: a key with no matching tag resolves false", async () => {
  const fetchImpl = async (url) => {
    if (url.includes("/tags")) return jsonResponse([{ tag: "P1:key:someoneelse2020" }]);
    throw new Error(`unexpected url: ${url}`);
  };
  const result = await lookupCitationKey("jimenez2023swebench", { fetchImpl });
  assert.deepEqual(result, { resolved: false });
});

test("lookupCitationKey: Zotero unreachable resolves null with a human-readable reason, never throws", async () => {
  const fetchImpl = async () => {
    throw new Error("ECONNREFUSED");
  };
  const result = await lookupCitationKey("smith2020", { fetchImpl });
  assert.equal(result.resolved, null);
  assert.match(result.reason, /Zotero unreachable/);
  assert.match(result.reason, /ECONNREFUSED/);
});

test("lookupCitationKey: an empty/missing citation key resolves false without any network call", async () => {
  let called = false;
  const fetchImpl = async () => {
    called = true;
    return jsonResponse([]);
  };
  const result = await lookupCitationKey("", { fetchImpl });
  assert.deepEqual(result, { resolved: false });
  assert.equal(called, false);
});

test("lookupCitationKey: tag matched, but fetching the item's title fails -- still resolved:true, title:null (the tag match IS the answer)", async () => {
  const fetchImpl = async (url) => {
    if (url.includes("/tags")) return jsonResponse([{ tag: "P1:key:margulies2005454" }]);
    if (url.includes("/items")) return jsonResponse(null, false, 500);
    throw new Error(`unexpected url: ${url}`);
  };
  const result = await lookupCitationKey("margulies2005454", { fetchImpl });
  assert.deepEqual(result, { resolved: true, tag: "P1:key:margulies2005454", title: null });
});

test("lookupCitationKey: tag matched, but the items lookup throws -- still resolved:true, title:null", async () => {
  const fetchImpl = async (url) => {
    if (url.includes("/tags")) return jsonResponse([{ tag: "P1:key:margulies2005454" }]);
    if (url.includes("/items")) throw new Error("network blip");
    throw new Error(`unexpected url: ${url}`);
  };
  const result = await lookupCitationKey("margulies2005454", { fetchImpl });
  assert.deepEqual(result, { resolved: true, tag: "P1:key:margulies2005454", title: null });
});
