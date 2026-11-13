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
    price = farm.last_price
    start = farm.config.start_capital
    total = farm.total_wealth(price)
    lines = [
        f"Stand: {farm.last_ts}  ({farm.bar_count} Kerzen verarbeitet)",
        f"Gesamtvermoegen: {total:.2f}  (Start {start:.2f}, {total / start - 1:+.1%})",
        f"  davon gesichert: {farm.vault:.2f}",
        f"  in Bots:         {sum(b.account.equity(price) for b in farm.alive):.2f}",
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
            f"{b.id:>4} {b.parent_id or '-':>6} {status:<10} {b.start_capital:>8.2f} {b.account.equity(price):>8.2f} "
            f"{b.secured:>9.2f} {b.total_profit(price):>+8.2f}  {strategies.describe(b.genome)}"
        )
    return "\n".join(lines)


def _config(args) -> FarmConfig:
    config = FarmConfig(start_capital=args.capital, use_llm=args.llm, seed=args.seed)
    if args.milestones:
        config.milestones = tuple(sorted(float(x) for x in args.milestones.split(",")))
    return config


def _load_data(args, bars: int):
    if args.csv:
        return data.from_csv(args.csv)
    if args.exchange:
        return data.from_exchange(args.exchange, args.symbol, args.timeframe, bars)
    return data.synthetic(bars, seed=args.seed)


def simulate(args) -> Farm:
    df = _load_data(args, args.bars)
    first = min(WARMUP + 1000, len(df) // 2)
    farm = Farm.start(_config(args), df, first - 1)
    shown = _print_events(farm, 0) if args.verbose else 0
    for i in range(first, len(df)):
        farm.step(df, i)
        if args.verbose:
            shown = _print_events(farm, shown)
    print(report(farm))
    hold = float(df["close"].iloc[-1] / df["close"].iloc[first - 1] - 1)
    print(f"\nZum Vergleich: einfach kaufen und halten haette {hold:+.1%} gebracht.")
    return farm


def paper(args) -> None:
    _run(args, live=False)


def live(args) -> None:
    _run(args, live=True)


def _run(args, live: bool) -> None:
    from .live import LiveExecutor, connect

    if not args.exchange:
        raise SystemExit("Bitte --exchange angeben (z. B. --exchange bitget --symbol BTC/USDT)")
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
        executor = LiveExecutor(connect(args.exchange, args.uta), args.symbol, args.capital, live_file, args.real_orders)
        if args.real_orders and not os.path.exists(state):
            executor.check_balance()
        print("ECHTE ORDERS sind eingeschaltet." if args.real_orders
              else "Probelauf: Orders werden nur angezeigt, nicht abgeschickt (--real-orders schaltet sie ein).")

    farm = Farm.load(state) if os.path.exists(state) else None
    shown = len(farm.events) if farm else 0
    while True:
        try:
            df = data.from_exchange(args.exchange, args.symbol, args.timeframe, 1500)
        except Exception as exc:  # Netzwerkfehler: spaeter erneut versuchen
            print(f"Kursdaten nicht abrufbar ({type(exc).__name__}: {exc}).")
            if args.once:
                raise SystemExit(1)
            time.sleep(args.interval)
            continue
        if farm is None:
            farm = Farm.start(_config(args), df, len(df) - 1)
        else:
            new = [i for i in range(len(df)) if str(df["timestamp"].iloc[i]) > farm.last_ts]
            if live and len(new) > 1:
                # Nach einer Pause wurde in der Zwischenzeit nichts echt gehandelt. Deshalb werden die
                # verpassten Kerzen nicht nachgespielt, sonst liefen Buch und echtes Konto auseinander.
                print(f"  {len(new) - 1} verpasste Kerzen uebersprungen (Bot war nicht aktiv).")
                new = new[-1:]
            for i in new:
                farm.step(df, i)
        shown = _settle(farm, executor, state, stop_file, check_file, shown, summary=True)
        if farm.halted or args.once:
            return
        # Bis zur naechsten Kerzen-Abfrage: Kurs in Echtzeit verfolgen.
        deadline = time.time() + args.interval
        while time.time() < deadline and not farm.halted:
            time.sleep(args.tick)
            try:
                price = data.last_price(args.exchange, args.symbol)
            except Exception:
                continue  # kurzer Netzwerkfehler: beim naechsten Tick erneut
            farm.tick(price, time.strftime("%Y-%m-%d %H:%M:%S"))
            shown = _settle(farm, executor, state, stop_file, check_file, shown, summary=False)
        if farm.halted:
            return


def _settle(farm: Farm, executor, state: str, stop_file: str, check_file: str, shown: int, summary: bool) -> int:
    """Nach jedem Schritt: Not-Aus pruefen, speichern, Ereignisse zeigen, echte Position angleichen."""
    if os.path.exists(stop_file) and not farm.halted:
        farm.halt("Not-Aus per STOP-Datei")
    farm.save(state)
    shown = _print_events(farm, shown)
    line = (f"{farm.last_ts}  Kurs {farm.last_price:.2f}  Vermoegen {farm.total_wealth():.2f}  "
            f"gesichert {farm.vault:.2f}  Bots {len(farm.alive)}")
    if executor:
        try:
            order = executor.sync(farm, farm.last_price)
        except RuntimeError as exc:  # unklare Order: sperren, bis ein Mensch nachgesehen hat
            with open(check_file, "w") as fh:
                fh.write(str(exc))
            raise SystemExit(str(exc))
        if order:
            verb = "ORDER" if order["real"] else "Probelauf, wuerde ordern"
            side = "Kauf" if order["side"] == "buy" else "Verkauf"
            print(f"  {verb}: {side} {order['amount']:.8f} zu {order['price']:.2f} ({order['cost']:.2f})")
        executor.save()
        line += f"  echt {executor.value(farm.last_price):.2f}"
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
        value = real["cash"] + real["units"] * farm.last_price
        print(f"\nEchtes Konto ({real['symbol']}): Wert {value:.2f} bei Budget {real['budget']:.2f}, "
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
        p.add_argument("--symbol", default="BTC/EUR")
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
