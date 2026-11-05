<#
push.ps1 - create the GitHub repo (if needed) and push this folder.

  .\push.ps1 -Token ghp_xxxxxxxxxxxxxxxx
  .\push.ps1 -Token $env:GITHUB_TOKEN -Repo api-strike -Private
  .\push.ps1 -Token ghp_... -User someoneelse -Repo api-strike

Token scopes needed: classic -> repo ; fine-grained -> Administration:Read+Write
                      + Contents:Read+Write on the target repo.

Nothing is written to disk; the token is only used for the two calls below.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Token,
  [string]$User = "",
  [string]$Repo = "api-strike",
  [string]$Description = "Portable quota / rate-limit pressure harness for the Google Generative Language API",
  [switch]$Private,
  [string]$Branch = "main"
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$git = if (Test-Path 'C:\Users\ZhangE2\.dsh\tools\mingit\cmd\git.exe') { 'C:\Users\ZhangE2\.dsh\tools\mingit\cmd\git.exe' } else { 'git' }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Invoke-GitHub {
  param([string]$Method, [string]$Path, $Body)
  $params = @{
    Uri         = "https://api.github.com$Path"
    Method      = $Method
    Headers     = @{
      Authorization          = "Bearer $Token"
      Accept                 = "application/vnd.github+json"
      "X-GitHub-Api-Version" = "2022-11-28"
      "User-Agent"           = "api-strike-push"
    }
  }
  if ($null -ne $Body) {
    $params.Body = ($Body | ConvertTo-Json -Depth 5)
    $params.ContentType = 'application/json'
  }
  return Invoke-RestMethod @params
}

# 1) who owns this token
$me = Invoke-GitHub -Method GET -Path '/user'
if (-not $User) { $User = $me.login }
Write-Host "[i] token -> $($me.login)   target -> $User/$Repo"

# 2) create the repo (idempotent: 422 means it already exists)
$exists = $false
try {
  Invoke-GitHub -Method GET -Path "/repos/$User/$Repo" | Out-Null
  $exists = $true
  Write-Host "[i] repo already exists"
} catch {
  if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
}
if (-not $exists) {
  $payload = @{ name = $Repo; description = $Description; private = [bool]$Private; auto_init = $false }
  Invoke-GitHub -Method POST -Path '/user/repos' -Body $payload | Out-Null
  Write-Host "[+] created https://github.com/$User/$Repo"
}

# 3) remote + push (token passed per-command, never written to .git/config)
$url = "https://github.com/$User/$Repo.git"
$remotes = & $git -C $here remote
if ($remotes -notcontains 'origin') {
  & $git -C $here remote add origin $url
} else {
  & $git -C $here remote set-url origin $url
}

$b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$Token"))
$env:GIT_TERMINAL_PROMPT = '0'
& $git -C $here -c "http.extraheader=AUTHORIZATION: basic $b64" push -u origin $Branch
$rc = $LASTEXITCODE
if ($rc -ne 0) { throw "git push failed with exit code $rc" }

Write-Host ""
Write-Host "[+] pushed: https://github.com/$User/$Repo"
