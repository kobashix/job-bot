# Job Bot 2.0 - Launch Browser with Remote Debugging

$port = 9223
$edgePath = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
$chromePath = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"

# 1. Check if port is already in use
try {
    $portCheck = Get-NetTCPConnection -LocalPort $port -ErrorAction Stop
    if ($portCheck) {
        Write-Host "[INFO] Port $port is already in use. A browser may already be running." -ForegroundColor Yellow
        Write-Host "If the bot still fails to connect, try closing all browser windows and running this script again." -ForegroundColor Gray
    }
} catch {
    # Port is free, proceed with launch
    if (Test-Path $edgePath) {
        Write-Host "Launching Edge on port $port..." -ForegroundColor Cyan
        Start-Process $edgePath -ArgumentList "--remote-debugging-port=$port", "--user-data-dir=$env:TEMP\job-bot-browser"
    } elseif (Test-Path $chromePath) {
        Write-Host "Launching Chrome on port $port..." -ForegroundColor Cyan
        Start-Process $chromePath -ArgumentList "--remote-debugging-port=$port", "--user-data-dir=$env:TEMP\job-bot-browser"
    } else {
        Write-Error "Could not find Microsoft Edge or Google Chrome."
        Write-Host "Please launch your browser manually with: --remote-debugging-port=$port"
        exit 1
    }
}

Write-Host "`n[SUCCESS] Browser setup ready for the bot." -ForegroundColor Green
Write-Host "Make sure you see a browser window open before running the apply script." -ForegroundColor Yellow
