<#
.SYNOPSIS
    One-click automation script for the Job Matcher application.

.DESCRIPTION
    Running .\run_job_matcher.ps1 performs the full workflow automatically:
      1. Verifies the Conda 'job-matcher' Python 3.11 environment (and optionally updates packages).
      2. Ensures .env exists (copying from .env.example if needed).
      3. Detects your active resume in data/ (.pdf, .docx, .txt, .md) and automatically
         triggers --rescore if your resume file was added or updated since the last run!
      4. Starts Docker Desktop and waits for the MySQL 8.0 container (job-matcher-mysql) to be healthy.
      5. Optionally runs the Pytest QA test suite (-RunTests).
      6. Executes the end-to-end Job Matcher pipeline (Ingest -> MySQL Store -> Hybrid Score -> Export).
      7. Opens today's generated Markdown report (reports/daily_matches_YYYY-MM-DD.md).

.EXAMPLE
    .\run_job_matcher.ps1
    Runs the full daily pipeline and opens the generated report.

.EXAMPLE
    .\run_job_matcher.ps1 -Rescore -MinScore 60
    Forces re-scoring of all database jobs with a minimum score threshold of 60.

.EXAMPLE
    .\run_job_matcher.ps1 -RunTests
    Runs the 75-test Pytest QA suite before executing the pipeline.
#>

[CmdletBinding()]
param (
    [double]$MinScore = 65.0,
    [switch]$Rescore,
    [switch]$SkipIngest,
    [switch]$RunTests,
    [switch]$InstallDeps,
    [switch]$NoOpenReport,
    [string]$ResumePath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$PythonExe = "C:\Users\jenge\anaconda3\envs\job-matcher\python.exe"
$PipExe    = "C:\Users\jenge\anaconda3\envs\job-matcher\Scripts\pip.exe"

Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host "               JOB MATCHER AUTOMATION RUNNER                  " -ForegroundColor Cyan
Write-Host "==============================================================" -ForegroundColor Cyan

# 1. Verify Conda Python Environment
if (-not (Test-Path $PythonExe)) {
    Write-Error "Conda environment 'job-matcher' not found at $PythonExe."
    exit 1
}

if ($InstallDeps) {
    Write-Host "`n[1/5] Installing/Updating Python dependencies from requirements.txt..." -ForegroundColor Yellow
    & $PipExe install -r (Join-Path $ProjectRoot "requirements.txt")
} else {
    Write-Host "`n[1/5] Python environment verified: $PythonExe" -ForegroundColor Green
}

# 2. Ensure .env configuration file exists
$EnvFile = Join-Path $ProjectRoot ".env"
$EnvExample = Join-Path $ProjectRoot ".env.example"
if (-not (Test-Path $EnvFile) -and (Test-Path $EnvExample)) {
    Copy-Item $EnvExample $EnvFile
    Write-Host "      Created .env from .env.example." -ForegroundColor Yellow
}

# 3. Auto-detect resume changes in data/ to trigger automatic re-scoring
$DataDir = Join-Path $ProjectRoot "data"
$HashStateFile = Join-Path $DataDir ".last_resume_hash"

$ActiveResume = & $PythonExe -c "from src.scoring.embed_scorer import discover_resume_file; print(discover_resume_file())" 2>$null
if ($LASTEXITCODE -eq 0 -and $ActiveResume) {
    $ActiveResume = $ActiveResume.Trim()
    Write-Host "`n[2/5] Active Resume detected: $ActiveResume" -ForegroundColor Green

    if (Test-Path $ActiveResume) {
        $CurrentHash = (Get-FileHash -Path $ActiveResume -Algorithm SHA256).Hash
        $PreviousHash = if (Test-Path $HashStateFile) { (Get-Content $HashStateFile -Raw).Trim() } else { "" }

        if ($PreviousHash -and ($CurrentHash -ne $PreviousHash)) {
            Write-Host "      Resume change detected since last run! Automatically enabling --rescore." -ForegroundColor Yellow
            $Rescore = $true
        }
        Set-Content -Path $HashStateFile -Value $CurrentHash -NoNewline
    }
} else {
    Write-Host "`n[2/5] Warning: Could not resolve active resume in data/." -ForegroundColor Yellow
}

# 4. Ensure Docker Desktop & MySQL 8.0 Container are Running
Write-Host "`n[3/5] Checking MySQL 8.0 database on localhost:3306..." -ForegroundColor Cyan
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
    Write-Host "      Starting Docker Desktop & MySQL 8.0 container (job-matcher-mysql)..." -ForegroundColor Yellow
    & docker desktop start
    & docker compose up -d --wait
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to start MySQL 8.0 container via Docker Compose."
        exit 1
    }
}
Write-Host "      MySQL 8.0 is online and healthy." -ForegroundColor Green

# 5. Optional QA Test Suite Execution
if ($RunTests) {
    Write-Host "`n[4/5] Running Pytest QA Test Suite..." -ForegroundColor Cyan
    & $PythonExe -m pytest
    if ($LASTEXITCODE -ne 0) {
        Write-Error "QA Test Suite failed! Aborting pipeline run."
        exit 1
    }
} else {
    Write-Host "`n[4/5] Skipping QA test suite (pass -RunTests to execute 75 unit tests)." -ForegroundColor DarkGray
}

# 6. Run End-to-End Job Matcher Pipeline
Write-Host "`n[5/5] Running Job Matcher Pipeline..." -ForegroundColor Cyan
$PipelineArgs = @("-m", "src.main", "--min-score", "$MinScore")
if ($Rescore) {
    $PipelineArgs += "--rescore"
}
if ($SkipIngest) {
    $PipelineArgs += "--skip-ingest"
}
if ($ResumePath -ne "") {
    $PipelineArgs += @("--resume", $ResumePath)
}

& $PythonExe @PipelineArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "Job Matcher pipeline exited with code $LASTEXITCODE."
    exit $LASTEXITCODE
}

# 7. Open Today's Generated Markdown Digest
$TodayDate = (Get-Date).ToString("yyyy-MM-dd")
$ReportPath = Join-Path $ProjectRoot "reports\daily_matches_$TodayDate.md"

if ((Test-Path $ReportPath) -and (-not $NoOpenReport)) {
    Write-Host "`nOpening daily report: $ReportPath" -ForegroundColor Green
    Invoke-Item $ReportPath
}
