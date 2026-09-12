$ErrorActionPreference = "Stop"

$smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("tacos-worker-smoke-" + [guid]::NewGuid())
$worker = Join-Path $PSScriptRoot "..\engine-dist\tacos-engine\tacos-engine.exe"

try {
    New-Item -ItemType Directory -Path $smokeRoot | Out-Null
    $source = Join-Path $smokeRoot "inventory.csv"
    Set-Content -Path $source -Value "sku,quantity`nTEST-SKU,1" -Encoding utf8
    $env:TACOS_DESKTOP_DATA_DIR = Join-Path $smokeRoot "appdata"
    $env:TACOS_CREDENTIAL_BACKEND = "sqlite_plaintext"

    $requests = @(
        (@{
            protocolVersion = 1
            id = "import-smoke"
            method = "importSyntheticCsv"
            params = @{ path = $source }
        } | ConvertTo-Json -Compress),
        (@{
            protocolVersion = 1
            id = "shutdown-smoke"
            method = "shutdown"
            params = @{}
        } | ConvertTo-Json -Compress)
    )

    $messages = $requests | & $worker | ForEach-Object { $_ | ConvertFrom-Json }
    $response = $messages | Where-Object { $_.id -eq "import-smoke" -and $_.type -eq "response" }
    if (-not $response.ok -or $response.result.rowsRead -ne 1) {
        throw "Packaged worker failed its CSV import smoke test."
    }
    Write-Output "Packaged worker CSV import passed."
}
finally {
    if (Test-Path -LiteralPath $smokeRoot) {
        Remove-Item -LiteralPath $smokeRoot -Recurse -Force
    }
}
