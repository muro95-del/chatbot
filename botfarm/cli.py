"""Kommandozeile: simulate (alte Kurse), paper (Spielgeld), live (echte Boerse), status."""

from __future__ import annotations

import argparse
import json
import os
import time

from . import data, strategies
from .config import FarmConfig
from .farm import WARMUP, Farm

STATE_DIR = os.environ.get("BOTFARM_STATE_DIR", "botfarm_state")


def _print_events(farm: Farm, start: int) -> int:
    for event in farm.events[start:]:
        who = f"Bot {event['bot']}" if event["bot"] else "Farm"
        print(f"  {event['ts']}  {who}: {event['message']}")
    return len(farm.events)


def report(farm: Farm) -> str:
    start = farm.config.start_capital
    total = farm.total_wealth()
    lines = [
        f"Stand: {farm.last_ts}  ({farm.bar_count} Kerzen verarbeitet)",
        f"Gesamtvermoegen: {total:.2f}  (Start {start:.2f}, {total / start - 1:+.1%})",
        f"  davon gesichert: {farm.vault:.2f}",
        f"  in Bots:         {sum(farm.equity_of(b) for b in farm.alive):.2f}",
        f"  freie Reserve:   {farm.treasury:.2f}",
        f"Bots: {len(farm.alive)} am Leben, {len(farm.bots) - len(farm.alive)} gestorben",
    ]
    if farm.halted:
        lines.append(f"ANGEHALTEN: {farm.halted}")
    lines.append("")
    lines.append(f"{'Bot':>4} {'Eltern':>6} {'Status':<10} {'Start':>8} {'Konto':>8} {'Gesichert':>9} {'Gewinn':>8}  Strategie")
    for b in farm.bots:
        status = {"active": "aktiv", "probation": "Probezeit", "dead": "tot"}[b.status]
        lines.append(
            f"{b.id:>4} {b.parent_id or '-':>6} {status:<10} {b.start_capital:>8.2f} {farm.equity_of(b):>8.2f} "
            f"{b.secured:>9.2f} {farm.profit_of(b):>+8.2f}  {strategies.describe(b.genome)}"
        )
    return "\n".join(lines)


def _symbols(args) -> tuple[str, ...]:
    return tuple(x.strip() for x in (args.symbols or args.symbol).split(",") if x.strip())


def _config(args) -> FarmConfig:
    minutes = {"m": 1, "h": 60, "d": 1440}[args.timeframe[-1]] * int(args.timeframe[:-1])
    config = FarmConfig(start_capital=args.capital, use_llm=args.llm, seed=args.seed, symbols=_symbols(args),
                        max_leverage=max(1, args.max_leverage), allow_short=args.short, bar_minutes=minutes)
    if args.milestones:
        config.milestones = tuple(sorted(float(x) for x in args.milestones.split(",")))
    return config


def _load_data(args, bars: int) -> dict:
    symbols = _symbols(args)
    if args.csv:
        return {symbols[0]: data.from_csv(args.csv)}
    if args.exchange:
        return data.frames_from_exchange(args.exchange, symbols, args.timeframe, bars)
    return data.synthetic_frames(symbols, bars, seed=args.seed)


def simulate(args) -> Farm:
    frames = _load_data(args, args.bars)
    length = len(next(iter(frames.values())))
    first = min(WARMUP + 1000, length // 2)
    config = _config(args)
    config.symbols = tuple(frames)
    farm = Farm.start(config, frames, first - 1)
    shown = _print_events(farm, 0) if args.verbose else 0
    for i in range(first, length):
        farm.step(frames, i)
        if args.verbose:
            shown = _print_events(farm, shown)
    print(report(farm))
    print("\nZum Vergleich: einfach kaufen und halten haette gebracht:")
    for symbol, df in frames.items():
        print(f"  {symbol}: {float(df['close'].iloc[-1] / df['close'].iloc[first - 1] - 1):+.1%}")
    return farm


def paper(args) -> None:
    _run(args, live=False)


def live(args) -> None:
    _run(args, live=True)


def _run(args, live: bool) -> None:
    from .live import LiveExecutor, connect

    if not args.exchange:
        raise SystemExit("Bitte --exchange angeben (z. B. --exchange bitget --symbols BTC/USDT,ETH/USDT)")
    if live and (args.max_leverage > 1 or args.short):
        raise SystemExit("Hebel und Short gibt es bisher nur mit Spielgeld (paper). Mit echtem Geld handelt "
                         "der Bot ohne Hebel, bis die Futures-Anbindung gegen die Boerse geprueft ist.")
    state = os.path.join(STATE_DIR, "state.json")
    live_file = os.path.join(STATE_DIR, "live.json")
    stop_file = os.path.join(STATE_DIR, "STOP")
    if live and os.path.exists(live_file):
        with open(live_file) as fh:
            was_real = bool(json.load(fh).get("real_orders"))
        if was_real != bool(args.real_orders):
            if not args.real_orders:
                raise SystemExit("Hier laeuft schon ein Bot mit ECHTEM Geld. Ein Probelauf wuerde dessen Stand ueberschreiben.")
            # Probelauf-Stand nie als echtes Konto weiterfuehren: zur Seite legen, nichts loeschen.
            archive = f"{STATE_DIR}_probelauf_{time.strftime('%Y%m%d_%H%M%S')}"
            os.replace(STATE_DIR, archive)
            print(f"Alter Probelauf-Stand wurde nach {archive} verschoben. Echtgeld startet frisch.")
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(state) and os.path.exists(live_file) != live:
        other = "echtem Geld" if not live else "Spielgeld"
        raise SystemExit(f"In {STATE_DIR}/ liegt ein Stand mit {other}. Verschiebe oder loesche den Ordner zuerst.")

    check_file = os.path.join(STATE_DIR, "BOERSE_PRUEFEN")
    if live and os.path.exists(check_file):
        raise SystemExit(f"Gesperrt: {open(check_file).read().strip()}\nPruefe dein Boersenkonto und loesche danach {check_file}.")

    dashboard = None
    if args.dashboard:
        import subprocess
        import sys

        app = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.py")
        # headless: sonst fragt Streamlit beim ersten Start im Fenster nach einer E-Mail und blockiert
        dashboard = subprocess.Popen([sys.executable, "-m", "streamlit", "run", app, "--server.headless", "true",
                                      "--browser.gatherUsageStats", "false", "--server.port", "8501"])
        import threading
        import webbrowser

        threading.Timer(4, webbrowser.open, ["http://localhost:8501"]).start()
    try:
        _loop(args, live, state, live_file, stop_file, check_file)
    finally:
        if dashboard:
            dashboard.terminate()


def _loop(args, live: bool, state: str, live_file: str, stop_file: str, check_file: str) -> None:
    from .live import LiveExecutor, connect

    executor = None
    if live:
        executor = LiveExecutor(connect(args.exchange, args.uta), _symbols(args), args.capital, live_file, args.real_orders)
        if args.real_orders and not os.path.exists(state):
            executor.check_balance()
        print("ECHTE ORDERS sind eingeschaltet." if args.real_orders
              else "Probelauf: Orders werden nur angezeigt, nicht abgeschickt (--real-orders schaltet sie ein).")

    farm = Farm.load(state) if os.path.exists(state) else None
    shown = len(farm.events) if farm else 0
    while True:
        try:
            frames = data.frames_from_exchange(args.exchange, _symbols(args), args.timeframe, 1500)
            df = next(iter(frames.values()))
        except Exception as exc:  # Netzwerkfehler: spaeter erneut versuchen
            print(f"Kursdaten nicht abrufbar ({type(exc).__name__}: {exc}).")
            if args.once:
                raise SystemExit(1)
            time.sleep(args.interval)
            continue
        wanted = _config(args)
        if farm is None:
            wanted.symbols = tuple(frames)
            farm = Farm.start(wanted, frames, len(df) - 1)
        else:
            # Coins und Hebel gelten immer so, wie der Bot gerade gestartet wurde.
            farm.config.symbols = tuple(frames)
            farm.config.max_leverage, farm.config.allow_short = wanted.max_leverage, wanted.allow_short
            farm.config.bar_minutes = wanted.bar_minutes
            new = [i for i in range(len(df)) if str(df["timestamp"].iloc[i]) > farm.last_ts]
            if live and len(new) > 1:
                # Nach einer Pause wurde in der Zwischenzeit nichts echt gehandelt. Deshalb werden die
                # verpassten Kerzen nicht nachgespielt, sonst liefen Buch und echtes Konto auseinander.
                print(f"  {len(new) - 1} verpasste Kerzen uebersprungen (Bot war nicht aktiv).")
                new = new[-1:]
            for i in new:
                farm.step(frames, i)
        shown = _settle(farm, executor, state, stop_file, check_file, shown, summary=True)
        if farm.halted or args.once:
            return
        # Bis zur naechsten Kerzen-Abfrage: Kurs in Echtzeit verfolgen.
        deadline = time.time() + args.interval
        while time.time() < deadline and not farm.halted:
            time.sleep(args.tick)
            try:
                prices = data.last_prices(args.exchange, farm.config.symbols)
            except Exception:
                continue  # kurzer Netzwerkfehler: beim naechsten Tick erneut
            farm.tick(prices, time.strftime("%Y-%m-%d %H:%M:%S"))
            shown = _settle(farm, executor, state, stop_file, check_file, shown, summary=False)
        if farm.halted:
            return


def _settle(farm: Farm, executor, state: str, stop_file: str, check_file: str, shown: int, summary: bool) -> int:
    """Nach jedem Schritt: Not-Aus pruefen, speichern, Ereignisse zeigen, echte Positionen angleichen."""
    if os.path.exists(stop_file) and not farm.halted:
        farm.halt("Not-Aus per STOP-Datei")
    farm.save(state)
    shown = _print_events(farm, shown)
    line = (f"{farm.last_ts}  Vermoegen {farm.total_wealth():.2f}  "
            f"gesichert {farm.vault:.2f}  Bots {len(farm.alive)}")
    if executor:
        try:
            orders = executor.sync(farm, farm.prices)
        except RuntimeError as exc:  # unklare Order: sperren, bis ein Mensch nachgesehen hat
            with open(check_file, "w") as fh:
                fh.write(str(exc))
            raise SystemExit(str(exc))
        for order in orders:
            verb = "ORDER" if order["real"] else "Probelauf, wuerde ordern"
            side = "Kauf" if order["side"] == "buy" else "Verkauf"
            print(f"  {verb}: {side} {order['amount']:.8f} {order['symbol']} zu {order['price']:.4f} ({order['cost']:.2f})")
        if summary:  # einmal pro Kerzen-Abfrage den echten Kontostand holen
            try:
                acct = executor.refresh_balance()
                coins = "".join(f" + {v:.6f} {c}" for c, v in acct["coins"].items() if v)
                line += f"  | Bitget-Konto: {acct['quote_total']:.2f} {acct['quote']}{coins}"
            except Exception as exc:
                print(f"  Kontostand nicht abrufbar ({type(exc).__name__}: {exc}).")
        executor.save()
        line += f"  | Bot-Anteil {executor.value(farm.prices):.2f}"
    if summary:
        print(line)
    return shown


def status(_args) -> None:
    state = os.path.join(STATE_DIR, "state.json")
    if not os.path.exists(state):
        raise SystemExit("Noch kein gespeicherter Stand. Starte zuerst 'paper'.")
    farm = Farm.load(state)
    print(report(farm))
    live_file = os.path.join(STATE_DIR, "live.json")
    if os.path.exists(live_file):
        import json

        with open(live_file) as fh:
            real = json.load(fh)
        units = real["units"] if isinstance(real["units"], dict) else {real.get("symbol", ""): real["units"]}
        value = real["cash"] + sum(u * farm.prices.get(sym, 0.0) for sym, u in units.items())
        print(f"\nAnteil des Bots: Wert {value:.2f} bei Budget {real['budget']:.2f}, "
              f"Gebuehren bisher {real['fees']:.2f}, {len(real['orders'])} Orders")
    print("\nLetzte Ereignisse:")
    _print_events(farm, max(0, len(farm.events) - 15))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="botfarm", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("simulate", simulate), ("paper", paper), ("live", live), ("status", status)):
        p = sub.add_parser(name)
        p.set_defaults(func=func)
        if name == "status":
            continue
        p.add_argument("--capital", type=float, default=100.0, help="Startgeld des ersten Bots")
        p.add_argument("--exchange", help="Boerse fuer echte Kursdaten, z. B. kraken oder binance")
        p.add_argument("--symbol", default="BTC/USDT", help="ein einzelner Coin")
        p.add_argument("--symbols", help="mehrere Coins, mit Komma getrennt, z. B. BTC/USDT,ETH/USDT,SOL/USDT")
        p.add_argument("--max-leverage", type=int, default=1, help="hoechster Hebel (1 = ohne Hebel)")
        p.add_argument("--short", action="store_true", help="auch auf fallende Kurse setzen")
        p.add_argument("--timeframe", default="1h", help="Kerzenlaenge, z. B. 5m, 15m, 1h")
        p.add_argument("--csv", help="Kursdaten aus CSV (timestamp,open,high,low,close,volume)")
        p.add_argument("--llm", action="store_true", help="Claude schlaegt zusaetzliche Strategien vor")
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--milestones", help="Gewinn-Stufen als Vielfache des Startgelds, z. B. 3,10")
        if name == "simulate":
            p.add_argument("--bars", type=int, default=8000)
            p.add_argument("--verbose", "-v", action="store_true", help="alle Ereignisse zeigen")
        else:
            p.add_argument("--interval", type=int, default=60, help="Sekunden zwischen zwei Kerzen-Abfragen")
            p.add_argument("--tick", type=int, default=10, help="Sekunden zwischen zwei Kurs-Abfragen")
            p.add_argument("--once", action="store_true", help="nur einen Durchlauf")
            p.add_argument("--dashboard", action="store_true", help="Dashboard im Browser mitstarten")
        if name == "live":
            p.add_argument("--real-orders", action="store_true", help="echte Orders abschicken (sonst nur Probelauf)")
            p.add_argument("--uta", action="store_true", help="Bitget: Unified Trading Account statt klassischem Spot-Konto")
    args = parser.parse_args(argv)
    args.func(args)
