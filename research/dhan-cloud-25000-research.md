# Dhan Cloud / Sandbox par 25,000 instrument tracker: research

Taiyar: 28-11-2026 · Source: Dhan ni official docs (docs.dhanhq.co, 25-11-2026 nu full export), DhanHQ-py SDK, Dhan support ane MadeForTrade forum.
Tracker = `antigravity-py/trackers/` (DISPLAY ONLY, order kyarey nahi).

---

## 1. Tunko jawab

| Prashna | Jawab |
|---|---|
| Sandbox ma 25,000 nu kaam thai shake? | **Na.** Sandbox ma live market data j nathi. Fakt order/portfolio/funds ane **historical candles** chhe. |
| Dhan Cloud ma 25,000 nu kaam thai shake? | **Ardhu.** Data collector chali shake, pan aapnu UI (port 8000), file cache ane bhavcopy history tya nahi chale. |
| Real-time data male? | **Ha, pan fakt live account + ₹499 Data plan thi.** Sandbox thi nahi. Cloud ma pan e j live token vapray chhe. |
| 5 connection ni limit "bypass" thay? | **Na.** Limit `clientId` par chhe. Dhan Cloud ma pan e j account, etle e j 5. Aa niyam todva/chhupavvano prayatna nahi karvo joie. Account block thai shake. |
| Kayadesar biju rasto? | **Ha, be.** (A) Ek feed-hub 5 connection kholi bot + tracker banne ne data aape. (B) Tracker WebSocket na badle **REST Market Quote** vapre: 1000 instrument/request × 1 request/sec, etle 25,000 ~25–30 sec ma, **0 WebSocket connection**. |

---

## 2. Sandbox (cloud.dhanhq.co/sandbox): shu chhe, shu nathi

Docs: base URL `https://sandbox.dhan.co/v2`. Sandbox token Dhan Cloud mathi bane chhe ane live token thi alag chhe.

**Sandbox ma male (22 endpoint):** Place/Modify/Cancel/Slice order, Order book, Trade book, Holdings, Positions, Convert position, Kill switch, eDIS, Fund limit, Margin calculator, Ledger, Trade history, **Intraday ane Daily historical candles**.

**Sandbox ma NATHI:**
- Live Market Feed (WebSocket ticks)
- Market Quote (LTP/OHLC/Full quote)
- Option Chain (Greeks/IV/OI)
- 20/200 level depth, News, Market Movers

**Nishkarsh:** Sandbox **order logic test** karva mate chhe, data tracking mate nahi. Aapno tracker order mukto j nathi, etle tene sandbox no koi fayado nathi. Haa, **bot (Expiry-Friday-5x) na order flow** ne paisa vagar test karva mate sandbox saru chhe.

---

## 3. Dhan Cloud: shu chhe

Dhan nu managed Python platform. Code browser ma lakho (ke AI agent pase lakhavo), Deploy karo, ane schedule par chalavo (dakhla tarike weekdays 09:20–15:30).

| Vat | Mahiti (docs) |
|---|---|
| Machine | 1 vCPU, 3 GB RAM, dar strategy alag instance |
| Python | 3.11 (badli na shakay) |
| Kimat | Credit system. Signup par 100 free credits, pachhi Dhan ledger mathi ₹100/200/500/1000 ni top-up. **Kalak nu chokkas rate docs ma nathi.** Dashboard na "Cost Estimator" ma jovu |
| Static IP | Joitu nathi, Dhan andar thi connect thay chhe |
| Co-location | Exchange ni najik, etle latency ochhi |
| Outbound HTTP | Koi pan public host ne (`api.dhan.co`, `nseindia.com`, ...) |
| **Blocked** | File lakhvi (`open(...,'w')`), filesystem (`os.path`), `os.getenv`, `subprocess`, **network port bind karvo**, Excel packages |
| Storage | Run vachhe kai j save nathi rahetu. Bahar na DB (Supabase/Firebase) ma mokalvu pade |
| Code scanner | Dar deploy par AI scan. Koi pan flag aave to deploy bandh |
| Data | Data API plan (₹499) joie. WebSocket limit e j: **5 connection × 5000** per clientId |

### Aapna tracker ne Dhan Cloud par lai javama loop holes / adchan

| # | Adchan | Asar |
|---|---|---|
| 1 | Port bind nathi thato | `main.py` (FastAPI, localhost:8000) ane UI tab tya **chali j na shake** |
| 2 | File lakhvi blocked | `history.py` no 100 divas bhavcopy cache, alerts/log files save na thay |
| 3 | `os.path`, `os.getenv` hard-blocked | Aapna code ma `os.path` ghani jagyae chhe, etle scanner deploy rokshe. Mota pher-pharo karva pade |
| 4 | Persistent state nathi | Dar restart par ATH/ATL, OI history, trend state shunya thi |
| 5 | Same 5-connection limit | Cloud ma 25,000 WS kholo to **tamaro bot (je 5 vapre chhe) no feed katay (error 805)** |
| 6 | Kimat agaau khabar nathi | 6.25 kalak × 5 divas × 4 athvadiya nu credit kharch estimator thi j khabar padshe |
| 7 | UI/alerts mate bahar no server joie | Cloud collector → bahar DB → tamaro PC/UI. Banne jagyae code ane jalavani jarur |

**Nishkarsh:** Dhan Cloud nano "signal script" (ek strategy, thoda instrument, order mukvana) mate banavelu chhe. Aapna jeva **25,000 instrument + UI + history** vala app mate te yogya nathi. Teno mukhya fayado (no static IP, co-location) **order mukvama** chhe, ane aapno tracker order mukto j nathi.

---

## 4. Real-time data: kaya rasta thi ketlu male

| Rasto | Instrument | Speed | WS connection vapray? | Kimat |
|---|---|---|---|---|
| Dhan WebSocket (FULL) | 5 × 5000 = 25,000 | Tick-by-tick | **Ha, badha 5** | ₹499/mahino |
| **Dhan REST Market Quote** (`/marketfeed/quote`) | 1000 / request | 1 request/sec, etle 25,000 ~25 sec ma ek vaar | **Na** | e j ₹499 plan |
| Dhan Option Chain API | 1 scrip / request | 1 req/3 sec, 10 scrip/8 sec, etle 218 stock ~3 min | Na | e j plan |
| Upstox (hal nu tracker) | WS 5000 + REST badha | WS ticks ~4,600 + REST ~25,000 dhime | Upstox nu, Dhan nahi | Upstox plan |

Full quote ma `last_price`, `average_price` (VWAP), `volume`, **`oi`**, `ohlc`, 5-level depth, buy/sell quantity, circuit limit, ane last trade time aave chhe. IV/Greeks aapno `ivcalc.py` jaate gani le chhe.

**Dhyan rakhvu:** Market Quote ni 1 req/sec limit aakha account ni chhe. Bot pan quote API vaparto hoy to banne vachhe vahenchani thay, etle tracker ne ~0.8 req/sec par rakhvu.

---

## 5. Loko Dhan Cloud ma shu karave chhe (forum/support)

- AI agent pase saral strategy lakhave chhe (EMA crossover, ORB, 15-min candle par option buy/sell) ane 09:20–15:30 schedule par chalave chhe.
- Static IP ni jhanjhat vagar order mukva mate. SEBI na niyam pachhi API order mate static IP jaruri chhe, ane Cloud ma te joitu nathi.
- Forum ma lokone prashna chhe: ek lambo script WebSocket loop sathe saro ke batch jobs. Teno official jawab nathi malyo.
- Mota scanner/dashboard (hajaro instrument) Cloud par chalavvana udaharan nathi malya. Loko te potana PC/VPS par chalave chhe.

---

## 6. Aapna mate best plan (mari salah)

**Pagthiyu 1, haman (code badlav vagar):** Tracker **Upstox** par PC par chalavo (`token.json`). Bot na 5 Dhan connection salamat rahe.

**Pagthiyu 2, sauthi saro fayado (nano code badlav):** Tracker ma navo provider mode **`dhan_rest`**:
- Dhan `/marketfeed/quote` thi 25,000 instrument **dar ~25–30 sec** (0.8 req/sec, 1000/request).
- **0 WebSocket connection**, etle bot ne kai asar nahi.
- Upstox token ni jarur nahi. Badho data ek j Dhan ₹499 plan thi.
- Tick-by-tick nahi, pan 25 sec refresh 5–15 min na spike/OI/trend alert mate puratu chhe.
- Kaam: `providers.py` ma `DhanRestProvider` (hal no `_RateLimit(1)` ane `/marketfeed/ltp` code chhe j), `test_tracker.py` ma tests, ane Settings ma `trkProvider = dhan_rest`.

**Pagthiyu 3, lambe gaale best (motu kaam):** Ek **feed-hub** process PC par 5 Dhan WS kholi 25,000 instrument le, ane bot + tracker banne ne local thi aape. Aathi badhu tick-by-tick male. Pan bot (Node, Expiry-Friday-5x) ma badlav joie ane jokham vadhare chhe. Bot hal ketla instrument subscribe kare chhe te pahela mapvu pade.

**Dhan Cloud / Sandbox no upyog:**
- Sandbox: **bot** na order flow test karva mate (paisa vagar). Tracker mate nahi.
- Dhan Cloud: tracker mate **nahi**. Bhavishya ma koi nani order-strategy static IP vagar chalavvi hoy to vicharvu.

---

## 7. Haju chokkas nathi (verify karvanu)

- Dhan Cloud credit no kalak dith rate (dashboard → Credits & Usage → Cost Estimator).
- REST Market Quote ni 100,000/divas "Data APIs" daily limit tene lagu pade ke nahi. Docs ma Market Quote mate daily limit "—" chhe. 6.25 kalak × 0.8 req/sec ≈ 18,000 request/divas, etle banne rite limit ni andar.
- Bot (Expiry-Friday-5x) hal quote API vapre chhe ke nahi. Te parthi tracker no quote rate nakki thashe.
- Full quote ma option mate `oi` sachu aave chhe. Live market ma ek vaar check karvu.

## Sources
- Dhan docs full export: https://docs.dhanhq.co/llms-full.txt (Feature Matrix, Sandbox, Rate Limits, Live Market Feed, Dhan Cloud: Compute Tiers, Credits, Execution Environment, Allowed Operations, Code Scanner)
- Dhan Cloud: https://docs.dhanhq.co/cloud
- What is Dhan Cloud: https://dhan.co/support/platforms/dhanhq-api/what-is-dhan-cloud/
- Dhan Cloud charges: https://dhan.co/support/platforms/dhanhq-api/what-are-the-charges-for-deploying-my-algo-on-dhan-cloud/
- Live feed instruments: https://dhan.co/support/platforms/dhanhq-api/how-many-instruments-can-i-track-using-dhan-s-live-market-feed/
- Forum, Cloud architecture: https://madefortrade.in/t/best-architecture-for-15-min-intraday-options-strategy-on-dhan-cloud-batch-jobs-vs-long-running-script/91302
- Forum, Cloud rate limits: https://madefortrade.in/t/does-orders-generated-via-colocated-dhan-cloud-have-rate-limits/91498
- Forum, live feed: https://madefortrade.in/t/now-live-live-market-feed-on-dhanhq-trading-apis/22672
