<#
.SYNOPSIS
    Sync Notion board card moves, auto-stamp Applied dates, and run the Time-to-Offer Forecast.
.EXAMPLE
    .\forecast_offer.ps1
#>
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$PythonExe = "C:\Users\jenge\anaconda3\envs\job-matcher\python.exe"

& $PythonExe -m src.analytics.forecaster
