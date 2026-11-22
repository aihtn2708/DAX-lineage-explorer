<#
.SYNOPSIS
    Adds "Live lineage" to Power BI Desktop's External Tools ribbon.

.DESCRIPTION
    Run once per PC, in Windows PowerShell, from the folder that holds this script:

        powershell -ExecutionPolicy Bypass -File .\install-live.ps1

    -Python        the Python to run `daxlin live` with (default: the py launcher, else python)
    -Port          the live page's port (default 8770)
    -DownloadTom   download Microsoft's Tabular Object Model library from nuget.org into
                   %LOCALAPPDATA%\PowerBIWorkspace\tom (skip it if the workspace companion, Tabular
                   Editor 2 or the SqlServer PowerShell module is installed: the watcher finds those)
    -TomPath       a folder that already holds Microsoft.AnalysisServices.Tabular.dll

    Writing to Desktop's External Tools folder needs administrator rights. Without them the
    registration file is written here, with the command to copy it. Restart Power BI Desktop
    afterwards: it reads the folder when it starts.
#>
param(
    [string] $Python = "",
    [int] $Port = 8770,
    [switch] $DownloadTom,
    [string] $TomPath = ""
)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$script = Join-Path $here "PowerBILive.ps1"
$repo = (Resolve-Path (Join-Path $here "..\..")).Path
$utf8 = New-Object System.Text.UTF8Encoding($false)

Get-ChildItem $here -File | Unblock-File -ErrorAction SilentlyContinue

# the Python that runs the live page server
$pythonArgs = @()
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $Python = "py"; $pythonArgs = @("-3") }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = (Get-Command python).Source }
    else { throw "Python was not found. Install Python 3.9 or later from python.org, or pass -Python <path to python.exe>." }
}
$check = & $Python @pythonArgs -c "import sys; sys.path.insert(0, r'$repo'); import dax_lineage.live; print(sys.version.split()[0])" 2>&1
if ($LASTEXITCODE -ne 0) { throw "Python could not load dax_lineage from $repo : $check" }
Write-Host "Python $check ($Python)"

$config = [ordered]@{ python = $Python; pythonArgs = $pythonArgs; repo = $repo; port = $Port }
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

[System.IO.File]::WriteAllText((Join-Path $here "live.config.json"), (ConvertTo-Json $config), $utf8)

$tool = Get-Content (Join-Path $here "PowerBILive.pbitool.template.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$tool.path = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$tool.arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $script + '" -Server "%server%" -Database "%database%"'
$json = ConvertTo-Json $tool

$common = [Environment]::GetEnvironmentVariable("CommonProgramFiles(x86)")
if (-not $common) { $common = $env:CommonProgramFiles }
$tools = Join-Path $common "Microsoft Shared\Power BI Desktop\External Tools"
$name = "PowerBILive.pbitool.json"
try {
    New-Item -ItemType Directory -Force -Path $tools | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $tools $name), $json, $utf8)
    Write-Host "Registered in $tools" -ForegroundColor Green
    Write-Host "Restart Power BI Desktop: 'Live lineage' appears on the External Tools ribbon."
} catch {
    $local = Join-Path $here $name
    [System.IO.File]::WriteAllText($local, $json, $utf8)
    Write-Host "No permission to write to $tools." -ForegroundColor Yellow
    Write-Host "Ask someone with administrator rights to run:"
    Write-Host "  Copy-Item `"$local`" `"$tools`""
}
