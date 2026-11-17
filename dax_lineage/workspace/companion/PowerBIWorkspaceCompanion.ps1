<#
.SYNOPSIS
    Power BI workspace companion: makes approved changes to the model open in Power BI Desktop.

.DESCRIPTION
    Started from Power BI Desktop's External Tools ribbon, which passes the local
    Analysis Services instance (-Server) and the model (-Database).

    1. Loads Microsoft's Tabular Object Model (TOM) library.
    2. Connects to the model open in Desktop.
    3. Asks the workspace for approved changes, and picks the one for this model.
    4. Shows every edit with what it will do, and changes nothing until you type APPLY.
    5. Saves a backup of the model's definition, makes the edits, and saves them to Desktop.
    6. Tells the workspace what it did. You check the model and save the .pbix as usual;
       the next scan confirms the change.

    An edit is skipped when its object no longer has the value the change expects, so a
    change approved last week cannot overwrite this morning's work. Renames of measures
    that visuals show are left for you to do in Desktop's Data pane, which updates the
    visuals; the companion lists them.

    Works in Windows PowerShell 5.1 and PowerShell 7. The rules mirror
    dax_lineage/workspace/apply_tmsl.py, which the tests run.

.EXAMPLE
    Registered by install-companion.ps1; Desktop runs it as
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File PowerBIWorkspaceCompanion.ps1 -Server "localhost:51542" -Database "<model id>"
#>
param(
    [Parameter(Mandatory = $true)][string] $Server,
    [string] $Database = "",
    [string] $Workspace = "",
    [string] $TomPath = "",
    [int] $Change = 0,
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

function Say([string] $text, [string] $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Done([int] $code) {
    Write-Host ""
    Read-Host "Press Enter to close this window" | Out-Null
    exit $code
}
function Prop($object, [string] $name) {
    if ($null -ne $object -and $object.PSObject.Properties.Name -contains $name) { return $object.$name }
    return $null
}

Say ""
Say "Power BI workspace companion" "Cyan"

# -- settings --------------------------------------------------------------------------
$config = $null
$configPath = Join-Path $PSScriptRoot "companion.config.json"
if (Test-Path $configPath) { $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json }
if (-not $Workspace) { $Workspace = Prop $config "workspace" }
if (-not $Workspace) { $Workspace = $env:DAXLIN_WORKSPACE_URL }
if (-not $Workspace) { $Workspace = "http://127.0.0.1:8766" }
$Workspace = $Workspace.TrimEnd("/")
if (-not $TomPath) { $TomPath = Prop $config "tomPath" }

# -- the Tabular Object Model ----------------------------------------------------------
function Find-Tom {
    $candidates = New-Object System.Collections.ArrayList
    if ($TomPath) { [void]$candidates.Add($TomPath) }
    [void]$candidates.Add((Join-Path $PSScriptRoot "lib"))
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
    Say "Microsoft's Tabular Object Model library was not found." "Red"
    Say "Run install-companion.ps1 -DownloadTom, or install Tabular Editor 2, or the SqlServer PowerShell module."
    Done 1
}
foreach ($name in @("Microsoft.AnalysisServices.Core.dll", "Microsoft.AnalysisServices.Tabular.Json.dll",
                    "Microsoft.AnalysisServices.dll", "Microsoft.AnalysisServices.Tabular.dll")) {
    $file = Join-Path $tomDir $name
    if (Test-Path $file) { [void][System.Reflection.Assembly]::LoadFrom($file) }
}

# -- the model open in Desktop ---------------------------------------------------------
try {
    $tabular = New-Object Microsoft.AnalysisServices.Tabular.Server
    $tabular.Connect("Data Source=$Server")
} catch {
    Say "Could not connect to Power BI Desktop at $Server : $($_.Exception.Message)" "Red"
    Done 1
}
$db = $null
if ($Database) {
    $db = $tabular.Databases.FindByName($Database)
    if (-not $db) { $db = $tabular.Databases.Find($Database) }
}
if (-not $db -and $tabular.Databases.Count -gt 0) { $db = $tabular.Databases[0] }
if (-not $db -or -not $db.Model) {
    Say "Power BI Desktop has no model open here (a report connected to a published model has none)." "Red"
    Done 1
}
$model = $db.Model
Say ("Connected to the model open in Desktop: {0} tables" -f $model.Tables.Count)

# -- the workspace ---------------------------------------------------------------------
function Call-Workspace([string] $method, [string] $path, $body) {
    $uri = $Workspace + $path
    if ($null -eq $body) { return Invoke-RestMethod -Method $method -Uri $uri -UseBasicParsing }
    $json = ConvertTo-Json -InputObject $body -Depth 8 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    return Invoke-RestMethod -Method $method -Uri $uri -Body $bytes -ContentType "application/json; charset=utf-8" -UseBasicParsing
}

try {
    # an object holding the list: Windows PowerShell 5.1 does not unroll a top-level JSON array
    $ready = @((Call-Workspace "GET" "/api/changes/ready" $null).changes)
} catch {
    Say "Could not reach the workspace at $Workspace : $($_.Exception.Message)" "Red"
    Say "Start it with run-workspace.bat (or daxlin workspace serve), or set the address in companion.config.json."
    Done 1
}

function Find-Object($op) {
    $isMeasure = $op.kind -eq "measure"
    if ($op.lineage_tag) {
        foreach ($t in $model.Tables) {
            $items = if ($isMeasure) { $t.Measures } else { $t.Columns }
            foreach ($item in $items) {
                if ($item.LineageTag -eq $op.lineage_tag) { return [pscustomobject]@{ Table = $t; Item = $item } }
            }
        }
    }
    foreach ($t in $model.Tables) {
        if ($t.Name -ne $op.table) { continue }
        $items = if ($isMeasure) { $t.Measures } else { $t.Columns }
        foreach ($item in $items) {
            if ($item.Name -eq $op.name) { return [pscustomobject]@{ Table = $t; Item = $item } }
        }
    }
    return $null
}

function Current-Value($item, [string] $property) {
    $value = $item.$property
    if ($property -eq "IsHidden") { if ($value) { return "true" } else { return "false" } }
    if ($null -eq $value) { return "" }
    return ([string]$value) -replace "`r`n", "`n"
}

function Normal([string] $text) { if ($null -eq $text) { return "" } return $text -replace "`r`n", "`n" }

# what each edit would do now: applied | already | skipped | manual
function Plan-Op($op) {
    if ($op.mode -eq "desktop") { return @{ status = "manual"; message = $op.reason } }
    $found = Find-Object $op
    if ($op.op -eq "remove") {
        if ($null -eq $found) { return @{ status = "already"; message = "not in the model" } }
        return @{ status = "applied"; message = "remove"; found = $found }
    }
    if ($null -eq $found) { return @{ status = "skipped"; message = "$($op.name) is not in the model" } }
    $now = Current-Value $found.Item $op.property
    if ($now -eq (Normal $op.new)) { return @{ status = "already"; message = "" } }
    if ($now -ne (Normal $op.old)) { return @{ status = "skipped"; message = "$($op.property) changed since the request" } }
    return @{ status = "applied"; message = ""; found = $found }
}

# which approved change is for this model: the one whose objects are here
$scored = foreach ($c in $ready) {
    $hits = 0
    foreach ($op in $c.ops) { if (Find-Object $op) { $hits++ } }
    [pscustomobject]@{ Change = $c; Hits = $hits }
}
$scored = @($scored | Where-Object { $_.Hits -gt 0 -or $Change -eq $_.Change.id })
if ($Change) { $scored = @($scored | Where-Object { $_.Change.id -eq $Change }) }
if ($scored.Count -eq 0) {
    Say ""
    Say "No approved change is for this model." "Yellow"
    if ($ready.Count) { Say ("Approved changes are waiting for: " + (($ready | ForEach-Object { $_.model } | Select-Object -Unique) -join ", ")) }
    Done 0
}
$pick = $scored[0]
if ($scored.Count -gt 1) {
    Say ""
    for ($i = 0; $i -lt $scored.Count; $i++) {
        $c = $scored[$i].Change
        Say ("  {0}) #{1} {2}: {3} (approved by {4})" -f ($i + 1), $c.id, $c.model, $c.title, $c.approved_by)
    }
    $answer = Read-Host "Which change? (number)"
    $index = 0
    if (-not [int]::TryParse($answer, [ref]$index) -or $index -lt 1 -or $index -gt $scored.Count) { Say "Nothing chosen."; Done 0 }
    $pick = $scored[$index - 1]
}
$change = $pick.Change

# -- show the plan ---------------------------------------------------------------------
Say ""
Say ("Change #{0} for {1}: {2}" -f $change.id, $change.model, $change.title) "Cyan"
Say ("Approved by {0}" -f $change.approved_by)
foreach ($w in $change.warnings) { Say "  warning: $w" "Yellow" }
Say ""
$plans = @{}
$toApply = 0
foreach ($op in $change.ops) {
    $p = Plan-Op $op
    $plans[[int]$op.seq] = $p
    $what = switch ($op.op) {
        "remove"     { "remove $($op.name)" }
        "rename"     { "rename $($op.name) to $($op.new)" }
        "expression" { "update the DAX of $($op.name) ($($op.reason))" }
        default      { "set $($op.property) of $($op.name) to '$($op.new)'" }
    }
    $color = switch ($p.status) { "applied" { "White" } "already" { "DarkGray" } "manual" { "Yellow" } default { "Red" } }
    $tag = switch ($p.status) { "applied" { "will do" } "already" { "already done" } "manual" { "you, in Desktop" } default { "skip" } }
    Say ("  {0,2}. [{1}] {2}{3}" -f $op.seq, $tag, $what, $(if ($p.message -and $p.status -ne "applied") { " - $($p.message)" } else { "" })) $color
    if ($p.status -eq "applied") { $toApply++ }
}

$results = New-Object System.Collections.ArrayList
function Add-Result([int] $seq, [string] $status, [string] $message) {
    [void]$results.Add([ordered]@{ seq = $seq; status = $status; message = $message })
}

if ($toApply -eq 0) {
    Say ""
    Say "Nothing to do here." "Yellow"
} elseif ($DryRun) {
    Say ""
    Say "Dry run: nothing was changed." "Yellow"
    Done 0
} else {
    Say ""
    $answer = Read-Host ("Type APPLY to make these {0} edits in the open model" -f $toApply)
    if ($answer -cne "APPLY") { Say "Nothing was changed."; Done 0 }

    # a backup of the model's definition before anything changes
    $backupDir = Join-Path $env:LOCALAPPDATA "PowerBIWorkspace\backups"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $backup = Join-Path $backupDir ("change-{0}-{1}.json" -f $change.id, (Get-Date -Format "yyyyMMdd-HHmmss"))
    $definition = [Microsoft.AnalysisServices.Tabular.JsonSerializer]::SerializeDatabase($db)
    [System.IO.File]::WriteAllText($backup, $definition, (New-Object System.Text.UTF8Encoding($false)))
    Say "Backup of the model's definition: $backup"

    $made = New-Object System.Collections.ArrayList
    try {
        foreach ($op in $change.ops) {
            $p = $plans[[int]$op.seq]
            if ($p.status -ne "applied") { continue }
            if ($op.op -eq "remove") {
                [void]$p.found.Table.Measures.Remove($p.found.Item)
            } elseif ($op.property -eq "IsHidden") {
                $p.found.Item.IsHidden = ($op.new -eq "true")
            } else {
                $p.found.Item.($op.property) = [string]$op.new
            }
            [void]$made.Add([int]$op.seq)
        }
        [void]$model.SaveChanges()
        Say ("Made {0} edits in the open model." -f $made.Count) "Green"
    } catch {
        $reason = $_.Exception.Message
        try { $model.UndoLocalChanges() } catch { }
        Say "Power BI Desktop refused the edits, so none were made: $reason" "Red"
        foreach ($seq in $made) { $plans[$seq] = @{ status = "failed"; message = $reason } }
        $made.Clear()
    }
}

foreach ($op in $change.ops) {
    $p = $plans[[int]$op.seq]
    Add-Result ([int]$op.seq) $p.status ([string]$p.message)
}
try {
    $body = [ordered]@{ results = $results; by = $env:USERNAME; machine = $env:COMPUTERNAME }
    [void](Call-Workspace "POST" ("/api/changes/{0}/applied" -f $change.id) $body)
    Say "Told the workspace what was done."
} catch {
    Say "Could not tell the workspace ($($_.Exception.Message)). The next scan still confirms the edits." "Yellow"
}

$manual = @($change.ops | Where-Object { $_.mode -eq "desktop" })
if ($manual.Count) {
    Say ""
    Say "Still to do yourself, in Desktop's Data pane (Desktop then updates the visuals and the DAX):" "Yellow"
    foreach ($op in $manual) { Say ("  rename {0} to {1}" -f $op.name, $op.new) "Yellow" }
}
Say ""
Say "Check the model in Desktop, then save the .pbix (Ctrl+S). The next scan confirms the change." "Cyan"
Done 0
