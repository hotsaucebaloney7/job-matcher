<#
.SYNOPSIS
    Undo the most recent accidental Notion board card move(s) and revert application_events.
.EXAMPLE
    .\undo_last_move.ps1
    .\undo_last_move.ps1 -Steps 2 -DryRun
#>
[CmdletBinding()]
param (
    [int]$Steps = 1,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$PythonExe = "C:\Users\jenge\anaconda3\envs\job-matcher\python.exe"

$ArgsList = @("-m", "src.exporters.notion_sync", "--undo", "--steps", "$Steps")
if ($DryRun) {
    $ArgsList += "--dry-run"
}

& $PythonExe @ArgsList
