$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$packageDir = Join-Path $source 'transfer_packages'
New-Item -ItemType Directory -Force -Path $packageDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$destination = Join-Path $packageDir "vr_test_5090_$stamp.zip"
$excluded = @(
    '.venv',
    '.venv_5090_llm',
    '.venv_5090_tts',
    '.artifact_work',
    '__pycache__',
    'models',
    'third_party',
    'transfer_packages',
    '.git'
)
$items = Get-ChildItem -LiteralPath $source -Force | Where-Object { $_.Name -notin $excluded }
if (-not $items) {
    throw 'No project files found to package.'
}
$items | Compress-Archive -DestinationPath $destination -CompressionLevel Optimal
$sizeMb = [math]::Round((Get-Item -LiteralPath $destination).Length / 1MB, 2)
Write-Host "Transfer ZIP ready: $destination"
Write-Host "Size: $sizeMb MB"
Write-Host 'Excluded: virtual environments, model weights, source dependencies, caches, and temporary files.'
