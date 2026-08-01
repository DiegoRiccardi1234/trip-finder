# TripFinder.spec — bundle Windows autonomo.
#
#   pyinstaller TripFinder.spec --noconfirm
#
# Il punto d'ingresso e' lo stesso lanciatore usato da sorgente
# (`scripts/launch_tray.py`): dichiara il workspace, avvia uvicorn su un thread
# e cede il thread principale a pystray, che su Windows ha bisogno lui del ciclo
# dei messaggi. Un secondo eseguibile, `Aggiorna.exe`, sostituisce il primo
# quando arriva una versione nuova: non puo' essere lo stesso programma, perche'
# un eseguibile in esecuzione non si sovrascrive da solo.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Gli adapter degli operatori si scoprono a runtime con `pkgutil.iter_modules`
# (app/providers/registry.py): non essendo mai importati per nome, se non
# entrano nel bundle il programma parte con **zero** operatori.
#
# `collect_submodules("app.providers")` qui non li trova — verificato: nel TOC
# non compariva nemmeno `app.providers.rail.trenitalia` — perche' importa il
# pacchetto e si arrende in silenzio se qualcosa non gli torna. Si guardano
# quindi i file, che e' l'unica fonte che non mente.
def _adapters() -> list[str]:
    trovati: list[str] = []
    for pacchetto in ("rail", "bus", "air", "ferry", "aggregator"):
        cartella = Path("app/providers") / pacchetto
        for file in sorted(cartella.glob("*.py")):
            if not file.stem.startswith("_"):
                trovati.append(f"app.providers.{pacchetto}.{file.stem}")
    if not trovati:
        raise SystemExit("nessun adapter trovato: il bundle sarebbe inutile")
    print(f"adapter inclusi: {len(trovati)}")
    return trovati


hiddenimports: list[str] = _adapters()
hiddenimports += collect_submodules("app.providers")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.protocols",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "aiosqlite",
    "orjson",
    "curl_cffi",
    "sse_starlette",
    # Icona nell'area di notifica: il backend Win32 viene importato per
    # piattaforma, quindi va nominato a mano.
    "pystray",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
]

datas: list[tuple[str, str]] = [
    ("app/static", "app/static"),
    # Dati del progetto, non dell'utente: correzioni geografiche, rotte note e
    # l'orario delle FAL ricavato dai loro manifesti.
    ("app/geo/overrides.json", "app/geo"),
    ("app/providers/known_routes.json", "app/providers"),
    ("app/providers/rail/data", "app/providers/rail/data"),
]
# curl_cffi porta con se' i binari di libcurl: senza, ogni richiesta muore.
for pacchetto in ("curl_cffi", "patchright"):
    try:
        datas += collect_data_files(pacchetto)
    except Exception:
        pass

excludes = ["tests", "matplotlib", "tkinter", "PyQt5", "PySide2", "PyQt6", "PySide6"]

analisi_app = Analysis(
    ["scripts/launch_tray.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)

analisi_agg = Analysis(
    ["scripts/updater.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)

# Le dipendenze in comune si scrivono una volta sola: senza, l'aggiornatore
# raddoppierebbe la dimensione dello zip per rifare quello che c'e' gia'.
MERGE((analisi_app, "TripFinder", "TripFinder"), (analisi_agg, "Aggiorna", "Aggiorna"))

pyz_app = PYZ(analisi_app.pure, analisi_app.zipped_data, cipher=block_cipher)
pyz_agg = PYZ(analisi_agg.pure, analisi_agg.zipped_data, cipher=block_cipher)

exe_app = EXE(
    pyz_app,
    analisi_app.scripts,
    [],
    exclude_binaries=True,
    name="TripFinder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Nessuna console, mai: il programma vive nell'area di notifica e una
    # finestra nera aperta per ore non direbbe niente a nessuno. I log vanno in
    # `data/logs/trip-finder.log`, che e' dove si va a guardare quando qualcosa
    # non parte.
    console=False,
    icon=None,
)

exe_agg = EXE(
    pyz_agg,
    analisi_agg.scripts,
    [],
    exclude_binaries=True,
    name="Aggiorna",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
)

COLLECT(
    exe_app,
    exe_agg,
    analisi_app.binaries,
    analisi_app.zipfiles,
    analisi_app.datas,
    analisi_agg.binaries,
    analisi_agg.zipfiles,
    analisi_agg.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TripFinder",
)
