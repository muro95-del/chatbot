@echo off
rem Doppelklick: Not-Aus. Der Bot verkauft alles und handelt nicht mehr.
if not exist "%~dp0botfarm_state" mkdir "%~dp0botfarm_state"
type nul > "%~dp0botfarm_state\STOP"
echo Not-Aus ausgeloest. Der laufende Bot verkauft beim naechsten Durchlauf alles und haelt an.
echo Das kann bis zu 5 Minuten dauern.
pause
