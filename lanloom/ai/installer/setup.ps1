# Lanloom local AI setup for this computer (Windows PowerShell 5.1 or newer).
# Installs a local model server, lets it listen in your network and pairs it with Lanloom.
param(
    [switch]$Yes,
    [string]$Model = ''
)
$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

$LanloomUrl = '__LANLOOM_URL__'
$PairCode = '__PAIR_CODE__'
$Port = __AI_PORT__

function Stop-Setup([int]$Code, [string]$Message) {
    Write-Host $Message -ForegroundColor Red
    exit $Code
}

function Ask([string]$Question, [bool]$Default = $true) {
    if ($Yes) { return $true }
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $a = Read-Host "$Question $hint"
    if ($a -match '^[Yy]') { return $true }
    if ($a -match '^[Nn]') { return $false }
    return $Default
}

# Returns @{ Code = <http status or 0>; Body = <object or $null> }
function Invoke-Api([string]$Path, [hashtable]$Payload) {
    $json = $Payload | ConvertTo-Json -Depth 5 -Compress
    try {
        $r = Invoke-RestMethod -Uri ($LanloomUrl + $Path) -Method Post -Body $json -ContentType 'application/json' -Headers @{ 'X-Lanloom' = '1' } -TimeoutSec 30 -UseBasicParsing
        return @{ Code = 200; Body = $r }
    } catch {
        $code = 0
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        return @{ Code = $code; Body = $null }
    }
}

# ---- 1. hardware
$cs = Get-CimInstance Win32_ComputerSystem
$ramGb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
$cores = [int](Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
$gpu = ''
foreach ($v in @(Get-CimInstance Win32_VideoController)) {
    if ($v.Name -match 'NVIDIA') { $gpu = 'nvidia'; break }
    if ($v.Name -match 'AMD|Radeon') { $gpu = 'amd' }
}
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x86_64' }
$gpuText = if ($gpu) { $gpu } else { 'none' }
Write-Host "This computer: $ramGb GB memory, $cores cores, GPU: $gpuText, $arch (windows)"

# ---- 2. hello
$hw = @{ ram_gb = $ramGb; cores = $cores; gpu = $gpu; arch = $arch; os = 'windows' }
$h = Invoke-Api '/api/ai/pair/hello' @{ code = $PairCode; hw = $hw }
if ($h.Code -eq 403) { Stop-Setup 2 'The pairing code is wrong or expired. Create a new one in Lanloom: Settings > AI > Another computer.' }
if ($h.Code -eq 429) { Stop-Setup 2 'Too many attempts. Wait a minute and run the script again.' }
if ($h.Code -eq 0) { Stop-Setup 2 "Lanloom is not reachable at $LanloomUrl. Check that this computer is in the same network and that Lanloom is running." }
if ($h.Code -ne 200) { Stop-Setup 2 "Lanloom answered with an unexpected status ($($h.Code)) at $LanloomUrl." }
$advice = $h.Body
$modelId = [string]$advice.model
if (-not $modelId) { Stop-Setup 2 'Lanloom did not recommend a model.' }
if ($Model) { $modelId = $Model }
if ($modelId -notmatch '^[A-Za-z0-9._:/-]{1,160}$') { Stop-Setup 1 'The model name contains characters that are not allowed.' }
if ($advice.speed -eq 'unsuitable' -and -not $Model) {
    Stop-Setup 3 'This computer does not have enough memory to run a useful local model. Use a public AI service in Lanloom instead (Settings > AI).'
}
if ($advice.speed -eq 'slow') {
    Write-Host 'Warning: this computer is slow for this model. Answers can take a minute.' -ForegroundColor Yellow
    if (-not (Ask 'Continue anyway?' $true)) { Stop-Setup 3 'Stopped. Use a public AI service in Lanloom instead (Settings > AI).' }
}
Write-Host "Model: $modelId (download about $($advice.download_gb) GB)"

# ---- 3. model server
function Find-Ollama {
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Ollama\ollama.exe", "$env:ProgramFiles\Ollama\ollama.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}
$ollama = Find-Ollama
if (-not $ollama) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Stop-Setup 4 'The local model server is not installed and winget is not available. Download it from https://ollama.com/download, install it and run this script again.'
    }
    if (-not (Ask 'The local model server is not installed. Install it now with winget?' $true)) {
        Stop-Setup 4 'The local model server is required. Install it from https://ollama.com/download and run this script again.'
    }
    & winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
    $ollama = Find-Ollama
    if (-not $ollama) { Stop-Setup 4 'The model server could not be installed. Get it from https://ollama.com/download.' }
}

# ---- 4. listen in the network
Write-Host 'The model server has no password: every device in your network can use it. Only continue in a network you trust.' -ForegroundColor Yellow
if (-not (Ask "Let the model server listen in your network on port $Port?" $true)) {
    Stop-Setup 3 'Stopped. The model server stays reachable only from this computer, so Lanloom cannot use it.'
}
$hostValue = "0.0.0.0:$Port"
[Environment]::SetEnvironmentVariable('OLLAMA_HOST', $hostValue, 'User')
$env:OLLAMA_HOST = $hostValue

Write-Host 'Restarting the model server so it listens in your network ...'
foreach ($name in @('ollama app', 'ollama')) {
    Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
$appExe = Join-Path (Split-Path $ollama -Parent) 'ollama app.exe'
if (Test-Path $appExe) {
    Start-Process -FilePath $appExe -WindowStyle Hidden
} else {
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$fwCmd = "New-NetFirewallRule -DisplayName 'Lanloom local AI' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private"
if ($isAdmin) {
    try {
        if (-not (Get-NetFirewallRule -DisplayName 'Lanloom local AI' -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName 'Lanloom local AI' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
        }
        Write-Host "Firewall rule added for port $Port (private networks only)."
    } catch {
        Write-Host "Could not add the firewall rule. Run this in an administrator PowerShell: $fwCmd" -ForegroundColor Yellow
    }
} else {
    Write-Host "Windows Firewall may block port $Port. If pairing fails, run this in an administrator PowerShell:"
    Write-Host "  $fwCmd"
}

# ---- 5. wait for the server
Write-Host 'Waiting for the model server ...'
$up = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/version" -TimeoutSec 3 -UseBasicParsing | Out-Null
        $up = $true; break
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $up) { Stop-Setup 5 'The model server did not start within 60 seconds.' }

# ---- 6. model
$have = $false
try { $have = [bool](& $ollama list 2>$null | Select-Object -Skip 1 | Where-Object { ($_ -split '\s+')[0] -eq $modelId }) } catch { }
if ($have) {
    Write-Host "Model $modelId is already installed."
} else {
    Write-Host "Downloading $modelId ..."
    & $ollama pull $modelId
    if ($LASTEXITCODE -ne 0) { Stop-Setup 5 "The model $modelId could not be downloaded." }
}

# ---- 7. pair
$p = Invoke-Api '/api/ai/pair' @{ code = $PairCode; port = $Port; model = $modelId }
if ($p.Code -ne 200) { Stop-Setup 5 "Lanloom could not pair with this computer (status $($p.Code))." }
$job = [string]$p.Body.id
if (-not $job) { Stop-Setup 5 'Lanloom did not return a pairing job.' }
Write-Host 'Waiting for Lanloom to reach this computer ...'
for ($n = 0; $n -lt 160; $n++) {
    $s = Invoke-Api '/api/ai/pair/status' @{ code = $PairCode; job = $job }
    if ($s.Code -eq 404) { Stop-Setup 5 'Lanloom does not know this pairing job any more. Run the script again.' }
    if ($s.Code -eq 200) {
        if ($s.Body.state -eq 'done') {
            $secs = [math]::Round([double]$s.Body.result.ms / 1000, 1)
            Write-Host "Lanloom received an answer from the model in $secs s. Local AI is ready." -ForegroundColor Green
            exit 0
        }
        if ($s.Body.state -eq 'error') {
            if ($s.Body.error -eq 'ai.err.lan_unreachable') {
                Write-Host "Lanloom could not reach this computer on port $Port. Windows Firewall is probably blocking it. Run this in an administrator PowerShell:" -ForegroundColor Yellow
                Write-Host "  $fwCmd"
                exit 5
            }
            Stop-Setup 5 "Pairing failed: $($s.Body.error)"
        }
    }
    Start-Sleep -Seconds 3
}
Stop-Setup 5 'Pairing did not finish within 8 minutes.'
