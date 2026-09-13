[CmdletBinding()]
param(
    [switch]$RecreateEnvironment
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Venv = Join-Path $Root ".publish-venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

Push-Location $Root
try {
    $SystemPython = Get-Command python.exe -ErrorAction SilentlyContinue
    $PythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $SystemPython -and -not $PythonLauncher) {
        throw "Python 3.12+ was not found. Install Python and ensure python.exe or py.exe is on PATH."
    }

    $ScannerPython = if ($SystemPython) { $SystemPython.Source } else { $PythonLauncher.Source }
    $ScannerArgs = if ($SystemPython) { @("tools/security_scan.py") } else { @("-3", "tools/security_scan.py") }
    Invoke-Checked $ScannerPython $ScannerArgs

    if ($RecreateEnvironment -and (Test-Path -LiteralPath $Venv)) {
        $ResolvedVenv = (Resolve-Path -LiteralPath $Venv).Path
        if (-not $ResolvedVenv.StartsWith($Root + [IO.Path]::DirectorySeparatorChar)) {
            throw "Refusing to remove a virtual environment outside the repository."
        }
        Remove-Item -LiteralPath $ResolvedVenv -Recurse -Force
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        if ($SystemPython) {
            Invoke-Checked $SystemPython.Source @("-m", "venv", $Venv)
        } else {
            Invoke-Checked $PythonLauncher.Source @("-3", "-m", "venv", $Venv)
        }
    }

    Invoke-Checked $VenvPython @("-m", "pip", "install", "--disable-pip-version-check", "-r", "plugins.v2/p115rapidretry/requirements.txt")
    Invoke-Checked $VenvPython @("-m", "compileall", "-q", "plugins.v2", "plugins.v3", "tests", "tools")
    Invoke-Checked $VenvPython @("tests/run_all.py")
    Invoke-Checked $VenvPython @("tools/security_scan.py")

    $Metadata = Get-Content "package.v2.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $PackageVersion = $Metadata.P115RapidRetry.version
    $CodeMatch = Select-String -Path "plugins.v2/p115rapidretry/__init__.py" -Pattern 'plugin_version = "([^"]+)"'
    $CodeVersion = $CodeMatch.Matches.Groups[1].Value
    if ($PackageVersion -ne $CodeVersion) {
        throw "Version mismatch: code=$CodeVersion package=$PackageVersion"
    }
    if (Test-Path -LiteralPath "package.v3.json") {
        $V3Metadata = Get-Content "package.v3.json" -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($PluginId in @("VarietySubscribeAssistant", "SubscribeLinkRenamer")) {
            $V3Entry = $V3Metadata.$PluginId
            if (-not $V3Entry -or $V3Entry.v3 -ne $true -or $V3Entry.system_version -ne ">=3.0.0") {
                throw "Invalid V3 manifest entry: $PluginId"
            }
            $V3Path = if ($PluginId -eq "VarietySubscribeAssistant") {
                "plugins.v3/varietysubscribeassistant/__init__.py"
            } else {
                "plugins.v3/subscribelinkrenamer/__init__.py"
            }
            $V3CodeMatch = Select-String -Path $V3Path -Pattern 'plugin_version = "([^"]+)"'
            $V3CodeVersion = $V3CodeMatch.Matches.Groups[1].Value
            if ($V3Entry.version -ne $V3CodeVersion) {
                throw "V3 version mismatch for ${PluginId}: code=$V3CodeVersion package=$($V3Entry.version)"
            }
        }
    }
    Write-Host "Verification passed for plugin version $CodeVersion." -ForegroundColor Green
}
finally {
    Pop-Location
}
