@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

rem ============ Einstellungen (hier aendern) ============
set CAPITAL=50
rem Coins, auf denen der Bot handeln darf (mit Komma getrennt, ohne Leerzeichen)
set SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,BNB/USDT,DOGE/USDT
rem Hebel und Short gelten nur fuer SPIELGELD. Mit echtem Geld handelt der Bot ohne Hebel.
set MAX_LEVERAGE=3
set SHORT=--short
rem Kerzenlaenge: 5m, 15m oder 1h. Kuerzer = mehr Trades, aber auch mehr Gebuehren.
set TIMEFRAME=15m
rem =======================================================

title Bot-Farm (%1)

rem --- Python pruefen ---
py -3.14 --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.14 wurde nicht gefunden. Bitte in PowerShell eingeben:  py install 3.14
  pause & exit /b 1
)

rem --- Altes Dashboard beenden, falls noch eins laeuft (sonst zeigt es veralteten Code) ---
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8501" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

rem --- Neueste Version holen (Fehler sind egal, z. B. ohne Internet) ---
git pull --quiet >nul 2>&1

rem --- Pakete einmalig installieren ---
if not exist ".installiert" (
  echo Installiere benoetigte Pakete, das dauert beim ersten Mal ein paar Minuten ...
  py -3.14 -m pip install --quiet -r requirements-botfarm.txt
  if errorlevel 1 ( echo Installation fehlgeschlagen. Bitte Screenshot an Claude schicken. & pause & exit /b 1 )
  echo ok> ".installiert"
)

if /i "%1"=="spielgeld" goto spielgeld

rem --- Bitget-Schluessel einmalig abfragen und im Windows-Benutzerkonto speichern ---
if "%BOTFARM_API_KEY%"=="" (
  echo.
  echo Einmalige Einrichtung: deine Bitget-API-Daten.
  echo Sie werden nur in deinem Windows-Benutzerkonto gespeichert, nicht auf GitHub.
  echo.
  set /p BOTFARM_API_KEY=API-Key einfuegen ^(Rechtsklick^), dann Enter: 
  set /p BOTFARM_API_SECRET=Secret Key einfuegen, dann Enter: 
  set /p BOTFARM_API_PASSPHRASE=Passphrase eintippen, dann Enter: 
)
if "%BOTFARM_API_KEY%"=="" ( echo Kein API-Key eingegeben. & pause & exit /b 1 )
setx BOTFARM_API_KEY "%BOTFARM_API_KEY%" >nul
setx BOTFARM_API_SECRET "%BOTFARM_API_SECRET%" >nul
setx BOTFARM_API_PASSPHRASE "%BOTFARM_API_PASSPHRASE%" >nul

if exist "botfarm_state\STOP" (
  echo.
  echo Der Not-Aus ist noch aktiv ^(Datei botfarm_state\STOP^).
  choice /m "Not-Aus aufheben und weitermachen"
  if errorlevel 2 exit /b 0
  del "botfarm_state\STOP"
)

set EXTRA=
if /i "%1"=="echtgeld" (
  echo.
  echo ================================================================
  echo   ACHTUNG: ECHTES GELD. Der Bot kauft und verkauft Coins auf Bitget
  echo   mit bis zu %CAPITAL% USDT. Verluste sind moeglich.
  echo ================================================================
  set /p OK=Zum Starten JA eintippen und Enter druecken: 
)
if /i "%1"=="echtgeld" (
  if /i not "%OK%"=="JA" ( echo Abgebrochen. & pause & exit /b 0 )
  set EXTRA=--real-orders
)

echo.
echo Bot laeuft. Das Dashboard oeffnet sich gleich im Browser.
echo Dieses Fenster offen lassen. Beenden: Fenster schliessen oder Strg+C.
echo.
py -3.14 -m botfarm live --exchange bitget --symbols "%SYMBOLS%" --timeframe %TIMEFRAME% --capital %CAPITAL% --uta --dashboard %EXTRA%
echo.
echo Der Bot wurde beendet.
pause
exit /b 0

:spielgeld
rem Spielgeld: echte Kurse, erfundenes Geld, kein Bitget-Schluessel noetig. Eigener Ordner fuer den Stand.
set BOTFARM_STATE_DIR=botfarm_state_demo
echo.
echo Bot laeuft mit SPIELGELD (%CAPITAL% USDT, erfunden). Das Dashboard oeffnet sich gleich im Browser.
echo Dieses Fenster offen lassen. Beenden: Fenster schliessen oder Strg+C.
echo.
py -3.14 -m botfarm paper --exchange bitget --symbols "%SYMBOLS%" --timeframe %TIMEFRAME% --capital %CAPITAL% --max-leverage %MAX_LEVERAGE% %SHORT% --dashboard
echo.
echo Der Bot wurde beendet.
pause
