# 31a4a9c8 -- PreToolUse dependency/package install verification guard.
# REFILED (original 23f21820 never shipped). Windows partner of
# dependency_install_guard.sh -- same logic, ASCII-only (PS 5.1 reads BOM-less
# UTF-8 as cp1252, so any non-ASCII byte would corrupt and break the parser).
#
# Threat class: the May 2026 CISA/NSA/Five Eyes joint advisory on AI-coding-agent
# supply-chain attacks -- a malicious or typosquatted package gets installed by an
# autonomous agent via pip/npm/uvx and its arbitrary setup/postinstall code runs,
# with no human ever seeing the package name before it lands on disk.
#
# BLOCKS (exit 2, fail-closed) any pip/pip3/py/npm/uvx/pixi/conda/poetry/pipx
# install invocation naming a package NOT already declared in this repo's
# manifests (pyproject.toml, package.json, pixi.toml [dependencies] /
# [pypi-dependencies] and their per-feature tables) or in
# .claude\hooks\verified_packages.txt (a durable allowlist appended to after a
# real registry lookup or explicit request_hitl approval).
#
# 8fae0e17 -- pixi is THIS repo's actual package manager (pixi.toml,
# AGENTS.md/CLAUDE.md) but was entirely unrecognized until this fix: "pixi
# add" names a new package exactly like "npm install <name>"; "pixi install"
# (no package arg) installs from pixi.lock/pixi.toml, exactly like bare
# "npm ci", so it is always allowed. conda install / poetry add / pipx
# install / "py -m pip install" are the same install-verb-plus-package-args
# shape and are recognized too. Bare "pixi run <task>" (e.g. "pixi run test")
# is NOT an install command and is never matched by the pixi pattern below.
#
# Manifest-only installs (-r requirements.txt, bare "npm install"/"npm ci",
# "pixi install", "conda install --file environment.yml", local/editable
# installs) are always allowed.
#
# Best-effort command parsing, not a full shell grammar -- a speed bump for the
# common case, not a sandbox. Fails OPEN on parse ambiguity outside the specific
# "unknown package name" match, which fails CLOSED by design.
# NOT hooks.ps1 (the token-rotation installer).
$ErrorActionPreference = 'SilentlyContinue'

try { $raw = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $raw) { exit 0 }

try { $payload = $raw | ConvertFrom-Json } catch { exit 0 }
if ($null -eq $payload) { exit 0 }

$tool = [string]$payload.tool_name
if (-not $tool) { exit 0 }
if ($tool -ne 'Bash') { exit 0 }

$cmd = $null
if ($payload.tool_input) { $cmd = [string]$payload.tool_input.command }
if (-not $cmd) { exit 0 }

$ScriptDir = $PSScriptRoot
$RepoRoot = Split-Path (Split-Path $ScriptDir -Parent) -Parent
$AllowList = Join-Path $ScriptDir 'verified_packages.txt'
$PyProject = Join-Path $RepoRoot 'pyproject.toml'
$PackageJson = Join-Path $RepoRoot 'package.json'
$PixiToml = Join-Path $RepoRoot 'pixi.toml'

function Normalize-PkgName {
    param([string]$Name)
    if (-not $Name) { return '' }
    return ($Name.ToLower() -replace '[-_.]+', '-')
}

# Package managers/build tools themselves are always safe to (re)install.
$BuiltinKnown = @('pip', 'pip3', 'setuptools', 'wheel', 'pip-tools', 'uv', 'npm', 'npx', 'corepack', 'pixi', 'conda', 'poetry', 'pipx')

$KnownSet = New-Object System.Collections.Generic.HashSet[string]
foreach ($n in $BuiltinKnown) { [void]$KnownSet.Add((Normalize-PkgName $n)) }

if (Test-Path $PyProject) {
    Get-Content $PyProject | ForEach-Object {
        if ($_ -match '^\s*"([A-Za-z0-9][A-Za-z0-9_.\-]*)') {
            [void]$KnownSet.Add((Normalize-PkgName $Matches[1]))
        }
    }
}
if (Test-Path $PackageJson) {
    Get-Content $PackageJson | ForEach-Object {
        if ($_ -match '^\s*"([^"]+)"\s*:\s*"') {
            [void]$KnownSet.Add((Normalize-PkgName $Matches[1]))
        }
    }
}
if (Test-Path $PixiToml) {
    # pixi.toml is real TOML (unlike pyproject.toml's array-of-quoted-strings
    # dependency lists) -- package names are bare, unquoted keys inside
    # specific tables: [dependencies], [pypi-dependencies], and their
    # per-feature counterparts [feature.<name>.dependencies] /
    # [feature.<name>.pypi-dependencies]. Track the current [section] and
    # only collect keys while inside one of those tables, so a top-level key
    # like "name" under [workspace] is never misread as a package name.
    $InDepSection = $false
    Get-Content $PixiToml | ForEach-Object {
        $line = $_
        if ($line -match '^\[(dependencies|pypi-dependencies|feature\.[^\]]+\.dependencies|feature\.[^\]]+\.pypi-dependencies)\]\s*$') {
            $InDepSection = $true
        } elseif ($line -match '^\[') {
            $InDepSection = $false
        } elseif ($InDepSection -and ($line -match '^([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*=')) {
            [void]$KnownSet.Add((Normalize-PkgName $Matches[1]))
        }
    }
}
if (Test-Path $AllowList) {
    Get-Content $AllowList | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) {
            [void]$KnownSet.Add((Normalize-PkgName $line))
        }
    }
}

function Test-Known {
    param([string]$Name)
    $n = Normalize-PkgName $Name
    if (-not $n) { return $true }
    return $KnownSet.Contains($n)
}

function Test-IsFlag {
    param([string]$Tok)
    return $Tok.StartsWith('-')
}

function Test-IsLocalPath {
    param([string]$Tok)
    if ($Tok -eq '.') { return $true }
    if ($Tok.StartsWith('./') -or $Tok.StartsWith('../') -or $Tok.StartsWith('/') -or $Tok.StartsWith('~')) { return $true }
    if ($Tok -match '^[A-Za-z]:') { return $true }
    return $false
}

$PipValueFlags = @('-r', '--requirement', '-c', '--constraint', '-i', '--index-url', '--extra-index-url',
    '-f', '--find-links', '-t', '--target', '--root', '--prefix', '-b', '--build', '--cache-dir', '--log',
    '--proxy', '--retries', '--timeout', '--trusted-host', '--python', '--config-settings')
$NpmValueFlags = @('--registry', '--scope', '--tag', '--save-prefix', '--workspace', '-w', '--prefix',
    '--cache', '--userconfig')
$PixiValueFlags = @('--manifest-path', '-f', '--feature', '--platform', '--host', '--build', '--git',
    '--branch', '--tag', '--rev', '--subdir')
$CondaValueFlags = @('-n', '--name', '-p', '--prefix', '-c', '--channel')
$PoetryValueFlags = @('-G', '--group', '-E', '--extras', '--source', '--python', '--platform')
$PipxValueFlags = @('--index-url', '--python', '--spec', '--pip-args', '--suffix')

$Flagged = $null
$Manager = $null

function Check-PipSegment {
    param([string]$Rest)
    if ($Rest -match '(^|\s)(-r|--requirement)(\s|$)') { return }
    $tokens = $Rest -split '\s+' | Where-Object { $_ -ne '' }
    $skipNext = $false
    foreach ($tok in $tokens) {
        if ($skipNext) { $skipNext = $false; continue }
        if (Test-IsFlag $tok) {
            if ($PipValueFlags -contains $tok) { $skipNext = $true }
            continue
        }
        if (Test-IsLocalPath $tok) { continue }
        $pkgname = ($tok -replace '[<>=!~; ].*', '') -replace '\[.*', ''
        if (-not $pkgname) { continue }
        if (-not (Test-Known $pkgname)) {
            $script:Flagged = $pkgname
            return
        }
    }
}

function Check-NpmSegment {
    param([string]$Rest)
    $tokens = $Rest -split '\s+' | Where-Object { $_ -ne '' }
    $skipNext = $false
    foreach ($tok in $tokens) {
        if ($skipNext) { $skipNext = $false; continue }
        if (Test-IsFlag $tok) {
            if ($NpmValueFlags -contains $tok) { $skipNext = $true }
            continue
        }
        if (Test-IsLocalPath $tok) { continue }
        $pkgname = $tok
        if ($pkgname.StartsWith('@')) {
            $rest2 = $pkgname.Substring(1)
            $slashIdx = $rest2.IndexOf('/')
            if ($slashIdx -ge 0) {
                $scope = $rest2.Substring(0, $slashIdx)
                $after = $rest2.Substring($slashIdx + 1)
                $atIdx = $after.IndexOf('@')
                if ($atIdx -ge 0) { $after = $after.Substring(0, $atIdx) }
                $pkgname = "@$scope/$after"
            }
        } else {
            $atIdx = $pkgname.IndexOf('@')
            if ($atIdx -ge 0) { $pkgname = $pkgname.Substring(0, $atIdx) }
        }
        if (-not $pkgname) { continue }
        if (-not (Test-Known $pkgname)) {
            $script:Flagged = $pkgname
            return
        }
    }
}

function Check-UvxSegment {
    param([string]$Rest)
    $tokens = $Rest -split '\s+' | Where-Object { $_ -ne '' }
    $skipNext = $false
    $fromNext = $false
    $pkg = $null
    foreach ($tok in $tokens) {
        if ($fromNext) { $pkg = $tok; $fromNext = $false; continue }
        if ($skipNext) { $skipNext = $false; continue }
        if (Test-IsFlag $tok) {
            if ($tok -eq '--from') { $fromNext = $true }
            elseif (@('--python', '--index', '--index-url', '--with') -contains $tok) { $skipNext = $true }
            continue
        }
        if (-not $pkg) { $pkg = $tok }
    }
    if (-not $pkg) { return }
    $pkgname = ($pkg -replace '[<>=!~; ].*', '') -replace '\[.*', '' -replace '@.*', ''
    if (-not $pkgname) { return }
    if (-not (Test-Known $pkgname)) {
        $script:Flagged = $pkgname
    }
}

function Check-PixiSegment {
    param([string]$Rest)
    # "pixi add" takes one or more positional package specs, e.g.
    # "pixi add numpy pandas==2.2 --feature dev". There is no manifest-file
    # flag to bypass wholesale (unlike pip's -r) -- every named package is
    # checked, same as npm install/add.
    $tokens = $Rest -split '\s+' | Where-Object { $_ -ne '' }
    $skipNext = $false
    foreach ($tok in $tokens) {
        if ($skipNext) { $skipNext = $false; continue }
        if (Test-IsFlag $tok) {
            if ($PixiValueFlags -contains $tok) { $skipNext = $true }
            continue
        }
        if (Test-IsLocalPath $tok) { continue }
        $pkgname = ($tok -replace '[<>=!~; ].*', '') -replace '\[.*', ''
        if (-not $pkgname) { continue }
        if (-not (Test-Known $pkgname)) {
            $script:Flagged = $pkgname
            return
        }
    }
}

function Check-CondaSegment {
    param([string]$Rest)
    # "conda install --file environment.yml" installs from an already-vetted
    # manifest file, not a new named package -- allow wholesale, same as
    # pip's -r/--requirement bypass.
    if ($Rest -match '(^|\s)--file(\s|$)') { return }
    $tokens = $Rest -split '\s+' | Where-Object { $_ -ne '' }
    $skipNext = $false
    foreach ($tok in $tokens) {
        if ($skipNext) { $skipNext = $false; continue }
        if (Test-IsFlag $tok) {
            if ($CondaValueFlags -contains $tok) { $skipNext = $true }
            continue
        }
        if (Test-IsLocalPath $tok) { continue }
        $pkgname = ($tok -replace '[<>=!~; ].*', '') -replace '\[.*', ''
        if (-not $pkgname) { continue }
        if (-not (Test-Known $pkgname)) {
            $script:Flagged = $pkgname
            return
        }
    }
}

function Check-PoetrySegment {
    param([string]$Rest)
    # "poetry add" takes one or more positional specs, e.g.
    # "poetry add requests@^2.31 --group dev".
    $tokens = $Rest -split '\s+' | Where-Object { $_ -ne '' }
    $skipNext = $false
    foreach ($tok in $tokens) {
        if ($skipNext) { $skipNext = $false; continue }
        if (Test-IsFlag $tok) {
            if ($PoetryValueFlags -contains $tok) { $skipNext = $true }
            continue
        }
        if (Test-IsLocalPath $tok) { continue }
        $pkgname = (($tok -replace '[<>=!~; ].*', '') -replace '\[.*', '') -replace '@.*', ''
        if (-not $pkgname) { continue }
        if (-not (Test-Known $pkgname)) {
            $script:Flagged = $pkgname
            return
        }
    }
}

function Check-PipxSegment {
    param([string]$Rest)
    # "pipx install <spec>" names exactly one tool, same shape as uvx.
    $tokens = $Rest -split '\s+' | Where-Object { $_ -ne '' }
    $skipNext = $false
    $pkg = $null
    foreach ($tok in $tokens) {
        if ($skipNext) { $skipNext = $false; continue }
        if (Test-IsFlag $tok) {
            if ($PipxValueFlags -contains $tok) { $skipNext = $true }
            continue
        }
        if (-not $pkg) { $pkg = $tok }
    }
    if (-not $pkg) { return }
    $pkgname = (($pkg -replace '[<>=!~; ].*', '') -replace '\[.*', '') -replace '@.*', ''
    if (-not $pkgname) { return }
    if (-not (Test-Known $pkgname)) {
        $script:Flagged = $pkgname
    }
}

# Split on &&, ||, ; into segments so each sub-command is inspected independently.
$segments = [regex]::Split($cmd, '&&|\|\||;')

foreach ($seg in $segments) {
    if ($Flagged) { break }
    $segTrim = $seg.Trim()
    if (-not $segTrim) { continue }

    if ($segTrim -match '^((python3?|py)\s+-m\s+)?pip3?\s+install(\s|$)') {
        $Manager = 'pip'
        $restStr = $segTrim -replace '^((python3?|py)\s+-m\s+)?pip3?\s+install\s*', ''
        Check-PipSegment $restStr
    } elseif ($segTrim -match '^npm\s+(install|i|add|ci)(\s|$)') {
        $Manager = 'npm'
        if ($segTrim -match '^npm\s+ci') {
            # `npm ci` installs strictly from the lockfile -- never a new package.
        } else {
            $restStr = $segTrim -replace '^npm\s+(install|i|add|ci)\s*', ''
            Check-NpmSegment $restStr
        }
    } elseif ($segTrim -match '^uvx(\s|$)') {
        $Manager = 'uvx'
        $restStr = $segTrim -replace '^uvx\s*', ''
        Check-UvxSegment $restStr
    } elseif ($segTrim -match '^pixi\s+add(\s|$)') {
        $Manager = 'pixi'
        $restStr = $segTrim -replace '^pixi\s+add\s*', ''
        Check-PixiSegment $restStr
    } elseif ($segTrim -match '^pixi\s+install(\s|$)') {
        # "pixi install" (no package arg -- that's "pixi add" above) installs
        # from pixi.lock/pixi.toml, same as bare "npm ci". Deliberately does
        # NOT match "pixi run ..." (e.g. "pixi run test"), which is not an
        # install command at all.
        $Manager = 'pixi'
    } elseif ($segTrim -match '^conda\s+install(\s|$)') {
        $Manager = 'conda'
        $restStr = $segTrim -replace '^conda\s+install\s*', ''
        Check-CondaSegment $restStr
    } elseif ($segTrim -match '^poetry\s+add(\s|$)') {
        $Manager = 'poetry'
        $restStr = $segTrim -replace '^poetry\s+add\s*', ''
        Check-PoetrySegment $restStr
    } elseif ($segTrim -match '^pipx\s+install(\s|$)') {
        $Manager = 'pipx'
        $restStr = $segTrim -replace '^pipx\s+install\s*', ''
        Check-PipxSegment $restStr
    }
}

if ($Flagged) {
    [Console]::Error.WriteLine(
        "Meridian dependency-install guard (31a4a9c8): BLOCKED $Manager install of unverified package '$Flagged'. " +
        "Per the May 2026 CISA/NSA/Five Eyes supply-chain advisory on malicious AI-agent package installs, an " +
        "unknown package must be verified BEFORE install. Do ONE of: (1) look '$Flagged' up on the official " +
        "registry (PyPI: https://pypi.org/pypi/$Flagged/json or https://www.npmjs.com/package/$Flagged) to " +
        "confirm it is the real, actively-maintained project -- not a typosquat -- then append the exact name " +
        "to .claude\hooks\verified_packages.txt and retry; or (2) call request_hitl(project_id, question) for " +
        "explicit human confirmation, then add it to the allowlist once approved. Packages already declared in " +
        "pyproject.toml/package.json/pixi.toml are pre-approved and never blocked."
    )
    exit 2
}

exit 0
