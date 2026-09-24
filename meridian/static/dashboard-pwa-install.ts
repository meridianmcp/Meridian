// afcbd8a2 — PWA install-prompt affordance.
//
// b03be6a6 already shipped the installability requirements (manifest.webmanifest,
// icons, network-first service worker, <link rel="manifest"> + SW registration in
// dashboard.html). What's missing is the actual install AFFORDANCE: Chrome/Edge/
// ChromeOS/Android suppress their own mini-infobar once a page calls
// event.preventDefault() on `beforeinstallprompt`, so without this module the
// site becomes installable but gives the user no visible way to install it.
//
// This module owns exactly that gap:
//   1. Listen for `beforeinstallprompt`, preventDefault() it, and stash the
//      event (browsers require the later `.prompt()` call to happen from a
//      real user gesture, so it can't be replayed automatically).
//   2. Show a small "Install app" button once the event fires.
//   3. On click, replay the stashed prompt and clear it either way (a captured
//      `beforeinstallprompt` event can only be used once).
//   4. Hide the button on `appinstalled`, and never show it at all when the
//      dashboard is already running as an installed app (standalone display
//      mode / iOS's legacy `navigator.standalone`).
//
// Firefox and Safari (desktop) never fire `beforeinstallprompt` at all — the
// button simply never appears there, which is the correct, standards-driven
// degrade (iOS Safari's install path is manual Share -> Add to Home Screen,
// which the existing apple-touch-icon/apple-mobile-web-app-* meta tags in
// dashboard.html already support).

interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
}

const PWA_INSTALL_BUTTON_ID = "pwa-install-button";

let deferredInstallPrompt: BeforeInstallPromptEvent | null = null;

function isRunningStandalone(): boolean {
  const mq =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(display-mode: standalone)").matches;
  // iOS Safari has no `display-mode` support; it exposes this legacy flag
  // instead when launched from an Add-to-Home-Screen icon.
  const iosStandalone = (navigator as unknown as { standalone?: boolean }).standalone === true;
  return Boolean(mq || iosStandalone);
}

function hideInstallButton(): void {
  const btn = document.getElementById(PWA_INSTALL_BUTTON_ID);
  if (btn) btn.remove();
}

async function handleInstallClick(): Promise<void> {
  const promptEvent = deferredInstallPrompt;
  // A captured beforeinstallprompt event is single-use regardless of the
  // outcome — clear it up front so a slow double-click can't replay it.
  deferredInstallPrompt = null;
  hideInstallButton();
  if (!promptEvent) return;
  try {
    await promptEvent.prompt();
    await promptEvent.userChoice;
  } catch {
    // Nothing actionable here — the browser already handled/declined the
    // prompt; a rejected userChoice promise isn't a real error condition.
  }
}

function showInstallButton(): void {
  if (isRunningStandalone() || document.getElementById(PWA_INSTALL_BUTTON_ID)) return;
  const btn = document.createElement("button");
  btn.id = PWA_INSTALL_BUTTON_ID;
  btn.type = "button";
  btn.className = "pwa-install-button";
  btn.textContent = "Install app";
  btn.title = "Install Meridian as an app (ChromeOS, Windows, macOS, Android, Linux)";
  btn.setAttribute("aria-label", "Install Meridian as an app");
  btn.addEventListener("click", () => {
    void handleInstallClick();
  });
  document.body.appendChild(btn);
}

function initPwaInstallPrompt(): void {
  if (isRunningStandalone()) return;

  window.addEventListener("beforeinstallprompt", (event: Event) => {
    // Suppress the browser's own mini-infobar; we show our own button so the
    // install action is discoverable rather than a passive, easy-to-miss bar.
    event.preventDefault();
    deferredInstallPrompt = event as BeforeInstallPromptEvent;
    showInstallButton();
  });

  window.addEventListener("appinstalled", () => {
    deferredInstallPrompt = null;
    hideInstallButton();
  });
}

initPwaInstallPrompt();

export { initPwaInstallPrompt, handleInstallClick, isRunningStandalone, PWA_INSTALL_BUTTON_ID };
