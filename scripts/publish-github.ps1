[CmdletBinding()]
param(
    [string]$RepoName = "moviepilot-115-rapid-retry",
    [string]$Owner = "",
    [ValidateSet("public", "private", "internal")]
    [string]$Visibility = "public",
    [string]$Description = "MoviePilot V2 115秒传安全重试插件",
    [string]$CommitMessage = "release: secure MoviePilot 115 rapid retry plugin",
    [switch]$RecreateTestEnvironment,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

if ($RepoName -notmatch '^[A-Za-z0-9._-]+$') {
    throw "RepoName contains unsupported characters."
}

$Git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $Git) { $Git = Get-Command git -ErrorAction SilentlyContinue }
if (-not $Git) {
    $KnownGitPaths = @(
        (Join-Path $env:ProgramFiles "Git\cmd\git.exe"),
        (Join-Path $env:ProgramFiles "Git\bin\git.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe")
    )
    foreach ($KnownGit in $KnownGitPaths) {
        if (Test-Path -LiteralPath $KnownGit) {
            $Git = Get-Item -LiteralPath $KnownGit
            break
        }
    }
}
$Gh = Get-Command gh.exe -ErrorAction SilentlyContinue
if (-not $Gh) { $Gh = Get-Command gh -ErrorAction SilentlyContinue }
if (-not $Gh) {
    $KnownGh = Join-Path $env:ProgramFiles "GitHub CLI\gh.exe"
    if (Test-Path -LiteralPath $KnownGh) { $Gh = Get-Item -LiteralPath $KnownGh }
}
if (-not $Gh) {
    $PortableGh = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs\GitHubCLI") -Filter "gh.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($PortableGh) { $Gh = $PortableGh }
}
if (-not $Git) { throw "Git was not found on PATH." }
if (-not $Gh) {
    throw "GitHub CLI was not found. Try winget --source winget, or run scripts/install-gh-portable.ps1."
}

Push-Location $Root
try {
    & (Join-Path $PSScriptRoot "verify.ps1") -RecreateEnvironment:$RecreateTestEnvironment
    if ($LASTEXITCODE -ne 0) { throw "Verification failed." }

    Invoke-Checked $Gh.Source @("auth", "status", "--hostname", "github.com")
    if (-not $Owner) {
        $Owner = (& $Gh.Source api user --jq ".login").Trim()
        if ($LASTEXITCODE -ne 0 -or -not $Owner) { throw "Unable to determine the authenticated GitHub owner." }
    }
    if ($Owner -notmatch '^[A-Za-z0-9-]+$') { throw "Owner contains unsupported characters." }
    $FullName = "$Owner/$RepoName"

    if ($DryRun) {
        Write-Host "Dry run passed. Would publish $FullName as $Visibility." -ForegroundColor Yellow
        return
    }

    Invoke-Checked $Gh.Source @("auth", "setup-git")
    $PreviousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Git.Source -C $Root rev-parse --is-inside-work-tree *> $null
        $RepoStatus = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousPreference
    }
    if ($RepoStatus -ne 0) {
        Invoke-Checked $Git.Source @("-C", $Root, "init", "-b", "main")
    }
    Invoke-Checked $Git.Source @("-C", $Root, "add", "--all")
    Invoke-Checked $Git.Source @("-C", $Root, "diff", "--cached", "--check")

    $ErrorActionPreference = "Continue"
    try {
        & $Git.Source -C $Root diff --cached --quiet
        $HasStagedChanges = $LASTEXITCODE -ne 0
    } finally {
        $ErrorActionPreference = $PreviousPreference
    }
    if ($HasStagedChanges) {
        $UserName = [string](& $Git.Source -C $Root config user.name)
        $UserEmail = [string](& $Git.Source -C $Root config user.email)
        $UserName = $UserName.Trim()
        $UserEmail = $UserEmail.Trim()
        if (-not $UserName -or -not $UserEmail) {
            throw "Git identity is missing. Configure git config user.name and user.email, then rerun."
        }
        Invoke-Checked $Git.Source @("-C", $Root, "commit", "-m", $CommitMessage)
    }
    Invoke-Checked $Git.Source @("-C", $Root, "branch", "-M", "main")

    $ErrorActionPreference = "Continue"
    try {
        & $Gh.Source repo view $FullName --json url *> $null
        $RepoExists = $LASTEXITCODE -eq 0
        & $Git.Source -C $Root remote get-url origin *> $null
        $HasOrigin = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $PreviousPreference
    }

    if (-not $RepoExists) {
        if ($HasOrigin) { throw "An origin remote already exists, but $FullName does not. Refusing to overwrite it." }
        $VisibilityFlag = "--$Visibility"
        Invoke-Checked $Gh.Source @("repo", "create", $FullName, $VisibilityFlag, "--description", $Description, "--source", $Root, "--remote", "origin")
    } else {
        $Expected = "https://github.com/$FullName.git"
        if (-not $HasOrigin) {
            Invoke-Checked $Git.Source @("-C", $Root, "remote", "add", "origin", $Expected)
        } else {
            $Actual = (& $Git.Source -C $Root remote get-url origin).Trim()
            if ($Actual -notin @($Expected, "git@github.com:$FullName.git")) {
                throw "Existing origin points to '$Actual', not '$FullName'. Refusing to overwrite it."
            }
        }
    }

    Invoke-Checked $Git.Source @("-C", $Root, "push", "--set-upstream", "origin", "main")
    $ErrorActionPreference = "Continue"
    try {
        & $Gh.Source repo edit $FullName --add-topic moviepilot --add-topic 115 --add-topic rapid-upload *> $null
        $TopicStatus = $LASTEXITCODE
        & $Gh.Source repo edit $FullName --enable-secret-scanning --enable-secret-scanning-push-protection *> $null
        $ScanningStatus = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousPreference
    }
    if ($TopicStatus -ne 0) { Write-Warning "Repository was pushed, but topics could not be updated." }
    if ($ScanningStatus -ne 0) { Write-Warning "GitHub secret-scanning push protection is unavailable for this account/repository." }

    Write-Host "Published: https://github.com/$FullName" -ForegroundColor Green
    Write-Host "MoviePilot market URL: https://github.com/$FullName" -ForegroundColor Green
}
finally {
    Pop-Location
}
