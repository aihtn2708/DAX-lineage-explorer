<#
.SYNOPSIS
    Live lineage watcher: sends the model open in Power BI Desktop to `daxlin live` whenever it changes.

.DESCRIPTION
    Started from Power BI Desktop's External Tools ribbon ("Live lineage"), which passes the local
    Analysis Services instance (-Server) and the model (-Database).

    1. Starts `daxlin live` on this PC if it is not running yet, and opens its page.
    2. Connects to the model open in Desktop with Microsoft's Tabular Object Model (TOM), read-only.
    3. About once a second, checks whether any measure or calculated column changed (name, DAX,
       state, last-changed time). When something did, sends the model's measures, columns and
       relationships to http://127.0.0.1:<port>/api/snapshot. Nothing else is sent, and nothing
       leaves this PC.
    4. Stops when Desktop closes, or when you close this window.

    Desktop changes the model when you commit an edit (Enter, or the tick in the formula bar), so
    the page follows commits, not keystrokes.

    Works in Windows PowerShell 5.1 and PowerShell 7.

.EXAMPLE
    Registered by install-live.ps1; Desktop runs it as
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File PowerBILive.ps1 -Server "localhost:51542" -Database "<model id>"
#>
param(
    [Parameter(Mandatory = $true)][string] $Server,
    [string] $Database = "",
    [int] $Port = 0,
    [int] $IntervalMs = 1000,
    [string] $ReportPath = "",
    [string] $TomPath = "",
    [switch] $NoBrowser
)

$ErrorActionPreference = "Stop"
function Say([string] $text, [string] $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Prop($object, [string] $name) {
    if ($null -ne $object -and $object.PSObject.Properties.Name -contains $name) { return $object.$name }
    return $null
}
function Quit([int] $code, [string] $message = "") {
    if ($message) { Say $message "Red" }
    Write-Host ""
    Read-Host "Press Enter to close this window" | Out-Null
    exit $code
}

$Host.UI.RawUI.WindowTitle = "Live lineage: watching Power BI Desktop"
Say ""
Say "Live lineage" "Cyan"

# -- settings written by install-live.ps1 ----------------------------------------------------------
$config = $null
$configPath = Join-Path $PSScriptRoot "live.config.json"
if (Test-Path $configPath) { $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json }
if (-not $Port) { $Port = [int](Prop $config "port") }
if (-not $Port) { $Port = 8770 }
if (-not $TomPath) { $TomPath = Prop $config "tomPath" }
$companionConfig = Join-Path $PSScriptRoot "..\workspace\companion\companion.config.json"
if (-not $TomPath -and (Test-Path $companionConfig)) {
    $TomPath = Prop (Get-Content $companionConfig -Raw -Encoding UTF8 | ConvertFrom-Json) "tomPath"
}
$base = "http://127.0.0.1:$Port"
$tokenFile = Join-Path $HOME ".dax_lineage\live.json"

# -- the live server -----------------------------------------------------------------------------------
function Server-Up {
    try {
        $hello = Invoke-RestMethod "$base/api/hello" -TimeoutSec 2
        return ($hello.app -eq "daxlin-live")
    } catch { return $false }
}
function Read-Token {
    if (-not (Test-Path $tokenFile)) { return "" }
    $info = Get-Content $tokenFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$info.port -ne $Port) { return "" }
    return [string]$info.token
}
function Start-LiveServer {
    $python = Prop $config "python"
    $pythonArgs = @(Prop $config "pythonArgs")
    $repo = Prop $config "repo"
    if (-not $python) {
        if (Get-Command py -ErrorAction SilentlyContinue) { $python = "py"; $pythonArgs = @("-3") }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { $python = "python"; $pythonArgs = @() }
        else { Quit 1 "Python was not found. Install Python 3.9 or later, or run install-live.ps1 -Python <path>." }
    }
    if (-not $repo) { $repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
    $arguments = @($pythonArgs | Where-Object { $_ }) + @("-m", "dax_lineage", "live", "--port", "$Port", "--no-open")
    Say "Starting the live page server ($python $($arguments -join ' ')) ..."
    Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $repo -WindowStyle Minimized | Out-Null
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (Server-Up) { return $true }
    }
    return $false
}

$started = $false
if (-not (Server-Up)) {
    if (-not (Start-LiveServer)) { Quit 1 "The live page server did not start. Run 'py -m dax_lineage live' in the repository folder to see why." }
    $started = $true
}
$token = Read-Token
if (-not $token) { Quit 1 "The live server's token file ($tokenFile) is missing or for another port." }
Say "Live page: $base/" "Green"
if ($started -and -not $NoBrowser) { Start-Process "$base/" }

# -- the Tabular Object Model ------------------------------------------------------------------------
function Find-Tom {
    $candidates = New-Object System.Collections.ArrayList
    if ($TomPath) { [void]$candidates.Add($TomPath) }
    [void]$candidates.Add((Join-Path $PSScriptRoot "lib"))
    [void]$candidates.Add((Join-Path $PSScriptRoot "..\workspace\companion\lib"))
    if ($env:LOCALAPPDATA) { [void]$candidates.Add((Join-Path $env:LOCALAPPDATA "PowerBIWorkspace\tom")) }
    $x86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if ($x86) { [void]$candidates.Add((Join-Path $x86 "Tabular Editor")) }
    if ($env:ProgramFiles) { [void]$candidates.Add((Join-Path $env:ProgramFiles "Tabular Editor")) }
    $module = Get-Module -ListAvailable -Name SqlServer -ErrorAction SilentlyContinue | Sort-Object Version -Descending | Select-Object -First 1
    if ($module) { [void]$candidates.Add($module.ModuleBase) }
    foreach ($dir in $candidates) {
        if ($dir -and (Test-Path (Join-Path $dir "Microsoft.AnalysisServices.Tabular.dll"))) { return $dir }
    }
    return $null
}
$tomDir = Find-Tom
if (-not $tomDir) {
    Quit 1 "Microsoft's Tabular Object Model library was not found. Run install-live.ps1 -DownloadTom, or install Tabular Editor 2 or the SqlServer PowerShell module."
}
foreach ($name in @("Microsoft.AnalysisServices.Core.dll", "Microsoft.AnalysisServices.Tabular.Json.dll",
                    "Microsoft.AnalysisServices.dll", "Microsoft.AnalysisServices.Tabular.dll")) {
    $file = Join-Path $tomDir $name
    if (Test-Path $file) { [void][System.Reflection.Assembly]::LoadFrom($file) }
}

# -- the model open in Desktop -------------------------------------------------------------------------
$script:tabular = $null
$script:db = $null
$script:canRefresh = $true
function Connect-Model {
    if ($script:tabular) { try { $script:tabular.Disconnect() } catch { } }
    $script:tabular = New-Object Microsoft.AnalysisServices.Tabular.Server
    $script:tabular.Connect("Data Source=$Server")
    $found = $null
    if ($Database) {
        $found = $script:tabular.Databases.FindByName($Database)
        if (-not $found) { $found = $script:tabular.Databases.Find($Database) }
    }
    if (-not $found -and $script:tabular.Databases.Count -gt 0) { $found = $script:tabular.Databases[0] }
    if (-not $found -or -not $found.Model) { throw "Power BI Desktop has no model open here (a report on a published model has none)." }
    $script:db = $found
}
function Load-Model {
    # read the model as it is now: refresh the metadata, or reconnect when this TOM version cannot
    if ($script:canRefresh) {
        try { $script:db.Refresh($true); return } catch { $script:canRefresh = $false }
    }
    Connect-Model
}
try { Connect-Model } catch { Quit 1 "Could not connect to Power BI Desktop at $Server : $($_.Exception.Message)" }

# which .pbix Desktop has open: the Analysis Services process listening on the port, its parent
# PBIDesktop.exe, and the file on that process's command line (when Desktop was opened with a file)
function Find-OpenFile {
    if ($ReportPath) { return $ReportPath }
    try {
        $portNumber = [int]($Server -replace '^.*:', '')
        $listener = Get-NetTCPConnection -LocalPort $portNumber -State Listen -ErrorAction Stop | Select-Object -First 1
        $engine = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        $desktop = Get-CimInstance Win32_Process -Filter "ProcessId = $($engine.ParentProcessId)"
        if ($desktop.CommandLine -match '"([^"]+\.(pbix|pbit|pbip))"') { return $Matches[1] }
        if ($desktop.CommandLine -match '\s([^\s"]+\.(pbix|pbit|pbip))') { return $Matches[1] }
    } catch { }
    return ""
}
$openFile = Find-OpenFile
$modelName = if ($openFile) { [System.IO.Path]::GetFileNameWithoutExtension($openFile) } else { "" }
if ($openFile) { Say "File: $openFile" } else { Say "Could not tell which file Desktop has open: visuals are not shown until you give the saved file on the page." "Yellow" }

function Text($value) { if ($null -eq $value) { return "" } return [string]$value }
function Stamp($value) { if ($null -eq $value) { return "" } try { return ([datetime]$value).ToUniversalTime().ToString("o") } catch { return [string]$value } }

function Snapshot {
    $model = $script:db.Model
    $measures = New-Object System.Collections.ArrayList
    $columns = New-Object System.Collections.ArrayList
    $tables = New-Object System.Collections.ArrayList
    $relationships = New-Object System.Collections.ArrayList
    $signature = New-Object System.Text.StringBuilder
    foreach ($table in $model.Tables) {
        [void]$tables.Add([ordered]@{ Name = $table.Name; IsHidden = [bool]$table.IsHidden })
        foreach ($m in $table.Measures) {
            [void]$measures.Add([ordered]@{
                Name = $m.Name; Table = $table.Name; Expression = (Text $m.Expression)
                Description = (Text $m.Description); FormatString = (Text $m.FormatString)
                DisplayFolder = (Text $m.DisplayFolder); IsHidden = [bool]$m.IsHidden
                State = (Text $m.State); ErrorMessage = (Text $m.ErrorMessage)
                LineageTag = (Text $m.LineageTag); ModifiedTime = (Stamp $m.ModifiedTime)
            })
            [void]$signature.Append("m|$($table.Name)|$($m.Name)|$(Stamp $m.ModifiedTime)|$($m.State)|$((Text $m.Expression).Length)`n")
        }
        foreach ($c in $table.Columns) {
            $type = Text $c.Type
            if ($type -eq "RowNumber") { continue }
            $expression = ""
            if ($type -eq "Calculated") { $expression = Text $c.Expression }
            [void]$columns.Add([ordered]@{
                Name = $c.Name; Table = $table.Name; Expression = $expression; Type = $type
                IsHidden = [bool]$c.IsHidden; DisplayFolder = (Text $c.DisplayFolder)
                State = (Text $c.State); ErrorMessage = (Text $c.ErrorMessage)
                LineageTag = (Text $c.LineageTag); ModifiedTime = (Stamp $c.ModifiedTime)
            })
            [void]$signature.Append("c|$($table.Name)|$($c.Name)|$(Stamp $c.ModifiedTime)|$($c.State)`n")
        }
    }
    foreach ($r in $model.Relationships) {
        if ($r.FromColumn -and $r.ToColumn) {
            [void]$relationships.Add([ordered]@{ FromTable = $r.FromColumn.Table.Name; FromColumn = $r.FromColumn.Name
                ToTable = $r.ToColumn.Table.Name; ToColumn = $r.ToColumn.Name; IsActive = [bool]$r.IsActive })
        }
    }
    return @{
        signature = $signature.ToString()
        body = [ordered]@{ source = $Server; name = $modelName; file = $openFile
                           measures = $measures; columns = $columns; tables = $tables; relationships = $relationships }
    }
}

function Send($body) {
    $json = ConvertTo-Json $body -Depth 6 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    return Invoke-RestMethod "$base/api/snapshot" -Method Post -Body $bytes -ContentType "application/json; charset=utf-8" `
        -Headers @{ "X-Live-Token" = $script:token } -TimeoutSec 30
}

Say "Watching the model. Edit a measure and press Enter: the live page follows. Close this window to stop."
$script:token = $token
$last = ""
$failures = 0
while ($true) {
    try {
        Load-Model
        $failures = 0
    } catch {
        $failures++
        if ($failures -ge 3) {
            Say "Power BI Desktop closed (or the model is gone): stopping." "Yellow"
            Start-Sleep -Seconds 3
            exit 0
        }
        Start-Sleep -Milliseconds $IntervalMs
        continue
    }
    $snap = Snapshot
    if ($snap.signature -ne $last) {
        try {
            $answer = Send $snap.body
            $last = $snap.signature
            if ($answer.changed) { Say ("{0}  sent a change" -f (Get-Date -Format "HH:mm:ss")) }
        } catch {
            $status = $null
            try { $status = [int]$_.Exception.Response.StatusCode } catch { }
            if ($status -eq 403) {
                $script:token = Read-Token   # the server restarted with a new token
            } elseif (-not (Server-Up)) {
                Say "The live page server stopped; starting it again ..." "Yellow"
                if (Start-LiveServer) { $script:token = Read-Token; $last = "" } else { Start-Sleep -Seconds 5 }
            } else {
                Say "Could not send the model: $($_.Exception.Message)" "Yellow"
            }
        }
    }
    Start-Sleep -Milliseconds $IntervalMs
}
