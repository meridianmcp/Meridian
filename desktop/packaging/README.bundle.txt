MeridianDesktop
================

Install this first: the Microsoft Visual C++ Redistributable (x64) 2015-2022.

MeridianDesktop needs the Microsoft Visual C++ Redistributable (x64) 2015-2022 installed
on this computer before it runs. The Visual C++ runtime is NOT included in this folder.

The redistributable is free. Get it from Microsoft:

    https://aka.ms/vs/17/release/vc_redist.x64.exe

Run that installer, then start MeridianDesktop.exe. Microsoft's own licence governs that
download and its use.

The Universal CRT (UCRT: ucrtbase.dll and the api-ms-win-*.dll files) comes with
Windows 10 and later, so you do not need to install it. It is not in this folder either.

If MeridianDesktop does not start and Windows reports a missing VCRUNTIME140.dll or
MSVCP140.dll, install the redistributable above and try again.

The other third-party licences and notices are in NOTICE.md and THIRD_PARTY_LICENSES.txt
in this folder. The licence text for the MeridianDesktop code is in LICENSES\MSL-1.0.txt.
