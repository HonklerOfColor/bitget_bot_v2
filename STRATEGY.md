# SHORT_BOT — Aktuelle Strategie (Code-Stand 22.07.2026)

## Überblick

**High-Frequency Spread-Scalping Bot** auf Bitget USDT-M Futures (Live, $128 Equity).
Post-Only Limit-Orders innerhalb des Bid-Ask Spreads. **SHORT-Only** (Backtest: Short 13× profitabler als Long).  
Hebel 3×, Loop alle 2s, 4 Symbole parallel.

---

## 🔧 Kern-Parameter

| Parameter | Wert | Beschreibung |
|-----------|------|-------------|
| **Symbole** | BTCUSDT, ETHUSDT, SOLUSDT | 3 Pairs parallel (XRP entfernt) |
| **Hebel** | **3×** | Aus Backtest optimiert (5× verliert 10× mehr) |
| **Richtung** | **LONG+SHORT** (`SHORT_ONLY=False`) | Funding-Signal: >+0.01%→SHORT, <−0.01%→LONG |
| **EMA-Filter** | **Aus** (`EMAFILTER=False`) | Deaktiviert |
| **Loop** | ~2 Sekunden | Pro Symbol sequentiell |
| **Order-Typ** | Post-Only Limit | Immer Maker (Fee-Rabatt) |
| **Max Spread** | 0.5% | Überspringen wenn breiter |
| **Offset** | 0.01% | Hauchdünn im Orderbook |
| **Min Notional** | $5 USDT pro Order | Sonst skip |
| **Funding-Filter** | Max 0.05% | Kein Short wenn Funding > 0.05% |
| **Account** | **Live** | $128 Equity (seit 10.07.2026) |

---

**Spread-Penetration (Entry-Preise):**
Alle Symbole: SHORT @ 70% (Bid + Spread × 0.7)

---

## 📦 Position Sizing

| Symbol | Min Qty | Ca. Notional |
|--------|---------|-------------|
| BTCUSDT | 0.001 | ~$64 |
| ETHUSDT | 0.05 | ~$150 |
| SOLUSDT | 0.2 | ~$16 |

Size wird dynamisch erhöht wenn `min_qty × mid_price < $5`.

---

## 🧭 Entry-Logik

1. **Orderbook Depth holen** (Bid/Ask/Spread via merge-depth)
2. **Auf bestehende Pending-Order prüfen** — überspringen wenn < 60s alt
3. **Stale-Order Cleanup**: nach 60s canceln + neu platzieren
4. **Einstiegspreis berechnen**: `Bid + (Spread × SHORT_PEN%)`
5. **Order-Grösse**: `MIN_SIZES[symbol]`, dynamisch ≥ $5 Notional
6. **📡 Coinglass Check** vor jeder Order:
   - Funding Rate + OI-Change
   - Wenn Funding > 0.05% → **skip** (zu crowded long)
   - Wenn kein Signal (None) → **skip**
7. **Richtung**: `SHORT_ONLY=False` → Richtung per **Funding-Signal**:
   - Funding > +0.01% → **SHORT** (Crowd long, Contrarian)
   - Funding < −0.01% → **LONG** (Crowd short, Contrarian)
   - Funding neutral + EMA-Filter an → EMA-Trend
   - Funding neutral + kein EMA-Filter → **skip** (kein Trade)
8. **Post-Only Limit-Order platzieren**
9. **0.5s warten**, dann Order-Status prüfen
10. **Gefüllt** → TP/SL via `place-pos-tpsl` + bot-seitiges Tracking
11. **Nicht gefüllt** → als pending speichern, nächster Cycle

---

## 🎯 TP/SL — Exchange + Bot-Seitig (Dual Protection)

### TP: Single-Level bei 2.0× ATR

```python
TP_LEVELS = [{"pct": 1.0, "atr_mult": 2.0}]  # 100% @ 2.0× ATR
```

- SHORT: `TP = Entry - (ATR × 2.0)`
- Wird via `place-pos-tpsl` auf der Exchange gesetzt (position-level)

### SL: Chart-basiert + ATR-Fallback

1. **Primär**: `calc_chart_sl()` — höchstes High der letzten 20 1H-Kerzen + 0.1%
2. **Fallback**: `1.5× ATR` wenn keine Chartdaten
3. **Safety**: SL nie näher als `0.5× ATR` vom Entry

### 🛡️ SL-Hit-Schutz (Funding-Puffer) — ab 02.08.2026

Wenn der SL getroffen würde, aber die **Funding-Rate positiv** ist (Longs zahlen Shorts → Short-Position verdient Funding):
- SL wird um **0.5×ATR** erweitert statt den Trade zu schliessen
- Die Funding-Zahlung kompensiert die offene Position
- Verhindert SL-Hits während Funding-gegenläufiger Phasen (nächtliche Whipsaws)

### ROE-Trailing (ab +3% Peak)

Sobald eine Position **≥3% Peak-ROE** erreicht:

1. **Peak-ROE tracken**: Höchster ROE seit Einstieg
2. **SL = 2% unter Peak**: Lockt Gewinn dynamisch
3. **Nur tighten**: SL wird nur enger (nie weiter weg)
4. **Check alle 2s**, Update auf Exchange via `place-pos-tpsl`

**Beispiel SHORT @77.995, ATR=0.319:**
- TP = 77.995 − (0.319 × 2.0) = **77.357**
- SL initial = höchstes High (20h) + 0.1% = **78.411**
- Bei Peak 3.8%: SL = **78.277** (lockt 1.8% Gewinn)
- Bei Peak 3.9%: SL = **78.262** (lockt 1.9% Gewinn)

### Verlustserien-Adaption

```python
LOSS_STREAK_THRESHOLD = 3  # Nach 3 Verlusten
SL_BASE_MULT = 0.30       # 0.30× ATR baseline
SL_MAX_MULT = 0.60        # 0.60× ATR bei Verlustserie
```

---

## 📊 PnL-Berechnung

- **Open Positions**: ROE = `unrealizedPL / marginSize × 100`
- **Closed Positions**: PnL aus Mark-Price bei TP/SL-Trigger
- **Mark-Price** wird für TP/SL-Prüfung verwendet (kein Bid/Ask — Spread-Spikes vermeiden)

---

## 🔄 Startup Behaviour

1. **Alle TPSL-Orders löschen** (cancel + pos_loss/pos_profit)
2. **Bestehende Positionen prüfen**: ATR berechnen, TP/SL via `place-pos-tpsl` setzen
3. **`self.positions` befüllen**: SL/TP/Entry/Size für bot-seitiges Tracking
4. **Trade-Logs laden** aus `spread_learnings.json`
5. **Haupt-Loop starten** (2s Intervall)

---

## 🧠 Trade Analysis (DeepSeek)

- Nach jedem Trade: Analyse via DeepSeek API
- PnL, Entry/Exit, ROE — geprompted für Lessons Learned
- Asynchron (eigener Thread), blockiert nicht

---

## 📁 State Management

| Datei | Inhalt |
|-------|--------|
| `bot_state.json` | Aktuelle SL/TP Levels (vom Dashboard gelesen) |
| `spread_learnings.json` | Trade-Log + Statistiken pro Symbol |
| `spread_scalper.log` | Runtime-Log (INFO-Level) |
| `watchdog.log` | Healthcheck-Log |

---

## 🏥 Healthcheck (Cron)

Alle 15 Minuten via `no_agent=True` Script:
- 0 Bot-Prozesse → Neustart + Telegram
- 1 Bot-Prozess → still (OK)
- 2+ Bot-Prozesse → Duplikate killen + Telegram

---

## ⚠️ Bekannte Limitationen

- **Funding neutral + kein EMA-Filter**: Kein Trade bei neutralem Funding — der Bot skipped
- **Demo `place-pos-tpsl`**: Bitget Demo akzeptiert den Call (code 00000) aber persistiert nicht
- **Single TP**: Nur TP1 auf Exchange gesetzt (Multi-Level-TP deaktiviert)
- **TPSL-Orders nicht lesbar**: `orders-plan`/`current-plan` geben 40404 → nur via Position-Felder
- **Telegram-Token nicht gesetzt**: `scalper_config.py` fehlt → Telegram-Benachrichtigungen inaktiv
- **0.4% Guard**: Sehr kleine SL-Anpassungen werden vom Trailing übersprungen

---

## Änderungshistorie

| Datum | Änderung |
|-------|----------|
| 17.07.2026 | **LONG+SHORT** (SHORT_ONLY=False), Funding-Signal-Steuerung |
| 17.07.2026 | TP 3.0→**2.0×ATR**, XRPUSDT entfernt |