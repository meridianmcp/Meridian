// Unit tests for the PWA install-prompt affordance (afcbd8a2).
import { describe, expect, it, vi } from "vitest";
import {
  PWA_INSTALL_BUTTON_ID,
  isRunningStandalone,
} from "./dashboard-pwa-install";

// Importing the module above already ran its top-level initPwaInstallPrompt()
// side effect (matchMedia is unimplemented in jsdom, so isRunningStandalone()
// is false at import time and the beforeinstallprompt/appinstalled listeners
// got registered on `window` for the rest of this file).

function makeBeforeInstallPromptEvent() {
  const event = new Event("beforeinstallprompt", { cancelable: true }) as Event & {
    prompt: () => Promise<void>;
    userChoice: Promise<{ outcome: string; platform: string }>;
  };
  event.prompt = vi.fn().mockResolvedValue(undefined);
  event.userChoice = Promise.resolve({ outcome: "accepted", platform: "web" });
  return event;
}

describe("isRunningStandalone", () => {
  it("is false when matchMedia is unimplemented (plain jsdom) and standalone is unset", () => {
    expect(isRunningStandalone()).toBe(false);
  });

  it("is true when the display-mode: standalone media query matches", () => {
    const original = window.matchMedia;
    // jsdom doesn't implement matchMedia at runtime; stub it for this test.
    window.matchMedia = vi.fn().mockReturnValue({ matches: true }) as unknown as typeof window.matchMedia;
    expect(isRunningStandalone()).toBe(true);
    window.matchMedia = original;
  });

  it("is true when navigator.standalone (legacy iOS) is set", () => {
    Object.defineProperty(navigator, "standalone", { value: true, configurable: true });
    expect(isRunningStandalone()).toBe(true);
    Object.defineProperty(navigator, "standalone", { value: undefined, configurable: true });
  });
});

describe("beforeinstallprompt handling", () => {
  it("preventDefault()s the event and shows the install button", () => {
    const event = makeBeforeInstallPromptEvent();
    const preventDefaultSpy = vi.spyOn(event, "preventDefault");

    window.dispatchEvent(event);

    expect(preventDefaultSpy).toHaveBeenCalled();
    const btn = document.getElementById(PWA_INSTALL_BUTTON_ID);
    expect(btn).not.toBeNull();
    expect(btn?.textContent).toBe("Install app");

    btn?.remove();
  });

  it("clicking the button replays the stashed prompt and removes the button", async () => {
    const event = makeBeforeInstallPromptEvent();
    window.dispatchEvent(event);

    const btn = document.getElementById(PWA_INSTALL_BUTTON_ID) as HTMLButtonElement;
    expect(btn).not.toBeNull();

    btn.click();
    // handleInstallClick is async (awaits prompt()/userChoice) — flush microtasks.
    await Promise.resolve();
    await Promise.resolve();

    expect(event.prompt).toHaveBeenCalledTimes(1);
    expect(document.getElementById(PWA_INSTALL_BUTTON_ID)).toBeNull();
  });

  it("does not insert a second button if beforeinstallprompt fires again before a click", () => {
    const first = makeBeforeInstallPromptEvent();
    window.dispatchEvent(first);
    const second = makeBeforeInstallPromptEvent();
    window.dispatchEvent(second);

    const buttons = document.querySelectorAll(`#${PWA_INSTALL_BUTTON_ID}`);
    expect(buttons.length).toBe(1);

    buttons[0].remove();
  });

  it("appinstalled removes the button and clears the stashed prompt", async () => {
    const event = makeBeforeInstallPromptEvent();
    window.dispatchEvent(event);
    expect(document.getElementById(PWA_INSTALL_BUTTON_ID)).not.toBeNull();

    window.dispatchEvent(new Event("appinstalled"));

    expect(document.getElementById(PWA_INSTALL_BUTTON_ID)).toBeNull();
  });
});
