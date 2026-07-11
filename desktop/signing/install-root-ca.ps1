<#
  Trust the World Herb Library code-signing root CA on this machine.

  After this runs, installers signed with the Library Tool signing certificate
  show the real publisher instead of "unknown publisher" and pass SmartScreen's
  publisher check ON THIS MACHINE. It changes nothing for anyone who has not
  run it: self-managed PKI is trust you install, not trust the world already
  has. Public downloaders still see the warning until the build is signed with
  a CA-issued certificate.

  Per-user install (default) needs no admin and trusts the root for you only.
  Pass -Machine to trust it for every user on the box (run from an elevated
  PowerShell).

  Usage:
    powershell -ExecutionPolicy Bypass -File install-root-ca.ps1
    powershell -ExecutionPolicy Bypass -File install-root-ca.ps1 -Machine
#>
param([switch]$Machine)

$ErrorActionPreference = 'Stop'
$cert = Join-Path $PSScriptRoot 'whl-code-root-ca.crt'
if (-not (Test-Path $cert)) { throw "Root cert not found next to this script: $cert" }

# certutil adds to the Trusted Root store without the interactive "install this
# certificate?" GUI prompt that Import-Certificate / the .NET store API raise,
# so this also works over remoting and in non-interactive shells. -user targets
# the current user's store (no admin); omitting it targets the machine store
# (needs elevation).
$args = @('-addstore', '-f', 'Root', $cert)
if (-not $Machine) { $args = @('-user') + $args }

& certutil @args
if ($LASTEXITCODE -ne 0) { throw "certutil failed with exit code $LASTEXITCODE" }

$scope = if ($Machine) { 'the machine (all users)' } else { 'the current user' }
Write-Host "Installed World Herb Library Root CA for $scope." -ForegroundColor Green
Write-Host "Signed Library Tool installers will now validate on this machine."
