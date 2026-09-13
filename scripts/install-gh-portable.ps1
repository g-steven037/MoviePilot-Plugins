[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ExistingGh = Get-Command gh.exe -ErrorAction SilentlyContinue
if (-not $ExistingGh) {
    $KnownGh = Join-Path $env:ProgramFiles "GitHub CLI\gh.exe"
    if (Test-Path -LiteralPath $KnownGh) {
        $ExistingGh = Get-Item -LiteralPath $KnownGh
    }
}
if ($ExistingGh) {
    & $ExistingGh.FullName --version
    Write-Host "GitHub CLI is already installed; portable installation is not needed." -ForegroundColor Green
    return
}

$Architecture = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
$Headers = @{ "User-Agent" = "MoviePilot-P115RapidRetry-Installer" }
$Release = Invoke-RestMethod -UseBasicParsing -Headers $Headers -Uri "https://api.github.com/repos/cli/cli/releases/latest"
$ZipAsset = $Release.assets | Where-Object { $_.name -match "^gh_.+_windows_${Architecture}\.zip$" } | Select-Object -First 1
$ChecksumAsset = $Release.assets | Where-Object { $_.name -match "^gh_.+_checksums\.txt$" } | Select-Object -First 1
if (-not $ZipAsset -or -not $ChecksumAsset) {
    throw "The official GitHub CLI release does not contain the expected Windows archive/checksum assets."
}

$TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$Work = Join-Path $TempRoot ("gh-portable-" + [guid]::NewGuid().ToString("N"))
$ZipPath = Join-Path $Work $ZipAsset.name
$ChecksumPath = Join-Path $Work $ChecksumAsset.name
New-Item -ItemType Directory -Path $Work | Out-Null

try {
    Invoke-WebRequest -UseBasicParsing -Headers $Headers -Uri $ZipAsset.browser_download_url -OutFile $ZipPath
    Invoke-WebRequest -UseBasicParsing -Headers $Headers -Uri $ChecksumAsset.browser_download_url -OutFile $ChecksumPath

    $ChecksumLine = Get-Content -LiteralPath $ChecksumPath -Encoding UTF8 | Where-Object { $_ -match "\s$([regex]::Escape($ZipAsset.name))$" } | Select-Object -First 1
    if (-not $ChecksumLine -or $ChecksumLine -notmatch "^([A-Fa-f0-9]{64})\s+") {
        throw "Unable to find the archive SHA-256 in the official checksum file."
    }
    $ExpectedHash = $Matches[1].ToUpperInvariant()
    $ActualHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($ActualHash -ne $ExpectedHash) {
        throw "GitHub CLI archive SHA-256 verification failed."
    }

    $Extracted = Join-Path $Work "extracted"
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $Extracted
    $GhExe = Get-ChildItem -LiteralPath $Extracted -Filter "gh.exe" -File -Recurse | Select-Object -First 1
    if (-not $GhExe) { throw "gh.exe was not found in the verified archive." }

    $Version = $Release.tag_name.TrimStart("v")
    $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\GitHubCLI\$Version\bin"
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Copy-Item -LiteralPath $GhExe.FullName -Destination (Join-Path $InstallDir "gh.exe") -Force

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $Entries = @($UserPath -split [IO.Path]::PathSeparator | Where-Object { $_ })
    if ($InstallDir -notin $Entries) {
        $NewPath = (@($Entries) + $InstallDir) -join [IO.Path]::PathSeparator
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    }
    $env:Path = $env:Path + [IO.Path]::PathSeparator + $InstallDir

    & (Join-Path $InstallDir "gh.exe") --version
    Write-Host "GitHub CLI installed from the verified official release." -ForegroundColor Green
    Write-Host "Open a new PowerShell window, then run: gh auth login" -ForegroundColor Green
}
finally {
    if (Test-Path -LiteralPath $Work) {
        $ResolvedWork = (Resolve-Path -LiteralPath $Work).Path
        if ($ResolvedWork.StartsWith($TempRoot, [StringComparison]::OrdinalIgnoreCase) -and (Split-Path $ResolvedWork -Leaf) -like "gh-portable-*") {
            Remove-Item -LiteralPath $ResolvedWork -Recurse -Force
        }
    }
}
