# Written offer for source code

This offer applies to MeridianDesktop 0.1.0 as distributed in its ONEDIR bundle. It covers
the Qt for Python and Qt components under LGPL-3.0-only that the bundle contains.

## Covered components

- PySide6 6.12.0, PySide6_Essentials 6.12.0, PySide6_Addons 6.12.0 and shiboken6 6.12.0
  (Qt for Python), under LGPL-3.0-only.
- Qt 6.12.0, under LGPL-3.0-only. The bundle holds these Qt libraries as separate DLL files
  in the application folder: Qt6Core.dll, Qt6Gui.dll and Qt6Widgets.dll. It also holds the
  platform plugins platforms/qwindows.dll and platforms/qoffscreen.dll, and the style plugin
  styles/qmodernwindowsstyle.dll. packaging/lgpl_manifest.toml is the authority for this list,
  and packaging/verify_bundle.py checks the bundle against it.

## Where to get the corresponding source

The upstream projects publish the corresponding source at the locations below. Each location
was checked with an HTTP HEAD request (curl -sI) on 2026-10-17.

| Component | Location | Result on 2026-10-17 |
|---|---|---|
| PySide6 and shiboken6 6.12.0, tag v6.12.0 (commit 6aefae25747a8d860d201f3ab2b2e199b9e8e006) | https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.12.0 | HTTP 200 |
| PySide6 and shiboken6 6.12.0, repository | https://code.qt.io/cgit/pyside/pyside-setup.git/ | HTTP 200 |
| Qt 6.12.0, source archive | https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz | HTTP 302 to a mirror, which returns HTTP 200 |
| Qt 6.12.0, SHA-256 of the source archive | https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz.sha256 | HTTP 200. Published value: 98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165 |
| Qt 6.12.0, release directory | https://download.qt.io/official_releases/qt/6.12/6.12.0/ | HTTP 200 |
| Qt 6.12.0, module archives | https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/ | HTTP 200 |

The archive was not downloaded for this check. The published SHA-256 above is the value Qt
publishes next to the archive.

## Written offer

For at least three years from the date you receive this copy of MeridianDesktop, and for as
long as we offer customer support for this version, we will give any third party who asks the
complete corresponding source code for the components above. There is no charge beyond the
cost of a physical copy. This is the written offer under GPL-3.0 section 6(b)(1), which LGPL-3.0
section 4(d)(0) applies to the corresponding source of the Qt components. Send requests to
hello@usemeridian.us.

The upstream locations in the table are also offered as equivalent access to the corresponding
source, under GPL-3.0 section 6(d).

## Host decision

This offer names the upstream locations only. It does not name a Meridian-hosted copy of the Qt
or PySide6 source. The components are used unmodified: this project does not patch PySide6 or Qt,
and packaging/build.py only deletes files that packaging/lgpl_manifest.toml forbids. The upstream
archives are public and were checked above, so the offer does not depend on a copy hosted by
Meridian. If the owner later hosts a copy, add its URL to the table and check it the same way.

## Replacing the Qt libraries

Qt is shipped as separate DLL files, not linked into MeridianDesktop.exe. You may replace them
with a compatible build of the same Qt version, as LGPL-3.0 permits. Run MeridianDesktop from the
folder that holds the replacement files.

## Licence texts in the bundle

- LICENSES/LGPL-3.0.txt, the LGPL-3.0 text.
- LICENSES/GPL-3.0.txt, the GPL-3.0 text, which LGPL-3.0 section 4 refers to.
- THIRD_PARTY_LICENSES.txt, in the bundle root. Its PySide6 and shiboken6 sections give the elected
  licence and the source offer reference.

## Scope of the offer

This offer also covers any other component that THIRD_PARTY_LICENSES.txt lists under LGPL-3.0-only
in this release. The table above covers the components shipped in MeridianDesktop 0.1.0.
