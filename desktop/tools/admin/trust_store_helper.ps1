<#
.SYNOPSIS
Lists and removes expired certificates from the Windows stores that Python and OpenSSL trust.

.DESCRIPTION
Scaffold for the Meridian admin helper. It is dry-run by default. Removal needs -Apply, a thumbprint of
40 hex characters, and a certificate that is both expired and on the allowlist below. Anything else is
refused. Every action is logged to %LOCALAPPDATA%\meridian\admin\actions.log.

This scaffold has no approval step. The planned design is: a Meridian request_hitl records the exact
action, the owner approves it, and only then does the owner start this script through a UAC prompt. Until
that is wired, the owner runs it from an elevated PowerShell, and -Apply is the only way to change a store.

.EXAMPLE
.\trust_store_helper.ps1 -Action list-expired

.EXAMPLE
.\trust_store_helper.ps1 -Action remove-expired -Thumbprint <40 hex characters>            # dry run
.\trust_store_helper.ps1 -Action remove-expired -Thumbprint <40 hex characters> -Apply    # removes it
#>
param(
    [ValidateSet('list-expired', 'remove-expired')]
    [string]$Action = 'list-expired',
    [string]$Thumbprint = '',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

$LogDir = Join-Path $env:LOCALAPPDATA 'meridian\admin'
$LogFile = Join-Path $LogDir 'actions.log'

# The only subjects this helper will ever touch. Anything else is refused, even when it is expired.
$AllowedSubjects = @('ISRG Root X2', 'DST Root CA X3')

$Stores = @(
    'Cert:\LocalMachine\Root',
    'Cert:\CurrentUser\Root',
    'Cert:\LocalMachine\CA',
    'Cert:\CurrentUser\CA'
)

function Write-HelperLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Add-Content -Path $LogFile -Value ('{0} {1}' -f (Get-Date -Format o), $Message)
    Write-Output $Message
}

function Test-AllowedSubject([string]$Subject) {
    foreach ($name in $AllowedSubjects) {
        if ($Subject -like "*CN=$name*") { return $true }
    }
    return $false
}

function Get-ExpiredCandidates {
    $now = Get-Date
    Get-ChildItem -Path $Stores -ErrorAction SilentlyContinue | Where-Object {
        $_.NotAfter -lt $now -and (Test-AllowedSubject $_.Subject)
    }
}

switch ($Action) {
    'list-expired' {
        $found = @(Get-ExpiredCandidates)
        if ($found.Count -eq 0) {
            Write-HelperLog 'No expired allowlisted certificates found.'
            exit 0
        }
        foreach ($cert in $found) {
            Write-HelperLog ('{0} | {1} | expired {2} | {3}' -f $cert.PSParentPath, $cert.Subject, $cert.NotAfter, $cert.Thumbprint)
        }
        exit 0
    }
    'remove-expired' {
        if ($Thumbprint -notmatch '^[0-9A-Fa-f]{40}$') {
            Write-HelperLog 'Refused: the thumbprint must be 40 hex characters.'
            exit 2
        }
        $wanted = $Thumbprint.ToUpper()
        $matches = @(Get-ExpiredCandidates | Where-Object { $_.Thumbprint -eq $wanted })
        if ($matches.Count -eq 0) {
            Write-HelperLog 'Refused: no expired allowlisted certificate has that thumbprint.'
            exit 3
        }
        foreach ($cert in $matches) {
            $where = '{0} {1} ({2})' -f $cert.PSParentPath, $cert.Subject, $cert.Thumbprint
            if (-not $Apply) {
                Write-HelperLog ('DRY RUN, nothing changed. Would remove {0}' -f $where)
                continue
            }
            Remove-Item -LiteralPath (Join-Path $cert.PSParentPath $cert.Thumbprint)
            Write-HelperLog ('Removed {0}' -f $where)
        }
        exit 0
    }
}
