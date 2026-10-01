// Minimal ambient type declaration for `chrome-remote-interface` (the Chrome
// DevTools Protocol client overleaf-login.js/ts uses to read cookies out of a
// dedicated, human-driven login window -- see that file's own header
// comment). This package ships no bundled `.d.ts` and, as of this migration,
// has no DefinitelyTyped (`@types/chrome-remote-interface`) package either --
// confirmed absent, not assumed -- so a local shim is the only option, per
// the TS-migration plan's own note that this gap would surface the first
// time this dependency is actually typed.
//
// This shim is DELIBERATELY partial: it declares only the CDP domains and
// methods this repository's own TypeScript sources actually call today
// (`Network.enable`/`getCookies`, `Page.enable`/`getFrameTree`, `close`, and
// the callable `CDP(options)` entry point itself) -- not the full Chrome
// DevTools Protocol surface (hundreds of domains/methods across
// `lib/protocol.json`). A later batch that starts calling more of the
// protocol from a TypeScript file (e.g. browser.ts) should EXTEND this file
// with the additional methods/domains it needs, rather than hand-roll a
// second, separately-shaped declaration for the same module.

declare module "chrome-remote-interface" {
  /** One cookie as CDP's `Network.getCookies` reports it. Only the fields
   * this codebase actually reads (`name`, `value`) are given precise types;
   * everything else CDP includes (domain, path, expires, httpOnly, secure,
   * sameSite, ...) is real but untyped here -- reflected honestly via the
   * index signature rather than enumerated speculatively. */
  export interface CDPCookie {
    name: string;
    value: string;
    [extra: string]: unknown;
  }

  export interface CDPGetCookiesResult {
    cookies: CDPCookie[];
  }

  export interface CDPNetworkDomain {
    enable(): Promise<void>;
    getCookies(params: { urls: string[] }): Promise<CDPGetCookiesResult>;
  }

  /** The one field of a CDP frame this codebase reads (`url`, via
   * `Page.getFrameTree`'s `frameTree.frame.url`). */
  export interface CDPFrame {
    url: string;
    [extra: string]: unknown;
  }

  export interface CDPFrameTree {
    frame: CDPFrame;
    childFrames?: CDPFrameTree[];
  }

  export interface CDPGetFrameTreeResult {
    frameTree: CDPFrameTree;
  }

  export interface CDPPageDomain {
    enable(): Promise<void>;
    getFrameTree(): Promise<CDPGetFrameTreeResult>;
  }

  export interface CDPClient {
    Network: CDPNetworkDomain;
    Page: CDPPageDomain;
    close(): Promise<void>;
  }

  export interface CDPOptions {
    port?: number;
    host?: string;
    target?: string;
    [extra: string]: unknown;
  }

  export default function CDP(options?: CDPOptions): Promise<CDPClient>;
}
