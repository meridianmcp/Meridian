// dashboard-gpu-offers.ts — the GPU offers tab (GPU offer catalog, pass 1: RunPod).
//
// Reads GET /compute/offers and shows the offers as a sortable table and a horizontal
// price chart. The server does the filtering and sorting; this file asks again whenever a
// control changes. Read only: nothing here creates, stops or bills a pod.
//
// Escaping rule (spec section 7): every value from the provider (names, ids, data center
// ids, warnings, error text) is written with textContent or a text node. Nothing from the
// provider is parsed as HTML, and no link or URL is ever taken from provider data. This file
// never assigns markup strings to the DOM; dashboard-gpu-offers.test.ts and a Python test check that.
//
// The fetch is injected, so the tests drive the tab with a stub and no server.

export const GPU_OFFERS_PATH = "/compute/offers";
export const GPU_OFFERS_CHART_LIMIT = 25;
export const GPU_OFFERS_SORT_KEYS = [
  "price_per_gpu_hour",
  "price_per_hour",
  "vram_gb_per_gpu",
  "provider",
  "stock",
] as const;
export const GPU_OFFERS_COUNTS: readonly number[] = [1, 2, 4, 8];

export type GpuOffersSortKey = (typeof GPU_OFFERS_SORT_KEYS)[number];
export type GpuOffersOrder = "asc" | "desc";
export type GpuCloudFilter = "" | "secure" | "community";

export interface GpuOffersFilters {
  gpuModel: string;
  cloudType: GpuCloudFilter;
  gpuCount: number;
  inStockOnly: boolean;
  maxPrice: string;
  sort: GpuOffersSortKey;
  order: GpuOffersOrder;
}

export const DEFAULT_GPU_OFFERS_FILTERS: GpuOffersFilters = {
  gpuModel: "",
  cloudType: "",
  gpuCount: 1,
  inStockOnly: false,
  maxPrice: "",
  sort: "price_per_gpu_hour",
  order: "asc",
};

export interface GpuOfferRow {
  id: string;
  provider: string;
  gpu: string | null;
  gpu_model: string | null;
  gpu_variant: string | null;
  gpu_count: number | null;
  max_gpu_count: number | null;
  cloud_type: string | null;
  data_center: string | null;
  stock_status: string | null;
  available: boolean | null;
  price_per_gpu_hour: number | null;
  price_per_hour: number | null;
  vram_gb_per_gpu: number | null;
  vram_gb: number | null;
  ram_gb: number | null;
  vcpu: number | null;
  price_as_of: string | null;
}

export interface GpuCodedError {
  code: string;
  message: string;
}

export interface GpuProviderState {
  name: string;
  status: string;
  prices_as_of: string | null;
  stock_as_of: string | null;
  checked_at: string | null;
  error: GpuCodedError | null;
  warnings: string[];
}

export interface GpuModelSummary {
  gpu_model: string | null;
  count: number;
  min_price_per_gpu_hour: number | null;
}

export interface GpuOffersBody {
  ok: boolean;
  status: string;
  error: GpuCodedError | null;
  credential_source?: string | null;
  providers: GpuProviderState[];
  offers: GpuOfferRow[];
  count: number;
  gpu_models: GpuModelSummary[];
  filters_applied: Record<string, unknown>;
}

export interface GpuOffersResponse {
  status: number;
  body: unknown;
}

export type GpuOffersFetch = (url: string) => Promise<GpuOffersResponse>;

export type GpuOffersTone = "ok" | "info" | "warn" | "error";

export interface GpuOffersState {
  tone: GpuOffersTone;
  title: string;
  lines: string[];
  hasData: boolean;
}

export interface GpuChartRow {
  offer: GpuOfferRow;
  /** Bar length as a percentage of the largest visible price, or null for a "no price" row. */
  widthPct: number | null;
}

export interface GpuOffersController {
  refresh(): Promise<void>;
  destroy(): void;
}

// ---------------------------------------------------------------------------
// Request URL
// ---------------------------------------------------------------------------

/**
 * The URL for the current filters. Only the parameters the server accepts are sent, and only when they differ
 * from the defaults. No key, token or other secret is ever part of this URL.
 */
export function buildGpuOffersUrl(filters: GpuOffersFilters): string {
  const params = new URLSearchParams();
  if (filters.gpuModel) params.set("gpu_model", filters.gpuModel);
  if (filters.cloudType) params.set("cloud_type", filters.cloudType);
  if (filters.gpuCount !== 1) params.set("gpu_count", String(filters.gpuCount));
  if (filters.inStockOnly) params.set("in_stock_only", "true");
  const cap = filters.maxPrice.trim();
  if (cap) params.set("max_price_per_gpu_hour", cap);
  if (filters.sort !== DEFAULT_GPU_OFFERS_FILTERS.sort) params.set("sort", filters.sort);
  if (filters.order !== "asc") params.set("order", filters.order);
  const query = params.toString();
  return query ? `${GPU_OFFERS_PATH}?${query}` : GPU_OFFERS_PATH;
}

/** The default fetch: same origin, JSON, and the body returned whatever the status is. */
export async function fetchGpuOffersJson(url: string): Promise<GpuOffersResponse> {
  const response = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return { status: response.status, body };
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/** A USD per-hour value, or "not reported" when it is unknown. Unknown is never shown as $0. */
export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "not reported";
  const fixed = value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  const decimals = fixed.includes(".") ? fixed.split(".")[1].length : 0;
  return `$${value.toFixed(Math.max(2, decimals))}`;
}

const GPU_OFFERS_STOCK_LABELS: Record<string, string> = { NONE: "none", LOW: "low", MEDIUM: "medium", HIGH: "high" };

/** The stock enum in lower case, or "unknown" for null. Labelled "for 1 GPU" when the machine has more GPUs. */
export function formatStockLabel(stock: string | null | undefined, gpuCount: number | null | undefined): string {
  const label = stock && GPU_OFFERS_STOCK_LABELS[stock] ? GPU_OFFERS_STOCK_LABELS[stock] : "unknown";
  return gpuCount && gpuCount > 1 ? `${label} (for 1 GPU)` : label;
}

/** An ISO UTC stamp as "2026-11-09 14:03:00 UTC", or "unknown" when it is missing. */
export function formatAsOf(stamp: string | null | undefined): string {
  if (!stamp) return "unknown";
  return `${stamp.replace("T", " ").replace("Z", "")} UTC`;
}

export function cloudLabel(cloud: string | null | undefined): string {
  if (cloud === "secure") return "Secure";
  if (cloud === "community") return "Community";
  if (cloud === "spot") return "Spot";
  return "not reported";
}

// ---------------------------------------------------------------------------
// State and chart (pure)
// ---------------------------------------------------------------------------

function gpuOffersBody(raw: unknown): GpuOffersBody | null {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const body = raw as GpuOffersBody;
  return Array.isArray(body.offers) && Array.isArray(body.providers) ? body : null;
}

/**
 * What to show above the table for one response. Every case the spec names has its own text: hosted, not
 * configured, stale (with the last good time), partial stock, an error with no data, and a bad request.
 */
/** The coded error from any body, even a refusal that has no offers or providers. */
function gpuOffersError(raw: unknown): GpuCodedError | null {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const error = (raw as { error?: unknown }).error;
  if (error === null || typeof error !== "object") return null;
  const { code, message } = error as { code?: unknown; message?: unknown };
  return typeof code === "string" && typeof message === "string" ? { code, message } : null;
}

export function describeGpuOffersState(response: GpuOffersResponse): GpuOffersState {
  const body = gpuOffersBody(response.body);
  const error = gpuOffersError(response.body);
  if (response.status === 403 && error?.code === "hosted_refused") {
    return { tone: "info", title: "GPU offers are available on local installs only.", lines: [], hasData: false };
  }
  if (response.status === 403) {
    return {
      tone: "error",
      title: "GPU offers can only be read from this machine's own dashboard.",
      lines: [],
      hasData: false,
    };
  }
  if (response.status === 400) {
    return {
      tone: "error",
      title: `Invalid request: ${error?.message ?? "check the filters and try again."}`,
      lines: [],
      hasData: false,
    };
  }
  if (body === null) {
    return {
      tone: "error",
      title: `Could not load GPU offers${response.status ? ` (HTTP ${response.status})` : ""}.`,
      lines: [],
      hasData: false,
    };
  }
  const providerWarnings = body.providers.flatMap((p) => p.warnings ?? []);
  if (body.status === "not_configured") {
    return {
      tone: "info",
      title: error?.message ?? "RunPod is not configured on this machine. Set RUNPOD_API_KEY, then refresh.",
      lines: [],
      hasData: false,
    };
  }
  if (body.status === "error") {
    return {
      tone: "error",
      title: `No offers could be read. ${error?.code ?? "error"}: ${error?.message ?? "the source failed."}`,
      lines: providerWarnings,
      hasData: false,
    };
  }
  if (body.status === "stale") {
    const lastGood = body.providers.find((p) => p.prices_as_of)?.prices_as_of ?? null;
    return {
      tone: "warn",
      title: `Showing last good data from ${formatAsOf(lastGood)}. Latest refresh failed: ${error?.message ?? "the source failed."}`,
      lines: providerWarnings,
      hasData: true,
    };
  }
  if (body.status === "partial") {
    return {
      tone: "warn",
      title: "Stock is unavailable for part of this list. Stock is shown as unknown, never as none.",
      lines: providerWarnings,
      hasData: true,
    };
  }
  return { tone: "ok", title: "", lines: providerWarnings, hasData: true };
}

/**
 * Bars for the chart, in the table's order. Bar length is the per-GPU price on a linear scale from zero, scaled to
 * the largest price shown. A row with no price gets no bar. It is listed as "no price", never drawn as zero.
 */
export function buildGpuChartRows(offers: readonly GpuOfferRow[], limit: number | null): GpuChartRow[] {
  const visible = limit === null ? offers.slice() : offers.slice(0, limit);
  const top = visible.reduce<number>((max, o) => {
    const price = o.price_per_gpu_hour;
    return price !== null && Number.isFinite(price) && price > max ? price : max;
  }, 0);
  return visible.map((offer) => {
    const price = offer.price_per_gpu_hour;
    if (price === null || !Number.isFinite(price) || top <= 0) return { offer, widthPct: null };
    return { offer, widthPct: (price / top) * 100 };
  });
}

/** Known provider names map to a palette class. Any other provider string is never used as a class name. */
export function providerClass(provider: string): string {
  return provider === "runpod" ? "gpu-bar--runpod" : "gpu-bar--other";
}

/** A cloud type from the fixed set, or "unknown". Provider text never becomes a class name. */
export function cloudClass(cloud: string | null | undefined): string {
  return cloud === "secure" || cloud === "community" || cloud === "spot" ? cloud : "unknown";
}

/** A stock enum from the fixed set, or "unknown". Provider text never becomes a class name. */
export function stockClass(stock: string | null | undefined): string {
  return stock === "NONE" || stock === "LOW" || stock === "MEDIUM" || stock === "HIGH" ? stock : "unknown";
}

// ---------------------------------------------------------------------------
// DOM (text nodes only)
// ---------------------------------------------------------------------------

function gpuEl<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function gpuLabelled(text: string, control: HTMLElement): HTMLLabelElement {
  const label = gpuEl("label", "gpu-offers-field");
  label.append(gpuEl("span", "gpu-offers-field-label", text), control);
  return label;
}

const GPU_OFFERS_SORT_HEADERS: { key: GpuOffersSortKey | null; label: string; title?: string }[] = [
  { key: null, label: "GPU model" },
  { key: "provider", label: "Provider" },
  { key: null, label: "Cloud" },
  { key: null, label: "Data center" },
  { key: "stock", label: "Stock" },
  { key: "price_per_gpu_hour", label: "$ / GPU-hr" },
  { key: "price_per_hour", label: "$ / machine-hr", title: "At the chosen GPU count" },
  { key: "vram_gb_per_gpu", label: "VRAM / GPU" },
  { key: null, label: "vCPU", title: "Not reported until RunPod's unit is confirmed" },
  { key: null, label: "RAM", title: "Not reported until RunPod's unit is confirmed" },
];

/**
 * Build the GPU offers tab inside ``root`` and return a controller. Refresh asks the server again, and the server's
 * cache decides whether RunPod is called. Only the most recent request paints, so a slow earlier response cannot
 * overwrite a newer one.
 */
export function mountGpuOffersTab(root: HTMLElement, fetchJson: GpuOffersFetch = fetchGpuOffersJson): GpuOffersController {
  let filters: GpuOffersFilters = { ...DEFAULT_GPU_OFFERS_FILTERS };
  let seq = 0;
  let showAll = false;
  let lastOffers: GpuOfferRow[] = [];
  let destroyed = false;

  root.replaceChildren();
  root.classList.add("gpu-offers");

  const head = gpuEl("div", "gpu-offers-head");
  head.append(
    gpuEl("div", "gpu-offers-title", "GPU offers"),
    gpuEl(
      "div",
      "gpu-offers-sub",
      "RunPod list prices and stock. Read only: nothing here creates, stops or bills a pod.",
    ),
  );

  const modelSelect = gpuEl("select", "gpu-offers-model");
  const cloudSelect = gpuEl("select", "gpu-offers-cloud");
  const countSelect = gpuEl("select", "gpu-offers-count");
  const stockToggle = gpuEl("input", "gpu-offers-stock");
  stockToggle.type = "checkbox";
  const capInput = gpuEl("input", "gpu-offers-cap");
  capInput.type = "number";
  capInput.min = "0";
  capInput.step = "any";
  capInput.placeholder = "any";
  const refreshButton = gpuEl("button", "gpu-offers-refresh secondary", "Refresh");
  refreshButton.type = "button";

  const cloudOptions: [GpuCloudFilter, string][] = [
    ["", "All clouds"],
    ["secure", "Secure"],
    ["community", "Community"],
  ];
  for (const [value, text] of cloudOptions) {
    const option = gpuEl("option", undefined, text);
    option.value = value;
    cloudSelect.append(option);
  }
  for (const count of GPU_OFFERS_COUNTS) {
    const option = gpuEl("option", undefined, `${count} GPU${count === 1 ? "" : "s"}`);
    option.value = String(count);
    countSelect.append(option);
  }
  cloudSelect.value = filters.cloudType;
  countSelect.value = String(filters.gpuCount);

  const controls = gpuEl("div", "gpu-offers-controls");
  controls.append(
    gpuLabelled("GPU model", modelSelect),
    gpuLabelled("Cloud", cloudSelect),
    gpuLabelled("GPUs in machine", countSelect),
    gpuLabelled("In stock only", stockToggle),
    gpuLabelled("Max $ / GPU-hr", capInput),
    refreshButton,
  );

  const status = gpuEl("div", "gpu-offers-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const asOf = gpuEl("div", "gpu-offers-asof");

  const tableWrap = gpuEl("div", "gpu-offers-table-wrap");
  const table = gpuEl("table", "gpu-offers-table");
  const thead = gpuEl("thead");
  const headRow = gpuEl("tr");
  const sortButtons = new Map<GpuOffersSortKey, HTMLButtonElement>();
  for (const header of GPU_OFFERS_SORT_HEADERS) {
    const th = gpuEl("th");
    if (header.title) th.title = header.title;
    if (header.key) {
      const key = header.key;
      const button = gpuEl("button", "gpu-offers-sort", header.label);
      button.type = "button";
      button.addEventListener("click", () => {
        if (filters.sort === key) {
          filters = { ...filters, order: filters.order === "asc" ? "desc" : "asc" };
        } else {
          filters = { ...filters, sort: key, order: "asc" };
        }
        void refresh();
      });
      sortButtons.set(key, button);
      th.append(button);
    } else {
      th.textContent = header.label;
    }
    headRow.append(th);
  }
  thead.append(headRow);
  const tbody = gpuEl("tbody");
  table.append(thead, tbody);
  tableWrap.append(table);

  const chartBox = gpuEl("section", "gpu-offers-chart");
  const chartHead = gpuEl("div", "gpu-offers-chart-head");
  const chartTitle = gpuEl("div", "gpu-offers-chart-title", "Price per GPU-hour, bars from $0");
  const showAllButton = gpuEl("button", "gpu-offers-showall secondary", "Show all");
  showAllButton.type = "button";
  chartHead.append(chartTitle, showAllButton);
  const chartList = gpuEl("ol", "gpu-offers-bars");
  chartBox.append(chartHead, chartList);

  root.append(head, controls, status, asOf, tableWrap, chartBox);

  function setBusy(busy: boolean): void {
    refreshButton.disabled = busy;
    root.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function paintStatus(state: GpuOffersState): void {
    status.replaceChildren();
    status.className = `gpu-offers-status gpu-offers-status--${state.tone}`;
    if (state.title) status.append(gpuEl("div", "gpu-offers-status-title", state.title));
    for (const line of state.lines) status.append(gpuEl("div", "gpu-offers-status-line", line));
  }

  function paintModels(summary: GpuModelSummary[]): void {
    const current = filters.gpuModel;
    modelSelect.replaceChildren();
    const all = gpuEl("option", undefined, "All GPU models");
    all.value = "";
    modelSelect.append(all);
    const models = summary.filter((m) => m.gpu_model !== null);
    for (const model of models) {
      const option = gpuEl(
        "option",
        undefined,
        `${model.gpu_model} (${model.count}, from ${formatUsd(model.min_price_per_gpu_hour)})`,
      );
      option.value = model.gpu_model ?? "";
      modelSelect.append(option);
    }
    if (current && !models.some((m) => m.gpu_model === current)) {
      const kept = gpuEl("option", undefined, current);
      kept.value = current;
      modelSelect.append(kept);
    }
    modelSelect.value = current;
  }

  function paintTable(offers: GpuOfferRow[], emptyText: string): void {
    tbody.replaceChildren();
    if (offers.length === 0) {
      const row = gpuEl("tr");
      const cell = gpuEl("td", "gpu-offers-empty", emptyText);
      cell.colSpan = GPU_OFFERS_SORT_HEADERS.length;
      row.append(cell);
      tbody.append(row);
      return;
    }
    for (const offer of offers) {
      const row = gpuEl("tr");
      const modelCell = gpuEl("td");
      modelCell.append(document.createTextNode(offer.gpu_model ?? "not reported"));
      if (offer.gpu_variant) modelCell.append(gpuEl("span", "gpu-offers-variant", ` ${offer.gpu_variant.toUpperCase()}`));
      if (offer.gpu) modelCell.title = offer.gpu;
      row.append(modelCell);
      row.append(gpuEl("td", undefined, offer.provider));
      const cloud = gpuEl("td");
      cloud.append(gpuEl("span", `gpu-cloud gpu-cloud--${cloudClass(offer.cloud_type)}`, cloudLabel(offer.cloud_type)));
      row.append(cloud);
      const dc = gpuEl("td", undefined, offer.data_center ?? "not reported");
      if (!offer.data_center) dc.title = "The provider reported stock for the whole cloud, not per data center.";
      row.append(dc);
      const stockCell = gpuEl(
        "td",
        `gpu-stock gpu-stock--${stockClass(offer.stock_status)}`,
        formatStockLabel(offer.stock_status, offer.gpu_count),
      );
      row.append(stockCell);
      row.append(gpuEl("td", "gpu-num", formatUsd(offer.price_per_gpu_hour)));
      const machine = gpuEl("td", "gpu-num", formatUsd(offer.price_per_hour));
      machine.title = `At ${offer.gpu_count ?? 1} GPU${(offer.gpu_count ?? 1) === 1 ? "" : "s"}`;
      row.append(machine);
      row.append(
        gpuEl(
          "td",
          "gpu-num",
          offer.vram_gb_per_gpu === null || offer.vram_gb_per_gpu === undefined ? "not reported" : `${offer.vram_gb_per_gpu} GB`,
        ),
      );
      const vcpu = gpuEl("td", "gpu-num", offer.vcpu === null ? "not reported" : String(offer.vcpu));
      if (offer.vcpu === null) vcpu.title = "RunPod's unit is not confirmed yet, so this is not guessed.";
      row.append(vcpu);
      const ram = gpuEl("td", "gpu-num", offer.ram_gb === null ? "not reported" : `${offer.ram_gb} GB`);
      if (offer.ram_gb === null) ram.title = "RunPod's unit is not confirmed yet, so this is not guessed.";
      row.append(ram);
      tbody.append(row);
    }
  }

  function paintSortState(): void {
    for (const [key, button] of sortButtons) {
      const active = filters.sort === key;
      const th = button.parentElement;
      if (th) th.setAttribute("aria-sort", active ? (filters.order === "asc" ? "ascending" : "descending") : "none");
      button.dataset.active = active ? "true" : "false";
    }
  }

  function paintChart(offers: GpuOfferRow[]): void {
    chartList.replaceChildren();
    const limit = showAll ? null : GPU_OFFERS_CHART_LIMIT;
    const rows = buildGpuChartRows(offers, limit);
    chartBox.setAttribute("aria-label", `Bar chart of ${rows.length} offers, price per GPU-hour from zero`);
    showAllButton.hidden = offers.length <= GPU_OFFERS_CHART_LIMIT;
    showAllButton.textContent = showAll ? `Show top ${GPU_OFFERS_CHART_LIMIT}` : `Show all ${offers.length}`;
    for (const { offer, widthPct } of rows) {
      const item = gpuEl("li", "gpu-bar-row");
      item.title = `Price as of ${formatAsOf(offer.price_as_of)}`;
      const label = gpuEl(
        "span",
        "gpu-bar-label",
        `${offer.provider} · ${offer.gpu_model ?? "not reported"} · ${cloudLabel(offer.cloud_type)} · ${offer.data_center ?? "cloud"}`,
      );
      item.append(label);
      if (widthPct === null) {
        item.append(gpuEl("span", "gpu-bar-nop", "no price"));
      } else {
        const track = gpuEl("span", `gpu-bar-track ${providerClass(offer.provider)} gpu-bar-cloud--${cloudClass(offer.cloud_type)}`);
        const bar = gpuEl("span", "gpu-bar");
        bar.style.width = `${widthPct.toFixed(2)}%`;
        track.append(bar);
        item.append(track);
        item.append(gpuEl("span", "gpu-bar-value", formatUsd(offer.price_per_gpu_hour)));
      }
      chartList.append(item);
    }
  }

  function syncControls(): void {
    cloudSelect.value = filters.cloudType;
    countSelect.value = String(filters.gpuCount);
    stockToggle.checked = filters.inStockOnly;
    capInput.value = filters.maxPrice;
  }

  async function refresh(): Promise<void> {
    if (destroyed) return;
    const mine = ++seq;
    syncControls();
    paintSortState();
    setBusy(true);
    paintStatus({ tone: "info", title: "Loading GPU offers…", lines: [], hasData: false });
    let response: GpuOffersResponse;
    try {
      response = await fetchJson(buildGpuOffersUrl(filters));
    } catch {
      if (destroyed || mine !== seq) return;
      setBusy(false);
      paintStatus({
        tone: "error",
        title: "Could not reach this Meridian server for GPU offers. Refresh to try again.",
        lines: [],
        hasData: false,
      });
      return;
    }
    if (destroyed || mine !== seq) return;
    setBusy(false);
    const state = describeGpuOffersState(response);
    paintStatus(state);
    const body = gpuOffersBody(response.body);
    if (body) {
      paintModels(body.gpu_models ?? []);
      asOf.textContent = body.providers.length
        ? `Prices as of ${formatAsOf(body.providers[0].prices_as_of)}. Stock as of ${formatAsOf(body.providers[0].stock_as_of)}.`
        : "";
      lastOffers = body.offers;
    } else {
      asOf.textContent = "";
      lastOffers = [];
    }
    const emptyText = state.hasData ? "No offers match these filters." : "No offers to show. See the status above.";
    paintTable(state.hasData ? lastOffers : [], emptyText);
    paintChart(state.hasData ? lastOffers : []);
  }

  modelSelect.addEventListener("change", () => {
    filters = { ...filters, gpuModel: modelSelect.value };
    void refresh();
  });
  cloudSelect.addEventListener("change", () => {
    filters = { ...filters, cloudType: cloudSelect.value as GpuCloudFilter };
    void refresh();
  });
  countSelect.addEventListener("change", () => {
    filters = { ...filters, gpuCount: Number(countSelect.value) };
    void refresh();
  });
  stockToggle.addEventListener("change", () => {
    filters = { ...filters, inStockOnly: stockToggle.checked };
    void refresh();
  });
  capInput.addEventListener("change", () => {
    filters = { ...filters, maxPrice: capInput.value };
    void refresh();
  });
  refreshButton.addEventListener("click", () => {
    void refresh();
  });
  showAllButton.addEventListener("click", () => {
    showAll = !showAll;
    paintChart(lastOffers);
  });

  return {
    refresh,
    destroy(): void {
      destroyed = true;
      seq += 1;
      root.replaceChildren();
    },
  };
}

const gpuOffersControllers = new WeakMap<HTMLElement, GpuOffersController>();

/**
 * Called by the dashboard when the GPU offers vtab is opened. The panel is built once per project, then refreshed on
 * every open. A missing panel (for example on a page without the tab) does nothing.
 */
export function loadGpuOffersTab(projectId: string): void {
  const root = document.getElementById(`gpu-offers-root-${projectId}`);
  if (!root) return;
  let controller = gpuOffersControllers.get(root);
  if (!controller) {
    controller = mountGpuOffersTab(root);
    gpuOffersControllers.set(root, controller);
  }
  void controller.refresh();
}
