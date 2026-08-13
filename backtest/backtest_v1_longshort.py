#!/usr/bin/env python3
"""
DS-SpreadScalper V1 Backtest — LONG+SHORT (beide Richtungen)
============================================================
Aktuelle V1 Bot-Parameter: TP 2.0×ATR, Hebel 3×, Chart-SL, ROE-Trailing
Im Gegensatz zum Live-Bot: BEIDE Richtungen (LONG + SHORT)
"""
import sys, json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/Users/andreas/bitget_bot_v1")

# ── Exakte Bot-Parameter ──
SYMBOLS = ["SOLUSDT", "BTCUSDT", "ETHUSDT"]
LEVERAGE = 3
MAKER_FEE = 0.0002
TAKER_FEE = 0.0006
BREAKEVEN_PNL_PCT = 0.03

PRICE_PLACES = {"SOLUSDT": 3, "BTCUSDT": 1, "ETHUSDT": 1}
MIN_SIZES = {"BTCUSDT": 0.001, "ETHUSDT": 0.05, "SOLUSDT": 0.2}

TP_LEVELS = [{"pct": 1.0, "atr_mult": 2.0, "label": "TP1"}]

DATA_DIR = Path("/Users/andreas/bitget_bot_v1/backtest")


def calc_atr(candles, idx, period=14):
    if idx < period:
        return None
    trs = []
    for i in range(idx - period + 1, idx + 1):
        h = float(candles[i][2])
        l = float(candles[i][3])
        pc = float(candles[i-1][4])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs) / len(trs)


def calc_sl(candles, idx, entry, side, pp):
    """Chart-SL wie Bot: 20 Kerzen + 0.1%, min 0.5×ATR, Fallback 1.5×ATR."""
    lookback = min(idx, 20)
    if lookback < 5:
        atr = calc_atr(candles, idx)
        if not atr:
            return None
        return round(entry + atr * 1.5, pp) if side == "short" else round(entry - atr * 1.5, pp)
    sub = candles[idx - lookback:idx]
    highs = [float(k[2]) for k in sub]
    lows = [float(k[3]) for k in sub]
    atr = calc_atr(candles, idx)
    if side == "short":
        sl = round(max(highs) * 1.001, pp)
        if atr and sl < entry + atr * 0.5:
            sl = round(entry + atr * 0.5, pp)
        return sl
    else:
        sl = round(min(lows) * 0.999, pp)
        if atr and sl > entry - atr * 0.5:
            sl = round(entry - atr * 0.5, pp)
        return sl


def run_backtest(symbol, candles):
    """Backtest mit LONG+SHORT — gleiche Spread-Penetration wie V1 Bot."""
    n = len(candles)
    trades = []
    position = None  # {entry, sl, tp, entry_idx, peak_roe, side}

    pp = PRICE_PLACES.get(symbol, 2)
    min_size = MIN_SIZES.get(symbol, 0.1)

    total_pnl = 0.0
    wins = 0
    losses = 0

    for i in range(50, n - 1):
        if i % 5000 == 0 and i > 0:
            pct = i / n * 100
            print(f"    {symbol}: {pct:.0f}% ({i}/{n}) — {len(trades)} Trades", end="\r")

        o, h, l, c = float(candles[i][1]), float(candles[i][2]), float(candles[i][3]), float(candles[i][4])

        if position:
            side = position["side"]
            entry, sl, tp, e_idx, peak_roe = position["entry"], position["sl"], position["tp"], position["entry_idx"], position["peak_roe"]

            # TP check (richtungsspezifisch)
            tp_hit = False
            if side == "short" and c <= tp:
                tp_hit = True
            elif side == "long" and c >= tp:
                tp_hit = True

            if tp_hit:
                pnl = (entry - tp) * position["size"] if side == "short" else (tp - entry) * position["size"]
                pnl -= abs(pnl) * TAKER_FEE
                total_pnl += pnl
                wins += 1
                trades.append({"entry": entry, "exit": tp, "pnl": pnl, "reason": "TP", "side": side,
                              "entry_ts": int(candles[e_idx][0]), "exit_ts": int(candles[i][0]),
                              "bars": i - e_idx})
                position = None
                continue

            # SL check (richtungsspezifisch)
            sl_hit = False
            if side == "short" and h >= sl:
                sl_hit = True
            elif side == "long" and l <= sl:
                sl_hit = True

            if sl_hit:
                pnl = (entry - sl) * position["size"] if side == "short" else (sl - entry) * position["size"]
                pnl -= abs(pnl) * TAKER_FEE
                total_pnl += pnl
                losses += 1
                trades.append({"entry": entry, "exit": sl, "pnl": pnl, "reason": "SL", "side": side,
                              "entry_ts": int(candles[e_idx][0]), "exit_ts": int(candles[i][0]),
                              "bars": i - e_idx})
                position = None
                continue

            # ROE-Trailing
            margin = entry * position["size"] / LEVERAGE
            if side == "short":
                u_pnl = (entry - c) * position["size"]
            else:
                u_pnl = (c - entry) * position["size"]
            roe = (u_pnl / margin) * 100 if margin > 0 else 0

            if roe > peak_roe:
                position["peak_roe"] = roe

            if position["peak_roe"] >= BREAKEVEN_PNL_PCT * 100:
                target_roe = position["peak_roe"] - 2.0
                pnl_target = target_roe / 100 * margin

                if side == "short":
                    new_sl = round(entry - pnl_target / position["size"], pp)
                    if new_sl > c and new_sl < position["sl"]:
                        position["sl"] = new_sl
                else:
                    new_sl = round(entry + pnl_target / position["size"], pp)
                    if new_sl < c and new_sl > position["sl"]:
                        position["sl"] = new_sl

        else:
            # Keine Position → eine Richtung wählen basierend auf Candle-Position
            atr = calc_atr(candles, i)
            if not atr or atr <= 0:
                continue

            spread = h - l
            if spread <= 0:
                continue
            spread_pct = spread / c
            if spread_pct > 0.005:
                continue

            # Richtung: SHORT wenn close im oberen Drittel, LONG wenn im unteren
            pos_in_candle = (c - l) / spread if spread > 0 else 0.5
            
            if pos_in_candle > 0.5:
                # SHORT versuchen (Preis ist im oberen Bereich)
                sp = 0.7
                entry_price = round(l + spread * sp, pp)
                sl_price = calc_sl(candles, i, entry_price, "short", pp)
                if sl_price:
                    tp_price = round(entry_price - atr * TP_LEVELS[0]["atr_mult"], pp)
                    notional = entry_price * min_size
                    if notional >= 5.0:
                        position = {
                            "side": "short", "entry": entry_price, "sl": sl_price,
                            "tp": tp_price, "size": min_size, "entry_idx": i, "peak_roe": 0.0,
                        }
            else:
                # LONG versuchen (Preis ist im unteren Bereich)
                lp = 0.3
                entry_price = round(l + spread * lp, pp)
                sl_price = calc_sl(candles, i, entry_price, "long", pp)
                if sl_price:
                    tp_price = round(entry_price + atr * TP_LEVELS[0]["atr_mult"], pp)
                    notional = entry_price * min_size
                    if notional >= 5.0:
                        position = {
                            "side": "long", "entry": entry_price, "sl": sl_price,
                            "tp": tp_price, "size": min_size, "entry_idx": i, "peak_roe": 0.0,
                        }

    print(f"    {symbol}: Fertig — {len(trades)} Trades         ")
    return trades, total_pnl, wins, losses


def main():
    print("=" * 65)
    print("  📊 V1 Backtest — LONG+SHORT (beide Richtungen)")
    print("  TP 2.0×ATR | 3× Hebel | Chart-SL | ROE-Trailing")
    print("  Daten: Bitget 1H (Mai–Jul 2026)")
    print("=" * 65)

    all_trades = {}
    grand_total = 0.0
    grand_trades = 0

    for sym in SYMBOLS:
        f = DATA_DIR / f"{sym}_1H.json"
        if not f.exists():
            print(f"\n  ❌ Keine Daten für {sym}")
            continue
        candles = json.loads(f.read_text())
        print(f"\n📡 {sym}: {len(candles)} Kerzen")
        trades, total_pnl, wins, losses = run_backtest(sym, candles)
        all_trades[sym] = trades

        if not trades:
            continue

        t = len(trades)
        wr = wins / t * 100
        avg = total_pnl / t
        win_pnl = sum(x["pnl"] for x in trades if x["pnl"] > 0)
        loss_pnl = abs(sum(x["pnl"] for x in trades if x["pnl"] < 0))
        pf = win_pnl / max(loss_pnl, 0.01)
        best = max(trades, key=lambda x: x["pnl"])
        worst = min(trades, key=lambda x: x["pnl"])
        longs = sum(1 for x in trades if x.get("side") == "long")
        shorts = sum(1 for x in trades if x.get("side") == "short")
        avg_bars = sum(x["bars"] for x in trades) / t

        print(f"\n  {'─'*58}")
        print(f"  {sym}")
        print(f"  {'─'*58}")
        print(f"    Trades:      {t:>4d}  ({wins}W / {losses}L)")
        print(f"    LONG/SHORT:  {longs}/{shorts}")
        print(f"    Win Rate:    {wr:>5.1f}%")
        print(f"    PnL:         {total_pnl:>+8.2f} USDT")
        print(f"    ⌀ PnL/Trade: {avg:>+8.2f} USDT")
        print(f"    Profit F.:   {pf:.2f}")
        print(f"    ⌀ Haltezeit: {avg_bars:.0f}h")
        print(f"    🏆 Best:      {best['pnl']:>+8.2f} ({best['reason']}, {best['side']})")
        print(f"    💀 Worst:     {worst['pnl']:>+8.2f} ({worst['reason']}, {worst['side']})")

        grand_total += total_pnl
        grand_trades += t

    print(f"\n  {'='*58}")
    print(f"  📊 GESAMT ({len(SYMBOLS)} Symbole)")
    print(f"  {'='*58}")
    print(f"    Trades:      {grand_trades:>4d}")
    print(f"    Gesamt PnL:  {grand_total:>+8.2f} USDT")

    # Vergleich: nur SHORT
    short_only_pnl = sum(sum(t["pnl"] for t in all_trades[s] if t.get("side") == "short") for s in SYMBOLS if s in all_trades)
    long_only_pnl = sum(sum(t["pnl"] for t in all_trades[s] if t.get("side") == "long") for s in SYMBOLS if s in all_trades)
    print(f"    Davon LONG:   {long_only_pnl:>+8.2f} USDT")
    print(f"    Davon SHORT:  {short_only_pnl:>+8.2f} USDT")
    print(f"  {'='*58}")
    print()


if __name__ == "__main__":
    main()
