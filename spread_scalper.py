"""
SHORT_BOT — High-Frequency Spread Scalping Bot
===============================================
Strategie: Spread-Penetration on Bitget Futures
- Liest Orderbook alle 2s
- Platziert Limit-Orders knapp über Bid / unter Ask
- Wenn eine Seite gefüllt wird, wird die andere zum TP
- Viele kleine Trades, Spread-Capture
"""
import sys, os, time, json, threading
from datetime import datetime
from loguru import logger

sys.path.insert(0, "/Users/andreas/bitget_bot_v1")
import bitget_client as client

# ── Config ───────────────────────────────────────────────────────────────────
SYMBOLS = ["BTCUSDT", "ETHUSDT"]  # SOLUSDT entfernt (Learning: SOL-Shorts −1.97 USDT, gestern −3.06)
LOOP_INTERVAL = 2          # Alle 2 Sekunden
LEVERAGE = 3  # 3× Hebel (Backtest: V10 1× verliert 10× weniger als 5×)
OFFSET_PCT = 0.0001        # 0.01% Offset (hauchduenn, um im Orderbook zu bleiben)
MAX_SPREAD_PCT = 0.005     # Max 0.5% Spread (sonst zu volatil)
TELEGRAM_ON = True

# 🧭 NUR LONG (Learning 26.08.: SHORTs −231 USDT über 1577 Trades)
SHORT_ONLY = False
LONG_ONLY = True
EMAFILTER = True        # Trendfilter: True = nur in EMA-Richtung traden (Backtest-Pflicht!)

# 🧠 Trade Analysis — DeepSeek nach jedem Trade
ANALYSIS_ENABLED = True   # False = deaktivieren (keine API-Kosten)

# ── Shared State (Dashboard-Kommunikation) ──
SHARED_STATE_PATH = os.path.join(os.path.dirname(__file__), "bot_state.json")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-chat"      # DeepSeek-V3 (Flash), wechseln auf "deepseek-reasoner" für DeepSeek-R1/Pro

# Symbol-spezifische Mindestmengen (updatet fuer 5 USDT Minimum UND Bitget Min-Qty Limits)
MIN_SIZES = {
    "BTCUSDT": 0.001,   # min_qty=0.001 → ~$63 (groesser als 5 USDT min)
    "ETHUSDT": 0.05,    # min_qty erhöht auf 0.05 (war 0.01, zu klein) → ~$150
    "SOLUSDT": 0.2,     # min_qty erhöht auf 0.2 (war 0.1) → ~$16
}

# 📊 Single-Level TP: 100% bei 2×ATR (bessere TP-Trefferquote als 3×)
TP_LEVELS = [
    {"pct": 1.0, "atr_mult": 2.5, "label": "TP1"},   # 100% @ 2.5× ATR (Learning: Gewinner laufen lassen, >2h = 71% WR)
]
SL_BASE_MULT = 0.30     # 0.30× ATR baseline
SL_MAX_MULT = 0.60      # 0.60× ATR bei Verlustserie (proportional zu SL_BASE)
BREAKEVEN_PNL_PCT = 0.03  # +3% unrealized → move SL to entry
LOSS_STREAK_THRESHOLD = 3  # Nach 3 Verlusten: scale SL bis 0.60×

# Preis-Rundung (Stellen nach Komma)
PRICE_PLACES = {
    "SOLUSDT": 3,
    "BTCUSDT": 1,
    "ETHUSDT": 1,
}

# 🎯 Spread-Penetration pro Symbol (0.0 = Bid, 1.0 = Ask)
SPREAD_PEN_SHORT = {}    # Keine Overrides nötig
SPREAD_PEN_LONG = {}    # Keine Overrides nötig (LONG via Hedge, SHORT_ONLY)

# 📡 Coinglass-style Sentiment Signals (via Bitget API — kein externer API-Key nötig)
MAX_FUNDING_RATE = 0.0005     # 0.05% — SHORT nur wenn Funding darunter (sonst extreme crowded long)
OI_MIN_CHANGE_PCT = -10       # Min OI-Change % im Ticker (Filter für Illiquidität)
# 📊 Richtungssignal — Funding Rate Schwelle
FUNDING_SIGNAL_THRESHOLD = 0.0002  # 0.02% — Funding > +0.02% → SHORT, < -0.02% → LONG (weniger Noise)

# Logger
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
logger.add("spread_scalper.log", level="INFO", format="{time:YYYY-MM-DD HH:mm:ss} | {message}")


class SpreadScalper:
    def __init__(self):
        self.running = True
        self.positions = {}   # symbol -> {
                              #   "side": "long"/"short"
                              #   "entry": price, "size": qty, "mark_price": current
                              #   "tp_level": 0 (aktuelles TP-Level)
                              #   "tp_prices": [tp]
                              #   "sl_price": current SL
                              #   "loss_streak": consecutive losses
                              # }
        self.pending_orders = {}  # symbol -> {"buy_id": "...", "sell_id": "...", "ts": timestamp}
        self.last_pnl_check = {}  # symbol -> timestamp (10s-TP1-Cooldown)
        self.peak_roe = {}        # symbol -> höchster ROE% (für Trailing Stop)
        
        # 🧠 Trade Learner
        self.trade_log = []  # Abgeschlossene Trades
        self.stats = {}      # Statistik pro Symbol
        self.analysis_lock = threading.Lock()
        self.load_learnings()
        
        logger.info("=" * 50)
        logger.info("🚀 SHORT_BOT gestartet")
        logger.info(f"   Symbole: {SYMBOLS}")
        logger.info(f"   Intervall: {LOOP_INTERVAL}s | Hebel: {LEVERAGE}x")
        logger.info(f"   Offset: {OFFSET_PCT*100:.3f}%")
        mode = "SHORT-Only" if SHORT_ONLY else "LONG+SHORT (Funding-Signal)"
        logger.info(f"   Modus: {mode} | EMA-Filter: {EMAFILTER}")
        self.print_stats()
        logger.info("=" * 50)
        
        # Startup: Prüfe bestehende Positionen auf fehlende TP/SL
        self._check_existing_tpsl()
    
    def _check_existing_tpsl(self):
        """Setze TP/SL fuer bestehende Positionen ohne — nutze ATR-basiertes Single TP"""
        for symbol in SYMBOLS:
            try:
                pos_raw = client.get_position(symbol)  # Volle raw API-Response
                if not pos_raw or float(pos_raw.get("total", 0)) == 0:
                    continue
                has_tp = bool(pos_raw.get("takeProfit", ""))
                has_sl = bool(pos_raw.get("stopLoss", ""))
                if not has_tp or not has_sl:
                    logger.info(f"🔧 Startup-Check für {symbol}: TP={has_tp}, SL={has_sl}")
                    
                    # Berechne ATR für TP/SL Levels
                    atr = self.calc_atr(symbol)
                    if not atr:
                        logger.warning(f"  ⏭️  ATR nicht verfügbar für {symbol}, skip")
                        continue
                    
                    entry_price = float(pos_raw.get("openPriceAvg", pos_raw.get("markPrice", 0)))
                    side = pos_raw["holdSide"]
                    
                    # Berechne Single TP/SL
                    levels = self.calc_tp_sl_levels(symbol, entry_price, side, atr)
                    if not levels:
                        continue
                    
                    logger.info(f"  📍 {side.upper()}: Entry={entry_price}, ATR={atr:.4f}")
                    logger.info(f"     TP={levels['tp_prices'][0]}, SL={levels['sl']}")
                    
                    self.set_tpsl_for_position(symbol, side, levels["tp_prices"], levels["sl"], float(pos_raw["total"]))
                    
                    # ⬇️ Bot-seitiges SL/TP-Tracking (Demo-fähig)
                    if symbol not in self.positions:
                        self.positions[symbol] = {}
                    self.positions[symbol].update({
                        "tp_prices": levels["tp_prices"],
                        "sl": levels["sl"],
                        "side": side,
                        "entry": entry_price,
                        "size": float(pos_raw["total"]),
                        "mark_price": float(pos_raw.get("markPrice", entry_price)),
                    })
            except Exception as e:
                logger.error(f"  ❌ {symbol}: {type(e).__name__}: {e}")
    
    def load_learnings(self):
        """Lade gespeicherte Learnings"""
        try:
            import json
            with open("spread_learnings.json") as f:
                data = json.load(f)
                self.trade_log = data.get("trades", [])
                self.stats = data.get("stats", {})
            logger.info(f"🧠 {len(self.trade_log)} Trades geladen")
        except:
            self.trade_log = []
            self.stats = {}
    
    def save_learnings(self):
        """Speichere Learnings"""
        try:
            import json
            with open("spread_learnings.json", "w") as f:
                json.dump({"trades": self.trade_log[-100:], "stats": self.stats}, f, indent=2)
        except:
            pass
    
    def calc_atr(self, symbol, period=14):
        """Berechne ATR (Average True Range) aus letzten Candles"""
        try:
            # Fetch 14 candles (1h timeframe für bessere Stabilität)
            klines = client.get_candles(symbol, "1H", limit=period + 1)
            if not klines or len(klines) < period:
                return None
            
            trs = []
            for i in range(1, len(klines)):
                h = float(klines[i][2])      # high
                l = float(klines[i][3])      # low
                c = float(klines[i-1][4])    # prev close
                tr = max(h - l, abs(h - c), abs(l - c))
                trs.append(tr)
            
            atr = sum(trs[-period:]) / period
            return atr
        except Exception as e:
            logger.debug(f"  ⏭️  ATR calc ({symbol}): {e}")
            return None
    
    def _calc_ema(self, symbol, period=20):
        """Berechne EMA(period) aus 1H Kerzen — für Trendfilter"""
        try:
            klines = client.get_candles(symbol, "1H", limit=period * 3)
            if not klines or len(klines) < period:
                return None
            closes = [float(k[4]) for k in klines[-period * 2:]]
            multiplier = 2 / (period + 1)
            ema = sum(closes[:period]) / period
            for price in closes[period:]:
                ema = (price - ema) * multiplier + ema
            return ema
        except Exception as e:
            logger.debug(f"  ⏭️ EMA calc ({symbol}): {e}")
            return None
    
    def calc_chart_sl(self, symbol, entry_price, side):
        """Berechne initialen SL basierend auf Chart-Struktur (letzte 20 1H Kerzen).
        SHORT: SL = höchstes High + 0.1% Buffer
        LONG:  SL = tiefstes Low - 0.1% Buffer
        Fallback: 1.5× ATR wenn keine Chartdaten verfügbar
        """
        try:
            klines = client.get_candles(symbol, "1H", limit=20)
            if not klines or len(klines) < 5:
                return None
            
            price_places = PRICE_PLACES.get(symbol, 2)
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            
            if side == "short":
                highest = max(highs)
                sl = round(highest * 1.001, price_places)  # 0.1% über Hoch
                # Sicherheit: SL nicht näher als 0.5× ATR
                atr = self.calc_atr(symbol)
                if atr and sl < entry_price + atr * 0.5:
                    sl = round(entry_price + atr * 0.5, price_places)
                return sl
            else:  # long
                lowest = min(lows)
                sl = round(lowest * 0.999, price_places)  # 0.1% unter Tief
                atr = self.calc_atr(symbol)
                if atr and sl > entry_price - atr * 0.5:
                    sl = round(entry_price - atr * 0.5, price_places)
                return sl
        except Exception as e:
            logger.debug(f"  ⏭️ Chart-SL ({symbol}): {e}")
            return None

    def calc_tp_sl_levels(self, symbol, entry_price, side, atr):
        """Berechne TP/SL Preise basierend auf ATR.
        SL ist immer 2% vom Entry (initial) — wird später dynamisch nachgeführt."""
        if not atr or atr <= 0:
            return None
        
        tp_prices = []
        price_places = PRICE_PLACES.get(symbol, 2)
        
        for level in TP_LEVELS:
            if side == "long":
                tp = round(entry_price + atr * level["atr_mult"], price_places)
            else:  # short
                tp = round(entry_price - atr * level["atr_mult"], price_places)
            tp_prices.append(tp)
        
        # SL: Chart-basiert (letzte 20 Kerzen) — falls verfügbar, sonst 1.5× ATR
        # Wird später dynamisch via ROE-Trailing nachgeführt
        try:
            sl = self.calc_chart_sl(symbol, entry_price, side)
            if sl is None:
                # Fallback: 1.5× ATR
                if side == "long":
                    sl = round(entry_price - atr * 1.5, price_places)
                else:
                    sl = round(entry_price + atr * 1.5, price_places)
        except:
            # Hard-Fallback: immer SL setzen, nie None
            if side == "long":
                sl = round(entry_price - atr * 1.5, price_places)
            else:
                sl = round(entry_price + atr * 1.5, price_places)
        
        return {"tp_prices": tp_prices, "sl": sl, "atr": atr}
    
    def record_trade(self, symbol, side, entry, exit_price, pnl, reason):
        """Zeichne Trade auf und lerne daraus"""
        trade = {
            "ts": datetime.now().isoformat(),
            "symbol": symbol, "side": side,
            "entry": entry, "exit": exit_price,
            "pnl": round(pnl, 4), "reason": reason,
        }
        self.trade_log.append(trade)
        
        if symbol not in self.stats:
            self.stats[symbol] = {"trades": 0, "wins": 0, "losses": 0,
                                  "total_pnl": 0.0, "consecutive_losses": 0}
        s = self.stats[symbol]
        s["trades"] += 1
        s["total_pnl"] += pnl
        if pnl > 0:
            s["wins"] += 1
            s["consecutive_losses"] = 0
        else:
            s["losses"] += 1
            s["consecutive_losses"] += 1
        
        # Adaptive Anpassungen
        # Adaptive Anpassungen
        if s["consecutive_losses"] >= 3:
            logger.warning(f"🧠 {symbol}: {s['consecutive_losses']}x Verlust — Spread vergroessern")
        if s["consecutive_losses"] >= 5:
            logger.warning(f"🧠 {symbol}: 5x Verlust — Pausiere fuer 5 Min")
        
        # 📱 Telegram (nur bei TP, kein SL-Spam)
        try:
            if reason != "SL":  # SL-Nachrichten unterdrücken
                import telegram_notify as tg
                icon = "🟢" if pnl > 0 else "🔴"
                tg_msg = f"{icon} {symbol} {side.upper()}\\nEntry={entry} → Exit={exit_price}\\n{pnl:+.4f} USDT | {reason}"
                tg.send(tg_msg)
        except Exception as tg_err:
            logger.warning(f"  ⚠️  Telegram error (record_trade): {tg_err}")
        
        self.save_learnings()
        self._analyze_trade_deepseek(symbol, side, entry, exit_price, pnl, reason)
        
        icon = "✅" if pnl > 0 else "❌"
        logger.info(f"{icon} TRADE: {symbol} {side} | {pnl:+.4f} USDT | {reason}")
    
    def print_stats(self):
        """Zeige gespeicherte Statistik"""
        if not self.stats:
            return
        logger.info("📊 Lernstatus:")
        for sym, s in sorted(self.stats.items()):
            wr = s["wins"] / max(s["trades"], 1) * 100
            logger.info(f"   {sym}: {s['trades']} Trades | {wr:.0f}% WR | {s['total_pnl']:+.2f} USDT")
    
    def _analyze_trade_deepseek(self, symbol, side, entry, exit_price, pnl, reason):
        """Systematische Trade-Analyse via DeepSeek (Background-Thread)"""
        if not ANALYSIS_ENABLED or not DEEPSEEK_API_KEY:
            return

        def _do_analysis():
            try:
                # Sammle Kontext: letzte 10 Trades für Pattern-Erkennung
                recent = self.trade_log[-11:-1] if len(self.trade_log) > 11 else self.trade_log[:-1]
                recent_summary = []
                for t in recent[-8:]:
                    r = t.get("reason", "")
                    p = t.get("pnl", 0)
                    recent_summary.append(f"  {t.get('side','?')} {t.get('symbol','')} | {p:+.4f} USDT | {r}")
                recent_str = "\n".join(recent_summary[-6:]) if recent_summary else "  (erster Trade)"

                # Statistik für dieses Symbol
                s = self.stats.get(symbol, {})
                symbol_wr = (s.get("wins", 0) / max(s.get("trades", 1), 1)) * 100
                symbol_pnl = s.get("total_pnl", 0)

                prompt = f"""Analyze this spread-scalp trade and extract ONE actionable lesson. Focus on patterns, not emotions.

Symbol: {symbol} | Side: {side}
Entry: {entry} → Exit: {exit_price}
PnL: {pnl:+.4f} USDT | Reason: {reason}

Symbol Stats:
- Trades: {s.get('trades', 0)} | WR: {symbol_wr:.0f}% | PnL: {symbol_pnl:+.2f} USDT
- Cons. Losses: {s.get('consecutive_losses', 0)}

Recent trades (last {min(6, len(recent_summary))}):
{recent_str}

Answer in GERMAN, max 2 sentences:
1) Was ist passiert? (1 Satz, technisch)
2) Lektion? (1 Satz, handelbar — z.B. 'bei Spread<X besser warten' oder 'SOL reagiert empfindlich auf BTC-Drops')"""

                import requests
                resp = requests.post(
                    "https://api.deepseek.com/beta/chat/completions",
                    headers={
                        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": DEEPSEEK_MODEL,
                        "messages": [
                            {"role": "system", "content": "Du analysierst Scalping-Trades. Antworte in DEUTSCH. Maximal 2 Sätze. Keine Ausreden — nur Fakten und handelbare Lektionen."},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 200,
                    },
                    timeout=20,
                )
                if resp.status_code == 200:
                    lesson = resp.json()["choices"][0]["message"]["content"].strip()
                    with self.analysis_lock:
                        if self.trade_log:
                            self.trade_log[-1]["lesson"] = lesson
                            logger.info(f"📖 {lesson}")
                            self.save_learnings()

            except Exception as e:
                logger.debug(f"  ⏭️ DeepSeek analysis skipped: {e}")

        threading.Thread(target=_do_analysis, daemon=True).start()
    
    def get_depth(self, symbol):
        """Hole Orderbook Depth"""
        try:
            data = client._get("/api/v2/mix/market/merge-depth", {
                "symbol": symbol, "productType": "USDT-FUTURES", "limit": "5"
            })
            if data.get("code") != "00000":
                return None
            d = data.get("data", {})
            bids = d.get("bids", [])
            asks = d.get("asks", [])
            if not bids or not asks:
                return None
            return {
                "bid": float(bids[0][0]),
                "bid_size": float(bids[0][1]),
                "ask": float(asks[0][0]),
                "ask_size": float(asks[0][1]),
                "spread": float(asks[0][0]) - float(bids[0][0]),
                "mid": (float(asks[0][0]) + float(bids[0][0])) / 2,
            }
        except Exception as e:
            logger.debug(f"Depth Error {symbol}: {e}")
            return None

    def get_coinglass_signals(self, symbol):
        """Hole Coinglass-artige Sentiment-Signale direkt aus Bitget-Ticker:
        - fundingRate: wer zahlt wem (positiv = Longs zahlen Shorts)
        - holdingAmount: Open Interest in Kontrakten
        - change24h: Preisänderung
        """
        try:
            ticker = client.get_ticker(symbol)
            if not ticker:
                return None

            funding_rate = float(ticker.get("fundingRate", 0))       # z.B. 0.000091 = 0.0091%
            oi_contracts = float(ticker.get("holdingAmount", 0))      # Open Interest in Kontrakten
            oi_change_pct = float(ticker.get("changeUtc24h", 0)) * 100  # 24h OI-Änderung in %
            price_change = float(ticker.get("change24h", 0)) * 100    # 24h Preisänderung in %

            # OI in USD schätzen
            mark = float(ticker.get("markPrice", 0))
            oi_usd = oi_contracts * mark if mark > 0 else 0

            return {
                "funding_rate": funding_rate,       # decimal (0.000091)
                "funding_pct": funding_rate * 100,  # Prozent (0.0091%)
                "oi_contracts": oi_contracts,
                "oi_usd": oi_usd,
                "oi_change_pct": oi_change_pct,     # 24h in %
                "price_change": price_change,        # 24h in %
                "is_bullish_funding": funding_rate > 0,  # Longs zahlen Shorts
            }
        except Exception as e:
            logger.debug(f"  ⏭️  Coinglass signals ({symbol}): {e}")
            return None

    def _decide_direction(self, symbol, cg, depth):
        """Entscheide LONG oder SHORT basierend auf Funding Rate + EMA.
        
        Signal-Logik:
        - Funding stark positiv (> +0.01%): Crowd ist long → SHORT (Contrarian)
        - Funding stark negativ (< -0.01%): Crowd ist short → LONG (Contrarian)
        - Funding neutral: EMA-Trendfilter (wenn aktiv) oder neutral → skip
        - SHORT_ONLY=True: immer SHORT (wie bisher)
        
        Returns: \"long\", \"short\", oder None (kein Trade)
        """
        if SHORT_ONLY:
            return "short"

        # Learning 26.08.: SHORTs −231 USDT über 1577 Trades → NUR LONG
        if LONG_ONLY:
            fr = cg["funding_rate"]
            # LONG nur bei negativem Funding (Crowd short) oder neutral + Preis > EMA
            if fr <= -FUNDING_SIGNAL_THRESHOLD:
                logger.info(f"  📡 Funding {cg['funding_pct']:+.4f}% → LONG (Crowd short)")
                return "long"
            if EMAFILTER:
                ema = self._calc_ema(symbol)
                if ema is not None and depth["mid"] > ema:
                    logger.debug(f"  📡 Preis ${depth['mid']:.2f} > EMA ${ema:.2f} → LONG")
                    return "long"
            logger.info(f"  ⏭️  LONG_ONLY: Funding {cg['funding_pct']:+.4f}% nicht negativ, Preis < EMA — kein Trade")
            return None

        fr = cg["funding_rate"]
        
        # 📡 Starkes Funding-Signal
        if fr >= FUNDING_SIGNAL_THRESHOLD:
            logger.info(f"  📡 Funding {cg['funding_pct']:+.4f}% → SHORT (Crowd long)")
            return "short"
        elif fr <= -FUNDING_SIGNAL_THRESHOLD:
            logger.info(f"  📡 Funding {cg['funding_pct']:+.4f}% → LONG (Crowd short)")
            return "long"
        
        # 📡 Neutrales Funding → EMA-Trendfilter
        if EMAFILTER:
            ema = self._calc_ema(symbol)
            if ema is not None:
                mid = depth["mid"]
                if mid > ema:
                    logger.debug(f"  📡 Preis ${mid:.2f} > EMA ${ema:.2f} → LONG")
                    return "long"
                else:
                    logger.debug(f"  📡 Preis ${mid:.2f} < EMA ${ema:.2f} → SHORT")
                    return "short"
        
        # Kein klares Signal → skip
        logger.info(f"  ⏭️  Funding {cg['funding_pct']:+.4f}% — neutral, kein Trade")
        return None

    def _get_funding_rate(self, symbol):
        """Aktuelle Funding-Rate holen (positiv = Longs zahlen Shorts)."""
        try:
            ticker = client.get_ticker(symbol)
            if not ticker:
                return None
            return float(ticker.get("fundingRate", 0))
        except:
            return None

    def get_position(self, symbol):
        """Aktuelle Position abrufen"""
        try:
            pos = client.get_position(symbol)
            if pos and float(pos.get("total", 0)) > 0:
                return {
                    "side": pos.get("holdSide", "long"),
                    "size": float(pos["total"]),
                    "entry": float(pos.get("openPriceAvg", 0)),
                    "pnl": float(pos.get("unrealizedPL", 0)),
                    "markPrice": float(pos.get("markPrice", 0)),
                    "margin": float(pos.get("marginSize", 0)),
                }
        except:
            pass
        return None
    
    def cancel_all_orders(self, symbol):
        """Alle offenen Orders für Symbol löschen"""
        try:
            client.cancel_tpsl_orders(symbol, extra_types=['pos_loss', 'pos_profit'])
        except:
            pass
    
    def place_limit_order(self, symbol, side, price, size):
        """Platziere Limit-Order (Post-Only = Maker)"""
        notional = float(price) * float(size)
        if notional < 4.95:
            logger.warning(f"  ⚠️  {symbol} {side}: size={size} @ ${price} = ${notional:.2f} < $5 — SKIPPE Order")
            return None
        order_side = "buy" if side == "long" else "sell"
        client_oid = f"{symbol}_{side}_{int(time.time()*1000)}_{os.urandom(2).hex()}"
        try:
            # Konvertiere zu Strings — Strings sind SICHER und vermeiden Scientific Notation!
            qty_str = str(float(size))  # "0.001", "0.05", etc
            price_str = str(float(price))
            
            # Log für Debugging
            logger.info(f"  📤 place_limit_order: {symbol} {side} | qty={qty_str} | price={price_str}")
            
            # Sanity-Check
            if not qty_str or qty_str == "0" or qty_str == "0.0":
                logger.warning(f"  ⚠️  {symbol} {side}: qty_str={qty_str} (raw size={size}) — SKIPPE Order (Größe ist 0!)")
                return None
            
            result = client._post("/api/v2/mix/order/place-order", {
                "symbol": symbol,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
                "marginMode": "isolated",
                "side": order_side,
                "tradeSide": "open",           # Pflichtfeld: Positionseröffnung!
                "orderType": "limit",
                "force": "post_only",          # Richtig: "force", nicht "timeInForce"!
                "price": price_str,
                "size": qty_str,
                "clientOid": client_oid,
            })
            code = result.get("code")
            if code == "00000":
                oid = result.get("data", {}).get("orderId", "?")
                logger.info(f"  📄 {side.upper()} Limit @ {price} (ID: {oid[:8]})")
                return oid
            elif code == "40004":  # Post-only would be taker
                logger.debug(f"  ⏭️  {side.upper()} Post-Only abgelehnt (waere Taker)")
            else:
                logger.debug(f"  ❌ {side.upper()} Order: {result.get('msg','?')[:50]}")
        except Exception as e:
            logger.debug(f"  ❌ {side.upper()} Error: {e}")
        return None
    
    def place_market_close(self, symbol, side, size):
        """Schliesse Position per Market-Order"""
        close_side = "sell" if side == "long" else "buy"
        try:
            result = client._post("/api/v2/mix/order/place-order", {
                "symbol": symbol,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
                "marginMode": "isolated",
                "side": close_side,
                "orderType": "market",
                "size": str(size),
                "tradeSide": "close",
            })
            if result.get("code") == "00000":
                logger.success(f"  ✅ Position CLOSED (Market)")
                return True
            # Flash-Close Fallback
            flash = client.close_all_positions(symbol)
            if flash.get("code") == "00000":
                logger.success(f"  ✅ Flash-Close")
                return True
        except:
            pass
        return False
    
    def _place_stop_order(self, symbol, side, trigger_price, size, label):
        """Platziere eine Stop-Market Close-Order (TP oder SL)."""
        close_side = "sell" if side == "long" else "buy"
        try:
            qty_str = str(float(size))
            trigger_str = str(float(trigger_price))
            client_oid = f"tpsl_{symbol}_{label}_{int(time.time()*1000)}_{os.urandom(2).hex()}"
            
            result = client._post("/api/v2/mix/order/place-order", {
                "symbol": symbol,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
                "marginMode": "isolated",
                "side": close_side,
                "tradeSide": "close",
                "orderType": "market",
                "size": qty_str,
                "triggerPrice": trigger_str,
                "triggerType": "mark_price",
                "clientOid": client_oid,
            })
            
            code = result.get("code")
            if code == "00000":
                oid = result.get("data", {}).get("orderId", "?")
                logger.info(f"  📄 {label} @ {trigger_price} (ID: {str(oid)[:8]})")
                return oid
            else:
                logger.debug(f"  ⚠️  {label}: {result.get('msg','?')[:60]}")
                return None
        except Exception as e:
            logger.debug(f"  ❌ {label}: {e}")
            return None
    
    def set_tpsl_for_position(self, symbol, side, tp_prices, sl_price, size):
        """Setze TP/SL via place-pos-tpsl (UTA-kompatibel, kein 40890-Limit).
        tp_prices: [tp1, tp2, tp3] — nutzt tp1 als einzigen TP.
        """
        try:
            self._cancel_plan_orders(symbol)
        except:
            pass
        self._fallback_tpsl(symbol, side, tp_prices, sl_price, size)
    
    def _cancel_plan_orders(self, symbol):
        """Cancel ALL TPSL-Orders für ein Symbol (ohne ID-Liste)."""
        try:
            client.cancel_tpsl_orders(symbol, extra_types=['pos_loss', 'pos_profit'])
        except:
            pass
    
    def _fallback_tpsl(self, symbol, side, tp_prices, sl_price, size):
        """Setze TP/SL via place-pos-tpsl (position-level, sichtbar in stopLoss-Feld).
        Funktioniert auf Live (Demo akzeptiert den Call, persistiert aber nicht)."""
        try:
            tp_price = tp_prices[0]

            # SL-Fallback falls None
            if sl_price is None:
                price_places = PRICE_PLACES.get(symbol, 2)
                try:
                    ticker = client.get_ticker(symbol)
                    mark = float(ticker['last']) if ticker else None
                except:
                    mark = None
                if mark:
                    if side == "short":
                        sl_price = round(mark * 1.015, price_places)
                    else:
                        sl_price = round(mark * 0.985, price_places)
                else:
                    entry = self.positions.get(symbol, {}).get("entry")
                    if entry:
                        sl_price = round(entry * 1.015, price_places) if side == "short" else round(entry * 0.985, price_places)
                    else:
                        sl_price = 0
                logger.warning(f"  ⚠️  SL war None, Fallback auf {sl_price}")

            payload = {
                "symbol": symbol,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
                "holdSide": side,
                "stopSurplusTriggerPrice": f"{tp_price}",
                "stopSurplusTriggerType": "mark_price",
                "stopLossTriggerPrice": f"{sl_price}",
                "stopLossTriggerType": "mark_price",
            }
            result = client._post("/api/v2/mix/order/place-pos-tpsl", payload)
            ok = result.get("code") == "00000"

            if ok:
                logger.info(f"  ✅ pos-tpsl TP={tp_price} SL={sl_price}")
                if symbol not in self.positions:
                    self.positions[symbol] = {}
                self.positions[symbol].update({
                    "tp_level": 0,
                    "tp_prices": tp_prices,
                    "sl": sl_price,
                    "original_size": size,
                })
            else:
                msg = result.get("msg", "?")
                logger.warning(f"  ⚠️  pos-tpsl Fehler ({symbol}): code={result.get('code')} {msg}")
        except Exception as e:
            logger.warning(f"  ⚠️  pos-tpsl Exception: {e}")
    
    def _notify_sl_move(self, symbol, side, pnl_pct, new_sl):
        """Sende Telegram bei SL-Verschiebung."""
        try:
            import telegram_notify as tg
            msg = f"🔒 {symbol} {side}\nPnL={pnl_pct:.1f}% → SL auf {new_sl}"
            tg.send(msg)
        except:
            pass
    
    def get_order_status(self, order_id, symbol):
        """Prüfe ob Order gefüllt wurde"""
        try:
            result = client._get("/api/v2/mix/order/detail", {
                "symbol": symbol,
                "productType": "USDT-FUTURES",
                "orderId": order_id,
            })
            if result.get("code") == "00000":
                data = result.get("data", {})
                state = data.get("state", "")
                if state == "filled":
                    return "filled", float(data.get("priceAvg", data.get("price", 0)))
                elif state in ("partial_fill", "partial_cancel"):
                    return "filled", float(data.get("priceAvg", 0))
                return "open", 0
        except:
            pass
        return "unknown", 0
    
    def run_cycle(self):
        """Haupt-Loop alle 2 Sekunden"""
        for symbol in SYMBOLS:
            try:
                pos = self.get_position(symbol)
                
                if pos:
                    # ── Position existiert → warte auf TP oder schliesse ──
                    depth = self.get_depth(symbol)
                    if not depth:
                        continue
                    
                    tp_prices = self.positions.get(symbol, {}).get("tp_prices")
                    
                    # Wenn TP noch nicht gesetzt, versuche erneut
                    if tp_prices is None:
                        # TP/SL nochmal versuchen
                        logger.debug(f"🔄 {symbol}: TP noch nicht gesetzt, versuche erneut")
                        atr = self.calc_atr(symbol)
                        if atr:
                            levels = self.calc_tp_sl_levels(symbol, float(pos["entry"]), pos["side"], atr)
                            if levels:
                                logger.info(f"  🎯 TP retry: {levels['tp_prices']}, SL: {levels['sl']}")
                                self.set_tpsl_for_position(symbol, pos["side"], levels["tp_prices"], levels["sl"], float(pos["size"]))
                                # Bei Erfolg in positions speichern
                                if symbol in self.positions:
                                    self.positions[symbol].update(levels)
                        continue
                    
                    # Ersten TP-Preis für Client-seitiges Monitoring nutzen
                    tp_price = tp_prices[0]
                    # Marktpreis (markPrice) statt Bid/Ask für SL/TP-Prüfung,
                    # da Bid/Ask durch Spread-Spikes zu Fehlauslösungen führen.
                    # Exchange verwendet Last-Price für Stop Loss.
                    mark_price = float(pos.get("markPrice", (depth["bid"] + depth["ask"]) / 2))
                    
                    if pos["side"] == "long":
                        # LONG: TP wenn Marktpreis >= TP-Preis
                        # SL nur prüfen wenn gesetzt (None = Nur Trailing)
                        sl_price = self.positions.get(symbol, {}).get("sl")
                        if mark_price >= tp_price:
                            logger.info(f"🎯 {symbol} LONG TP erreicht @ {mark_price:.2f} >= {tp_price:.2f}")
                            exit_price = mark_price
                            actual_pnl = (exit_price - pos["entry"]) * pos["size"]
                            self.record_trade(symbol, "long", pos["entry"], exit_price, actual_pnl, "TP")
                            self.place_market_close(symbol, pos["side"], pos["size"])
                            self.positions.pop(symbol, None)
                            self.peak_roe.pop(symbol, None)
                        elif sl_price is not None and mark_price <= sl_price:
                            logger.warning(f"🛑 {symbol} LONG SL hit @ {mark_price:.2f} <= {sl_price:.2f}")
                            actual_pnl = (sl_price - pos["entry"]) * pos["size"]
                            self.record_trade(symbol, "long", pos["entry"], sl_price, actual_pnl, "SL")
                            self.place_market_close(symbol, pos["side"], pos["size"])
                            self.positions.pop(symbol, None)
                            self.peak_roe.pop(symbol, None)
                    else:
                        # SHORT: TP wenn Marktpreis <= TP-Preis
                        # SL nur prüfen wenn gesetzt (None = Nur Trailing)
                        sl_price = self.positions.get(symbol, {}).get("sl")
                        if mark_price <= tp_price:
                            logger.info(f"🎯 {symbol} SHORT TP erreicht @ {mark_price:.2f} <= {tp_price:.2f}")
                            exit_price = mark_price
                            actual_pnl = (pos["entry"] - exit_price) * pos["size"]
                            self.record_trade(symbol, "short", pos["entry"], exit_price, actual_pnl, "TP")
                            self.place_market_close(symbol, pos["side"], pos["size"])
                            self.positions.pop(symbol, None)
                            self.peak_roe.pop(symbol, None)
                        elif sl_price is not None and mark_price >= sl_price:
                            # 🛡️ SL-Hit-Schutz: Wenn Funding positiv (Longs zahlen Shorts),
                            # SL erweitern statt schliessen — Funding kompensiert die offene Position
                            funding = self._get_funding_rate(symbol)
                            if funding is not None and funding > 0:
                                atr = self.calc_atr(symbol)
                                new_sl = sl_price + (atr * 0.5) if atr else sl_price * 1.003
                                new_sl = round(new_sl, PRICE_PLACES.get(symbol, 2))
                                logger.warning(f"🛡️ {symbol} SHORT SL-Hit, aber Funding {funding*100:+.4f}% positiv → "
                                               f"SL erweitert von {sl_price:.2f} auf {new_sl:.2f}")
                                self.positions[symbol]["sl"] = new_sl
                                # Exchange-SL ebenfalls anpassen
                                try:
                                    self.set_tpsl_for_position(symbol, "short", tp_prices, new_sl, pos["size"])
                                except:
                                    pass
                            else:
                                logger.warning(f"🛑 {symbol} SHORT SL hit @ {mark_price:.2f} >= {sl_price:.2f}")
                                actual_pnl = (pos["entry"] - sl_price) * pos["size"]
                                self.record_trade(symbol, "short", pos["entry"], sl_price, actual_pnl, "SL")
                                self.place_market_close(symbol, pos["side"], pos["size"])
                                self.positions.pop(symbol, None)
                                self.peak_roe.pop(symbol, None)
                    
                    # ── Dynamischer SL (alle 10s nachgeführt) ──
                    # ROE-basiertes Trailing: SL 2% unter Peak-ROE.
                    # Läuft bei jeder offenen Position kontinuierlich mit.
                    if pos and tp_prices:
                        now = time.time()
                        last = self.last_pnl_check.get(symbol, 0)
                        if now - last >= 10:
                            self.last_pnl_check[symbol] = now
                            try:
                                current_sl = self.positions.get(symbol, {}).get("sl")
                                mark = float(pos.get("markPrice", depth["mid"]))
                                side = pos["side"]
                                
                                entry_price = float(pos["entry"])
                                pos_size = float(pos["size"])
                                pnl = float(pos.get("pnl", 0))
                                # Exchange-Margin bevorzugen, Fallback auf Bot-Berechnung
                                margin = float(pos.get("margin", 0))
                                if margin == 0:
                                    margin = pos_size * mark / LEVERAGE
                                roe_pct = (pnl / margin) * 100 if margin > 0 else 0

                                # Peak-ROE aktualisieren
                                if roe_pct > self.peak_roe.get(symbol, -9999):
                                    self.peak_roe[symbol] = roe_pct

                                # ROE-Trailing: aktiv ab 3% Peak-ROE
                                # SL immer auf Break-Even (Entry) sobald 3% erreicht,
                                # danach 2% unter dem höchsten erreichten ROE nachführen
                                if self.peak_roe.get(symbol, 0) >= (BREAKEVEN_PNL_PCT * 100):
                                    target_roe = self.peak_roe[symbol] - 2.0  # 2% unter Peak
                                    pnl_target = target_roe / 100 * margin
                                    if side == "short":
                                        new_sl = round(entry_price - pnl_target / pos_size, PRICE_PLACES.get(symbol, 2))
                                        if current_sl is None:
                                            if new_sl > mark:
                                                self.positions[symbol]["sl"] = new_sl
                                                self.set_tpsl_for_position(symbol, "short", tp_prices, new_sl, pos_size)
                                                logger.info(f"🔒 {symbol} SHORT: Trailing-SL gesetzt → {new_sl} (Peak={self.peak_roe[symbol]:.1f}%)")
                                                self._notify_sl_move(symbol, "SHORT", roe_pct, new_sl)
                                        else:
                                            if new_sl > mark and new_sl < current_sl:
                                                self.positions[symbol]["sl"] = new_sl
                                                self.set_tpsl_for_position(symbol, "short", tp_prices, new_sl, pos_size)
                                                logger.info(f"🔒 {symbol} SHORT: SL enger → {new_sl} (Peak={self.peak_roe[symbol]:.1f}%)")
                                                self._notify_sl_move(symbol, "SHORT", roe_pct, new_sl)
                                    else:  # long
                                        new_sl = round(entry_price + pnl_target / pos_size, PRICE_PLACES.get(symbol, 2))
                                        if current_sl is None:
                                            if new_sl < mark:
                                                self.positions[symbol]["sl"] = new_sl
                                                self.set_tpsl_for_position(symbol, "long", tp_prices, new_sl, pos_size)
                                                logger.info(f"🔒 {symbol} LONG: Trailing-SL gesetzt → {new_sl} (Peak={self.peak_roe[symbol]:.1f}%)")
                                                self._notify_sl_move(symbol, "LONG", roe_pct, new_sl)
                                        else:
                                            if new_sl < mark and new_sl > current_sl:
                                                self.positions[symbol]["sl"] = new_sl
                                                self.set_tpsl_for_position(symbol, "long", tp_prices, new_sl, pos_size)
                                                logger.info(f"🔒 {symbol} LONG: SL enger → {new_sl} (Peak={self.peak_roe[symbol]:.1f}%)")
                                                self._notify_sl_move(symbol, "LONG", roe_pct, new_sl)
                            except Exception as sl_e:
                                logger.debug(f"  ⚠️ Dynamischer SL {symbol}: {sl_e}")
                    
                else:
                    depth = self.get_depth(symbol)
                    if not depth:
                        continue
                    
                    spread_pct = depth["spread"] / depth["mid"]
                    if spread_pct > MAX_SPREAD_PCT:
                        logger.debug(f"⏭️  {symbol}: Spread zu gross ({spread_pct*100:.3f}%)")
                        continue
                    
                    # ❗ Prüfe ob schon eine Order schwebt — keine neue platzieren!
                    pending = self.pending_orders.get(symbol)
                    if pending:
                        # Alte Orders (>60s) cenceln und neu versuchen
                        if time.time() - pending.get("ts", 0) > 60:
                            for otype in ("buy_id", "sell_id"):
                                oid = pending.get(otype)
                                if oid:
                                    try:
                                        client._post("/api/v2/mix/order/cancel-order", {
                                            "symbol": symbol, "productType": "USDT-FUTURES",
                                            "marginCoin": "USDT", "orderId": oid
                                        })
                                    except:
                                        pass
                            self.pending_orders.pop(symbol, None)
                            logger.info(f"🔄 {symbol}: Stale Order gecancelt, versuche neu")
                        else:
                            logger.debug(f"⏳ {symbol}: Order noch offen — warte ({int(time.time()-pending.get('ts',0))}s)")
                            continue
                    
                    # Berechne Einstiegspreise (innerhalb des Spreads)
                    price_place = PRICE_PLACES.get(symbol, 2)
                    spread_size = depth["spread"]
                    # Per-Symbol Spread-Penetration (Default 30%/70%)
                    short_pen = SPREAD_PEN_SHORT.get(symbol, 0.7)
                    long_pen = SPREAD_PEN_LONG.get(symbol, 0.3)
                    # BUY bei long_pen%, SELL bei short_pen% vom Spread
                    bid_price = depth["bid"] + (spread_size * long_pen)
                    ask_price = depth["bid"] + (spread_size * short_pen)
                    # Stelle sicher dass BUY < SELL (minimaler Abstand)
                    bid_price = min(bid_price, ask_price - (spread_size * 0.05))
                    bid_offset = round(bid_price, price_place)
                    ask_offset = round(ask_price, price_place)
                    order_size = MIN_SIZES.get(symbol, 0.1)
                    # Size dynamisch an Minimum 5 USDT anpassen
                    min_notional = 5 / depth["mid"]
                    if min_notional > order_size:
                        order_size = min_notional
                    order_size = round(order_size, 4)  # 4 Dezimalstellen
                    notional_usd = order_size * depth["mid"]
                    if notional_usd < 4.95:  # Floating-point-Safety: 4.95 statt 5.0
                        logger.warning(f"⚠️  {symbol}: size={order_size} Qty={order_size:.4f} @ ${depth['mid']:.4f} = ${notional_usd:.2f} < $5 — skippe")
                        continue
                    
                    # 📡 Coinglass Sentiment-Check vor Orderplatzierung
                    cg = self.get_coinglass_signals(symbol)
                    if cg is None:
                        logger.debug(f"  ⏭️ {symbol}: Keine Coinglass-Signale — überspringe")
                        continue

                    # 🕐 Handelsfenster-Sperre: Verluststunden 2-4/14/17/20/22 Uhr
                    # (Simulation über 1577 Trades: diese Stunden = −323 USDT, 10-13 Uhr war GUT!)
                    _h = time.localtime().tm_hour
                    if _h in (2, 3, 4, 14, 17, 20, 22):
                        logger.info(f"  ⏭️ {symbol}: Handelsfenster gesperrt ({_h}:00, Verluststunde) — kein neuer Trade")
                        continue

                    # 📡 Richtung per Funding-Signal entscheiden
                    direction = self._decide_direction(symbol, cg, depth)
                    if direction is None:
                        logger.debug(f"  ⏭️ {symbol}: Kein klares Richtungssignal — überspringe")
                        continue
                    
                    if direction == "long":
                        # 📡 Funding Rate Check für LONG: nicht longen wenn zu negativ (crowded shorts)
                        if cg["funding_rate"] < -MAX_FUNDING_RATE:
                            logger.info(f"  ⏭️ {symbol}: Funding {cg['funding_pct']:.4f}% < {-MAX_FUNDING_RATE*100:.2f}% — zu bearish, kein Long")
                            logger.info(f"     📡 OI {cg['oi_usd']/1e6:.0f}M$ | 24h {cg['price_change']:+.2f}% | Funding {cg['funding_pct']:+.4f}%")
                            continue
                        buy_id = self.place_limit_order(symbol, "long", round(bid_offset, price_place), order_size)
                        sell_id = None
                        logger.info(f"  📈 {symbol}: LONG Order | 📡 Funding {cg['funding_pct']:+.4f}% | OI {cg['oi_usd']/1e6:.0f}M$")
                    else:
                        # 📡 Funding Rate Check für SHORT: nicht shorten wenn zu positiv (crowded longs)
                        if cg["funding_rate"] > MAX_FUNDING_RATE:
                            logger.info(f"  ⏭️ {symbol}: Funding {cg['funding_pct']:.4f}% > {MAX_FUNDING_RATE*100:.2f}% — zu bullish, kein Short")
                            logger.info(f"     📡 OI {cg['oi_usd']/1e6:.0f}M$ | 24h {cg['price_change']:+.2f}% | Funding {cg['funding_pct']:+.4f}%")
                            continue
                        buy_id = None
                        sell_id = self.place_limit_order(symbol, "short", round(ask_offset, price_place), order_size)
                        logger.info(f"  📉 {symbol}: SHORT Order | 📡 Funding {cg['funding_pct']:+.4f}% | OI {cg['oi_usd']/1e6:.0f}M$")
                    
                    if buy_id or sell_id:
                        self.pending_orders[symbol] = {"buy_id": buy_id, "sell_id": sell_id, "ts": time.time()}
                    
                    # Warte kurz und prüfe welche gefüllt wurde
                    time.sleep(0.5)
                    
                    if buy_id:
                        status, price = self.get_order_status(buy_id, symbol)
                        if status == "filled":
                            # BUY gefuellt -> LONG Position
                            logger.success(f"📈 {symbol} LONG gefuellt @ {price}")

                            # Berechne ATR für TP/SL
                            atr = self.calc_atr(symbol)
                            if atr:
                                levels = self.calc_tp_sl_levels(symbol, price, "long", atr)
                                if levels:
                                    logger.info(f"  🎯 TP: {levels['tp_prices']}, SL: {levels['sl']}")
                                    self.set_tpsl_for_position(symbol, "long", levels["tp_prices"], levels["sl"], order_size)
                                else:
                                    # Fallback auf spread-basiert
                                    tp = round(price + depth["spread"], PRICE_PLACES.get(symbol, 2))
                                    sl = round(price - depth["spread"] * 0.5, PRICE_PLACES.get(symbol, 2))
                                    self.set_tpsl_for_position(symbol, "long", [tp, tp, tp], sl, order_size)
                            else:
                                tp = round(price + depth["spread"], PRICE_PLACES.get(symbol, 2))
                                sl = round(price - depth["spread"] * 0.5, PRICE_PLACES.get(symbol, 2))
                                self.set_tpsl_for_position(symbol, "long", [tp, tp, tp], sl, order_size)
                            
                            if symbol in self.positions:
                                self.positions[symbol].update({
                                    "side": "long", "entry": price, "size": order_size,
                                    "mark_price": depth["mid"],
                                    "tp_prices": levels["tp_prices"] if levels and levels.get("tp_prices") else [tp, tp, tp],
                                    "sl": levels["sl"] if levels and levels.get("sl") else sl,
                                })
                            else:
                                self.positions[symbol] = {
                                    "side": "long", "entry": price, "size": order_size,
                                    "mark_price": depth["mid"],
                                    "tp_prices": levels["tp_prices"] if levels and levels.get("tp_prices") else [tp, tp, tp],
                                    "sl": levels["sl"] if levels and levels.get("sl") else sl,
                                }
                            if sell_id:
                                client._post("/api/v2/mix/order/cancel-order", {
                                    "symbol": symbol, "productType": "USDT-FUTURES",
                                    "marginCoin": "USDT", "orderId": sell_id
                                })
                            continue

                    if sell_id:
                        status, price = self.get_order_status(sell_id, symbol)
                        if status == "filled":
                            # SELL gefuellt -> SHORT Position
                            logger.success(f"📉 {symbol} SHORT gefuellt @ {price}")

                            # Berechne ATR für TP/SL
                            atr = self.calc_atr(symbol)
                            if atr:
                                levels = self.calc_tp_sl_levels(symbol, price, "short", atr)
                                if levels:
                                    logger.info(f"  🎯 TP: {levels['tp_prices']}, SL: {levels['sl']}")
                                    self.set_tpsl_for_position(symbol, "short", levels["tp_prices"], levels["sl"], order_size)
                                else:
                                    # Fallback auf spread-basiert
                                    tp = round(price - depth["spread"], PRICE_PLACES.get(symbol, 2))
                                    sl = round(price + depth["spread"] * 0.5, PRICE_PLACES.get(symbol, 2))
                                    self.set_tpsl_for_position(symbol, "short", [tp, tp, tp], sl, order_size)
                            else:
                                tp = round(price - depth["spread"], PRICE_PLACES.get(symbol, 2))
                                sl = round(price + depth["spread"] * 0.5, PRICE_PLACES.get(symbol, 2))
                                self.set_tpsl_for_position(symbol, "short", [tp, tp, tp], sl, order_size)
                            
                            # MERGE statt OVERWRITE — TP/SL-Tracking erhalten
                            if symbol in self.positions:
                                self.positions[symbol].update({
                                    "side": "short", "entry": price, "size": order_size,
                                    "mark_price": depth["mid"],
                                    "tp_prices": levels["tp_prices"] if levels and levels.get("tp_prices") else [tp, tp, tp],
                                    "sl": levels["sl"] if levels and levels.get("sl") else sl,
                                })
                            else:
                                self.positions[symbol] = {
                                    "side": "short", "entry": price, "size": order_size,
                                    "mark_price": depth["mid"],
                                    "tp_prices": levels["tp_prices"] if levels and levels.get("tp_prices") else [tp, tp, tp],
                                    "sl": levels["sl"] if levels and levels.get("sl") else sl,
                                }
                            if buy_id:
                                client._post("/api/v2/mix/order/cancel-order", {
                                    "symbol": symbol, "productType": "USDT-FUTURES",
                                    "marginCoin": "USDT", "orderId": buy_id
                                })
                            continue
                    
                    # Kein Fill → Order bleibt als pending, Stale-Timeout (60s) cancelled später
                    logger.debug(f"  ⏳ {symbol}: Order nicht gefuellt — warte auf Stale-Timeout")
                
            except Exception as e:
                logger.error(f"❌ {symbol} Error: {e}")
    
    def run(self):
        """Main Loop"""
        # ── Startup: Alte TPSL-Orders aller Symbole löschen (inkl. pos_loss/profit) ──
        for sym in SYMBOLS:
            try:
                client.cancel_tpsl_orders(sym, extra_types=['pos_loss', 'pos_profit'])
            except:
                pass
        
        cycle = 0
        while self.running:
            try:
                cycle += 1
                start = time.time()
                
                self.run_cycle()
                # Positions-State für Dashboard in JSON schreiben
                try:
                    state = {}
                    for sym, pdata in self.positions.items():
                        if pdata.get("tp_prices"):
                            state[sym] = {
                                "sl": pdata.get("sl"),
                                "tp": pdata.get("tp_prices", [None])[0],
                            }
                    if state:
                        with open(SHARED_STATE_PATH, "w") as f:
                            json.dump(state, f)
                except:
                    pass
                
                # Alle 15 Zyklen (~30s) Prüfe TP/SL auf offenen Positionen und closed positions
                if cycle % 15 == 0:
                    self._verify_tpsl_on_positions()
                    self._check_closed_positions()
                
                elapsed = time.time() - start
                if elapsed < LOOP_INTERVAL:
                    time.sleep(LOOP_INTERVAL - elapsed)
                    
            except KeyboardInterrupt:
                logger.info("⏹️  Bot gestoppt")
                break
            except Exception as e:
                logger.error(f"❌ Main Loop Error: {e}")
                time.sleep(LOOP_INTERVAL)
    
    def _verify_tpsl_on_positions(self):
        """Prüfe alle offenen Positionen auf korrektes bot-seitiges SL/TP-Tracking:
        - Prüft ob self.positions für jede Position SL/TP gespeichert hat
        - Wenn nicht (z.B. nach Bot-Neustart), berechne neu und speichere
        - Läuft alle ~30s (alle 15 Zyklen)
        """
        for symbol in SYMBOLS:
            try:
                pos_raw = client.get_position(symbol)
                if not pos_raw or float(pos_raw.get("total", 0)) == 0:
                    continue

                stored = self.positions.get(symbol, {})
                has_sl = stored.get("sl") is not None
                has_tp = stored.get("tp_prices") is not None

                if has_sl and has_tp:
                    continue  # Alles OK

                logger.info(f"🔧 Bot-SL fehlt für {symbol}: SL={has_sl} TP={has_tp} — berechne neu")

                side = pos_raw.get("holdSide", "short")
                entry = float(pos_raw.get("openPriceAvg", 0))
                if not entry:
                    continue

                atr = self.calc_atr(symbol)
                if atr:
                    levels = self.calc_tp_sl_levels(symbol, entry, side, atr)
                else:
                    levels = None

                if levels:
                    tp_prices = levels["tp_prices"]
                    sl_price = levels["sl"]
                else:
                    # Fallback: 1.5% vom Marktpreis
                    mark = float(pos_raw.get("markPrice", entry))
                    price_places = PRICE_PLACES.get(symbol, 2)
                    if side == "short":
                        sl_price = round(mark * 1.015, price_places)
                        tp_price = round(mark * 0.985, price_places)
                    else:
                        sl_price = round(mark * 0.985, price_places)
                        tp_price = round(mark * 1.015, price_places)
                    tp_prices = [tp_price, tp_price, tp_price]

                # Best-Effort: Exchange-TPSL versuchen (funktioniert auf Live, Demo ignoriert)
                try:
                    self.set_tpsl_for_position(symbol, side, tp_prices, sl_price, float(pos_raw["total"]))
                except:
                    pass

                # Bot-seitig tracken (funktioniert auf Live UND Demo)
                if symbol not in self.positions:
                    self.positions[symbol] = {}
                self.positions[symbol].update({
                    "tp_prices": tp_prices,
                    "sl": sl_price,
                    "side": side,
                    "entry": entry,
                    "size": float(pos_raw["total"]),
                    "mark_price": float(pos_raw.get("markPrice", entry)),
                })
                logger.info(f"  ✅ {symbol}: Bot-SL={sl_price} TP={tp_prices[0]} gespeichert")

            except Exception as e:
                logger.debug(f"  ⏭️  {symbol} verify: {e}")
    
    def _check_closed_positions(self):
        """Überprüfe ob Positionen geschlossen wurden (TP/SL Hit) und sende Telegram mit P&L"""
        for symbol in SYMBOLS:
            try:
                pos_raw = client.get_position(symbol)
                
                # Wenn Position = 0, war sie offen und ist jetzt closed
                if not pos_raw or float(pos_raw.get("total", 0)) == 0:
                    # Prüfe ob wir diese Position in self.positions tracked haben
                    if symbol in self.positions:
                        old_pos = self.positions[symbol]
                        
                        # Berechne P&L
                        entry_price = old_pos.get("entry", 0)
                        if pos_raw:
                            mark_price = float(pos_raw.get("markPrice", entry_price))
                        else:
                            # Position wurde vom Exchange geschlossen (TP/SL)
                            # Verwende aktuellen Ticker-Preis als Schätzung
                            try:
                                ticker = client.get_ticker(symbol)
                                mark_price = float(ticker.get("last", entry_price))
                            except:
                                mark_price = entry_price
                        side = old_pos.get("side", "?")
                        size = old_pos.get("size", 0)
                        
                        if side == "long":
                            pnl = (mark_price - entry_price) * size
                            exit_reason = "TP hit" if mark_price > entry_price else "SL hit"
                        else:  # short
                            pnl = (entry_price - mark_price) * size
                            exit_reason = "TP hit" if mark_price < entry_price else "SL hit"
                        
                        # Telegram Nachricht mit P&L (nur TP, kein SL-Spam)
                        try:
                            if "TP" in exit_reason:  # Nur TP-Benachrichtigungen
                                import telegram_notify as tg
                                icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
                                reason_emoji = "✅" if "TP" in exit_reason else "🛑"
                                tg_msg = f"{icon} {symbol} {side.upper()} CLOSED\n{pnl:+.4f} USDT | {reason_emoji} {exit_reason}"
                                tg.send(tg_msg)
                            else:
                                icon = "⚪"
                            logger.info(f"{icon} Position closed: {symbol} {side.upper()} | {pnl:+.4f} USDT | {exit_reason}")
                        except Exception as tg_err:
                            logger.warning(f"  ⚠️  Telegram send failed: {tg_err}")
                        
                        # Remove position tracking
                        del self.positions[symbol]
                        
            except Exception as e:
                logger.debug(f"  ⏭️  {symbol} closed-check: {e}")


if __name__ == "__main__":
    bot = SpreadScalper()
    bot.run()
