<#
.SYNOPSIS
    One-click Notion 'Want to Apply' Resume Tailoring Runner for Gemini 4 Argon.

.DESCRIPTION
    Running .\tailor_resumes.ps1 performs the full automated tailoring workflow:
      1. Verifies the Conda 'job-matcher' Python 3.11 environment and ensures MySQL 8.0 is online.
      2. Queries your Notion 'Job Applications & Matches' database for all cards where Status = 'Want to Apply'.
      3. Loads your active resume (e.g. data\Josh_Engelhardt_Resume-v3.docx) and correlates each job with
         its full description, matched skill groups, and missing ATS keywords from MySQL.
      4. Writes a '🎯 Argon Resume Tailoring Kit' (matched/missing skills callout + copy-ready prompt +
         optional GEMINI_API_KEY autopilot output) directly inside each Notion card's page body!
      5. Saves the master Argon prompt packet to data\tailored\argon_tailoring_prompt_YYYY-MM-DD.md
         AND automatically copies it to your Windows Clipboard so you can immediately press Ctrl+V
         into Gemini 4 Argon!

.EXAMPLE
    .\tailor_resumes.ps1
    Processes all 'Want to Apply' cards in Notion, updates their Notion pages, and copies the Argon prompt to clipboard.
#>

[CmdletBinding()]
param (
    [switch]$NoOpenFile,
    [switch]$NoClipboard
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$PythonExe = "C:\Users\jenge\anaconda3\envs\job-matcher\python.exe"

Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host "       NOTION 'WANT TO APPLY' -> ARGON RESUME TAILOR          " -ForegroundColor Cyan
Write-Host "==============================================================" -ForegroundColor Cyan

if (-not (Test-Path $PythonExe)) {
    Write-Error "Conda environment 'job-matcher' not found at $PythonExe."
    exit 1
}

# Ensure MySQL 8.0 is online so full job descriptions & keyword breakdowns can be loaded
$PortOpen = $false
try {
    $TcpClient = New-Object System.Net.Sockets.TcpClient
    $ConnectAsync = $TcpClient.BeginConnect("127.0.0.1", 3306, $null, $null)
    $Wait = $ConnectAsync.AsyncWaitHandle.WaitOne(1000, $false)
    if ($Wait -and $TcpClient.Connected) {
        $PortOpen = $true
    }
    $TcpClient.Close()
} catch {
    $PortOpen = $false
}

if (-not $PortOpen) {
    Write-Host "`nStarting Docker Desktop & MySQL 8.0 container (job-matcher-mysql)..." -ForegroundColor Yellow
    & docker desktop start
    & docker compose up -d --wait
}

Write-Host "`nRunning Notion 'Want to Apply' Queue Processor..." -ForegroundColor Cyan
& $PythonExe -m src.exporters.resume_tailor
if ($LASTEXITCODE -ne 0) {
    Write-Error "Resume tailor exited with code $LASTEXITCODE."
    exit $LASTEXITCODE
}

$TodayDate = (Get-Date).ToString("yyyy-MM-dd")
$PromptPath = Join-Path $ProjectRoot "data\tailored\argon_tailoring_prompt_$TodayDate.txt"

if (Test-Path $PromptPath) {
    if (-not $NoClipboard) {
        Get-Content -Path $PromptPath -Raw -Encoding UTF8 | Set-Clipboard
        Write-Host "`n[CLIPBOARD READY] Copied Argon Tailoring Prompt to your Windows Clipboard!" -ForegroundColor Green
        Write-Host "                  Just press Ctrl+V in your Gemini 4 Argon chat!" -ForegroundColor Green
    }
    if (-not $NoOpenFile) {
        Write-Host "Opening prompt packet: $PromptPath" -ForegroundColor Cyan
        Invoke-Item $PromptPath
    }
}
