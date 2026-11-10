// Tests for the GPU offers tab (dashboard-gpu-offers.ts). Runs in jsdom with a stubbed fetch: no server, no network.
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_GPU_OFFERS_FILTERS,
  buildGpuChartRows,
  buildGpuOffersUrl,
  cloudClass,
  cloudLabel,
  describeGpuOffersState,
  fetchGpuOffersJson,
  formatAsOf,
  formatStockLabel,
  formatUsd,
  GPU_OFFERS_CHART_LIMIT,
  loadGpuOffersTab,
  mountGpuOffersTab,
  providerClass,
  stockClass,
  type GpuOfferRow,
  type GpuOffersBody,
  type GpuOffersFetch,
  type GpuOffersResponse,
} from "./dashboard-gpu-offers";

// Read relative to the repo root (vitest runs from there). import.meta.url is not a file: URL under jsdom.
const MODULE_SOURCE = readFileSync(path.resolve(process.cwd(), "meridian/static/dashboard-gpu-offers.ts"), "utf8");
const INJECTED = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>';

function row(overrides: Partial<GpuOfferRow> = {}): GpuOfferRow {
  return {
    id: "runpod:gpu:secure:-",
    provider: "runpod",
    gpu: "NVIDIA H100 80GB HBM3",
    gpu_model: "h100-80gb",
    gpu_variant: null,
    gpu_count: 1,
    max_gpu_count: 8,
    cloud_type: "secure",
    data_center: "US-TX-3",
    stock_status: "LOW",
    available: true,
    price_per_gpu_hour: 2.99,
    price_per_hour: 2.99,
    vram_gb_per_gpu: 80,
    vram_gb: 80,
    ram_gb: null,
    vcpu: null,
    price_as_of: "2026-11-09T14:03:00Z",
    ...overrides,
  };
}

function body(overrides: Partial<GpuOffersBody> = {}): GpuOffersBody {
  return {
    ok: true,
    status: "ok",
    error: null,
    credential_source: "env",
    providers: [
      {
        name: "runpod",
        status: "ok",
        prices_as_of: "2026-11-09T14:03:00Z",
        stock_as_of: "2026-11-09T14:03:00Z",
        checked_at: "2026-11-09T14:03:00Z",
        error: null,
        warnings: [],
      },
    ],
    offers: [row()],
    count: 1,
    gpu_models: [{ gpu_model: "h100-80gb", count: 1, min_price_per_gpu_hour: 2.99 }],
    filters_applied: {},
    ...overrides,
  };
}

function ok(payload: GpuOffersBody): GpuOffersResponse {
  return { status: 200, body: payload };
}

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/** Mount the tab and load it once, the way loadGpuOffersTab does when the vtab opens. */
async function mountLoaded(fetchJson: GpuOffersFetch) {
  const controller = mountGpuOffersTab(root, fetchJson);
  await controller.refresh();
  return controller;
}

let root: HTMLElement;

beforeEach(() => {
  document.body.innerHTML = "";
  root = document.createElement("div");
  document.body.append(root);
  (window as unknown as { __pwned?: number }).__pwned = undefined;
});

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

// --- request URL --------------------------------------------------------------------------------------

describe("buildGpuOffersUrl", () => {
  it("is the bare path when every filter is at its default", () => {
    expect(buildGpuOffersUrl(DEFAULT_GPU_OFFERS_FILTERS)).toBe("/compute/offers");
  });

  it("sends only the parameters that differ from the defaults", () => {
    const url = buildGpuOffersUrl({
      ...DEFAULT_GPU_OFFERS_FILTERS,
      gpuModel: "h100-80gb",
      cloudType: "secure",
      gpuCount: 4,
      inStockOnly: true,
      maxPrice: " 2.5 ",
      sort: "stock",
      order: "desc",
    });
    const params = new URL(url, "http://local").searchParams;
    expect([...params.keys()].sort()).toEqual(
      ["cloud_type", "gpu_count", "gpu_model", "in_stock_only", "max_price_per_gpu_hour", "order", "sort"].sort(),
    );
    expect(params.get("max_price_per_gpu_hour")).toBe("2.5");
    expect(params.get("order")).toBe("desc");
  });

  it("never carries a key or any other parameter the server does not accept", () => {
    const url = buildGpuOffersUrl({ ...DEFAULT_GPU_OFFERS_FILTERS, gpuModel: "x" });
    expect(url).not.toMatch(/api_key|token|key=/i);
  });
});

// --- formatting -----------------------------------------------------------------------------------------

describe("formatting", () => {
  it("shows unknown prices as not reported, never as zero", () => {
    expect(formatUsd(null)).toBe("not reported");
    expect(formatUsd(undefined)).toBe("not reported");
    expect(formatUsd(Number.NaN)).toBe("not reported");
    expect(formatUsd(2.99)).toBe("$2.99");
    expect(formatUsd(0.4567)).toBe("$0.4567");
    expect(formatUsd(2)).toBe("$2.00");
  });

  it("labels stock for one GPU when the machine has more", () => {
    expect(formatStockLabel(null, 1)).toBe("unknown");
    expect(formatStockLabel("NONE", 1)).toBe("none");
    expect(formatStockLabel("HIGH", 1)).toBe("high");
    expect(formatStockLabel("LOW", 4)).toBe("low (for 1 GPU)");
  });

  it("formats a UTC stamp, and shows unknown for a missing one", () => {
    expect(formatAsOf("2026-11-09T14:03:00Z")).toBe("2026-11-09 14:03:00 UTC");
    expect(formatAsOf(null)).toBe("unknown");
  });

  it("maps cloud, provider and stock to fixed class names, never raw provider text", () => {
    expect(cloudLabel("community")).toBe("Community");
    expect(cloudLabel(null)).toBe("not reported");
    expect(cloudClass(INJECTED)).toBe("unknown");
    expect(providerClass(INJECTED)).toBe("gpu-bar--other");
    expect(providerClass("runpod")).toBe("gpu-bar--runpod");
    expect(stockClass(INJECTED)).toBe("unknown");
    expect(stockClass("HIGH")).toBe("HIGH");
  });
});

// --- state -----------------------------------------------------------------------------------------------

describe("describeGpuOffersState", () => {
  it("says hosted installs are local only, with a 403 hosted_refused", () => {
    const state = describeGpuOffersState({
      status: 403,
      body: { ok: false, error: { code: "hosted_refused", message: "x" }, providers: [], offers: [] },
    });
    expect(state.tone).toBe("info");
    expect(state.title).toBe("GPU offers are available on local installs only.");
    expect(state.hasData).toBe(false);
  });

  it("treats any other 403 as a local-only refusal", () => {
    const state = describeGpuOffersState({ status: 403, body: { error: { code: "local_only", message: "x" } } });
    expect(state.tone).toBe("error");
  });

  it("shows the fixed message for an invalid request", () => {
    const state = describeGpuOffersState({
      status: 400,
      body: { error: { code: "invalid_filter", message: "sort must be one of: price_per_gpu_hour." } },
    });
    expect(state.tone).toBe("error");
    expect(state.title).toContain("sort must be one of");
  });

  it("explains a missing key without a fetch-style error", () => {
    const state = describeGpuOffersState(
      ok(
        body({
          status: "not_configured",
          ok: false,
          error: { code: "not_configured", message: "RunPod is not configured on this machine. Set RUNPOD_API_KEY, then refresh." },
          offers: [],
          providers: [],
        }),
      ),
    );
    expect(state.tone).toBe("info");
    expect(state.title).toContain("RUNPOD_API_KEY");
    expect(state.hasData).toBe(false);
  });

  it("shows stale data with the last good time and the refresh error", () => {
    const state = describeGpuOffersState(
      ok(body({ status: "stale", error: { code: "upstream_error", message: "RunPod returned an error." } })),
    );
    expect(state.tone).toBe("warn");
    expect(state.title).toBe(
      "Showing last good data from 2026-11-09 14:03:00 UTC. Latest refresh failed: RunPod returned an error.",
    );
    expect(state.hasData).toBe(true);
  });

  it("says stock is unknown, never none, when a stock read failed", () => {
    const state = describeGpuOffersState(ok(body({ status: "partial" })));
    expect(state.tone).toBe("warn");
    expect(state.title).toContain("never as none");
    expect(state.hasData).toBe(true);
  });

  it("reports an error with no data using the fixed code and message", () => {
    const state = describeGpuOffersState(
      ok(body({ status: "error", error: { code: "timeout", message: "The RunPod catalog request timed out." }, offers: [] })),
    );
    expect(state.tone).toBe("error");
    expect(state.title).toContain("timeout");
    expect(state.hasData).toBe(false);
  });

  it("is ok with data and surfaces provider warnings as lines", () => {
    const state = describeGpuOffersState(
      ok(body({ providers: [{ ...body().providers[0], warnings: ["1 price value(s) were zero."] }] })),
    );
    expect(state.tone).toBe("ok");
    expect(state.lines).toEqual(["1 price value(s) were zero."]);
  });

  it("treats an unreadable body as an error and names the HTTP status", () => {
    expect(describeGpuOffersState({ status: 500, body: "<html>" }).title).toBe("Could not load GPU offers (HTTP 500).");
    expect(describeGpuOffersState({ status: 200, body: null }).tone).toBe("error");
  });
});

// --- chart ---------------------------------------------------------------------------------------------

describe("buildGpuChartRows", () => {
  it("scales bars linearly from zero to the largest visible price", () => {
    const rows = buildGpuChartRows(
      [row({ id: "a", price_per_gpu_hour: 2 }), row({ id: "b", price_per_gpu_hour: 1 }), row({ id: "c", price_per_gpu_hour: 0.5 })],
      null,
    );
    expect(rows.map((r) => r.widthPct)).toEqual([100, 50, 25]);
  });

  it("draws no bar for an unknown price, so it is never a zero-length bar", () => {
    const rows = buildGpuChartRows([row({ price_per_gpu_hour: null }), row({ price_per_gpu_hour: 3 })], null);
    expect(rows[0].widthPct).toBeNull();
    expect(rows[1].widthPct).toBe(100);
  });

  it("keeps the table order and honours the limit", () => {
    const many = Array.from({ length: 30 }, (_, i) => row({ id: `r${i}`, price_per_gpu_hour: 1 + i }));
    expect(buildGpuChartRows(many, GPU_OFFERS_CHART_LIMIT)).toHaveLength(25);
    expect(buildGpuChartRows(many, null)).toHaveLength(30);
    expect(buildGpuChartRows(many, 3)[0].offer.id).toBe("r0");
  });

  it("gives no bars at all when no offer has a price", () => {
    const rows = buildGpuChartRows([row({ price_per_gpu_hour: null })], null);
    expect(rows.every((r) => r.widthPct === null)).toBe(true);
  });
});

// --- fetch wrapper -------------------------------------------------------------------------------------

describe("fetchGpuOffersJson", () => {
  it("returns the status and the parsed body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ status: 200, json: async () => ({ offers: [] }) })),
    );
    expect(await fetchGpuOffersJson("/compute/offers")).toEqual({ status: 200, body: { offers: [] } });
  });

  it("returns a null body when the response is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        status: 502,
        json: async () => {
          throw new SyntaxError("not json");
        },
      })),
    );
    expect(await fetchGpuOffersJson("/compute/offers")).toEqual({ status: 502, body: null });
  });
});

// --- DOM -----------------------------------------------------------------------------------------------

describe("mountGpuOffersTab", () => {
  it("renders provider and GPU strings as text, so injected markup never becomes an element", async () => {
    const hostile = row({
      provider: INJECTED,
      gpu_model: INJECTED,
      gpu: INJECTED,
      data_center: INJECTED,
      cloud_type: "secure",
    });
    const fetchJson: GpuOffersFetch = vi.fn(async () => ok(body({ offers: [hostile], gpu_models: [] })));
    await mountLoaded(fetchJson);
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector("script")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(root.textContent).toContain(INJECTED);
  });

  it("builds the model selector from the summary as text", async () => {
    const fetchJson: GpuOffersFetch = vi.fn(async () =>
      ok(body({ gpu_models: [{ gpu_model: INJECTED, count: 2, min_price_per_gpu_hour: 1 }] })),
    );
    await mountLoaded(fetchJson);
    expect(root.querySelector("img")).toBeNull();
    const options = [...root.querySelectorAll<HTMLOptionElement>(".gpu-offers-model option")].map((o) => o.textContent);
    expect(options.some((text) => text?.includes(INJECTED))).toBe(true);
  });

  it("asks the server again with the new sort when a sortable column is clicked", async () => {
    const fetchJson = vi.fn<GpuOffersFetch>(async () => ok(body()));
    await mountLoaded(fetchJson);
    const priceButton = [...root.querySelectorAll<HTMLButtonElement>("button.gpu-offers-sort")].find(
      (b) => b.textContent === "$ / GPU-hr",
    );
    priceButton?.click();
    await flush();
    expect(fetchJson).toHaveBeenLastCalledWith("/compute/offers?order=desc");
    const providerButton = [...root.querySelectorAll<HTMLButtonElement>("button.gpu-offers-sort")].find(
      (b) => b.textContent === "Provider",
    );
    providerButton?.click();
    await flush();
    expect(fetchJson).toHaveBeenLastCalledWith("/compute/offers?sort=provider");
  });

  it("refreshes from the refresh button", async () => {
    const fetchJson = vi.fn<GpuOffersFetch>(async () => ok(body()));
    await mountLoaded(fetchJson);
    (root.querySelector(".gpu-offers-refresh") as HTMLButtonElement).click();
    await flush();
    expect(fetchJson).toHaveBeenCalledTimes(2);
  });

  it("shows the hosted notice on a hosted install and no table rows", async () => {
    const fetchJson: GpuOffersFetch = vi.fn(async () => ({
      status: 403,
      body: { error: { code: "hosted_refused", message: "x" }, providers: [], offers: [] },
    }));
    await mountLoaded(fetchJson);
    expect(root.querySelector(".gpu-offers-status")?.textContent).toContain("local installs only");
    expect(root.querySelector(".gpu-offers-empty")?.textContent).toBe("No offers to show. See the status above.");
  });

  it("shows stale rows under the stale banner", async () => {
    const fetchJson: GpuOffersFetch = vi.fn(async () =>
      ok(body({ status: "stale", error: { code: "rate_limited", message: "RunPod rate-limited the request." } })),
    );
    await mountLoaded(fetchJson);
    expect(root.querySelector(".gpu-offers-status--warn")).not.toBeNull();
    expect(root.querySelectorAll("tbody tr").length).toBe(1);
  });

  it("reports a network failure in words and keeps the controls", async () => {
    const fetchJson: GpuOffersFetch = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    await mountLoaded(fetchJson);
    expect(root.querySelector(".gpu-offers-status--error")?.textContent).toContain("Could not reach");
    expect(root.querySelector(".gpu-offers-refresh")).not.toBeNull();
  });

  it("paints only the most recent response when an earlier one arrives last", async () => {
    const pending: Array<(value: GpuOffersResponse) => void> = [];
    const fetchJson: GpuOffersFetch = vi.fn(
      () => new Promise<GpuOffersResponse>((resolve) => pending.push(resolve)),
    );
    const controller = mountGpuOffersTab(root, fetchJson);
    const second = row({ id: "second", provider: "second-provider" });
    const first = row({ id: "first", provider: "first-provider" });
    // Two requests in flight. The refresh button is disabled while loading, so the second request is made
    // through the controller: pending[0] is the older one, pending[1] the newer one.
    void controller.refresh();
    void controller.refresh();
    pending[1](ok(body({ offers: [second] })));
    await flush();
    pending[0](ok(body({ offers: [first] })));
    await flush();
    expect(root.textContent).toContain("second-provider");
    expect(root.textContent).not.toContain("first-provider");
  });

  it("shows a bounded chart with a show-all control", async () => {
    const many = Array.from({ length: 30 }, (_, i) => row({ id: `r${i}`, price_per_gpu_hour: 1 + i * 0.1 }));
    const fetchJson: GpuOffersFetch = vi.fn(async () => ok(body({ offers: many, count: 30 })));
    await mountLoaded(fetchJson);
    expect(root.querySelectorAll(".gpu-bar-row").length).toBe(GPU_OFFERS_CHART_LIMIT);
    const showAll = root.querySelector(".gpu-offers-showall") as HTMLButtonElement;
    expect(showAll.textContent).toBe("Show all 30");
    showAll.click();
    expect(root.querySelectorAll(".gpu-bar-row").length).toBe(30);
  });

  it("labels a no-price row and draws it with no bar", async () => {
    const fetchJson: GpuOffersFetch = vi.fn(async () =>
      ok(body({ offers: [row({ price_per_gpu_hour: null, price_per_hour: null })] })),
    );
    await mountLoaded(fetchJson);
    expect(root.querySelector(".gpu-bar-nop")?.textContent).toBe("no price");
    expect(root.querySelector(".gpu-bar")).toBeNull();
  });

  it("removes its content when destroyed", async () => {
    const fetchJson: GpuOffersFetch = vi.fn(async () => ok(body()));
    const controller = mountGpuOffersTab(root, fetchJson);
    await controller.refresh();
    controller.destroy();
    expect(root.children.length).toBe(0);
    await controller.refresh();
    expect(fetchJson).toHaveBeenCalledTimes(1);
  });

  it("gives the status region an accessible live label and a labelled chart", async () => {
    await mountLoaded(vi.fn(async () => ok(body())));
    expect(root.querySelector(".gpu-offers-status")?.getAttribute("role")).toBe("status");
    expect(root.querySelector(".gpu-offers-chart")?.getAttribute("aria-label")).toContain("Bar chart of 1 offers");
  });
});

describe("loadGpuOffersTab", () => {
  it("does nothing when the panel is not on the page", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    expect(() => loadGpuOffersTab("missing-project")).not.toThrow();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("mounts the panel once per project and refreshes it on every open", async () => {
    root.id = "gpu-offers-root-proj-1";
    const fetchSpy = vi.fn(async (_url: string, _init?: RequestInit) => ({ status: 200, json: async () => body() }));
    vi.stubGlobal("fetch", fetchSpy);
    loadGpuOffersTab("proj-1");
    await flush();
    loadGpuOffersTab("proj-1");
    await flush();
    expect(root.querySelectorAll("table").length).toBe(1);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy.mock.calls[0][0]).toBe("/compute/offers");
  });
});

// --- source guard ------------------------------------------------------------------------------------

describe("source guard", () => {
  it("never uses innerHTML or dangerouslySetInnerHTML, so provider text cannot become markup", () => {
    expect(MODULE_SOURCE).not.toMatch(/innerHTML/);
    expect(MODULE_SOURCE).not.toMatch(/dangerouslySetInnerHTML/);
  });
});
