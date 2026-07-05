# Meridian Android (TWA) — sideload-only APK prep  ·  sprint item `56860f5d`

This directory is **prep only**. It seeds a separate `meridian-android` repo (copy
these files there, or build in place). It wraps the existing Meridian dashboard PWA
(`https://usemeridian.us`) in a minimal Android APK using Google's
[bubblewrap](https://github.com/GoogleChromeLabs/bubblewrap) TWA (Trusted Web Activity)
generator.

**Scope (per item `56860f5d`): sideload-only, for Adam's personal phone.**
- ❌ **SKIP** the Play Store listing.
- ❌ **SKIP** the $25 Google Play developer registration fee.
- ❌ **SKIP** Play App Signing (Google-managed keys). We sign locally with our own keystore.
- ✅ Produce a locally-signed release APK and `adb install` / manually sideload it.

### What is done vs. what is the HUMAN's step

| Done here (executor-completable, in this repo) | Remains the HUMAN's step (needs real JDK + Android SDK + a device) |
| --- | --- |
| `twa-manifest.json` — populated from the **real** `meridian/static/manifest.webmanifest` | Install JDK 17 + Android SDK cmdline-tools (see Prerequisites) |
| `.well-known/assetlinks.json` — template keyed to the release package, with a fingerprint placeholder | Generate the keystore (`keytool -genkeypair …`) |
| This README — exact commands + signing + sideload instructions | Run `bubblewrap init` + `bubblewrap build` (produces the signed APK) |
| — | Read the SHA-256 fingerprint and paste it into `assetlinks.json` |
| — | `adb install` / sideload the APK onto the phone |

Nothing below was executed by the executor — `bubblewrap`, `gradle`, `keytool`, and
`adb` are not installed in the prep environment and require the maintainer's toolchain.

---

## Values baked into `twa-manifest.json`

Every value is grounded in the live PWA manifest
(`meridian/static/manifest.webmanifest`, served at `/manifest.webmanifest`). Do **not**
hand-edit these to diverge from the web manifest.

| twa-manifest field | Value | Source |
| --- | --- | --- |
| `packageId` | `us.usemeridian.twa` | reverse-DNS of `usemeridian.us` + `.twa` |
| `host` | `usemeridian.us` | production origin (AGENTS.md / CLAUDE.local.md) |
| `name` / `launcherName` | `Meridian` | manifest `name` / `short_name` |
| `themeColor` | `#4a9eff` | manifest `theme_color` |
| `backgroundColor` | `#0d0f12` | manifest `background_color` |
| `display` | `standalone` | manifest `display` |
| `startUrl` | `https://usemeridian.us/dashboard` | manifest `start_url` = `/dashboard` |
| `iconUrl` | `https://usemeridian.us/static/icon-512.png` | manifest 512 icon (`purpose: any`) |
| `maskableIconUrl` | `https://usemeridian.us/static/icon-512.png` | manifest 512 icon (`purpose: maskable`) |
| `webManifestUrl` | `https://usemeridian.us/manifest.webmanifest` | server root route (`server.py` `web_manifest`) |
| `fallbackType` | `customtabs` | Custom Tabs fallback when Chrome/TWA unavailable |

**Assumptions / notes (nothing fabricated):**
- The web manifest has **no** `orientation` field → `twa-manifest.json` uses `"default"`.
- The web manifest has **no** app shortcuts → `shortcuts: []`.
- The web manifest has a dedicated 192px icon (`/static/icon-192.png`) but bubblewrap
  derives all launcher densities from the single high-res `iconUrl` (512px), so the 192
  icon is intentionally not referenced separately.
- `themeColorDark` / `navigationColor*` are set to the manifest `background_color`
  (`#0d0f12`) since the web manifest defines no separate dark/nav colors; adjust to taste.
- `appVersionName` = `1`, `appVersionCode` = `1` — first build. Bump `appVersionCode`
  (integer, must increase) on every subsequent APK you install.

---

## Prerequisites (HUMAN — one-time toolchain setup)

All of these need the maintainer's machine; none are done by the executor.

1. **JDK 17** (Temurin/Adoptium recommended). Verify: `java -version` → `17.x`.
2. **Android SDK cmdline-tools** — either full Android Studio, or standalone
   `commandlinetools`. bubblewrap can auto-download/manage an SDK on first `init` if you
   let it. Ensure `ANDROID_HOME` (or `ANDROID_SDK_ROOT`) points at the SDK if you supply
   your own.
3. **Node.js 18+** (for the bubblewrap CLI).
4. **bubblewrap CLI**:
   ```bash
   npm i -g @bubblewrap/cli
   ```
5. **adb** (Android Platform Tools) — only needed for the USB `adb install` sideload path.

---

## Build steps (HUMAN)

Run these from a fresh working copy of the `meridian-android` repo (or from this
`android/` directory after copying it out).

### 1. Generate the signing keystore (HUMAN)

Local, self-managed key — **not** Play App Signing. Keep this keystore + password safe;
losing it means you cannot ship an update that upgrades in place.

```bash
keytool -genkeypair \
  -v \
  -keystore ./android.keystore \
  -alias meridian \
  -keyalg RSA \
  -keysize 2048 \
  -validity 10000 \
  -storetype PKCS12 \
  -dname "CN=Meridian, O=Meridian, C=US"
```

`-keystore ./android.keystore` and `-alias meridian` match the `signingKey` block in
`twa-manifest.json`. You will be prompted for a keystore password (and key password —
use the same for PKCS12). Do **not** commit the keystore or its password.

### 2. Initialize the TWA project (HUMAN)

Two equivalent options — pick one:

**Option A — from the live web manifest (bubblewrap fetches it):**
```bash
bubblewrap init --manifest https://usemeridian.us/manifest.webmanifest
```
Then, when prompted, accept the defaults but override to match this repo's values
(packageId `us.usemeridian.twa`, launcher name `Meridian`, signing key
`./android.keystore` alias `meridian`). Easiest is to overwrite the generated
`twa-manifest.json` with the one in this directory and re-run step 3.

**Option B — from the checked-in manifest (recommended; deterministic):**
```bash
# from inside android/ (or wherever you copied twa-manifest.json)
bubblewrap init --manifest ./twa-manifest.json
```
This uses the exact, reviewed values in `twa-manifest.json` — no interactive drift.

### 3. Build the signed APK (HUMAN)

```bash
bubblewrap build
```

When prompted for the keystore/key password, supply the ones from step 1. Output:

- **`app-release-signed.apk`** — the locally-signed release APK to sideload.
- (`app-release-bundle.aab` is also produced but is only needed for the Play Store,
  which we are skipping — ignore it.)

### 4. Get the SHA-256 fingerprint for assetlinks (HUMAN)

Needed to fill in `.well-known/assetlinks.json` (see below). Either:

```bash
bubblewrap fingerprint list
```
or directly via keytool:
```bash
keytool -list -v -keystore ./android.keystore -alias meridian
```
Copy the **SHA256** certificate fingerprint (the colon-separated hex, e.g.
`AB:CD:…:EF`).

---

## Sideload onto the phone (HUMAN)

**USB / adb path:**
```bash
adb install ./app-release-signed.apk
# to replace an already-installed build:
adb install -r ./app-release-signed.apk
```

**Manual path (no cable):** transfer `app-release-signed.apk` to the phone (email,
Drive, USB file copy), tap it in the Files app, and allow **"Install unknown apps"** for
whichever app is opening it when Android prompts.

---

## Digital Asset Links — `.well-known/assetlinks.json`

A TWA runs **full-screen (no browser address bar)** only if the target domain publishes a
Digital Asset Links file that verifies this app owns the domain. Without it, the app still
works and opens the dashboard — it just shows a thin Custom-Tabs address bar at the top.
For a sideload-only, single-phone build that is acceptable; wiring assetlinks is an
**optional follow-up**.

To enable full-screen:

1. Build + read the release keystore's **SHA-256 fingerprint** (step 4 above).
2. Paste it into `.well-known/assetlinks.json` in place of
   `<SHA256_FINGERPRINT_PLACEHOLDER>` (the `package_name` is already
   `us.usemeridian.twa`).
3. Serve that file at **`https://usemeridian.us/.well-known/assetlinks.json`** with
   `Content-Type: application/json`.

### Server route to serve assetlinks (OUT OF SCOPE — do NOT add here)

The Meridian server (`meridian/server.py`) currently has **no**
`/.well-known/assetlinks.json` route. Adding one is a **separate sprint item** and is
intentionally not done in this prep. For reference, the minimal addition would mirror the
existing `web_manifest` handler — a `@app.get("/.well-known/assetlinks.json")` route
returning the JSON with `media_type="application/json"`, plus adding the path to the
unauthenticated allowlist near `server.py` line 802 (the tuple that already whitelists
`/manifest.webmanifest`, `/sw.js`, and the `/.well-known/oauth-*` paths). **Do not add it
as part of this item.**

---

## Recap

- Prep artifacts (`twa-manifest.json`, `assetlinks.json` template, this README) are
  complete and grounded in the real PWA manifest.
- The actual build (JDK 17 + Android SDK, keystore generation, `bubblewrap build`,
  fingerprint read, and device install) is the **maintainer's** step — it cannot be done
  in the executor environment.
