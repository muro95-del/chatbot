# Bot-Farm

Ein Start-Bot handelt eine Kryptowährung. Verdient er Geld, sichert er einen Teil davon und
gründet mit einem weiteren Teil Kind-Bots mit abgewandelten Strategien. Bots, die verlieren,
werden beendet.

Es gibt drei Betriebsarten: Spielgeld (`paper`), Probelauf an der echten Börse (`live`, zeigt
Orders nur an) und echte Orders (`live --real-orders`). Die Anbindung für echte Orders ist nur
gegen eine nachgebaute Börse getestet, noch nie gegen eine echte.

## Windows: per Doppelklick starten

Im Ordner liegen vier Dateien zum Doppelklicken:

- `Bot starten - Spielgeld.bat`: echte Kurse, erfundenes Geld, braucht keinen Bitget-Schlüssel.
- `Bot starten - Probelauf.bat`: verbindet sich mit Bitget, kauft aber nichts.
- `Bot starten - ECHTGELD.bat`: handelt mit echtem Geld, nach Eingabe von `JA`.
- `Bot stoppen (Not-Aus).bat`: der laufende Bot verkauft alles und hält an.

Beim ersten Start werden die Pakete installiert und die Bitget-API-Daten abgefragt. Sie werden
im Windows-Benutzerkonto gespeichert, nicht im Ordner. Budget, Coins und Hebel stehen oben in
`windows/run.bat`. Das Fenster muss offen bleiben, solange der Bot laufen soll.

## Starten

```bash
pip install -r requirements-botfarm.txt
```

Test auf alten Kursen einer Börse (hier Bitget, Bitcoin gegen USDT):

```bash
python -m botfarm simulate --exchange bitget --symbols BTC/USDT,ETH/USDT,SOL/USDT --bars 8000 -v
```

Dauerbetrieb mit Spielgeld (Stand liegt in `botfarm_state/`):

```bash
python -m botfarm paper --exchange bitget --symbols BTC/USDT,ETH/USDT,SOL/USDT --capital 100
```

Stand ansehen:

```bash
python -m botfarm status
```

Dashboard im Browser (Kurs, Vermögen, Tresor, alle Bots, Ereignisse; live):

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
python -m botfarm live --exchange bitget --symbols BTC/USDT,ETH/USDT,SOL/USDT --capital 50 --dashboard
```

   Zeigt Bitget beim Erstellen des Schlüssels „Unified trading account“ statt „Spot“, läuft dein
   Konto als Unified Trading Account. Dann bei jedem `live`-Befehl `--uta` anhängen und beim
   Schlüssel unter „Unified trading account“ **Manage** (Kontostand) und **Trade** (Orders)
   anhaken.

4. Echte Orders: vorher den Ordner `botfarm_state/` vom Probelauf verschieben oder löschen.

```bash
python -m botfarm live --exchange bitget --symbols BTC/USDT,ETH/USDT,SOL/USDT --capital 50 --dashboard --real-orders
```

So ist das Geld geschützt:

- `--capital` ist das Budget. Der Bot kauft nie für mehr, auch wenn mehr auf dem Konto liegt.
  Gesicherter Gewinn bleibt als USDT auf der Börse liegen und wird nicht mehr gehandelt.
- Beim ersten Start prüft er, ob das Budget frei auf dem Konto liegt.
- Mehrere Bots können denselben Coin handeln. Auf der Börse gibt es je Coin nur eine gemeinsame
  Position; wer welchen Anteil hat, steht in der Buchführung der Farm.
- Ist nach einer Order unklar, ob sie ausgeführt wurde, sperrt sich der Bot
  (`botfarm_state/BOERSE_PRUEFEN`), statt ein zweites Mal zu kaufen.
- War der Bot aus, spielt er verpasste Kerzen nicht nach, sondern macht beim aktuellen Kurs
  weiter.
- Not-Aus per `STOP`-Datei verkauft auch die echte Position.

Buchführung und echtes Konto weichen leicht voneinander ab, weil echte Orders nie genau zum
Schlusskurs ausgeführt werden. `status` und das Dashboard zeigen beide Werte.

## Mehrere Coins, Hebel und Short

- `--symbols BTC/USDT,ETH/USDT,SOL/USDT` gibt die Coins vor. Jeder Bot handelt genau einen Coin.
  Für jeden neuen Bot werden Kandidaten auf allen Coins im Backtest verglichen; der beste gewinnt,
  egal auf welchem Coin.
- `--max-leverage 3` erlaubt Hebel bis 3x, `--short` erlaubt Bots, die auf fallende Kurse setzen.
  Jede Position ist isoliert: Ein Bot kann höchstens sein eigenes Geld verlieren. Fällt die
  Position unter die Mindest-Sicherheit, wird sie liquidiert und der Bot stirbt. Gebühren fallen
  auf die volle, gehebelte Position an, dazu laufende Finanzierungskosten.
- **Hebel und Short gibt es bisher nur mit Spielgeld.** `live` lehnt beides ab und handelt Spot
  ohne Hebel, auf mehreren Coins. Die Futures-Anbindung für echtes Geld fehlt noch.

Auf Zufallskursen (6 Durchläufe, 3 Coins) war die Farm mit 3x-Hebel und Short in 5 von 6
Durchläufen schlechter als ohne Hebel, zweimal mit rund −40 %. Ohne Hebel lag sie zwischen
+41 % und +187 %. Zufallskurse sagen nichts über echte Märkte, aber Hebel erhöht das Risiko
sicher und den Gewinn nicht.

## Strategien und Indikatoren

Sechs Familien: Kreuzung gleitender Durchschnitte (EMA), RSI-Gegenbewegung, Ausbruch aus einer
Kursspanne (Donchian), Bollinger-Bänder, MACD und Supertrend (ATR-basiert), jeweils mit
optionalem Trendfilter. Welcher Indikator mit welchen Werten auf welchem Coin läuft, entscheidet
der Backtest. Es gibt keinen Indikator, der grundsätzlich der beste ist.

## Echtzeit

Im Dauerbetrieb (`paper` und `live`) fragt der Bot alle 10 Sekunden den aktuellen Kurs ab
(`--tick`). Dabei greifen sofort: die Verlustgrenze pro Bot, die Gewinnsicherung und der globale
Stopp. Gekauft und verkauft nach Strategie wird weiterhin am Schluss jeder Kerze; die
Kerzenlänge bestimmt `--timeframe` (die Windows-Startdateien nutzen 15 Minuten). Alle Zeitangaben
der Farm zählen in Kerzen: Bei 15-Minuten-Kerzen dauert eine „Woche“ aus 168 Kerzen 42 Stunden.

Kürzere Kerzen bedeuten mehr Trades und damit mehr Gebühren. Das Dashboard aktualisiert sich
alle 5 Sekunden.

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
