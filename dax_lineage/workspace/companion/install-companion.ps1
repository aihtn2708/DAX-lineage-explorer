<#
.SYNOPSIS
    Registers the Power BI workspace companion in Power BI Desktop's External Tools ribbon.

.DESCRIPTION
    Run once per PC, in Windows PowerShell, from the folder that holds this script:

        powershell -ExecutionPolicy Bypass -File .\install-companion.ps1 -Workspace http://127.0.0.1:8766

    -Workspace     the workspace's address (default http://127.0.0.1:8766, the workspace on this PC)
    -DownloadTom   download Microsoft's Tabular Object Model library from nuget.org into
                   %LOCALAPPDATA%\PowerBIWorkspace\tom (skip it if Tabular Editor 2 or the
                   SqlServer PowerShell module is installed; the companion finds those)
    -TomPath       a folder that already holds Microsoft.AnalysisServices.Tabular.dll
    -Token         your API token from the workspace's My account page, when it uses sign-in;
                   kept in %APPDATA%\PowerBIWorkspace\companion.json, readable only by you

    Writing to Desktop's External Tools folder needs administrator rights. Without them the
    registration file is written here, with the command to copy it. Restart Power BI Desktop
    afterwards: it reads the folder when it starts.
#>
param(
    [string] $Workspace = "http://127.0.0.1:8766",
    [switch] $DownloadTom,
    [string] $TomPath = "",
    [string] $Token = ""
)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$script = Join-Path $here "PowerBIWorkspaceCompanion.ps1"
$utf8 = New-Object System.Text.UTF8Encoding($false)

Get-ChildItem $here -File | Unblock-File -ErrorAction SilentlyContinue

$config = [ordered]@{ workspace = $Workspace.TrimEnd("/") }
if ($TomPath) { $config.tomPath = $TomPath }

if ($DownloadTom) {
    $target = Join-Path $env:LOCALAPPDATA "PowerBIWorkspace\tom"
    $package = Join-Path $env:TEMP "PowerBIWorkspace-tom.zip"
    $unpacked = Join-Path $env:TEMP "PowerBIWorkspace-tom"
    Write-Host "Downloading Microsoft.AnalysisServices.retail.amd64 from nuget.org ..."
    Invoke-WebRequest "https://www.nuget.org/api/v2/package/Microsoft.AnalysisServices.retail.amd64" -OutFile $package -UseBasicParsing
    if (Test-Path $unpacked) { Remove-Item $unpacked -Recurse -Force }
    Expand-Archive $package -DestinationPath $unpacked -Force
    $lib = Get-ChildItem (Join-Path $unpacked "lib") -Directory | Where-Object { $_.Name -like "net4*" } | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $lib) { throw "The package has no .NET Framework library folder; pass -TomPath instead." }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item (Join-Path $lib.FullName "*.dll") $target -Force
    $config.tomPath = $target
    Write-Host "Tabular Object Model library: $target"
}

[System.IO.File]::WriteAllText((Join-Path $here "companion.config.json"), (ConvertTo-Json $config), $utf8)
Write-Host "Workspace address: $($config.workspace)"

if ($Token) {
    $mine = Join-Path $env:APPDATA "PowerBIWorkspace"
    New-Item -ItemType Directory -Force -Path $mine | Out-Null
    $file = Join-Path $mine "companion.json"
    [System.IO.File]::WriteAllText($file, (ConvertTo-Json ([ordered]@{ token = $Token })), $utf8)
    # readable by this user only
    & icacls $file /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "Your token is saved in $file"
}

$tool = Get-Content (Join-Path $here "PowerBIWorkspace.pbitool.template.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$tool.path = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$tool.arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $script + '" -Server "%server%" -Database "%database%"'
$json = ConvertTo-Json $tool

$common = [Environment]::GetEnvironmentVariable("CommonProgramFiles(x86)")
if (-not $common) { $common = $env:CommonProgramFiles }
$tools = Join-Path $common "Microsoft Shared\Power BI Desktop\External Tools"
$name = "PowerBIWorkspace.pbitool.json"
try {
    New-Item -ItemType Directory -Force -Path $tools | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $tools $name), $json, $utf8)
    Write-Host "Registered in $tools" -ForegroundColor Green
    Write-Host "Restart Power BI Desktop: 'Workspace changes' appears on the External Tools ribbon."
} catch {
    $local = Join-Path $here $name
    [System.IO.File]::WriteAllText($local, $json, $utf8)
    Write-Host "No permission to write to $tools." -ForegroundColor Yellow
    Write-Host "Ask someone with administrator rights to run:"
    Write-Host "  Copy-Item `"$local`" `"$tools`""
}
