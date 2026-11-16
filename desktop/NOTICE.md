# Third-party notices

MeridianDesktop, wave 1 (0.1.0). This file lists the third-party software in the ONEDIR bundle,
with its licence and the bundle path of its licence text. Bundle paths are relative to the bundle
root, `dist/MeridianDesktop`. packaging/build.py writes THIRD_PARTY_LICENSES.txt at the bundle root.
That file has one section per bundled distribution and per native runtime, and each section starts
with "== name version (SPDX) ==". The build fails if a listed item has no licence text or no version,
or if a GPL-only or AGPL licence is found.

## Qt for Python and Qt: LGPL-3.0-only (elected)

| Name | Version | SPDX | Source | Licence text in the bundle |
|---|---|---|---|---|
| PySide6 | 6.12.0 | LGPL-3.0-only (elected) | https://code.qt.io/cgit/pyside/pyside-setup.git/ | `LICENSES/LGPL-3.0.txt`. The distribution's own file is `_internal/pyside6-6.12.0.dist-info/licenses/LicenseRef-Qt-Commercial.txt`, which is not the elected licence. |
| PySide6_Essentials | 6.12.0 | LGPL-3.0-only (elected) | https://code.qt.io/cgit/pyside/pyside-setup.git/ | `LICENSES/LGPL-3.0.txt`. The distribution's own file is `_internal/pyside6_essentials-6.12.0.dist-info/licenses/LicenseRef-Qt-Commercial.txt`, which is not the elected licence. |
| PySide6_Addons | 6.12.0 | LGPL-3.0-only (elected) | https://code.qt.io/cgit/pyside/pyside-setup.git/ | `LICENSES/LGPL-3.0.txt`. The distribution's own file is `_internal/pyside6_addons-6.12.0.dist-info/licenses/LicenseRef-Qt-Commercial.txt`, which is not the elected licence. |
| shiboken6 | 6.12.0 | LGPL-3.0-only (elected) | https://code.qt.io/cgit/pyside/pyside-setup.git/ | `LICENSES/LGPL-3.0.txt`. The distribution's own file is `_internal/shiboken6-6.12.0.dist-info/licenses/LicenseRef-Qt-Commercial.txt`, which is not the elected licence. |
| Qt | 6.12.0 | LGPL-3.0-only (elected) | https://download.qt.io/official_releases/qt/6.12/6.12.0/ | `LICENSES/LGPL-3.0.txt` |

- Election: this project uses PySide6 and Qt under LGPL-3.0-only only. It does not elect
  GPL-2.0-only, GPL-3.0-only, or the Qt commercial licence.
- Upstream licence metadata, verbatim, for the four PySide and shiboken distributions:
  `License: LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`, with
  `License-File: LicenseRef-Qt-Commercial.txt`. The file LicenseRef-Qt-Commercial.txt is shipped by
  the upstream distributions and does not apply to this project.
- Licence texts: `LICENSES/LGPL-3.0.txt`. The GPL-3.0 text, which LGPL-3.0 section 4 refers to, is
  `LICENSES/GPL-3.0.txt`.
- Linking: Qt is shipped as separate, dynamically linked DLL files in the ONEDIR bundle, so a user
  can replace them. The app is folder-mode only.
- Qt libraries in the bundle: Qt6Core.dll, Qt6Gui.dll and Qt6Widgets.dll, with the plugins
  platforms/qwindows.dll, platforms/qoffscreen.dll and styles/qmodernwindowsstyle.dll. The
  PySide6 and shiboken6 binaries are in `_internal/PySide6/` and `_internal/shiboken6/`. They are
  PySide6/QtCore.pyd, QtGui.pyd, QtWidgets.pyd, pyside6.abi3.dll, shiboken6/Shiboken.pyd and
  shiboken6.abi3.dll.
- The two extension binaries, `pyside6.abi3.dll` (in `_internal/PySide6/`) and `shiboken6.abi3.dll` (in
  `_internal/shiboken6/`), are the runtime "Qt for Python extension binaries" in `packaging/native_runtimes.toml`.
  That entry is LGPL-3.0-only and pins each file by its path.
- Source offer: see `OFFER-SOURCE.md`, which names the upstream source locations.
- Dev environment only: PySide6_WebEngine 6.12.0.140 and PySide6_Pdf 6.12.0.140 are installed as
  PySide6 requirements. Their modules are excluded from the bundle, and their DLLs are pruned.

## Windows system libraries: icuuc.dll and icuin.dll

- `icuuc.dll` is imported by `Qt6Core.dll`. The import table was read with pefile. The DLL is not
  bundled. It is provided by Windows 10 version 1703 and later, as part of the Windows ICU libraries.
- `icuin.dll` is the other Windows ICU library, and it is provided by Windows 10 version 1703 and
  later. It is not bundled. A byte search of `Qt6Core.dll` finds the name `icuuc.dll` but not
  `icuin.dll`, so `icuin.dll` is documented here as part of the Windows ICU pair, not as a direct
  import of Qt6Core.dll.

## Native runtimes

Each runtime's licence text is in THIRD_PARTY_LICENSES.txt, in the section whose header is shown
below. The source texts are in `packaging/licenses/native/` and are pinned in
`packaging/licenses/SHA256SUMS`. They are the licence files of the same conda packages that the pixi
environment was built from. Versions come from the conda-meta files of that environment.

| Runtime | Version | SPDX | Files in the bundle | Source (upstream licence page) | Licence text in the bundle |
|---|---|---|---|---|---|
| CPython | 3.12.15 | PSF-2.0 | python312.dll, python3.dll and 16 extension modules (.pyd) | https://docs.python.org/3/license.html | `THIRD_PARTY_LICENSES.txt`, section `== CPython 3.12.15 (PSF-2.0) ==` |
| OpenSSL | 3.6.5 | Apache-2.0 | libcrypto-3-x64.dll, libssl-3-x64.dll | https://openssl-library.org/source/license/index.html | `THIRD_PARTY_LICENSES.txt`, section `== OpenSSL 3.6.5 (Apache-2.0) ==` |
| libffi | 3.7.0 | MIT | ffi-8.dll | https://github.com/libffi/libffi/blob/master/LICENSE | `THIRD_PARTY_LICENSES.txt`, section `== libffi 3.7.0 (MIT) ==` |
| expat | 2.8.5 | MIT | libexpat.dll | https://github.com/libexpat/libexpat/blob/master/expat/COPYING | `THIRD_PARTY_LICENSES.txt`, section `== expat 2.8.5 (MIT) ==` |
| bzip2 | 1.0.8 | bzip2-1.0.6 | libbz2.dll | https://gitlab.com/bzip2/bzip2 | `THIRD_PARTY_LICENSES.txt`, section `== bzip2 1.0.8 (bzip2-1.0.6) ==` |
| XZ Utils | 5.8.3 | 0BSD | liblzma.dll | https://github.com/tukaani-project/xz/blob/master/COPYING.0BSD | `THIRD_PARTY_LICENSES.txt`, section `== XZ Utils 5.8.3 (0BSD) ==` |
| zlib | 1.3.2 | Zlib | zlib.dll | https://zlib.net/zlib_license.html | `THIRD_PARTY_LICENSES.txt`, section `== zlib 1.3.2 (Zlib) ==` |
| SQLite | 3.54.0 | blessing (public domain dedication) | sqlite3.dll (library for the `_sqlite3` module) | https://www.sqlite.org/copyright.html | `THIRD_PARTY_LICENSES.txt`, section `== SQLite 3.54.0 (blessing) ==` |
| Qt for Python extension binaries | 6.12.0 | LGPL-3.0-only (elected) | pyside6.abi3.dll (`_internal/PySide6/`), shiboken6.abi3.dll (`_internal/shiboken6/`) | https://www.qt.io/development/qt-framework/qt-licensing | `LICENSES/LGPL-3.0.txt`. THIRD_PARTY_LICENSES.txt section `== Qt for Python extension binaries 6.12.0 (LGPL-3.0-only) ==` |

- The CPython text is the PSF licence, version 2. conda-forge labels the same package `Python-2.0`.
  This file uses the SPDX identifier PSF-2.0 for the text.
- The full file list, and the sha256 of each bundled copy keyed by its path relative to the bundle root,
  are in `packaging/native_runtimes.toml`. The build fails if a listed file is missing from the bundle,
  if a bundled copy of a listed file has no sha256 for its path, or if a native runtime DLL in the bundle
  is not listed there. A bundled copy whose sha256 differs from the value declared for its path is printed
  as RELEASE BLOCKED.
- SQLite 3.54.0 is dedicated to the public domain by its authors. Its licence text is the copyright statement
  at https://www.sqlite.org/copyright.html, fetched on 2026-10-19 and pinned as
  `packaging/licenses/native/sqlite.txt`. That text is not a conda package file. The version is from the
  `libsqlite-3.54.0` conda-meta record and the DLL's file version. conda-forge records the licence as the
  SPDX identifier `blessing`, which `packaging/lgpl_manifest.toml` allows for native runtimes.

## Prerequisites

These two Microsoft components are not in the bundle. The build deletes them, and verify_bundle.py fails
if any copy is present. Install them on the computer before you run MeridianDesktop.

- **Microsoft Visual C++ 2015-2022 Redistributable (x64).** It provides the Visual C++ runtime: MSVCP140.dll,
  MSVCP140_1.dll, MSVCP140_2.dll, VCRUNTIME140.dll and VCRUNTIME140_1.dll. Install it from Microsoft at
  https://aka.ms/vs/17/release/vc_redist.x64.exe. Microsoft's own licence governs that download and its use.
  The upstream page is https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files.
- **Universal CRT (UCRT).** It provides ucrtbase.dll and the api-ms-win-*.dll files. It comes with Windows 10
  and later, so it needs no install. The upstream page is
  https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment.

The file `packaging/README.bundle.txt` is copied into the bundle as `README.txt`. It tells the user to install
the redistributable first.

## Other bundled Python distributions

Each licence file is in the bundle inside its own dist-info folder, and in THIRD_PARTY_LICENSES.txt
under the section header shown.

| Distribution | Version | SPDX | Source | Licence text in the bundle |
|---|---|---|---|---|
| keyring | 25.7.0 | MIT | https://github.com/jaraco/keyring | `_internal/keyring-25.7.0.dist-info/licenses/LICENSE` |
| jaraco.classes | 3.4.0 | MIT | https://github.com/jaraco/jaraco.classes | `_internal/jaraco.classes-3.4.0.dist-info/LICENSE` |
| jaraco.functools | 4.6.0 | MIT | https://github.com/jaraco/jaraco.functools | `_internal/jaraco_functools-4.6.0.dist-info/licenses/LICENSE` |
| jaraco.context | 6.1.2 | MIT | https://github.com/jaraco/jaraco.context | `_internal/jaraco_context-6.1.2.dist-info/licenses/LICENSE` |
| more-itertools | 11.2.1 | MIT | https://github.com/more-itertools/more-itertools | `_internal/more_itertools-11.2.1.dist-info/licenses/LICENSE` |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | https://github.com/enthought/pywin32-ctypes | `_internal/pywin32_ctypes-0.2.3.dist-info/LICENSE.txt` |
| certifi | 2026.10.23 | MPL-2.0 | https://github.com/certifi/python-certifi | `_internal/certifi-2026.10.23.dist-info/licenses/LICENSE` |

The Qt-family distributions (PySide6, PySide6_Essentials, PySide6_Addons, shiboken6) are listed in the
Qt section above.

- setuptools is not bundled. The build excludes it, because nothing the app or its direct
  dependencies imports it.
- importlib_metadata is not bundled. The locked Python 3.12 environment does not contain it, and
  keyring requires it only below Python 3.12. It is not listed above because it is not in the bundle.

## Build tool: PyInstaller 6.22.3 (bootloader and runtime exception)

PyInstaller is used to build the bundle. Its package is not bundled, but two parts of its code are embedded
in `MeridianDesktop.exe`, under two licences that PyInstaller's COPYING.txt states:

- The bootloader and the boot module (`pyiboot01_bootstrap`) are GPL-2.0-or-later WITH Bootloader-exception
  (SPDX). The Bootloader Exception gives unlimited permission to link or embed the compiled bootloader into
  programs distributed without restriction from those files, so the program may carry its own licence.
- The run-time hooks and fake modules (`_pyi_rth_*`, `pyi_rth_*`, `pyi_splash*`) are Apache-2.0. Apache-2.0
  section 4 applies to them: a copy of the licence is included below, and no NOTICE text is attributed to them.

The licence file is `COPYING.txt`, from the PyInstaller 6.22.3 distribution
(https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/COPYING.txt). THIRD_PARTY_LICENSES.txt includes
the bootloader section "PyInstaller 6.22.3 (GPL-2.0-or-later WITH Bootloader-exception)" and the run-time
hooks section "PyInstaller 6.22.3 (Apache-2.0)", with the Apache-2.0 text. The build fails if this file does
not name PyInstaller and both licences.

## Desktop code and the repository licence

The repository's LICENSE file is Meridian Source License 1.0 (MSL-1.0). A copy of that text is in the
bundle as `LICENSES/MSL-1.0.txt`. MSL-1.0 section 6 requires the licence text to be retained on
redistribution. The copy in the bundle is byte-identical to the repository's LICENSE file, which
packaging/build.py checks.

## meridian-docs and meridian-latex

- meridian-docs (MIT) is not used in wave 1. If a later wave bundles it, its MIT notice and copyright
  line must be added here.
- meridian-latex is excluded and not bundled. The desktop does not include, copy, vendor or bundle any
  meridian-latex code. A later wave may connect to a user-installed engine over loopback. That component
  is covered by its own licence, which is not reproduced here.

## Licence texts shipped in the bundle

- `LICENSES/LGPL-3.0.txt`: LGPL-3.0. Its SHA-256 is pinned. Its provenance against the FSF copy is not
  verified in this change.
- `LICENSES/GPL-3.0.txt`: GPL-3.0, from https://www.gnu.org/licenses/gpl-3.0.txt, fetched on 2026-10-17.
  Its SHA-256 (3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986) is pinned.
- `LICENSES/MSL-1.0.txt`: the repository's MSL-1.0 licence text.
- `THIRD_PARTY_LICENSES.txt`: every licence file listed above, by section.

## Open items

- Repository licence: the LICENSE file is MSL-1.0, but EULA.txt section 1 says the repository licence
  is still to be decided. The owner must reconcile the two.
- Counsel review: EULA.txt and OFFER-SOURCE.md carry the marker "DRAFT - NOT FOR RELEASE". Counsel
  removes it after review. packaging/build.py --release refuses to build while it is present.
- Source offer host: the owner must confirm the upstream-only host decision in OFFER-SOURCE.md.
- LICENSES/LGPL-3.0.txt provenance against the FSF copy is not verified.
- PyInstaller licences: settled from PyInstaller's COPYING.txt. The bootloader and boot module carry the
  Bootloader Exception (GPL-2.0-or-later WITH Bootloader-exception). The run-time hooks and fake modules are
  Apache-2.0. The earlier label "PyInstaller-exception" was wrong and has been removed.
