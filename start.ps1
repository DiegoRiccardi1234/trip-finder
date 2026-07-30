# Avvio di Trip Finder. Prepara quello che manca e apre il browser.
#
#   .\start.ps1              icona nell'area di notifica, nessuna finestra
#   .\start.ps1 -Console     server in primo piano, log a schermo (debug)
#   .\start.ps1 -Port 9000   altra porta

param(
    [int]$Port = 8010,
    [switch]$NoBrowser,
    [switch]$Console
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = ".\.venv\Scripts\python.exe"
$pythonw = ".\.venv\Scripts\pythonw.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creo l'ambiente virtuale..." -ForegroundColor Cyan
    python -m venv .venv
    & $python -m pip install --upgrade pip --quiet
    & $python -m pip install -r requirements.txt
}

# L'ambiente puo' essere di una versione precedente, senza le librerie
# dell'icona: si controlla che ci siano invece di scoprirlo con un errore
# invisibile quando la finestra non c'e' piu'.
& $python -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('pystray') and u.find_spec('PIL') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installo le librerie mancanti..." -ForegroundColor Cyan
    & $python -m pip install -r requirements.txt
}

if (-not (Test-Path "data\stations.csv") -or -not (Test-Path "data\airports.csv")) {
    Write-Host "Scarico i dataset geografici (una volta sola, circa 29 MB)..." -ForegroundColor Cyan
    & $python scripts\fetch_datasets.py
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Creato .env. L'IA resta spenta finche' non ci metti OPENROUTER_API_KEY." -ForegroundColor Yellow
}

if (-not $Console) {
    # Modo normale: il server vive dietro l'icona nell'area di notifica, e da
    # li' si chiude. La scelta della porta la fa lo script Python, che deve
    # saperla fare comunque anche quando viene lanciato da solo.
    $argomenti = @("scripts\launch_tray.py")
    if ($PSBoundParameters.ContainsKey("Port")) { $argomenti += @("--port", "$Port") }
    if ($NoBrowser) { $argomenti += "--no-browser" }

    Start-Process -FilePath $pythonw -ArgumentList $argomenti -WorkingDirectory $PSScriptRoot
    Write-Host "`nTrip Finder sta partendo." -ForegroundColor Green
    Write-Host "Cerca la bussola fra le icone accanto all'orologio: da li' si apre e si chiude." -ForegroundColor DarkGray
    Write-Host "I log stanno in data\logs\trip-finder.log.`n" -ForegroundColor DarkGray
    return
}

# La porta puo' essere occupata da un altro programma, o da un Trip Finder
# rimasto aperto: invece di morire con "address already in use" si cerca la
# prima libera. Chi ha chiesto una porta esplicita se la tiene.
function Test-PortaLibera([int]$numero) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $numero)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

if (-not $PSBoundParameters.ContainsKey("Port")) {
    $scelta = $Port
    while ($scelta -lt $Port + 20 -and -not (Test-PortaLibera $scelta)) { $scelta++ }
    if ($scelta -ne $Port) {
        Write-Host "La porta $Port e' occupata: uso la $scelta." -ForegroundColor Yellow
        $Port = $scelta
    }
}

$url = "http://127.0.0.1:$Port/"
Write-Host "`nTrip Finder su $url" -ForegroundColor Green
Write-Host "Chiudi questa finestra (o Ctrl+C) per fermarlo.`n" -ForegroundColor DarkGray

if (-not $NoBrowser) {
    # Il browser si apre quando il server risponde davvero, non dopo un numero
    # fisso di secondi: la prima volta l'indice geografico ci mette un po' a
    # costruirsi, e una pagina aperta troppo presto mostra un errore di
    # connessione che sembra un guasto e non lo e'.
    Start-Job -ScriptBlock {
        $scadenza = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $scadenza) {
            try {
                Invoke-WebRequest -Uri $using:url -UseBasicParsing -TimeoutSec 3 | Out-Null
                Start-Process $using:url
                return
            } catch {
                Start-Sleep -Milliseconds 700
            }
        }
    } | Out-Null
}

& $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
