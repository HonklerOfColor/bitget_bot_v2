#!/usr/bin/env python3
"""
Dual-Bot Dashboard — V1 (Spread-Scalper) + V2 (MRv2)
=====================================================
Live-Update alle 5s. Zeigt offene Positionen beider Bots.
Strg+C zum Beenden.
"""
import json
import subprocess
import time
import signal
import sys
import re
from datetime import datetime
from pathlib import Path

V1_DIR = Path("/Users/andreas/bitget_bot_v1")
V2_DIR = Path("/Users/andreas/bitget_bot_V2")
FETCH_SCRIPT = V1_DIR / "fetch_bot.py"
V1_VENV = V1_DIR / ".venv" / "bin" / "python3"
V2_VENV = V2_DIR / ".venv" / "bin" / "python3"
REFRESH = 5


def fetch_bot(bot_dir, venv_python):
    try:
        result = subprocess.run(
            [str(venv_python), str(FETCH_SCRIPT), str(bot_dir)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return [], f"Fehler: {result.stderr.strip()}"
        try:
            return json.loads(result.stdout), None
        except json.JSONDecodeError as e:
            return [], f"JSON-Fehler: {e}"
    except subprocess.TimeoutExpired:
        return [], "Timeout (>10s)"
    except FileNotFoundError:
        return [], f"Python nicht gefunden: {venv_python}"


def fmt(v, decimals=2):
    if v is None:
        return "---"
    return f"{v:.{decimals}f}"


def calc_trade_summary():
    """Alle Trades aus Log/CSV: Summe Gewinne/Verluste."""
    v1_wins = v1_losses = v2_wins = v2_losses = 0.0

    # V1: spread_scalper.log
    try:
        for line in Path(str(V1_DIR / "spread_scalper.log")).read_text().splitlines():
            if "TRADE:" not in line:
                continue
            m = re.search(r"([+-]?[\d.]+) USDT", line)
            if m:
                pnl = float(m.group(1))
                if pnl >= 0:
                    v1_wins += pnl
                else:
                    v1_losses += abs(pnl)
    except:
        pass

    # V2: trades_v2.csv
    try:
        for line in Path(str(V2_DIR / "trades_v2.csv")).read_text().splitlines():
            parts = line.split(",")
            if len(parts) < 7 or parts[0] == "ts":
                continue
            pnl_str = parts[6].strip()
            if pnl_str:
                pnl = float(pnl_str)
                if pnl >= 0:
                    v2_wins += pnl
                else:
                    v2_losses += abs(pnl)
    except:
        pass

    return v1_wins, v1_losses, v2_wins, v2_losses


def render():
    v1_positions, v1_err = fetch_bot(V1_DIR, V1_VENV)
    v2_positions, v2_err = fetch_bot(V2_DIR, V2_VENV)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    v1_w, v1_l, v2_w, v2_l = calc_trade_summary()
    lines = []

    lines.append(f"{'='*80}")
    lines.append(f"  SHORT_BOT {v1_w-v1_l:>+7.2f} USDT  |  V2 {v2_w-v2_l:>+7.2f} USDT  |  {now}")
    lines.append(f"{'='*80}")

    # V1
    lines.append(f"\n  🔴 SHORT_BOT (LIVE)")
    if v1_err:
        lines.append(f"  ❌ {v1_err}")
    elif not v1_positions:
        lines.append(f"  📭 Keine offenen Positionen")
    else:
        lines.append(f"  {'Symbol':<10} {'Side':<7} {'Size':>10} {'Entry':>12} {'Mark':>12} {'PnL':>10} {'ROE':>7} {'SL':>12} {'TP':>12}")
        lines.append(f"  {'─'*92}")
        for p in v1_positions:
            margin = p.get("margin", 0)
            roe = (p["pnl"] / margin) * 100 if margin > 0 else 0
            lines.append(f"  {p['symbol']:<10} {p['side'].upper():<7} {fmt(p['size'],4):>10} "
                         f"{fmt(p['entry'],4):>12} {fmt(p['mark'],4):>12} "
                         f"{p['pnl']:>+10.2f} {roe:>+6.1f}% {fmt(p['sl']):>12} {fmt(p['tp']):>12}")

    # V2
    lines.append(f"\n  🔵 V2 — MRv2 RSI (DEMO)")
    if v2_err:
        lines.append(f"  ❌ {v2_err}")
    elif not v2_positions:
        lines.append(f"  📭 Keine offenen Positionen")
    else:
        lines.append(f"  {'Symbol':<10} {'Side':<7} {'Size':>10} {'Entry':>12} {'Mark':>12} {'PnL':>10} {'ROE':>7} {'SL':>12} {'TP':>12}")
        lines.append(f"  {'─'*92}")
        for p in v2_positions:
            margin = p.get("margin", 0)
            roe = (p["pnl"] / margin) * 100 if margin > 0 else 0
            lines.append(f"  {p['symbol']:<10} {p['side'].upper():<7} {fmt(p['size'],4):>10} "
                         f"{fmt(p['entry'],4):>12} {fmt(p['mark'],4):>12} "
                         f"{p['pnl']:>+10.2f} {roe:>+6.1f}% {fmt(p['sl']):>12} {fmt(p['tp']):>12}")

    # Summary
    total_v1 = sum(p["pnl"] for p in v1_positions)
    total_v2 = sum(p["pnl"] for p in v2_positions)
    lines.append(f"\n{'='*80}")
    lines.append(f"  SHORT_BOT PnL: {total_v1:>+8.2f} USDT  |  V2 PnL: {total_v2:>+8.2f} USDT  |  "
                 f"Gesamt: {total_v1+total_v2:>+8.2f} USDT")
    lines.append(f"{'='*80}")

    return "\n".join(lines)


def run_live():
    try:
        signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
        while True:
            print("\033c", end="")
            try:
                out = render()
                print(out, flush=True)
            except Exception as e:
                print(f"\n  ⚠️ Render-Fehler: {e}")
                print(f"  Nächster Versuch in {REFRESH}s...")
            time.sleep(REFRESH)
    except (KeyboardInterrupt, SystemExit):
        print("\n👋 Dashboard beendet.")
        sys.exit(0)


def main():
    print(render())


if __name__ == "__main__":
    if "--once" in sys.argv:
        main()
    else:
        run_live()
