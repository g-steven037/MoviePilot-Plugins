[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SettingsPath = Join-Path $Root ".publish-settings.json"

function Find-Gh {
    $Command = Get-Command gh.exe -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    $Known = Join-Path $env:ProgramFiles "GitHub CLI\gh.exe"
    if (Test-Path -LiteralPath $Known) { return $Known }
    $Portable = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs\GitHubCLI") -Filter "gh.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($Portable) { return $Portable.FullName }
    throw "未找到 GitHub CLI。请先安装：winget install --id GitHub.cli --source winget"
}

function Find-OrInstall-Git {
    $Command = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $Command) { $Command = Get-Command git -ErrorAction SilentlyContinue }
    if ($Command) { return $Command.Source }

    $KnownPaths = @(
        (Join-Path $env:ProgramFiles "Git\cmd\git.exe"),
        (Join-Path $env:ProgramFiles "Git\bin\git.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe")
    )
    foreach ($Known in $KnownPaths) {
        if (Test-Path -LiteralPath $Known) { return $Known }
    }

    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $Winget) { throw "未找到 Git，也未找到 winget，无法自动安装 Git。" }
    Write-Host "首次运行需要 Git，正在通过 winget 官方社区源安装 Git for Windows..." -ForegroundColor Yellow
    & $Winget.Source install --id Git.Git --source winget --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0) { throw "Git 自动安装失败，请手动执行 winget install --id Git.Git --source winget。" }

    foreach ($Known in $KnownPaths) {
        if (Test-Path -LiteralPath $Known) { return $Known }
    }
    throw "Git 已安装，但暂时找不到 git.exe；请关闭窗口后再次双击发布脚本。"
}

function Apply-WindowsProxy {
    if ($env:HTTPS_PROXY -or $env:https_proxy) { return }
    try {
        $Internet = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction Stop
        if (-not $Internet.ProxyEnable -or -not $Internet.ProxyServer) { return }
        $Proxy = [string]$Internet.ProxyServer
        if ($Proxy -match '(?:^|;)https=([^;]+)') { $Proxy = $Matches[1] }
        elseif ($Proxy -match '(?:^|;)http=([^;]+)') { $Proxy = $Matches[1] }
        if ($Proxy -notmatch '^https?://') { $Proxy = "http://$Proxy" }
        $env:HTTPS_PROXY = $Proxy
        $env:HTTP_PROXY = $Proxy
        Write-Host "已为本次发布继承 Windows 代理（地址已隐藏）。" -ForegroundColor DarkGray
    } catch {
        # Direct connection remains the fallback. Never log proxy credentials/errors.
    }
}

function Login-GitHub([string]$Gh) {
    $PreviousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Gh auth status --hostname github.com *> $null
        $StatusCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousPreference
    }
    if ($StatusCode -eq 0) { return }

    Write-Host "GitHub 尚未登录，将直接使用 Personal Access Token（不再打开浏览器）。" -ForegroundColor Yellow
    Write-Host "请使用 classic Token，并授予 repo、read:org、gist、workflow 权限；建议设置较短有效期。" -ForegroundColor Yellow
    Write-Host "Token 输入不会回显，也不会由本脚本写入文件或日志。直接回车可取消。" -ForegroundColor DarkGray
    $SecureToken = Read-Host "GitHub Token" -AsSecureString
    if ($SecureToken.Length -eq 0) { throw "GitHub 登录已取消。" }
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
    try {
        $PlainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
        $ErrorActionPreference = "Continue"
        try {
            $PlainToken | & $Gh auth login --hostname github.com --git-protocol https --with-token
            $TokenLoginCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousPreference
        }
        if ($TokenLoginCode -ne 0) { throw "Token 登录失败；请检查网络、Token 是否有效，以及 repo/read:org/gist/workflow 权限。" }
    } finally {
        if ($PlainToken) { $PlainToken = $null }
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
        $SecureToken.Dispose()
    }
}

function Ensure-GitIdentity([string]$Git) {
    $PreviousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Git -C $Root rev-parse --is-inside-work-tree *> $null
        $RepoStatus = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousPreference
    }
    if ($RepoStatus -ne 0) {
        & $Git -C $Root init -b main
        if ($LASTEXITCODE -ne 0) { throw "无法初始化 Git 仓库。" }
    }
    $Name = [string](& $Git -C $Root config user.name)
    $Email = [string](& $Git -C $Root config user.email)
    $Name = $Name.Trim()
    $Email = $Email.Trim()
    if (-not $Name) {
        $Name = (Read-Host "首次设置 Git 提交用户名").Trim()
        if (-not $Name) { throw "Git 用户名不能为空。" }
        & $Git -C $Root config user.name $Name
    }
    if (-not $Email) {
        $Email = (Read-Host "首次设置 Git 提交邮箱（可用 GitHub noreply 邮箱）").Trim()
        if ($Email -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { throw "Git 邮箱格式无效。" }
        & $Git -C $Root config user.email $Email
    }
}

function Load-OrCreateSettings {
    if (Test-Path -LiteralPath $SettingsPath) {
        return Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $RepoName = (Read-Host "GitHub 仓库名 [moviepilot-115-rapid-retry]").Trim()
    if (-not $RepoName) { $RepoName = "moviepilot-115-rapid-retry" }
    if ($RepoName -notmatch '^[A-Za-z0-9._-]+$') { throw "仓库名格式无效。" }
    $Visibility = (Read-Host "仓库可见性 public/private [public]").Trim().ToLowerInvariant()
    if (-not $Visibility) { $Visibility = "public" }
    if ($Visibility -notin @("public", "private")) { throw "可见性只能是 public 或 private。" }
    $Settings = [ordered]@{ repo_name = $RepoName; visibility = $Visibility }
    $Settings | ConvertTo-Json | Set-Content -LiteralPath $SettingsPath -Encoding UTF8
    return [pscustomobject]$Settings
}

Push-Location $Root
try {
    Apply-WindowsProxy
    $Gh = Find-Gh
    $Git = Find-OrInstall-Git
    Login-GitHub $Gh
    Ensure-GitIdentity $Git
    $Settings = Load-OrCreateSettings

    & (Join-Path $PSScriptRoot "publish-github.ps1") `
        -RepoName $Settings.repo_name `
        -Visibility $Settings.visibility
    if ($LASTEXITCODE -ne 0) { throw "发布脚本执行失败。" }
} catch {
    Write-Host "发布失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}
