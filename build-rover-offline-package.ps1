[CmdletBinding()]
param(
  [ValidatePattern('^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$')]
  [string]$Version = '1.0.0',

  [string]$WheelhousePath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$packageDirectory = Join-Path $projectRoot 'dist-packages'
$stageRoot = Join-Path $packageDirectory '_rover-one-offline-stage'
$offlineDist = Join-Path $projectRoot 'rover-offline\dist'
$runtimeRequirements = Join-Path $projectRoot 'orange_pi\gateway\requirements-runtime.txt'
$archiveName = "ROVER-ONE-local-offline-v$Version.zip"
$archivePath = Join-Path $packageDirectory $archiveName
$archiveHashPath = "$archivePath.sha256"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Assert-ChildPath {
  param([string]$Parent, [string]$Child)

  $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
  $childFull = [System.IO.Path]::GetFullPath($Child)
  if (-not $childFull.StartsWith($parentFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to operate outside the package directory: $childFull"
  }
}

function Copy-DirectoryContents {
  param([string]$Source, [string]$Destination)

  if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "Directory not found: $Source"
  }
  New-Item -ItemType Directory -Path $Destination -Force | Out-Null
  Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
  }
}

function Copy-ProjectFile {
  param([string]$RelativePath)

  $source = Join-Path $projectRoot $RelativePath
  $destination = Join-Path $stageRoot $RelativePath
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "File not found: $source"
  }
  New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
  Copy-Item -LiteralPath $source -Destination $destination -Force
}

Assert-ChildPath -Parent $packageDirectory -Child $stageRoot
Assert-ChildPath -Parent $packageDirectory -Child $archivePath
Assert-ChildPath -Parent $packageDirectory -Child $archiveHashPath

Write-Host '==> Building the ROVER ONE static web app'
Push-Location $projectRoot
try {
  & npm.cmd run build:rover-offline
  if ($LASTEXITCODE -ne 0) {
    throw "Web build failed with exit code $LASTEXITCODE"
  }
}
finally {
  Pop-Location
}

if (-not (Test-Path -LiteralPath (Join-Path $offlineDist 'index.html') -PathType Leaf)) {
  throw "Static build did not create index.html: $offlineDist"
}

New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null
if (Test-Path -LiteralPath $stageRoot) {
  Remove-Item -LiteralPath $stageRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null

Copy-DirectoryContents -Source $offlineDist -Destination (Join-Path $stageRoot 'web')

$runtimeFiles = @(
  'orange_pi\README.md',
  'orange_pi\gateway\app.py',
  'orange_pi\gateway\protocol.py',
  'orange_pi\gateway\safety.py',
  'orange_pi\gateway\serial_link.py',
  'orange_pi\gateway\README.md',
  'orange_pi\gateway\requirements.txt',
  'orange_pi\gateway\requirements-runtime.txt',
  'orange_pi\deploy\create-rover-hotspot.sh',
  'orange_pi\deploy\gateway.env.example',
  'orange_pi\deploy\install-offline.sh',
  'orange_pi\deploy\rollback-offline.sh',
  'orange_pi\deploy\verify-offline.sh',
  'orange_pi\deploy\avahi\rover-one.service',
  'orange_pi\deploy\nginx\rover-one.conf',
  'orange_pi\deploy\systemd\rover-gateway.service'
)
$runtimeFiles | ForEach-Object { Copy-ProjectFile -RelativePath $_ }

Copy-Item -LiteralPath (Join-Path $projectRoot 'rover-offline\PACKAGE-README.md') -Destination (Join-Path $stageRoot 'README.md') -Force
[System.IO.File]::WriteAllText((Join-Path $stageRoot 'VERSION'), "$Version`n", $utf8NoBom)

$licenseFiles = @(
  @{ Source = 'node_modules\react\LICENSE'; Destination = 'licenses\react-LICENSE.txt' },
  @{ Source = 'node_modules\react-dom\LICENSE'; Destination = 'licenses\react-dom-LICENSE.txt' },
  @{ Source = 'node_modules\scheduler\LICENSE'; Destination = 'licenses\scheduler-LICENSE.txt' }
)
foreach ($license in $licenseFiles) {
  $licenseSource = Join-Path $projectRoot $license.Source
  if (Test-Path -LiteralPath $licenseSource -PathType Leaf) {
    $licenseDestination = Join-Path $stageRoot $license.Destination
    New-Item -ItemType Directory -Path (Split-Path -Parent $licenseDestination) -Force | Out-Null
    Copy-Item -LiteralPath $licenseSource -Destination $licenseDestination -Force
  }
}

$wheelhouseDestination = Join-Path $stageRoot 'wheelhouse\py311-linux-aarch64'
New-Item -ItemType Directory -Path $wheelhouseDestination -Force | Out-Null
if ($WheelhousePath) {
  $wheelhouseSource = [System.IO.Path]::GetFullPath($WheelhousePath)
  Write-Host "==> Copying ARM64 wheelhouse: $wheelhouseSource"
  Copy-DirectoryContents -Source $wheelhouseSource -Destination $wheelhouseDestination
}
else {
  $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
  if (-not $pythonCommand) {
    throw 'Python was not found; python -m pip is required to build the ARM64 wheelhouse'
  }
  Write-Host '==> Downloading Debian/Armbian 12, Python 3.11, ARM64 wheels'
  $pipDownloadArguments = @(
    '-m', 'pip', 'download',
    '--disable-pip-version-check',
    '--progress-bar', 'off',
    '--only-binary=:all:',
    '--platform', 'manylinux2014_aarch64',
    '--implementation', 'cp',
    '--python-version', '311',
    '--abi', 'cp311',
    '--dest', $wheelhouseDestination,
    '-r', $runtimeRequirements
  )
  & $pythonCommand.Source @pipDownloadArguments
  if ($LASTEXITCODE -ne 0) {
    throw "ARM64 wheel download failed with exit code $LASTEXITCODE"
  }
}

$wheelNames = Get-ChildItem -LiteralPath $wheelhouseDestination -Filter '*.whl' -File | Select-Object -ExpandProperty Name
foreach ($requiredWheel in @('fastapi-', 'uvicorn-', 'pyserial-', 'pydantic_core-')) {
  if (-not ($wheelNames | Where-Object { $_.StartsWith($requiredWheel, [System.StringComparison]::OrdinalIgnoreCase) })) {
    throw "Wheelhouse is missing: $requiredWheel"
  }
}

$manifest = [ordered]@{
  name = 'ROVER ONE local offline remote control'
  version = $Version
  target = 'Debian/Armbian 12 arm64'
  python = '3.11'
  webRuntime = 'nginx static files'
  gatewayRuntime = 'FastAPI + Uvicorn + pyserial'
  nodeRequiredOnRobot = $false
  createdUtc = [DateTime]::UtcNow.ToString('o')
}
$manifestJson = $manifest | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $stageRoot 'manifest.json'), "$manifestJson`n", $utf8NoBom)

$stageUri = New-Object System.Uri(($stageRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar))
$hashLines = Get-ChildItem -LiteralPath $stageRoot -Recurse -File |
  Sort-Object FullName |
  ForEach-Object {
    $fileUri = New-Object System.Uri($_.FullName)
    $relative = [System.Uri]::UnescapeDataString($stageUri.MakeRelativeUri($fileUri).ToString())
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $relative"
  }
[System.IO.File]::WriteAllLines((Join-Path $stageRoot 'SHA256SUMS'), $hashLines, $utf8NoBom)

foreach ($target in @($archivePath, $archiveHashPath)) {
  if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Force
  }
}

Write-Host "==> Creating archive: $archiveName"
$archiveItems = Get-ChildItem -LiteralPath $stageRoot -Force | Select-Object -ExpandProperty FullName
Compress-Archive -Path $archiveItems -DestinationPath $archivePath -CompressionLevel Optimal

$archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText($archiveHashPath, "$archiveHash  $archiveName`n", $utf8NoBom)

Remove-Item -LiteralPath $stageRoot -Recurse -Force

Write-Host ''
Write-Host 'Offline package created:'
Write-Host "  $archivePath"
Write-Host "  $archiveHashPath"
