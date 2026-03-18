# Job Bot 2.0 - Windows 11 / PowerShell Setup Script

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   JOB BOT 2.0 - POWERSHELL SETUP      " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. Check for Python
if (!(Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python not found. Please install Python 3.9+ from python.org."
    exit 1
}

# 2. Create Virtual Environment
if (!(Test-Path .venv)) {
    Write-Host "[1/4] Creating virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
} else {
    Write-Host "[1/4] Virtual environment already exists." -ForegroundColor Green
}

# 3. Install Requirements
Write-Host "[2/4] Installing dependencies..." -ForegroundColor Yellow
& .venv\Scripts\pip.exe install -r requirements.txt

# 4. Install Playwright Browsers
Write-Host "[3/4] Installing Playwright browsers..." -ForegroundColor Yellow
& .venv\Scripts\playwright.exe install chromium

# 5. Initialize Database
if (!(Test-Path jobs.db)) {
    Write-Host "[4/4] Initializing database..." -ForegroundColor Yellow
    & .venv\Scripts\python.exe init_db.py
} else {
    Write-Host "[4/4] Database already exists." -ForegroundColor Green
}

Write-Host "`n[SUCCESS] Setup complete! You can now run the bot." -ForegroundColor Green
Write-Host "To run the bot: .\.venv\Scripts\python.exe unified_apply.py" -ForegroundColor Cyan
