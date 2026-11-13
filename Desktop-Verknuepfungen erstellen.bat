@echo off
rem Doppelklick: legt Verknuepfungen auf dem Desktop an, damit der Bot mit einem Klick startet.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $w=New-Object -ComObject WScript.Shell;" ^
  "foreach($n in 'Spielgeld','Probelauf','ECHTGELD'){" ^
  "  $s=$w.CreateShortcut(\"$d\Bot starten - $n.lnk\");" ^
  "  $s.TargetPath=\"%~dp0Bot starten - $n.bat\"; $s.WorkingDirectory=\"%~dp0\"; $s.Save() };" ^
  "$s=$w.CreateShortcut(\"$d\Bot stoppen (Not-Aus).lnk\");" ^
  "$s.TargetPath=\"%~dp0Bot stoppen (Not-Aus).bat\"; $s.WorkingDirectory=\"%~dp0\"; $s.Save()"
echo Fertig. Auf dem Desktop liegen jetzt vier Verknuepfungen.
pause
