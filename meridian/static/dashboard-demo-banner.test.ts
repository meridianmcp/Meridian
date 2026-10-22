// 90952bad - the fixed /demo banner used to paint over the hamburger and the waffle
// launcher on a phone, where it wraps to ~76px. dashboard.html now carries a small
// inline script that publishes the banner's measured height as --demo-banner-h so
// dashboard.css can reserve exactly that much room above the app. This runs the REAL
// script, sliced out of the template, in jsdom.
import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const template = readFileSync(path.resolve("meridian/templates/dashboard.html"), "utf-8");

function bannerScript(): string {
  const banner = template.indexOf('id="demo-banner"');
  expect(banner).toBeGreaterThan(-1);
  const start = template.indexOf("<script>", banner);
  const end = template.indexOf("</script>", start);
  // It must belong to the demo-only block, right after the banner it measures.
  expect(template.indexOf("{% endif %}", banner)).toBeGreaterThan(end);
  expect(template.slice(banner, start)).not.toContain("<script");
  return template.slice(start + "<script>".length, end);
}

const SCRIPT = bannerScript();
const VAR = "--demo-banner-h";

function mountBanner(height: number): HTMLElement {
  document.body.innerHTML = '<div id="demo-banner">Demo mode</div>';
  const banner = document.getElementById("demo-banner") as HTMLElement;
  setHeight(banner, height);
  return banner;
}

function setHeight(banner: HTMLElement, height: number) {
  banner.getBoundingClientRect = () => ({ height, width: 375, top: 0, left: 0, right: 375, bottom: height, x: 0, y: 0, toJSON() {} }) as DOMRect;
}

const published = () => document.documentElement.style.getPropertyValue(VAR);

let restoreFonts: (() => void) | null = null;

/** jsdom has no FontFaceSet: give document a minimal one so 'loadingdone' can be fired. */
function installFonts(): EventTarget {
  const fonts = new EventTarget();
  const had = Object.getOwnPropertyDescriptor(document, "fonts");
  Object.defineProperty(document, "fonts", { configurable: true, value: fonts });
  restoreFonts = () => {
    if (had) Object.defineProperty(document, "fonts", had);
    else delete (document as any).fonts;
  };
  return fonts;
}

describe("demo banner height publication (--demo-banner-h)", () => {
  const originalRO = (window as any).ResizeObserver;

  beforeEach(() => {
    document.documentElement.style.removeProperty(VAR);
    delete (window as any).ResizeObserver;
  });
  afterEach(() => {
    (window as any).ResizeObserver = originalRO;
    restoreFonts?.();
    restoreFonts = null;
    document.body.innerHTML = "";
    document.documentElement.style.removeProperty(VAR);
  });

  const run = () => new Function(SCRIPT)();

  it("publishes the banner height straight away, rounded UP so no sub-pixel strip stays covered", () => {
    mountBanner(76.2);
    run();
    expect(published()).toBe("77px");
  });

  it("publishes a whole-pixel height unchanged (the 32px desktop banner)", () => {
    mountBanner(32);
    run();
    expect(published()).toBe("32px");
  });

  it("re-measures when the viewport is resized (the banner re-wraps)", () => {
    const banner = mountBanner(32);
    run();
    setHeight(banner, 76.8);
    window.dispatchEvent(new Event("resize"));
    expect(published()).toBe("77px");
  });

  it("re-measures on window load, which does not wait for a rendered frame", () => {
    const banner = mountBanner(72);
    run();
    setHeight(banner, 76.8); // the web font arrived and the banner grew
    window.dispatchEvent(new Event("load"));
    expect(published()).toBe("77px");
  });

  it("re-measures when a web font finishes loading (the font swap changes the wrapped height)", () => {
    const fonts = installFonts();
    const banner = mountBanner(72);
    run();
    expect(published()).toBe("72px");
    setHeight(banner, 76.8);
    fonts.dispatchEvent(new Event("loadingdone"));
    expect(published()).toBe("77px");
  });

  it("follows the banner through ResizeObserver when it is available", () => {
    let notify: (() => void) | null = null;
    let observed: Element | null = null;
    (window as any).ResizeObserver = class {
      constructor(cb: () => void) {
        notify = cb;
      }
      observe(el: Element) {
        observed = el;
      }
    };
    const banner = mountBanner(32);
    run();
    expect(observed).toBe(banner);
    setHeight(banner, 90.1);
    notify!();
    expect(published()).toBe("91px");
  });

  it("works without ResizeObserver or document.fonts (older browsers)", () => {
    mountBanner(50);
    expect(() => run()).not.toThrow();
    expect(published()).toBe("50px");
  });

  it("does nothing, and does not throw, when the page has no demo banner", () => {
    document.body.innerHTML = "<div></div>";
    expect(() => run()).not.toThrow();
    expect(published()).toBe("");
  });
});

describe("demo banner reservation wiring (template + stylesheet)", () => {
  const css = readFileSync(path.resolve("meridian/static/dashboard.css"), "utf-8");

  it("keeps the script inside the demo-only block so other pages never carry it", () => {
    const blockStart = template.indexOf("{% if demo_mode %}<div id=\"demo-banner\"");
    expect(blockStart).toBeGreaterThan(-1);
    const blockEnd = template.indexOf("{% endif %}", blockStart);
    const inside = template.slice(blockStart, blockEnd);
    expect(inside).toContain(VAR);
    expect(template.slice(0, blockStart)).not.toContain(VAR);
    expect(template.slice(blockEnd)).not.toContain(VAR);
  });

  it("reserves the published height on .app and moves the phone drawer and hamburger with it", () => {
    expect(css).toMatch(/\.app\s*\{\s*padding-top:\s*var\(--demo-banner-h,\s*0px\);\s*\}/);
    expect(css).toMatch(/\.sidebar\s*\{\s*top:\s*var\(--demo-banner-h,\s*0px\);\s*\}/);
    expect(css).toMatch(/#sidebar-toggle\s*\{\s*top:\s*calc\(var\(--demo-banner-h,\s*0px\)\s*\+\s*10px\);\s*\}/);
  });
});
