@echo off
rem Doppio click per avviare Trip Finder: prepara quello che manca, avvia il
rem server e apre il browser da solo. Windows apre i .ps1 nell'editor invece di
rem eseguirli, quindi serve questo passaggio per farlo partire con un doppio
rem click. Questa finestra si chiude da sola: da li' in poi il server vive
rem dietro l'icona a forma di bussola accanto all'orologio, e da quella si
rem chiude. Per vedere i log a schermo: start.ps1 -Console
title Trip Finder
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*

rem Se e' andato storto qualcosa la finestra resta aperta, altrimenti il
rem messaggio d'errore sparirebbe insieme alla finestra.
if errorlevel 1 (
    echo.
    echo Trip Finder si e' fermato con un errore. Il testo qui sopra dice perche'.
    echo.
    pause
)
