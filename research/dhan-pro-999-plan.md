# Dhan Pro (₹999) પ્લાનનો પૂરો ઉપયોગ: કામની યોજના

તારીખ: 28-11-2026 · આધાર: Dhan ના official docs (docs.dhanhq.co) અને આપણો tracker code (`antigravity-py/trackers/`)

**મૂળ નિયમ:** Tracker ફક્ત ભાવ અને alert બતાવે છે, ક્યારેય order મૂકતો નથી. Bot ના 5 Dhan connection ને હાથ નહીં લગાડીએ.

---

## ૧. Pro પ્લાનમાં શું મળે છે અને આપણે શું વાપરીશું

| Pro ની સુવિધા | શું આપે છે | આપણા કામમાં ઉપયોગ | Connection વપરાય? | ફાયદો |
|---|---|---|---|---|
| **Market Quote (REST)** | 1 request માં 1000 instrument: LTP, VWAP, OI, Volume, depth | 25,000 strike ના ભાવ દર ~30 સેકન્ડે | **ના** | ⭐⭐⭐ ખૂબ મોટો |
| **News with sentiment** | Headline, stock સાથે જોડાણ, પોઝિટિવ/નેગેટિવ | 218 F&O stock ની news alert, sentiment સાથે | ના | ⭐⭐⭐ |
| **Market Movers** | Highest OI, OI gainers/losers, Top volume, Price gainers/losers | "Hot list": કયા strike માં હલચલ છે તે તરત | ના | ⭐⭐⭐ |
| **Technical Data** | RSI, EMA, ADX, ATR, MACD, Pivot (1/5/15 મિનિટ, Daily) | Alert ને trend સાથે ચકાસવો (ખોટા alert ઓછા થાય) | ના | ⭐⭐ |
| Option Chain (Greeks, IV) | 1 stock, 3 સેકન્ડે એક | આપણા ગણેલા IV સાથે સરખામણી | ના | ⭐ ચકાસણી માટે |
| 20 લેવલ depth | 50 instrument / connection | ટોચના 50 strike માં મોટા buyer/seller | ⚠️ અલગ server, ચકાસવું પડશે | ⭐⭐ |
| 200 લેવલ depth | 1 instrument / connection | ખૂબ ઓછો ઉપયોગ | ⚠️ | ⭐ છોડી દઈએ |
| CAS live feed | Closing auction ભાવ (3:30 પછી) | Expiry દિવસે closing ભાવનો અંદાજ | **હા** (5 માંથી એક) | ⭐ ફક્ત connection ખાલી હોય તો |
| જૂનો data (5 વર્ષ intraday) | Backtest | News અને alert નો ફાયદો માપવો | ના | ⭐⭐ |

---

## ૨. કામનાં પગથિયાં

| # | ક્યારે | શું કરવું | ક્યાં (repo / જગ્યા) | કોણ |
|---|---|---|---|---|
| **0** | સોમવાર સવારે 09:00 પહેલાં | Tracker Upstox પર ચાલુ. Bot ખરેખર કેટલા Dhan connection અને instrument વાપરે છે તે માપવું | `C:\TRACKING 25000` + `Expiry-Friday-5x` | DP545 session (માપવાનું જ, ફેરફાર નહીં) |
| **1** | અઠવાડિયું 1 | નવો **`dhan_rest`** mode: 25,000 strike ના ભાવ Dhan REST Quote થી, 0 connection | `EMA-NEW` → `antigravity-py/trackers/providers.py` | હું (cloud): code + test + PR. DP545: live ચકાસણી |
| **2** | અઠવાડિયું 1–2 | **Dhan News**: 218 stock ની news દર 30 સેકન્ડે, sentiment સાથે. જૂના NSE/Google સ્ત્રોત backup તરીકે | `trackers/news.py` + UI tab | હું |
| **3** | અઠવાડિયું 2 | **Market Movers**: દર 1–2 મિનિટે OI gainers/losers, Top volume → tracker માં "Hot list" અને alert | `trackers/` માં નવી file + UI | હું |
| **4** | અઠવાડિયું 2–3 | **Technical Data**: 218 stock ના RSI/ADX/EMA દર 5 મિનિટે. Alert પર "trend સાથે / વિરુદ્ધ" નિશાન | `trackers/trend.py` + `stock_options.py` | હું |
| **5** | અઠવાડિયું 3 | **20 લેવલ depth**: ટોચના 50 strike. પહેલાં ચકાસવું કે આ connection bot ના 5 માં ગણાય છે કે નહીં | `trackers/providers.py` | હું + DP545 ચકાસણી |
| **6** | અઠવાડિયું 1–4 | **નફો માપવો** (paper scorecard): દરેક alert/news પછી 15/30/60 મિનિટે ભાવ કેટલો ચાલ્યો | `trackers/` + રોજનો report | આપમેળે |
| **7** | 4 અઠવાડિયા પછી | નિર્ણય: Pro રાખવો કે ₹499 પર પાછા જવું | — | તમે, report જોઈને |

---

## ૩. API મર્યાદાનું બજેટ (Dhan ની મર્યાદાની અંદર રહીશું)

| API | Dhan ની મર્યાદા | આપણો વપરાશ | બાકી જગ્યા |
|---|---|---|---|
| Market Quote | 1 request/સેકન્ડ | 0.8/સેકન્ડ (25,000 દર ~30 સેકન્ડે) | 20% bot માટે |
| Data APIs (News, Movers, Technical) | 5 request/સેકન્ડ, 1,00,000/દિવસ | ~1/સેકન્ડ, ~20,000/દિવસ | પુષ્કળ |
| Option Chain | 3 સેકન્ડે 1 | ફક્ત ચકાસણી માટે, ક્યારેક | — |
| WebSocket | 5 connection (bot વાપરે છે) | **0** | Bot સલામત |

Rate limit (RL001 / 429) આવે તો આપમેળે ધીમું પડવું અને ફરી પ્રયત્ન કરવો. તેનો test પણ બનાવીશું.

---

## ૪. નફો કેવી રીતે માપીશું

| શું માપવું | કેવી રીતે | પાસ ગણાય જો |
|---|---|---|
| News sentiment | પોઝિટિવ news પછી stock/option 15/30/60 મિનિટમાં ઉપર ગયો? | 55%+ સાચા, ખર્ચ બાદ નફો |
| Market Movers hot list | Hot list માં આવેલા strike પછી ચાલ્યા? | સામાન્ય strike કરતાં વધારે ચાલ |
| Technical filter | "trend સાથે" alert, "trend વિરુદ્ધ" કરતાં સારા? | ખોટા alert ઓછામાં ઓછા 20% ઘટે |
| 20 depth | મોટા buyer પછી ભાવ વધ્યો? | સ્પષ્ટ ફરક દેખાય |

**પ્રામાણિક વાત:** કોઈ પણ data નફાની ગેરંટી નથી. આપણા જૂના અભ્યાસમાં news પછી સરેરાશ ફક્ત +0.05% થી +0.10% ચાલ હતી. એટલે 4 અઠવાડિયા paper પર માપીશું, અને ફાયદો દેખાય તો જ alert માં ઉમેરીશું.

**ખર્ચનો હિસાબ:** Pro = ₹999/મહિને. ₹499 કરતાં ₹500 વધારે. એટલે Pro ની સુવિધાઓ (News, Movers, Technical) મહિને ₹500 થી વધુ ફાયદો આપે તો જ Pro રાખવો.

---

## ૫. જોખમ અને સાવચેતી

| જોખમ | બચાવ |
|---|---|
| Tracker bot નું Dhan connection કાપી નાખે (error 805) | `dhan_rest` mode માં 0 connection. Depth connection વાપરતાં પહેલાં અલગથી ચકાસણી |
| Bot અને tracker બંને Quote API વાપરે તો 1/સેકન્ડ ની મર્યાદા ઓળંગાય | Tracker 0.8/સેકન્ડ પર. Bot quote વાપરે છે કે નહીં, તે પગથિયા 0 માં માપીશું |
| Token પૂરો થાય કે ખોટો હોય | Tracker આપમેળે Upstox પર જાય (આ સુવિધા પહેલેથી છે) |
| Token ની નકલ git માં જાય | `dhan.json` ક્યારેય commit નહીં. Token print નહીં |
| ખોટું order | Tracker માં order નો code જ નથી. Display only |

---

## ૬. હજુ ચકાસવાનું બાકી

| વાત | ક્યારે / કેવી રીતે |
|---|---|
| 20 લેવલ depth નું connection bot ના 5 માં ગણાય છે કે નહીં | પગથિયું 5. Bot બંધ હોય ત્યારે (શનિ-રવિ) 1 connection ખોલીને જોવું |
| Option નો OI REST Quote માં સાચો આવે છે | સોમવારે market ચાલુ થયા પછી broker app સાથે સરખાવવો |
| Technical Data ના જવાબમાં ખરેખર કયાં fields આવે છે (docs માં હજુ લખ્યું નથી) | પહેલા call માં જોઈને code ગોઠવવો |
| Bot હાલ કેટલાં connection અને instrument વાપરે છે | પગથિયું 0 |
