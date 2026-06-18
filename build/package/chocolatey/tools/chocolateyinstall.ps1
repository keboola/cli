# Chocolatey install script for kbagent. Downloads the signed Windows .exe zip from
# cli-dist.keboola.com and shims `kbagent` onto PATH. {URL} and {CHECKSUM} are
# substituted by the release workflow. No Python runtime required.
$ErrorActionPreference = 'Stop'
$toolsDir = "$(Split-Path -parent $MyInvocation.MyCommand.Definition)"

$packageArgs = @{
  packageName   = 'keboola-cli2'
  unzipLocation = $toolsDir
  url64bit      = '{URL}'
  checksum64    = '{CHECKSUM}'
  checksumType64= 'sha256'
}

Install-ChocolateyZipPackage @packageArgs
# The extracted kbagent.exe in $toolsDir is auto-shimmed onto PATH by Chocolatey.
