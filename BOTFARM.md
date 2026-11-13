# Bot-Farm

Ein Start-Bot handelt eine Kryptowährung. Verdient er Geld, sichert er einen Teil davon und
gründet mit einem weiteren Teil Kind-Bots mit abgewandelten Strategien. Bots, die verlieren,
werden beendet.

Es gibt drei Betriebsarten: Spielgeld (`paper`), Probelauf an der echten Börse (`live`, zeigt
Orders nur an) und echte Orders (`live --real-orders`). Die Anbindung für echte Orders ist nur
gegen eine nachgebaute Börse getestet, noch nie gegen eine echte.

## Windows: per Doppelklick starten

Im Ordner liegen drei Dateien zum Doppelklicken:

- `Bot starten - Probelauf.bat`: verbindet sich mit Bitget, kauft aber nichts.
- `Bot starten - ECHTGELD.bat`: handelt mit echtem Geld, nach Eingabe von `JA`.
- `Bot stoppen (Not-Aus).bat`: der laufende Bot verkauft alles und hält an.

Beim ersten Start werden die Pakete installiert und die Bitget-API-Daten abgefragt. Sie werden
im Windows-Benutzerkonto gespeichert, nicht im Ordner. Budget und Handelspaar stehen oben in
`windows/run.bat`. Das Fenster muss offen bleiben, solange der Bot laufen soll.

## Starten

```bash
pip install -r requirements-botfarm.txt
```

Test auf alten Kursen einer Börse (hier Bitget, Bitcoin gegen USDT):

```bash
python -m botfarm simulate --exchange bitget --symbol BTC/USDT --bars 8000 -v
```

Dauerbetrieb mit Spielgeld (prüft alle 5 Minuten auf neue Stundenkerzen, Stand liegt in `botfarm_state/`):

```bash
python -m botfarm paper --exchange bitget --symbol BTC/USDT --capital 100
```

Stand ansehen:

```bash
python -m botfarm status
```

Dashboard im Browser (Vermögen, Tresor, alle Bots, Ereignisse; lädt jede Minute neu):

```bash
streamlit run dashboard.py
```

Läuft noch keine Farm, zeigt das Dashboard eine Demo-Simulation mit Zufallskursen.

Not-Aus (alle Positionen verkaufen, nichts mehr handeln):

```bash
touch botfarm_state/STOP
```

Ohne `--exchange` läuft `simulate` auf künstlichen Zufallskursen. Das zeigt nur, dass die
Mechanik funktioniert, und sagt nichts über Gewinne aus.

## Echtes Geld

1. Auf der Börse einen API-Schlüssel anlegen, der nur Spot-Handel darf. **Auszahlungen nicht
   erlauben.**
2. Schlüssel nur lokal als Umgebungsvariablen setzen (Bitget braucht auch die Passphrase):

```bash
export BOTFARM_API_KEY="..."
export BOTFARM_API_SECRET="..."
export BOTFARM_API_PASSPHRASE="..."
```

3. Probelauf: verbindet sich mit dem Konto, zeigt aber nur an, was er ordern würde.

```bash
python -m botfarm live --exchange bitget --symbol BTC/USDT --capital 50 --dashboard
```

   Zeigt Bitget beim Erstellen des Schlüssels „Unified trading account“ statt „Spot“, läuft dein
   Konto als Unified Trading Account. Dann bei jedem `live`-Befehl `--uta` anhängen und beim
   Schlüssel unter „Unified trading account“ **Manage** (Kontostand) und **Trade** (Orders)
   anhaken.

4. Echte Orders: vorher den Ordner `botfarm_state/` vom Probelauf verschieben oder löschen.

```bash
python -m botfarm live --exchange bitget --symbol BTC/USDT --capital 50 --dashboard --real-orders
```

So ist das Geld geschützt:

- `--capital` ist das Budget. Der Bot kauft nie für mehr, auch wenn mehr auf dem Konto liegt.
  Gesicherter Gewinn bleibt als USDT auf der Börse liegen und wird nicht mehr gehandelt.
- Beim ersten Start prüft er, ob das Budget frei auf dem Konto liegt.
- Alle Bots handeln dasselbe Paar. Auf der Börse gibt es deshalb nur eine gemeinsame Position;
  wer welchen Anteil hat, steht in der Buchführung der Farm.
- Ist nach einer Order unklar, ob sie ausgeführt wurde, sperrt sich der Bot
  (`botfarm_state/BOERSE_PRUEFEN`), statt ein zweites Mal zu kaufen.
- War der Bot aus, spielt er verpasste Kerzen nicht nach, sondern macht beim aktuellen Kurs
  weiter.
- Not-Aus per `STOP`-Datei verkauft auch die echte Position.

Buchführung und echtes Konto weichen leicht voneinander ab, weil echte Orders nie genau zum
Schlusskurs ausgeführt werden. `status` und das Dashboard zeigen beide Werte.

## Wie es funktioniert

**Geld.** Der erste Bot bekommt das Startgeld (Standard 100). Von außen kommt nie Geld dazu.
Jeder Euro liegt in genau einem von drei Töpfen: in einem Bot, im Tresor (gesichert, wird nie
wieder gehandelt) oder in der Reserve (Restgeld gestorbener Bots).

**Gewinnsicherung.** Erreicht der Gewinn eines Bots eine Stufe, wandert ein Teil des Gewinns in
den Tresor. Wie viel, hängt von der Aussicht ab:

- Läuft der Bot gerade gut (zuletzt im Plus, kaum Rückschlag), sichert er nur 15 % und handelt
  mit dem Rest weiter auf die nächste Stufe zu.
- Läuft er schwach, sichert er bis zu 50 %.
- Fällt der Gewinn um 30 % vom Höchststand zurück, sichert er sofort 50 % des verbliebenen
  Gewinns, auch ohne Stufe.

Die Stufen sind Vielfache des Startgelds, standardmäßig 0,25 / 0,5 / 1 / 2 / 3 / 5 / 10. Mit
`--milestones 3,10` wird bei 100 Start erst bei 300 und 1000 Gewinn gesichert.

**Sterben.** Ein Bot wird beendet, wenn er 30 % von seinem Höchststand verliert oder vier
Wochen in Folge keinen Gewinn macht. Sein Restgeld geht an den Eltern-Bot oder in die Reserve.

**Vermehrung.** Einmal pro Woche darf ein Bot mit mindestens 15 % Gewinn im Konto ein Kind
gründen. Es bekommt die Hälfte dieses Gewinns, nie das Startgeld. Dafür werden zwölf
Strategie-Varianten auf den letzten 1000 Kerzen getestet; nur die beste, die dort Gewinn bei
begrenztem Einbruch macht, wird genommen. Das Kind handelt zuerst eine Woche mit Spielgeld und
bekommt sein Geld erst, wenn es dabei im Plus liegt. Mit `--llm` schlägt zusätzlich Claude
Varianten vor (braucht `ANTHROPIC_API_KEY`); sie müssen denselben Test bestehen.

**Sicherheit.** Höchstens 8 Bots gleichzeitig. Fällt das Gesamtvermögen unter 60 % des
Startgelds, hält die Farm alles an.

Alle Werte stehen in `botfarm/config.py`.

## Strategien

Vier einfache Familien, jeweils nur Kaufen und Halten oder Nicht-Halten (kein Hebel, kein
Leerverkauf): Kreuzung gleitender Durchschnitte, RSI-Gegenbewegung, Ausbruch aus einer
Kursspanne (Donchian) und Bollinger-Bänder, jeweils mit optionalem Trendfilter.

## Grenzen

- Ein bestandener Backtest ist keine Gewinngarantie. Die Auswahl der besten Variante aus
  vielen neigt dazu, Zufallstreffer zu bevorzugen; die Probezeit fängt das nur teilweise ab.
- Gehandelt wird zum Schlusskurs der Kerze mit pauschal 0,1 % Gebühr und 0,05 % Kursabweichung.
  Echte Ausführungen können schlechter sein.
- Im Dauerbetrieb werden die Signale auf den letzten 1500 Kerzen berechnet.

## Tests

```bash
python -m pytest tests -q
```
