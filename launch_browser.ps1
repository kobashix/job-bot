# Job Bot 2.0 - Launch Browser with Remote Debugging

$port = 9223
$edgePath = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
$chromePath = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"

if (Test-Path $edgePath) {
    Write-Host "Launching Edge on port $port..." -ForegroundColor Cyan
    Start-Process $edgePath -ArgumentList "--remote-debugging-port=$port", "--user-data-dir=$env:TEMP\job-bot-browser"
} elseif (Test-Path $chromePath) {
    Write-Host "Launching Chrome on port $port..." -ForegroundColor Cyan
    Start-Process $chromePath -ArgumentList "--remote-debugging-port=$port", "--user-data-dir=$env:TEMP\job-bot-browser"
} else {
    Write-Error "Could not find Microsoft Edge or Google Chrome."
    Write-Host "Please launch your browser manually with: --remote-debugging-port=$port"
}
