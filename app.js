/* ============================================
   EMA STRATEGY DASHBOARD - APP.JS
   Complete Trading Strategy Engine
   ============================================ */

// ============================================
// CONFIG
// ============================================
const CONFIG = {
    emaPeriods: [9, 21, 50, 100, 200],
    emaColors: {
        9: '#ffd60a',
        21: '#2962ff',
        50: '#3b82f6',
        100: '#f72585',
        200: '#ff8c00'
    },
    emaLineWidths: {
        9: 1.5,
        21: 1.5,
        50: 1.5,
        100: 1,
        200: 1
    },
    candleUp: '#00f5d4',
    candleDown: '#f72585',
    wickUp: '#00f5d4',
    wickDown: '#f72585',
    riskRewardRatio1: 1,
    riskRewardRatio2: 2,
    // ============================================
    // GAMMA BLAST DETECTION PARAMETERS (expiry-day option explosion)
    // greeks come back 0 from the API, so we detect the *symptoms* of a blast:
    // cheap option + sharp spot move + rapid premium surge in the blast window.
    // (Parameters only — detection logic added in the next step.)
    // ============================================
    gammaBlast: {
        enabled: true,
        onlyExpiryDay: true,        // only on the day of expiry (highest gamma)
        windowStartIST: '13:00',    // blast usually builds in the afternoon
        windowEndIST: '15:25',      // stop a little before close
        minPremium: 7,              // ₹ — ignore dead near-zero options (filters expiry noise)
        maxPremium: 60,             // ₹ — cheap enough to multi-bag
        atmStrikesRange: 3,         // watch ATM ± this many strikes (OTM that can blast)
        spotMovePct: 0.0015,        // index must move >= 0.15% ...
        spotLookbackSec: 180,       // ... within the last 3 minutes (sharp move)
        premiumSurgePct: 50,        // option premium up >= 50% ...
        premiumLookbackSec: 120,    // ... within the last 2 minutes  => BLAST
        oiChangePct: 10,            // optional: OI change vs prev_oi (confirmation)
        requireVolumeRising: true,  // optional: volume must be increasing
        cooldownSec: 60             // min gap between blast alerts
    },
    LIVE_TRADING: false,         // ⚠️ false = PAPER (simulated) orders only. Real auto-buy needs a trading token + ARM.
    lotSize: { NIFTY: 65, BANKNIFTY: 30, SENSEX: 20, RELIANCE: 500, TATASTEEL: 5500 },
    spike: {                     // 3 PM spike catcher (2:55-3:05 sharp move)
        startIST: '14:55', endIST: '15:05',
        movePct: 0.0007,         // index move >= 0.07% within lookback -> spike
        lookbackSec: 120         // over last ~2 minutes
    },
    // POINT-based exits take priority over the % ones below (set a pts value to 0 to fall back to %).
    optStopPts: 8,               // FIXED stop loss = this many PREMIUM POINTS below entry
    optTakeProfitPts: 13,        // HARD take-profit = this many PREMIUM POINTS above entry  (R:R 1 : 1.63)
    optStopPct: 8,               // fallback: stop = this % below entry premium (used only if optStopPts = 0)
    optTakeProfitPct: 5,         // fallback: take-profit = this % above entry (used only if optTakeProfitPts = 0)
    chargesPerTrade: 60,         // Rs charges deducted per round-trip trade (brokerage+taxes est.)
    trailProfitActivatePct: 30,  // once premium is +this% from entry, start trailing the profit
    trailProfitGapPct: 20,       // trailing-profit stop sits this % below the peak (locks profit)
    optStrikeRange: 10,          // ATM ± this many strikes (option chain depth)
    requireInsideBar: true,      // true = ONLY inside-bar breakout (trend-aligned, high quality) — prevents counter-trend spam
    maxTradesPerDay: 6,          // safety: stop over-trading
    onePositionAtATime: true,    // no stacking — only one open trade at a time
    maxDailyLossRs: 5000,        // KILL-SWITCH: stop auto-trade if today's loss exceeds this (Rs)
    maxDailyProfitRs: 0,         // optional: stop after this profit (0 = off)
    requireOptionMomentum: true, // only buy an option that is itself rising (avoid decay/falling)
    insideBarCloseConfirm: true, // wait for the breakout candle to CLOSE beyond the inside bar (no fake wicks)
    motherBarMinPct: 0.0008,     // mother bar range must be >= 0.08% of price (skip tiny consolidations)
    signalCooldownMs: 30000,
    maxSignals: 20,
    maxTrades: 50,
    updateIntervalMs: 2000,
    candleRefreshMs: 30000,   // re-fetch full real candles every 30s (corrects OHLC)
    symbols: {
        NIFTY: { name: 'NIFTY 50', basePrice: 24350, volatility: 0.0008 },
        SENSEX: { name: 'SENSEX', basePrice: 77000, volatility: 0.0008 },
        BANKNIFTY: { name: 'BANK NIFTY', basePrice: 52800, volatility: 0.0012 },
        RELIANCE: { name: 'RELIANCE', basePrice: 2945, volatility: 0.0006 },
        TATASTEEL: { name: 'TATA STEEL', basePrice: 148, volatility: 0.001 }
    }
};

// ============================================
// LIVE DATA (Upstox via local proxy server.py)
// ============================================
const LIVE = {
    enabled: true,            // use real Upstox data
    allowSimulation: false,   // NO fake data — real data only (user requirement)
    active: false,            // true once real candles loaded
    authenticated: false,
    lastCandleRefresh: 0,
    marketOpen: false,        // is NSE market currently open
    wasMarketOpen: false,     // for detecting open/close transitions
    sse: null,                // EventSource for live ticks
    sseConnected: false,
    lastTickTime: 0,
    ticks: {},                // symbol -> latest live price
    optPrem: {},              // "STRIKE-CE"/"STRIKE-PE" -> latest premium
    optQuote: { ce: null, pe: null },  // current ATM call/put {strike,key,ltp}  (for new entries)
    chartCe: null,            // pinned strike for the CALL chart line {strike,key}
    chartPe: null,            // pinned strike for the PUT chart line {strike,key}
    lastOptLineTime: 0
};

// ---- Market hours (NSE: Mon–Fri, 09:15–15:30 IST) ----
function nowIST() {
    const now = new Date();
    const utcMs = now.getTime() + now.getTimezoneOffset() * 60000;
    return new Date(utcMs + 5.5 * 3600000);
}

function isMarketOpen() {
    const ist = nowIST();
    const day = ist.getDay();            // 0 = Sun, 6 = Sat
    if (day === 0 || day === 6) return false;
    const mins = ist.getHours() * 60 + ist.getMinutes();
    return mins >= (9 * 60 + 15) && mins <= (15 * 60 + 30);
}

// "Live" = we are actually receiving ticks (Upstox only streams during market
// hours). More reliable than the PC clock for deciding when to update.
function feedLive() {
    return LIVE.sseConnected && (Date.now() - LIVE.lastTickTime < 10000);
}

async function apiGet(path) {
    const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
    if (res.status === 401) {
        LIVE.authenticated = false;
        return { status: 'unauthorized' };
    }
    if (!res.ok) throw new Error('API ' + res.status);
    return res.json();
}

// ---- Persist trades + option orders to the server (survive reloads) ----
let _saveTimer = null;
function saveSession() {
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => {
        try {
            fetch('/api/session/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    trades: state.trades,
                    paperOrders: state.paperOrders,
                    tradeCounter: state.tradeCounter
                })
            });
        } catch (e) { /* ignore */ }
    }, 400);
}

async function loadSession() {
    try {
        const r = await apiGet('/api/session/load');
        const s = r && r.session;
        if (!s) return;
        if (Array.isArray(s.trades)) {
            state.trades = s.trades.map(t => ({
                ...t,
                time: new Date(t.time),
                exitTime: t.exitTime ? new Date(t.exitTime) : null
            }));
        }
        if (Array.isArray(s.paperOrders)) {
            state.paperOrders = s.paperOrders.map(o => ({ ...o, time: new Date(o.time) }));
        }
        state.tradeCounter = s.tradeCounter || state.trades.reduce((m, t) => Math.max(m, t.id || 0), 0);
    } catch (e) { /* ignore */ }
}

async function checkAuth() {
    try {
        const r = await apiGet('/api/auth-status');
        LIVE.authenticated = !!r.authenticated;
        state.lastAuthInfo = r;
        updateDataBanner(r);
        return r;
    } catch (e) {
        LIVE.authenticated = false;
        state.lastAuthInfo = null;
        updateDataBanner(null);
        return null;
    }
}

async function fetchRealCandles(symbol, timeframe) {
    // timeframe (1/3/5/15/30) -> v3 unit=minutes, interval=timeframe
    const r = await apiGet(`/api/intraday?symbol=${symbol}&unit=minutes&interval=${timeframe}`);
    if (r.status === 'ok' && Array.isArray(r.candles) && r.candles.length) {
        return r.candles.map(c => ({
            time: c.time,
            open: parseFloat(c.open.toFixed(2)),
            high: parseFloat(c.high.toFixed(2)),
            low: parseFloat(c.low.toFixed(2)),
            close: parseFloat(c.close.toFixed(2))
        }));
    }
    if (r.status === 'unauthorized') LIVE.authenticated = false;
    return null;
}

// ---- All-index live ticker ----
function renderIndexTicker() {
    const el = document.getElementById('index-ticker');
    if (!el) return;
    el.innerHTML = Object.keys(CONFIG.symbols).map(sym => {
        const ltp = LIVE.ticks[sym];
        const active = sym === state.symbol ? 'active' : '';
        return `<div class="idx-item ${active}" data-sym="${sym}">
            <span class="idx-name">${CONFIG.symbols[sym].name}</span>
            <span class="idx-ltp">${ltp != null ? formatPrice(ltp) : '--'}</span>
        </div>`;
    }).join('');
}

async function updateAllLtp() {
    try {
        const syms = Object.keys(CONFIG.symbols).join(',');
        const r = await apiGet('/api/ltp?symbols=' + syms);
        if (r.status === 'ok' && r.prices) {
            for (const s in r.prices) if (r.prices[s] != null) LIVE.ticks[s] = r.prices[s];
        }
    } catch (e) { /* ignore */ }
    renderIndexTicker();
}

async function fetchLTP(symbol) {
    const r = await apiGet(`/api/ltp?symbols=${symbol}`);
    if (r.status === 'ok' && r.prices && r.prices[symbol] != null) {
        return r.prices[symbol];
    }
    return null;
}

// ---- Live WebSocket feed via server SSE (/api/stream) ----
function startLiveFeed() {
    if (LIVE.sse) return;                 // already running
    try {
        const es = new EventSource('/api/stream');
        LIVE.sse = es;

        es.onmessage = (ev) => {
            try {
                const tick = JSON.parse(ev.data);
                LIVE.sseConnected = true;
                LIVE.lastTickTime = Date.now();
                if (tick.symbol && tick.ltp != null) {
                    LIVE.ticks[tick.symbol] = tick.ltp;
                    renderIndexTicker();
                    if (tick.symbol === state.symbol) applyLiveTick(tick.ltp);
                }
            } catch (e) { /* ignore malformed */ }
        };

        es.onerror = () => {
            // EventSource auto-reconnects; just mark disconnected meanwhile
            LIVE.sseConnected = false;
        };
    } catch (e) {
        console.warn('SSE not available:', e);
    }
}

// Apply a single live tick to the current candle (real-time price ticking)
function applyLiveTick(ltp) {
    if (!LIVE.active || !LIVE.authenticated) return;
    if (!state.candleData.length) return;
    // No clock gate: if a tick arrived, the market is live by definition.

    const lastCandle = state.candleData[state.candleData.length - 1];
    const nowSec = Math.floor(Date.now() / 1000);
    const candleEnd = lastCandle.time + state.timeframe * 60;

    if (nowSec >= candleEnd) {
        state.candleData.push({ time: candleEnd, open: ltp, high: ltp, low: ltp, close: ltp });
        if (state.candleData.length > 500) state.candleData.shift();
    } else {
        lastCandle.close = parseFloat(ltp.toFixed(2));
        lastCandle.high = parseFloat(Math.max(lastCandle.high, ltp).toFixed(2));
        lastCandle.low = parseFloat(Math.min(lastCandle.low, ltp).toFixed(2));
    }

    // Light, immediate update of just the last bar + price stats
    if (state.candleSeries) state.candleSeries.update(state.candleData[state.candleData.length - 1]);
    updateStatsBar();
}

function updateDataBanner(authInfo) {
    const banner = document.getElementById('data-banner');
    const text = document.getElementById('data-source-text');
    const loginLink = document.getElementById('data-login-link');
    const expiry = document.getElementById('data-expiry');
    if (!banner) return;

    banner.classList.remove('live', 'sim');
    loginLink.style.display = 'none';
    expiry.textContent = '';

    if (LIVE.active && LIVE.authenticated) {
        if (LIVE.marketOpen) {
            banner.classList.add('live');
            text.textContent = '🟢 LIVE — Upstox real market data (market open)';
        } else {
            banner.classList.add('sim');
            text.textContent = '🔴 MARKET CLOSED — showing last session data (no live updates)';
        }
        if (authInfo && authInfo.expires_at) {
            const d = new Date(authInfo.expires_at * 1000);
            expiry.textContent = 'Token valid till ' + d.toLocaleString('en-IN', { timeZone: IST_TZ });
        }
    } else if (LIVE.authenticated && !LIVE.active) {
        banner.classList.add('sim');
        text.textContent = '🟡 Connected — loading real data…';
    } else {
        // Not authenticated / connection failed — do NOT show fake data
        banner.classList.add('sim');
        text.textContent = '❌ Upstox connection failed — check token (run server.py)';
        loginLink.style.display = 'inline-block';
    }
}

// ============================================
// STATE
// ============================================
const state = {
    symbol: 'NIFTY',
    timeframe: 1,
    candleData: [],
    emaData: {},
    signals: [],
    trades: [],
    paperOrders: [],
    gamma: { spotHist: [], premHist: {}, lastBlastTime: 0 },
    autoFit: true,            // when ON, charts auto-adjust to the latest candles
    spikeMode: false,         // 3 PM spike catcher (2:55-3:05)
    lastSpikeTime: 0,
    strategy: 'inside_bar',   // live strategy: inside_bar | bollinger | supertrend | orb
    orbDoneDay: null,
    autoTrade: true,          // when ON, option trades are taken automatically on signals (auto-pilot)
    liveArmed: false,         // when ON, REAL broker orders are placed (real money) — default OFF
    realOrdersToday: 0,       // safety counter
    tradeCounter: 0,
    lastSignalTime: 0,
    lastInsideBarTime: 0,
    lastAuthInfo: null,
    showEMAs: true,
    showSignals: true,
    showLevels: true,
    chart: null,
    candleSeries: null,
    emaLines: {},
    markers: [],
    updateInterval: null,
    priceLines: []
};

// ============================================
// UTILITY FUNCTIONS
// ============================================
function formatPrice(price) {
    if (price === undefined || price === null) return '--';
    return price.toFixed(2);
}

// All times shown in Indian Standard Time (IST) regardless of the PC's timezone.
const IST_TZ = 'Asia/Kolkata';

function formatTime(date) {
    return date.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: IST_TZ });
}

function formatTimeShort(date) {
    return date.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: IST_TZ });
}

function timeToChartTime(date) {
    // Lightweight charts expects UTC timestamp in seconds
    return Math.floor(date.getTime() / 1000);
}

function generateId() {
    return Math.random().toString(36).substring(2, 9);
}

// ============================================
// DATA GENERATION (Simulated Market Data)
// ============================================
function generateInitialCandles(symbol, timeframe, count = 200) {
    const config = CONFIG.symbols[symbol];
    const candles = [];
    const now = new Date();
    const intervalMs = timeframe * 60 * 1000;

    let price = config.basePrice;
    // Create a trend pattern
    const trendCycles = [
        { start: 0, end: 60, bias: -0.3 },   // downtrend
        { start: 60, end: 100, bias: -0.1 },  // consolidation
        { start: 100, end: 150, bias: 0.3 },  // uptrend
        { start: 150, end: 200, bias: 0.1 }   // slight up
    ];

    for (let i = 0; i < count; i++) {
        const time = new Date(now.getTime() - (count - i) * intervalMs);

        // Find current trend bias
        let bias = 0;
        for (const cycle of trendCycles) {
            if (i >= cycle.start && i < cycle.end) {
                bias = cycle.bias;
                break;
            }
        }

        const vol = config.volatility * price;
        const change = (Math.random() - 0.5 + bias * 0.1) * vol;
        const open = price;
        const close = open + change;
        const high = Math.max(open, close) + Math.random() * vol * 0.5;
        const low = Math.min(open, close) - Math.random() * vol * 0.5;

        candles.push({
            time: timeToChartTime(time),
            open: parseFloat(open.toFixed(2)),
            high: parseFloat(high.toFixed(2)),
            low: parseFloat(low.toFixed(2)),
            close: parseFloat(close.toFixed(2))
        });

        price = close;
    }

    return candles;
}

function generateNextCandle(lastCandle, symbol, timeframe) {
    const config = CONFIG.symbols[symbol];
    const vol = config.volatility * lastCandle.close;
    const change = (Math.random() - 0.5) * vol * 1.5;
    const open = lastCandle.close;
    const close = open + change;
    const high = Math.max(open, close) + Math.random() * vol * 0.4;
    const low = Math.min(open, close) - Math.random() * vol * 0.4;

    return {
        time: lastCandle.time + timeframe * 60,
        open: parseFloat(open.toFixed(2)),
        high: parseFloat(high.toFixed(2)),
        low: parseFloat(low.toFixed(2)),
        close: parseFloat(close.toFixed(2))
    };
}

// ============================================
// EMA CALCULATION
// ============================================
function calculateEMA(candles, period) {
    const emaData = [];
    const multiplier = 2 / (period + 1);

    if (candles.length < period) return emaData;

    // Initial SMA
    let sum = 0;
    for (let i = 0; i < period; i++) {
        sum += candles[i].close;
    }
    let ema = sum / period;
    emaData.push({ time: candles[period - 1].time, value: parseFloat(ema.toFixed(2)) });

    // EMA calculation
    for (let i = period; i < candles.length; i++) {
        ema = (candles[i].close - ema) * multiplier + ema;
        emaData.push({ time: candles[i].time, value: parseFloat(ema.toFixed(2)) });
    }

    return emaData;
}

function calculateAllEMAs(candles) {
    const emas = {};
    for (const period of CONFIG.emaPeriods) {
        emas[period] = calculateEMA(candles, period);
    }
    return emas;
}

function getLatestEMA(period) {
    const data = state.emaData[period];
    if (!data || data.length === 0) return null;
    return data[data.length - 1].value;
}

function getPreviousEMA(period, offset = 1) {
    const data = state.emaData[period];
    if (!data || data.length < offset + 1) return null;
    return data[data.length - 1 - offset].value;
}

// ============================================
// INSIDE BAR (INSIDE CANDLE) DETECTION
// An inside bar = current candle fully inside the previous candle's range:
//   current.high <= prev.high  AND  current.low >= prev.low
// ============================================
function isInsideBar(current, prev) {
    if (!current || !prev) return false;
    return current.high <= prev.high && current.low >= prev.low;
}

// ============================================
// ALTERNATE STRATEGIES (live) — Bollinger / Supertrend / ORB
// (backtest winners; selectable from the toolbar dropdown)
// ============================================
function generateAltSignal(strategy, candles, price, e9, e21, e50) {
    if (strategy === 'bollinger') return sigBollinger(candles, price);
    if (strategy === 'supertrend') return sigSupertrend(candles, price);
    if (strategy === 'orb') return sigORB(candles, price);
    return null;
}

function _mkSignal(type, name, price) {
    return {
        type, name, price,
        sl: type === 'BUY' ? price * 0.999 : price * 1.001,   // nominal (option uses optStopPct)
        strength: 4, time: new Date()
    };
}

function sigBollinger(c, price, period = 20, mult = 2) {
    if (c.length < period + 1) return null;
    const band = (endIdx) => {
        let sum = 0;
        for (let i = endIdx - period + 1; i <= endIdx; i++) sum += c[i].close;
        const m = sum / period;
        let v = 0;
        for (let i = endIdx - period + 1; i <= endIdx; i++) v += (c[i].close - m) ** 2;
        const sd = Math.sqrt(v / period);
        return { up: m + mult * sd, lo: m - mult * sd };
    };
    const last = c.length - 1;
    const cur = band(last), prev = band(last - 1);
    const c1 = c[last].close, c0 = c[last - 1].close;
    if (c0 <= prev.up && c1 > cur.up) return _mkSignal('BUY', 'Bollinger Breakout (Up)', price);
    if (c0 >= prev.lo && c1 < cur.lo) return _mkSignal('SELL', 'Bollinger Breakout (Down)', price);
    return null;
}

function sigSupertrend(c, price, period = 10, mult = 3) {
    const n = c.length;
    if (n < period + 2) return null;
    const tr = new Array(n).fill(0);
    for (let i = 1; i < n; i++) {
        tr[i] = Math.max(c[i].high - c[i].low, Math.abs(c[i].high - c[i - 1].close), Math.abs(c[i].low - c[i - 1].close));
    }
    const atr = new Array(n).fill(null);
    let s = 0; for (let i = 1; i <= period; i++) s += tr[i];
    atr[period] = s / period;
    for (let i = period + 1; i < n; i++) atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period;
    let trend = 1, prevTrend = 1;
    for (let i = period; i < n; i++) {
        const hl2 = (c[i].high + c[i].low) / 2;
        const ub = hl2 + mult * atr[i], lb = hl2 - mult * atr[i];
        prevTrend = trend;
        if (c[i].close > ub) trend = 1;
        else if (c[i].close < lb) trend = -1;
    }
    if (prevTrend === -1 && trend === 1) return _mkSignal('BUY', 'Supertrend Flip (Up)', price);
    if (prevTrend === 1 && trend === -1) return _mkSignal('SELL', 'Supertrend Flip (Down)', price);
    return null;
}

function sigORB(c, price) {
    const n = c.length;
    if (n < 17) return null;
    // group: today's candles (same calendar day as last candle, IST)
    const istDay = (t) => { const d = new Date(t * 1000 + (new Date().getTimezoneOffset() * 60000) + (5.5 * 3600000)); return d.toISOString().slice(0, 10); };
    const today = istDay(c[n - 1].time);
    const dayCandles = c.filter(x => istDay(x.time) === today);
    if (dayCandles.length < 16) return null;
    const opening = dayCandles.slice(0, 15);
    const hi = Math.max(...opening.map(x => x.high));
    const lo = Math.min(...opening.map(x => x.low));
    const close = c[n - 1].close;
    if (state.orbDoneDay === today) return null;
    if (close > hi) { state.orbDoneDay = today; return _mkSignal('BUY', 'ORB Breakout (Up)', price); }
    if (close < lo) { state.orbDoneDay = today; return _mkSignal('SELL', 'ORB Breakdown (Down)', price); }
    return null;
}

// ============================================
// SIGNAL DETECTION ENGINE
// ============================================
function detectSignals() {
    const now = Date.now();
    if (now - state.lastSignalTime < CONFIG.signalCooldownMs) return;

    const candles = state.candleData;
    if (candles.length < 3) return;

    const currentCandle = candles[candles.length - 1];
    const prevCandle = candles[candles.length - 2];
    const price = currentCandle.close;

    const ema9 = getLatestEMA(9);
    const ema21 = getLatestEMA(21);
    const ema50 = getLatestEMA(50);
    const ema100 = getLatestEMA(100);
    const ema200 = getLatestEMA(200);

    const prevEma9 = getPreviousEMA(9);
    const prevEma21 = getPreviousEMA(21);

    if (!ema9 || !ema21 || !ema50 || !prevEma9 || !prevEma21) return;

    // ----- Identify the inside-bar setup -----
    // closeConfirm: use only CLOSED candles — inside bar = candle[-3], its mother
    //   = candle[-4], breakout confirmed by candle[-2]'s CLOSE.
    // otherwise: inside bar = last closed candle [-2], breakout watched live on [-1].
    const cc = CONFIG.insideBarCloseConfirm;
    const inside = cc ? candles[candles.length - 3] : candles[candles.length - 2];
    const mother = cc ? candles[candles.length - 4] : candles[candles.length - 3];
    const breakCandle = cc ? candles[candles.length - 2] : currentCandle;

    const insideBar = !!(inside && mother) && isInsideBar(inside, mother);
    // mother bar must be big enough — skip tiny meaningless consolidations
    const motherBigEnough = !!mother &&
        ((mother.high - mother.low) / price) >= CONFIG.motherBarMinPct;

    let signal = null;

    if (state.strategy && state.strategy !== 'inside_bar') {
        // ===== SELECTED ALT STRATEGY (Bollinger / Supertrend / ORB) =====
        signal = generateAltSignal(state.strategy, candles, price, ema9, ema21, ema50);
    } else if (CONFIG.requireInsideBar) {
        // ===== INSIDE BAR BREAKOUT ENTRY (accurate) =====
        // 1) Inside bar confirmed on closed candles.  2) Mother bar big enough.
        // 3) Breakout in EMA trend direction (close-confirmed).  4) One per setup.
        if (insideBar && motherBigEnough && state.lastInsideBarTime !== inside.time) {
            const bullish = ema9 > ema21 && ema21 > ema50 && price > ema21;
            const bearish = ema9 < ema21 && ema21 < ema50 && price < ema21;
            const buf = Math.max(0.05, inside.high * 0.0001);   // noise buffer

            const brokeUp   = cc ? (breakCandle.close > inside.high) : (breakCandle.high > inside.high + buf);
            const brokeDown = cc ? (breakCandle.close < inside.low)  : (breakCandle.low  < inside.low  - buf);

            if (bullish && brokeUp) {
                signal = {
                    type: 'BUY',
                    name: 'Inside Bar Breakout (Bullish)',
                    reason: `${cc ? 'Closed' : 'Broke'} above inside-bar high ${formatPrice(inside.high)} in uptrend (9>21>50)`,
                    price: price,
                    sl: inside.low - 2,
                    strength: calculateBuyStrength(price, ema9, ema21, ema50, ema100, ema200)
                };
                state.lastInsideBarTime = inside.time;
            } else if (bearish && brokeDown) {
                signal = {
                    type: 'SELL',
                    name: 'Inside Bar Breakout (Bearish)',
                    reason: `${cc ? 'Closed' : 'Broke'} below inside-bar low ${formatPrice(inside.low)} in downtrend (9<21<50)`,
                    price: price,
                    sl: inside.high + 2,
                    strength: calculateSellStrength(price, ema9, ema21, ema50, ema100, ema200)
                };
                state.lastInsideBarTime = inside.time;
            }
        }
    } else {

    // ---- BUY SIGNALS ----

    // 1. Golden Cross: EMA 9 crosses above EMA 21
    if (prevEma9 <= prevEma21 && ema9 > ema21) {
        const strength = calculateBuyStrength(price, ema9, ema21, ema50, ema100, ema200);
        signal = {
            type: 'BUY',
            name: 'EMA 9/21 Golden Cross',
            reason: `EMA 9 (${formatPrice(ema9)}) crossed above EMA 21 (${formatPrice(ema21)})`,
            price: price,
            sl: Math.min(ema21, currentCandle.low) - 2,
            strength: strength,
            time: new Date()
        };
    }

    // 2. Price bounces off EMA 21 in uptrend
    else if (ema9 > ema21 && ema21 > ema50 && prevCandle.low <= ema21 * 1.001 && currentCandle.close > ema9) {
        const strength = calculateBuyStrength(price, ema9, ema21, ema50, ema100, ema200);
        signal = {
            type: 'BUY',
            name: 'EMA 21 Bounce (Uptrend)',
            reason: `Price bounced off EMA 21 support in uptrend`,
            price: price,
            sl: ema21 - (price - ema21) * 0.5,
            strength: Math.min(strength + 1, 5)
        };
    }

    // 3. All EMAs aligned bullish + price above all
    else if (price > ema9 && ema9 > ema21 && ema21 > ema50 && ema50 > (ema100 || 0) && currentCandle.close > currentCandle.open) {
        if (Math.random() > 0.7) { // Reduce frequency of this signal
            signal = {
                type: 'BUY',
                name: 'Full Bullish Alignment',
                reason: `All EMAs aligned: 9 > 21 > 50 > 100, Price above all`,
                price: price,
                sl: ema9 - 3,
                strength: 5
            };
        }
    }

    // ---- SELL SIGNALS ----

    // 1. Death Cross: EMA 9 crosses below EMA 21
    else if (prevEma9 >= prevEma21 && ema9 < ema21) {
        const strength = calculateSellStrength(price, ema9, ema21, ema50, ema100, ema200);
        signal = {
            type: 'SELL',
            name: 'EMA 9/21 Death Cross',
            reason: `EMA 9 (${formatPrice(ema9)}) crossed below EMA 21 (${formatPrice(ema21)})`,
            price: price,
            sl: Math.max(ema21, currentCandle.high) + 2,
            strength: strength
        };
    }

    // 2. Rejection from EMA 21 in downtrend
    else if (ema9 < ema21 && ema21 < ema50 && prevCandle.high >= ema21 * 0.999 && currentCandle.close < ema9) {
        const strength = calculateSellStrength(price, ema9, ema21, ema50, ema100, ema200);
        signal = {
            type: 'SELL',
            name: 'EMA 21 Rejection (Downtrend)',
            reason: `Price rejected from EMA 21 resistance in downtrend`,
            price: price,
            sl: ema21 + (ema21 - price) * 0.5,
            strength: Math.min(strength + 1, 5)
        };
    }

    // 3. All EMAs aligned bearish
    else if (price < ema9 && ema9 < ema21 && ema21 < ema50 && currentCandle.close < currentCandle.open) {
        if (Math.random() > 0.7) {
            signal = {
                type: 'SELL',
                name: 'Full Bearish Alignment',
                reason: `All EMAs aligned bearish: 9 < 21 < 50, Price below all`,
                price: price,
                sl: ema9 + 3,
                strength: 5
            };
        }
    }

    } // end of EMA-only mode (else)

    if (signal) {
        signal.id = generateId();
        signal.time = new Date();
        signal.candleTime = currentCandle.time;

        // Calculate targets
        const risk = Math.abs(signal.price - signal.sl);
        if (signal.type === 'BUY') {
            signal.target1 = parseFloat((signal.price + risk * CONFIG.riskRewardRatio1).toFixed(2));
            signal.target2 = parseFloat((signal.price + risk * CONFIG.riskRewardRatio2).toFixed(2));
        } else {
            signal.target1 = parseFloat((signal.price - risk * CONFIG.riskRewardRatio1).toFixed(2));
            signal.target2 = parseFloat((signal.price - risk * CONFIG.riskRewardRatio2).toFixed(2));
        }

        signal.sl = parseFloat(signal.sl.toFixed(2));
        signal.price = parseFloat(signal.price.toFixed(2));

        addSignal(signal);
        state.lastSignalTime = now;

        // Show signal overlay
        showSignalOverlay(signal);

        // Add marker to chart
        addChartMarker(signal);

        // Alert (sound + browser notification)
        fireAlert(signal);

        // Take the actual trade on the OPTION (BUY->ATM Call, SELL->ATM Put)
        // — only when Auto-Trade is ON.
        if (state.autoTrade) openOptionTrade(signal);
    }

    // Update rule indicators
    updateRuleIndicators(price, ema9, ema21, ema50, ema100, ema200, prevEma9, prevEma21, insideBar);
}

function calculateBuyStrength(price, ema9, ema21, ema50, ema100, ema200) {
    let strength = 0;
    if (ema9 > ema21) strength++;
    if (price > ema9) strength++;
    if (ema21 > ema50) strength++;
    if (ema50 > (ema100 || 0)) strength++;
    if (price > (ema200 || 0)) strength++;
    return Math.min(strength, 5);
}

function calculateSellStrength(price, ema9, ema21, ema50, ema100, ema200) {
    let strength = 0;
    if (ema9 < ema21) strength++;
    if (price < ema9) strength++;
    if (ema21 < ema50) strength++;
    if (ema50 < (ema100 || Infinity)) strength++;
    if (price < (ema200 || Infinity)) strength++;
    return Math.min(strength, 5);
}

// ============================================
// RULE INDICATORS UPDATE
// ============================================
function updateRuleIndicators(price, ema9, ema21, ema50, ema100, ema200, prevEma9, prevEma21, insideBar) {
    // Inside bar (entry trigger)
    setRuleActive('rule-ib', !!insideBar, insideBar && ema9 >= ema21 ? 'buy' : 'sell');

    // Buy rules
    setRuleActive('rule-b1', prevEma9 <= prevEma21 && ema9 > ema21, 'buy');
    setRuleActive('rule-b2', price > ema9 && price > ema21, 'buy');
    setRuleActive('rule-b3', ema9 > ema21 && ema21 > ema50, 'buy');
    setRuleActive('rule-b4', ema9 > ema21 && price >= ema21 * 0.998, 'buy');
    setRuleActive('rule-b5', price > ema50, 'buy');

    // Sell rules
    setRuleActive('rule-s1', prevEma9 >= prevEma21 && ema9 < ema21, 'sell');
    setRuleActive('rule-s2', price < ema9 && price < ema21, 'sell');
    setRuleActive('rule-s3', ema9 < ema21 && ema21 < ema50, 'sell');
    setRuleActive('rule-s4', ema9 < ema21 && price <= ema21 * 1.002, 'sell');
    setRuleActive('rule-s5', price < ema50, 'sell');
}

function setRuleActive(ruleId, isActive, type) {
    const el = document.getElementById(ruleId);
    if (!el) return;

    el.classList.remove('active', 'active-sell');
    const check = el.querySelector('.rule-check');

    if (isActive) {
        el.classList.add(type === 'buy' ? 'active' : 'active-sell');
        check.textContent = '✓';
    } else {
        check.textContent = '○';
    }
}

// ============================================
// SIGNAL MANAGEMENT
// ============================================
function addSignal(signal) {
    state.signals.unshift(signal);
    if (state.signals.length > CONFIG.maxSignals) {
        state.signals.pop();
    }
    renderSignalsList();
    updateSignalCard(signal);
}

function renderSignalsList() {
    const container = document.getElementById('signals-list');
    if (!container) return;

    if (state.signals.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">📡</div>
                <div>Monitoring for signals...</div>
            </div>
        `;
        return;
    }

    container.innerHTML = state.signals.slice(0, 10).map(s => `
        <div class="signal-item ${s.type.toLowerCase()}">
            <span class="signal-badge ${s.type.toLowerCase()}">${s.type}</span>
            <div class="signal-info">
                <span class="signal-name">${s.name}</span>
                <span class="signal-time">${formatTimeShort(s.time)}</span>
            </div>
            <span class="signal-price" style="color: ${s.type === 'BUY' ? 'var(--buy-color)' : 'var(--sell-color)'}">
                ${formatPrice(s.price)}
            </span>
        </div>
    `).join('');
}

function updateSignalCard(signal) {
    const card = document.getElementById('stat-signal');
    const value = document.getElementById('signal-value');
    const dots = document.querySelectorAll('.strength-dot');

    card.classList.remove('buy-signal', 'sell-signal');

    if (signal) {
        card.classList.add(signal.type === 'BUY' ? 'buy-signal' : 'sell-signal');
        value.textContent = signal.type;
        value.style.color = signal.type === 'BUY' ? 'var(--buy-color)' : 'var(--sell-color)';

        dots.forEach((dot, i) => {
            dot.classList.remove('active', 'active-sell');
            if (i < signal.strength) {
                dot.classList.add(signal.type === 'BUY' ? 'active' : 'active-sell');
            }
        });
    }
}

function showSignalOverlay(signal) {
    const overlay = document.getElementById('signal-overlay');
    const popup = overlay.querySelector('.signal-popup');
    const icon = overlay.querySelector('.signal-icon');
    const title = overlay.querySelector('.signal-title');
    const detail = overlay.querySelector('.signal-detail');

    popup.className = `signal-popup ${signal.type === 'BUY' ? 'buy-popup' : 'sell-popup'}`;
    icon.textContent = signal.type === 'BUY' ? '🟢' : '🔴';
    title.textContent = `${signal.type} @ ${formatPrice(signal.price)}`;
    title.style.color = signal.type === 'BUY' ? 'var(--buy-color)' : 'var(--sell-color)';
    detail.textContent = signal.name;

    overlay.style.display = 'block';

    setTimeout(() => {
        overlay.style.display = 'none';
    }, 3000);
}

// ============================================
// ALERTS (sound beep + browser notification)
// ============================================
let _audioCtx = null;
function beep(type) {
    try {
        _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
        const ctx = _audioCtx;
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain); gain.connect(ctx.destination);
        osc.type = 'sine';
        osc.frequency.value = type === 'BUY' ? 880 : 440;   // higher beep for BUY
        gain.gain.setValueAtTime(0.0001, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.4);
        osc.start();
        osc.stop(ctx.currentTime + 0.4);
    } catch (e) { /* audio not allowed yet */ }
}

function fireAlert(signal) {
    // 1) sound
    beep(signal.type);
    // 2) on-screen popup is already shown by showSignalOverlay()
    // 3) browser notification
    try {
        if ('Notification' in window && Notification.permission === 'granted') {
            const side = signal.type === 'BUY' ? 'CALL (CE)' : 'PUT (PE)';
            new Notification(`${signal.type} signal — ${state.symbol}`, {
                body: `${signal.name}\nEntry ${formatPrice(signal.price)} | SL ${formatPrice(signal.sl)} | → ${side}`,
                icon: ''
            });
        }
    } catch (e) { /* ignore */ }
}

function requestAlertPermission() {
    try {
        if ('Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission();
        }
    } catch (e) { /* ignore */ }
}

// ============================================
// OPTION ORDER (Call/Put) — paper by default
// On a BUY signal -> ATM Call (CE); on SELL -> ATM Put (PE)
// ============================================
async function handleOptionOrder(signal) {
    const side = signal.type === 'BUY' ? 'CE' : 'PE';
    try {
        const r = await apiGet(`/api/atm?symbol=${state.symbol}&side=${side}`);
        if (r.status !== 'ok' || !r.instrument_key) return;

        const qty = CONFIG.lotSize[state.symbol] || 0;
        const order = {
            time: new Date(),
            symbol: state.symbol,
            side: side,                       // CE / PE
            signalType: signal.type,          // BUY / SELL
            strike: r.strike,
            expiry: r.expiry,
            instrumentKey: r.instrument_key,
            entry: r.ltp,                     // option premium at signal
            qty: qty,
            mode: CONFIG.LIVE_TRADING ? 'LIVE' : 'PAPER'
        };

        if (CONFIG.LIVE_TRADING) {
            // ⚠️ Real order placement requires a TRADING token (not the read-only
            // analytics token) + an explicit backend /api/order endpoint. Left as
            // a deliberate stop so real money is never spent by accident.
            console.warn('LIVE_TRADING is on but real order placement is not armed.');
        }

        state.paperOrders.unshift(order);
        if (state.paperOrders.length > 50) state.paperOrders.pop();
        renderPaperOrders();
        saveSession();
    } catch (e) {
        console.warn('option order failed:', e);
    }
}

function renderPaperOrders() {
    const box = document.getElementById('option-orders-list');
    if (!box) return;
    if (!state.paperOrders.length) {
        box.innerHTML = `<div class="empty-state"><div class="empty-icon">🎯</div><div>No option orders yet…</div></div>`;
        return;
    }
    box.innerHTML = state.paperOrders.slice(0, 12).map(o => `
        <div class="opt-order ${o.side === 'CE' ? 'buy' : 'sell'}">
            <span class="opt-badge ${o.side === 'CE' ? 'buy' : 'sell'}">${o.side}</span>
            <div class="opt-info">
                <span class="opt-name">${o.symbol} ${o.strike} ${o.side}</span>
                <span class="opt-sub">${o.expiry} · ${formatTimeShort(o.time)} · ${o.mode}</span>
            </div>
            <div class="opt-px">
                <span class="opt-prem">₹${formatPrice(o.entry)}</span>
                <span class="opt-qty">x${o.qty}</span>
            </div>
        </div>
    `).join('');
}

// ============================================
// GAMMA BLAST DETECTION
// Detects an explosive option-premium move (expiry-day) by tracking premium
// velocity + a sharp spot move, using the parameters in CONFIG.gammaBlast.
// ============================================
function pushHist(hist, v, now, maxAgeSec) {
    hist.push({ t: now, v: v });
    const cutoff = now - maxAgeSec * 1000;
    while (hist.length && hist[0].t < cutoff) hist.shift();
}

function pctChangeOver(hist, lookbackSec) {
    if (!hist || hist.length < 2) return 0;
    const last = hist[hist.length - 1];
    const cutoff = last.t - lookbackSec * 1000;
    let base = null;
    for (const s of hist) { if (s.t >= cutoff) { base = s.v; break; } }
    if (base == null || base === 0) return 0;
    return (last.v - base) / base * 100;
}

function inSpikeWindow() {
    const ist = nowIST();
    const mins = ist.getHours() * 60 + ist.getMinutes();
    const toM = (hm) => { const p = hm.split(':'); return (+p[0]) * 60 + (+p[1]); };
    return mins >= toM(CONFIG.spike.startIST) && mins <= toM(CONFIG.spike.endIST);
}

// 3 PM spike catcher: in the window, a sharp index move -> enter ATM option in
// that direction to ride the end-of-day spike.
function detectSpike() {
    if (!state.spikeMode || !LIVE.active || !LIVE.authenticated) return;
    if (!feedLive() || !inSpikeWindow()) return;
    if (state.trades.some(t => t.status === 'ACTIVE')) return;     // one position
    const now = Date.now();
    if (now - state.lastSpikeTime < 60000) return;                 // cooldown
    const c = state.candleData;
    if (c.length < 4) return;

    // index % move over the lookback (in candles)
    const tf = state.timeframe;
    const back = Math.max(1, Math.round(CONFIG.spike.lookbackSec / (tf * 60)) || 2);
    const cur = c[c.length - 1].close;
    const base = c[Math.max(0, c.length - 1 - back)].close;
    if (!base) return;
    const movePct = (cur - base) / base;
    if (Math.abs(movePct) < CONFIG.spike.movePct) return;

    const up = movePct > 0;
    const signal = {
        type: up ? 'BUY' : 'SELL',
        name: '3PM Spike',
        price: cur,
        sl: up ? cur * 0.999 : cur * 1.001,   // nominal (option uses its own SL)
        strength: 5,
        time: new Date(),
        candleTime: c[c.length - 1].time
    };
    state.lastSpikeTime = now;
    addSignal(signal);
    showSignalOverlay(signal);
    addChartMarker(signal);
    fireAlert(signal);
    openOptionTrade(signal);     // BUY ATM Call (up) / Put (down) — ride the spike
}

function inBlastWindow() {
    const g = CONFIG.gammaBlast;
    const ist = nowIST();
    const mins = ist.getHours() * 60 + ist.getMinutes();
    const toM = (hm) => { const p = hm.split(':'); return (+p[0]) * 60 + (+p[1]); };
    return mins >= toM(g.windowStartIST) && mins <= toM(g.windowEndIST);
}

// Max Pain / OI Support / OI Resistance are shown in the header strip ONLY.
// They used to be drawn as price lines on the index chart, but their axis badges
// stacked on top of the EMA labels and buried the price scale. Chart = EMA lines only.
function updateOiLevels(r) {
    if (!state.candleSeries || !state.oiLines) return;
    for (const k of Object.keys(state.oiLines)) {        // clear any lines left from a previous build
        const ex = state.oiLines[k];
        if (ex && ex.line) { try { state.candleSeries.removePriceLine(ex.line); } catch (e) {} }
    }
    state.oiLines = {};
}

// Click a strike in the option chain -> load that strike's CE & PE into the charts
async function selectStrike(strike) {
    const row = state.lastChain && state.lastChain.strikes.find(s => s.strike === strike);
    if (!row) return;
    LIVE.chartCe = { strike, key: row.ce && row.ce.key };
    LIVE.chartPe = { strike, key: row.pe && row.pe.key };
    state.callData = []; state.putData = [];
    if (state.callSeries) state.callSeries.setData([]);
    if (state.putSeries) state.putSeries.setData([]);
    LIVE.lastOptLineTime = 0;
    const loadLeg = async (leg, which, series) => {
        if (!leg || !leg.key) return;
        try {
            const r = await apiGet(`/api/optintraday?key=${encodeURIComponent(leg.key)}&interval=${state.timeframe}`);
            if (r.status === 'ok' && Array.isArray(r.candles)) { state[which] = r.candles; if (series) series.setData(r.candles); }
        } catch (e) { /* ignore */ }
    };
    await Promise.all([loadLeg(LIVE.chartCe, 'callData', state.callSeries), loadLeg(LIVE.chartPe, 'putData', state.putSeries)]);
    fitAllCharts();
    const p = document.getElementById('oc-panel'); if (p) p.style.display = 'none';  // close chain
    console.log('Charting strike', strike);
}

// build/extend an option candle (OHLC) from live premium, aligned to candle grid
function updateOptCandle(arr, series, ltp, candleTime) {
    if (ltp == null || !series || !arr) return;
    const last = arr[arr.length - 1];
    if (!last || candleTime > last.time) {
        arr.push({ time: candleTime, open: ltp, high: ltp, low: ltp, close: ltp });
        if (arr.length > 500) arr.shift();
    } else if (candleTime === last.time) {
        last.close = ltp;
        last.high = Math.max(last.high, ltp);
        last.low = Math.min(last.low, ltp);
    } else {
        return; // stale
    }
    series.update(arr[arr.length - 1]);
}

async function pollOptions() {
    if (!LIVE.authenticated || !feedLive()) return;   // only while market is live

    try {
        const r = await apiGet(`/api/optionwatch?symbol=${state.symbol}&range=${CONFIG.optStrikeRange}`);
        if (r.status !== 'ok' || !r.strikes) return;
        const now = Date.now();
        state.lastChain = r;   // for click-to-chart on a strike

        // PCR + Option Chain (header chip + vertical dropdown)
        if (r.pcr != null) {
            const bias = r.pcr >= 1.2 ? 'BULLISH' : r.pcr <= 0.8 ? 'BEARISH' : 'NEUTRAL';
            const col = r.pcr >= 1.2 ? 'var(--buy-color)' : r.pcr <= 0.8 ? 'var(--sell-color)' : 'var(--text-secondary)';
            const cr = (n) => (n >= 1e7 ? (n / 1e7).toFixed(2) + 'Cr' : (n / 1e5).toFixed(2) + 'L');

            const cv = document.getElementById('pcr-chip-val');
            const cb = document.getElementById('pcr-chip-bias');
            if (cv) { cv.textContent = r.pcr.toFixed(2); cv.style.color = col; }
            if (cb) { cb.textContent = bias; cb.style.color = col; }

            const head = document.getElementById('oc-head');
            if (head) head.innerHTML = `${r.symbol} ${r.expiry} (spot ${r.spot})<br>` +
                `<span style="font-size:10px;">🎯 Max Pain: <b>${r.max_pain ?? '--'}</b> · ` +
                `🟢 Support: <b style="color:var(--buy-color)">${r.oi_support ?? '--'}</b> · ` +
                `🔴 Resist: <b style="color:var(--sell-color)">${r.oi_resistance ?? '--'}</b></span>`;
            updateOiLevels(r);   // draw Max Pain / S/R lines on the index chart
            const body = document.getElementById('oc-body');
            if (body) {
                const fmt = (n) => { n = n || 0; return n >= 1e7 ? (n/1e7).toFixed(1)+'Cr' : n >= 1e5 ? (n/1e5).toFixed(1)+'L' : n >= 1e3 ? (n/1e3).toFixed(1)+'K' : Math.round(n); };
                const pctCell = (leg, itm) => leg ? `<td class="${leg.pct>=0?'up':'down'} ${itm}">${leg.pct>0?'+':''}${leg.pct}%</td>` : `<td>--</td>`;
                const iv = (leg) => (leg && leg.greeks && leg.greeks.iv != null) ? leg.greeks.iv : '--';
                body.innerHTML = r.strikes.map(s => {
                    const atm = s.strike === r.atm_strike;
                    const ceItm = s.strike < r.spot ? 'oc-itm' : '';   // ITM calls below spot
                    const peItm = s.strike > r.spot ? 'oc-itm' : '';   // ITM puts above spot
                    const ce = s.ce, pe = s.pe;
                    const sel = (LIVE.chartCe && s.strike === LIVE.chartCe.strike) ? 'oc-sel' : '';
                    return `<tr class="${atm ? 'oc-atm' : ''} ${sel}" data-strike="${s.strike}" style="cursor:pointer">
                        <td class="${ceItm}">${ce ? fmt(ce.oi) : '--'}</td>
                        <td class="${ceItm}">${ce ? fmt(ce.volume) : '--'}</td>
                        <td class="${ceItm}">${iv(ce)}</td>
                        <td class="oc-ce ${ceItm}">${ce ? ce.ltp : '--'}</td>
                        ${pctCell(ce, ceItm)}
                        <td class="oc-strike">${s.strike}</td>
                        ${pctCell(pe, peItm)}
                        <td class="oc-pe ${peItm}">${pe ? pe.ltp : '--'}</td>
                        <td class="${peItm}">${iv(pe)}</td>
                        <td class="${peItm}">${pe ? fmt(pe.volume) : '--'}</td>
                        <td class="${peItm}">${pe ? fmt(pe.oi) : '--'}</td>
                    </tr>`;
                }).join('');
            }
            const foot = document.getElementById('oc-foot');
            if (foot) {
                foot.innerHTML = `PCR = Put OI ÷ Call OI = ${cr(r.total_pe_oi)} ÷ ${cr(r.total_ce_oi)} = ` +
                    `<span style="color:${col}">${r.pcr.toFixed(2)} (${bias})</span>`;
            }
        }

        // 1) latest premium per strike+side  (for trade management)
        LIVE.optPrem = {};
        for (const s of r.strikes) {
            if (s.ce) LIVE.optPrem[`${s.strike}-CE`] = s.ce.ltp;
            if (s.pe) LIVE.optPrem[`${s.strike}-PE`] = s.pe.ltp;
        }

        // 2) current ATM quote (for NEW entries)
        const atmRow = r.strikes.find(s => s.strike === r.atm_strike) || r.strikes[Math.floor(r.strikes.length / 2)];
        if (atmRow) {
            LIVE.optQuote.ce = { strike: atmRow.strike, key: atmRow.ce && atmRow.ce.key, ltp: atmRow.ce && atmRow.ce.ltp };
            LIVE.optQuote.pe = { strike: atmRow.strike, key: atmRow.pe && atmRow.pe.key, ltp: atmRow.pe && atmRow.pe.ltp };
        }
        if (!LIVE.chartCe && atmRow) LIVE.chartCe = { strike: atmRow.strike, key: atmRow.ce && atmRow.ce.key };
        if (!LIVE.chartPe && atmRow) LIVE.chartPe = { strike: atmRow.strike, key: atmRow.pe && atmRow.pe.key };

        // live greeks on the Call/Put chart tags — for the SELECTED (charted) strike
        {
            const ceRow = r.strikes.find(s => LIVE.chartCe && s.strike === LIVE.chartCe.strike) || atmRow;
            const peRow = r.strikes.find(s => LIVE.chartPe && s.strike === LIVE.chartPe.strike) || atmRow;
            const g = (leg) => (leg && leg.greeks) ? leg.greeks : {};
            const ct = document.getElementById('tag-call');
            const pt = document.getElementById('tag-put');
            const lot = CONFIG.lotSize[state.symbol] || 1;
            const lotCost = (ltp) => '₹' + Math.round(ltp * lot).toLocaleString('en-IN');
            if (ct && ceRow && ceRow.ce) { const gc = g(ceRow.ce); ct.textContent = `🟢 ${ceRow.strike}CE  ₹${ceRow.ce.ltp} ×${lot}=${lotCost(ceRow.ce.ltp)}  Δ${gc.delta}  IV${gc.iv}%`; }
            if (pt && peRow && peRow.pe) { const gp = g(peRow.pe); pt.textContent = `🔴 ${peRow.strike}PE  ₹${peRow.pe.ltp} ×${lot}=${lotCost(peRow.pe.ltp)}  Δ${gp.delta}  IV${gp.iv}%`; }
        }

        // 3) extend the CALL/PUT candle charts from their PINNED strike (continuous with history)
        const candleTime = state.candleData.length
            ? state.candleData[state.candleData.length - 1].time
            : Math.floor(now / 1000);
        const ceLtp = LIVE.chartCe && LIVE.optPrem[`${LIVE.chartCe.strike}-CE`];
        const peLtp = LIVE.chartPe && LIVE.optPrem[`${LIVE.chartPe.strike}-PE`];
        updateOptCandle(state.callData, state.callSeries, ceLtp, candleTime);
        updateOptCandle(state.putData, state.putSeries, peLtp, candleTime);
        // keep signal arrows in sync on all 3 charts
        setAllMarkers();

        // 3) update active option trades (trailing SL on premium)
        resolveOptionTrades();

        // 4) gamma blast (premium velocity) — only in the blast window
        if (CONFIG.gammaBlast.enabled && inBlastWindow()) {
            pushHist(state.gamma.spotHist, r.spot, now, 360);
            for (const s of r.strikes) {
                for (const sd of ['ce', 'pe']) {
                    const leg = s[sd];
                    if (leg && leg.key) {
                        if (!state.gamma.premHist[leg.key]) state.gamma.premHist[leg.key] = [];
                        pushHist(state.gamma.premHist[leg.key], leg.ltp, now, 360);
                    }
                }
            }
            detectGammaBlast(r);
        }
    } catch (e) { /* ignore poll errors */ }
}

function detectGammaBlast(data) {
    const g = CONFIG.gammaBlast;
    if (g.onlyExpiryDay && !data.is_expiry_day) return;
    const now = Date.now();
    if (now - state.gamma.lastBlastTime < g.cooldownSec * 1000) return;

    // 1) sharp spot move
    const spotMove = pctChangeOver(state.gamma.spotHist, g.spotLookbackSec);  // signed %
    if (Math.abs(spotMove) / 100 < g.spotMovePct) return;

    // up move -> calls blast; down move -> puts blast
    const side = spotMove > 0 ? 'ce' : 'pe';

    for (const s of data.strikes) {
        const leg = s[side];
        if (!leg || !leg.key) continue;
        const prem = leg.ltp;
        if (prem < g.minPremium || prem > g.maxPremium) continue;        // cheap-option filter

        const surge = pctChangeOver(state.gamma.premHist[leg.key], g.premiumLookbackSec);
        if (surge < g.premiumSurgePct) continue;                         // premium not exploding

        // optional OI confirmation
        if (g.oiChangePct > 0 && leg.prev_oi) {
            const oiChg = (leg.oi - leg.prev_oi) / leg.prev_oi * 100;
            if (Math.abs(oiChg) < g.oiChangePct) { /* soft check, don't block */ }
        }

        fireGammaBlast({
            symbol: data.symbol, strike: s.strike, side: side.toUpperCase(),
            key: leg.key, prem: prem, surge: surge, spotMove: spotMove, expiry: data.expiry
        });
        state.gamma.lastBlastTime = now;
        break;   // one blast per cooldown
    }
}

function fireGammaBlast(b) {
    // loud triple beep
    beep('BUY'); setTimeout(() => beep('BUY'), 150); setTimeout(() => beep('BUY'), 300);
    try {
        if ('Notification' in window && Notification.permission === 'granted') {
            new Notification(`🚀 GAMMA BLAST — ${b.symbol}`, {
                body: `${b.strike} ${b.side} +${b.surge.toFixed(0)}% → ₹${formatPrice(b.prem)} | spot ${b.spotMove > 0 ? '+' : ''}${b.spotMove.toFixed(2)}%`
            });
        }
    } catch (e) { /* ignore */ }

    state.paperOrders.unshift({
        time: new Date(), symbol: b.symbol, side: b.side, signalType: 'BLAST',
        strike: b.strike, expiry: b.expiry, instrumentKey: b.key,
        entry: b.prem, qty: CONFIG.lotSize[b.symbol] || 0, mode: '🚀 BLAST'
    });
    if (state.paperOrders.length > 50) state.paperOrders.pop();
    renderPaperOrders();
    saveSession();
    console.log('%c🚀 GAMMA BLAST', 'background:#ffd60a;color:#000;font-weight:bold;padding:2px 8px;border-radius:4px;', b);
}

// ============================================
// OPTION TRADE MANAGEMENT
// Trades are taken on the OPTION (BUY signal -> ATM Call, SELL -> ATM Put).
// Entry = option premium; exit by TRAILING stop on the premium; P&L in Rs.
// ============================================
async function openOptionTrade(signal) {
    // ---- discipline guards (prevent spam / over-trading) ----
    // 1) one position at a time
    if (CONFIG.onePositionAtATime && state.trades.some(t => t.status === 'ACTIVE')) return;
    // 2) daily trade cap
    const todayStr = new Date().toDateString();
    const todayCount = state.trades.filter(t => t.time && new Date(t.time).toDateString() === todayStr).length;
    if (todayCount >= CONFIG.maxTradesPerDay) return;
    // 3) KILL-SWITCH: stop trading if today's loss/profit limit is hit
    const pnlToday = todayRealizedPnlRs();
    if (CONFIG.maxDailyLossRs > 0 && pnlToday <= -CONFIG.maxDailyLossRs) { tripKillSwitch('loss', pnlToday); return; }
    if (CONFIG.maxDailyProfitRs > 0 && pnlToday >= CONFIG.maxDailyProfitRs) { tripKillSwitch('profit', pnlToday); return; }

    const side = signal.type === 'BUY' ? 'CE' : 'PE';

    // 4) Option momentum — only buy an option that is itself rising (not decaying)
    if (CONFIG.requireOptionMomentum && !optionRising(side)) return;

    // get the ATM option (prefer the live quote; else fetch fresh)
    let opt = LIVE.optQuote[side.toLowerCase()];
    if (!opt || !opt.key || !opt.ltp) {
        try {
            const r = await apiGet(`/api/atm?symbol=${state.symbol}&side=${side}`);
            if (r.status === 'ok' && r.instrument_key) {
                opt = { strike: r.strike, key: r.instrument_key, ltp: r.ltp, expiry: r.expiry };
            }
        } catch (e) { /* ignore */ }
    }
    if (!opt || !opt.ltp || opt.ltp <= 0) return;   // no tradable premium

    const entry = parseFloat(opt.ltp.toFixed(2));
    // POINT stop wins when configured; % stop is the fallback. Never let the stop go <= 0.
    const rawSl = CONFIG.optStopPts > 0
        ? entry - CONFIG.optStopPts
        : entry * (1 - CONFIG.optStopPct / 100);
    const fixedSl = parseFloat(Math.max(0.05, rawSl).toFixed(2));

    state.tradeCounter++;
    const trade = {
        id: state.tradeCounter,
        time: signal.time || new Date(),
        symbol: state.symbol,
        timeframe: state.timeframe,
        type: signal.type,             // BUY/SELL (signal direction)
        side: side,                    // CE / PE (what we actually buy)
        strike: opt.strike,
        instrumentKey: opt.key,
        expiry: opt.expiry || (LIVE.optQuote.expiry || ''),
        entry: entry,                  // option premium at entry
        initialSl: fixedSl,            // FIXED stop loss (does not trail down)
        sl: fixedSl,                   // current effective stop (fixed, or trailing-profit once active)
        risk: parseFloat((entry - fixedSl).toFixed(2)),
        peak: entry,                   // best premium reached
        trailActive: false,           // becomes true once profit-trailing kicks in
        exit: null,
        exitTime: null,
        qty: CONFIG.lotSize[state.symbol] || 0,
        status: 'ACTIVE',
        pnl: 0,                        // premium points
        reason: signal.name
    };

    state.trades.unshift(trade);
    if (state.trades.length > CONFIG.maxTrades) state.trades.pop();
    renderTradeLog();
    saveSession();

    // place a REAL broker BUY if LIVE is armed
    if (state.liveArmed && trade.qty > 0) {
        trade.live = true;
        const r = await placeRealOrder('BUY', trade.instrumentKey, trade.qty);
        trade.orderId = (r && r.result && r.result.data && r.result.data.order_id) || '';
        if (r && r.status !== 'ok') trade.orderNote = 'BUY order failed';
        renderTradeLog();
        saveSession();
    }
}

// ---- risk control helpers ----
function todayRealizedPnlRs() {
    const today = new Date().toDateString();
    let rs = 0;
    for (const t of state.trades) {
        if (t.status === 'ACTIVE') continue;
        if (!t.time || new Date(t.time).toDateString() !== today) continue;
        rs += (t.pnl || 0) * (CONFIG.lotSize[t.symbol] || 1) - (t.charges || 0);
    }
    return rs;
}

function tripKillSwitch(kind, pnl) {
    if (state._killSwitch) return;          // once per day
    state._killSwitch = true;
    state.autoTrade = false;
    syncAutoTradeUI();                      // turns BOTH auto-trade controls off
    beep('SELL'); setTimeout(() => beep('SELL'), 200);
    try {
        if ('Notification' in window && Notification.permission === 'granted')
            new Notification('🛑 Auto-Trade STOPPED', { body: `Daily ${kind} limit hit (₹${Math.round(pnl)}). Auto-trade turned OFF for safety.` });
    } catch (e) {}
    alert(`🛑 Daily ${kind} limit hit (₹${Math.round(pnl)}).\nAuto-Trade has been turned OFF for the day.`);
}

// is the option we're about to buy actually rising? (avoid buying a falling/decaying option)
function optionRising(side) {
    const arr = side === 'CE' ? state.callData : state.putData;
    if (!arr || arr.length < 3) return true;           // not enough data -> don't block
    const last = arr[arr.length - 1].close;
    const prev = arr[arr.length - 3].close;            // ~2 candles ago
    return last >= prev;                               // premium not falling
}

// ============================================
// REAL ORDERS (broker) — only when LIVE is armed
// ============================================
const MAX_REAL_ORDERS_PER_DAY = 50;   // safety cap

async function placeRealOrder(transaction_type, instrumentKey, qty) {
    if (!state.liveArmed) return { status: 'skipped' };
    if (state.realOrdersToday >= MAX_REAL_ORDERS_PER_DAY) {
        console.warn('Real-order daily cap reached');
        return { status: 'capped' };
    }
    try {
        const res = await fetch('/api/order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ instrument_key: instrumentKey, transaction_type, quantity: qty, product: 'I', order_type: 'MARKET' })
        });
        const r = await res.json();
        state.realOrdersToday++;
        if (r.status !== 'ok') {
            console.error('Order failed', r);
            beep('SELL');
        }
        return r;
    } catch (e) {
        console.error('Order error', e);
        return { status: 'error', message: String(e) };
    }
}

async function toggleLiveArm() {
    const btn = document.getElementById('btn-livearm');
    if (state.liveArmed) {
        state.liveArmed = false;
    } else {
        // check trading token
        let hasToken = false;
        try { const s = await apiGet('/api/trading-status'); hasToken = s.trading_token; } catch (e) {}
        if (!hasToken) {
            const tok = window.prompt('Paste your Upstox TRADING access token (Algo-Trading app → Access Token, NOT the Analytics one):');
            if (!tok) return;
            try {
                await fetch('/api/trading-token', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: tok.trim() }) });
            } catch (e) { alert('Could not save token'); return; }
        }
        const ok = window.confirm('⚠️ ARM LIVE TRADING?\n\nReal orders will be placed with REAL MONEY on every signal (and on Exit). A wrong signal = real loss.\n\nProceed?');
        if (!ok) return;
        state.liveArmed = true;
    }
    if (btn) {
        btn.classList.toggle('active', state.liveArmed);
        btn.innerHTML = btn.innerHTML.replace(/LIVE: (ON|OFF)/, 'LIVE: ' + (state.liveArmed ? 'ON' : 'OFF'));
        btn.style.background = state.liveArmed ? 'var(--sell-color)' : '';
        btn.style.color = state.liveArmed ? '#fff' : 'var(--sell-color)';
    }
}

// Manually close an active trade at the current premium (LTP)
function exitTrade(id) {
    const t = state.trades.find(x => x.id === id);
    if (!t || t.status !== 'ACTIVE') return;
    const ltp = LIVE.optPrem[`${t.strike}-${t.side}`];
    const px = (ltp != null) ? ltp : t.entry;     // fallback to entry if no live price
    t.status = 'MANUAL EXIT';
    t.exit = parseFloat(px.toFixed(2));
    t.exitTime = new Date();
    t.pnl = parseFloat((t.exit - t.entry).toFixed(2));   // long premium
    t.charges = CONFIG.chargesPerTrade;
    // close the REAL position if this was a live order
    if (t.live && state.liveArmed) placeRealOrder('SELL', t.instrumentKey, t.qty);
    renderTradeLog();
    saveSession();
}

// FIXED stop loss + TRAILING PROFIT (lock gains once in profit).
// Long premium for both CE & PE.
function resolveOptionTrades() {
    let changed = false;
    const actPct = CONFIG.trailProfitActivatePct / 100;
    const gapPct = CONFIG.trailProfitGapPct / 100;

    state.trades.forEach(trade => {
        if (trade.status !== 'ACTIVE') return;
        const prem = LIVE.optPrem[`${trade.strike}-${trade.side}`];
        if (prem == null) return;                       // no fresh premium this tick

        if (prem > trade.peak) trade.peak = prem;       // new high premium

        // HARD TAKE-PROFIT — POINT target wins when configured, % is the fallback.
        const tgt = CONFIG.optTakeProfitPts > 0
            ? trade.entry + CONFIG.optTakeProfitPts
            : (CONFIG.optTakeProfitPct > 0 ? trade.entry * (1 + CONFIG.optTakeProfitPct / 100) : Infinity);
        if (prem >= tgt) {
            trade.status = CONFIG.optTakeProfitPts > 0
                ? 'TP +' + CONFIG.optTakeProfitPts + 'pt'
                : 'TP ' + CONFIG.optTakeProfitPct + '%';
            trade.exit = parseFloat(prem.toFixed(2));
            trade.exitTime = new Date();
            trade.pnl = parseFloat((trade.exit - trade.entry).toFixed(2));
            trade.charges = CONFIG.chargesPerTrade;
            if (trade.live && state.liveArmed) placeRealOrder('SELL', trade.instrumentKey, trade.qty);
            changed = true;
            return;                                     // done — trade closed at target
        }

        // Trailing PROFIT: once premium is up >= activate%, lock profit by trailing
        // a stop gap% below the peak. The stop only ratchets UP, never below fixed SL.
        if (trade.peak >= trade.entry * (1 + actPct)) {
            trade.trailActive = true;
            const lock = parseFloat((trade.peak * (1 - gapPct)).toFixed(2));
            if (lock > trade.sl) trade.sl = lock;       // move stop up to lock profit
        }
        // (if not yet activated, sl stays at the FIXED initial SL — no trailing down)

        if (prem <= trade.sl) {                         // stop hit -> exit
            trade.status = trade.trailActive ? 'PROFIT TRAIL' : 'SL HIT';
            trade.exit = parseFloat(prem.toFixed(2));
            trade.exitTime = new Date();
            trade.pnl = parseFloat((trade.exit - trade.entry).toFixed(2));  // long premium
            trade.charges = CONFIG.chargesPerTrade;
            if (trade.live && state.liveArmed) placeRealOrder('SELL', trade.instrumentKey, trade.qty);
            changed = true;
        }
    });
    renderTradeLog();
    if (changed) saveSession();
}

// Live Open Positions — a separate, glanceable table of ACTIVE option trades only,
// with target (+13pt), stop (−8pt), live LTP, live P&L and a progress bar.
function renderPositions() {
    const tbody = document.getElementById('positions-body');
    if (!tbody) return;
    const act = state.trades.filter(t => t.status === 'ACTIVE');
    const cnt = document.getElementById('pos-count');
    const netEl = document.getElementById('pos-net');
    if (cnt) cnt.textContent = act.length ? `(${act.length})` : '';
    if (!act.length) {
        tbody.innerHTML = `<tr><td colspan="11" style="text-align:center;padding:16px;color:var(--text-muted);">No open positions — waiting for signals…</td></tr>`;
        if (netEl) netEl.textContent = '';
        return;
    }
    let net = 0;
    tbody.innerHTML = act.map(t => {
        const lot = CONFIG.lotSize[t.symbol] || 1;
        const ltp = LIVE.optPrem[`${t.strike}-${t.side}`];
        const tgt = CONFIG.optTakeProfitPts > 0 ? t.entry + CONFIG.optTakeProfitPts
                    : t.entry * (1 + CONFIG.optTakeProfitPct / 100);
        const pnlPts = (ltp != null) ? ltp - t.entry : 0;
        const pnlRs = pnlPts * lot;
        net += pnlRs;
        const cls = pnlPts > 0 ? 'profit' : pnlPts < 0 ? 'loss' : '';
        const lo = t.initialSl ?? t.sl, span = (tgt - lo) || 1;
        const pct = Math.max(0, Math.min(100, ((ltp ?? t.entry) - lo) / span * 100));
        const sideColor = t.side === 'CE' ? 'var(--buy-color)' : 'var(--sell-color)';
        return `<tr>
            <td>${t.id}</td>
            <td>${formatTimeShort(t.time)}</td>
            <td style="font-weight:700;color:${sideColor};">${t.type} ${t.strike}${t.side}</td>
            <td>₹${formatPrice(t.entry)}</td>
            <td style="color:var(--buy-color);">₹${formatPrice(tgt)}</td>
            <td style="color:var(--sell-color);">₹${formatPrice(t.initialSl)}</td>
            <td>${ltp != null ? '₹' + formatPrice(ltp) : '--'}</td>
            <td style="color:var(--buy-color);">₹${formatPrice(t.peak)}</td>
            <td class="${cls}" style="font-weight:700;">${pnlRs > 0 ? '+' : ''}${pnlRs.toFixed(0)}</td>
            <td><div style="background:rgba(255,255,255,.08);border-radius:4px;height:6px;width:70px;overflow:hidden;">
                <div style="height:100%;width:${pct}%;background:${pnlPts >= 0 ? 'var(--buy-color)' : 'var(--sell-color)'};transition:width .3s;"></div></div></td>
            <td><button class="exit-btn" data-id="${t.id}">Exit</button></td>
        </tr>`;
    }).join('');
    if (netEl) {
        netEl.textContent = (net >= 0 ? '+' : '') + net.toFixed(0) + ' ₹';
        netEl.style.color = net > 0 ? 'var(--buy-color)' : net < 0 ? 'var(--sell-color)' : '';
    }
}

function renderTradeLog(filter = 'all') {
    const tbody = document.getElementById('trade-log-body');
    if (!tbody) return;

    updatePnLSummary();   // summary always reflects ALL trades
    renderTradeHistory(); // full-details table (all trades)
    renderPositions();    // live open-positions table (active only)

    let trades = state.trades;

    if (filter === 'buy') trades = trades.filter(t => t.type === 'BUY');
    else if (filter === 'sell') trades = trades.filter(t => t.type === 'SELL');
    else if (filter === 'active') trades = trades.filter(t => t.status === 'ACTIVE');

    if (trades.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="11" style="text-align:center; padding: 20px; color: var(--text-muted);">
                    📊 No trades yet. Waiting for signals...
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = trades.map(t => {
        const rowClass = t.type === 'BUY' ? 'buy-row' : 'sell-row';
        const active = t.status === 'ACTIVE';
        const lot = CONFIG.lotSize[t.symbol] || 1;
        // live unrealised P&L for active trades, realised for closed
        const ltp = active ? LIVE.optPrem[`${t.strike}-${t.side}`] : t.exit;
        const pnlPts = active ? (ltp != null ? ltp - t.entry : 0) : t.pnl;
        const pnlRs = pnlPts * lot - (active ? 0 : (t.charges || 0));   // closed trades net of charges
        const statusClass = active ? 'active-trade' : (t.pnl >= 0 ? 'tp-hit' : 'sl-hit');
        const pnlClass = pnlPts > 0 ? 'profit' : pnlPts < 0 ? 'loss' : '';
        const trailColor = active ? 'var(--warn-color)' : 'var(--text-muted)';
        const optLabel = t.side ? `${t.strike}${t.side}` : t.type;
        const sideColor = t.side === 'CE' ? 'var(--buy-color)' : 'var(--sell-color)';
        return `
            <tr class="${rowClass}">
                <td>${t.id}</td>
                <td>${formatTimeShort(t.time)}</td>
                <td style="font-weight:700;color:${sideColor};">${t.type} ${optLabel}</td>
                <td>₹${formatPrice(t.entry)}</td>
                <td style="color: var(--sell-color);">₹${formatPrice(t.initialSl)}</td>
                <td style="color: ${trailColor}; font-weight:600;">₹${formatPrice(t.sl)}</td>
                <td style="color: var(--buy-color);">₹${formatPrice(t.peak)}</td>
                <td><span class="trade-status ${statusClass}">${t.status}</span></td>
                <td class="${pnlClass}" style="font-weight:700;">${pnlRs > 0 ? '+' : ''}${pnlRs.toFixed(0)}</td>
                <td style="color: var(--text-secondary); font-family: var(--font-primary);">${t.reason}</td>
                <td>${active ? `<button class="exit-btn" data-id="${t.id}">Exit</button>` : '--'}</td>
            </tr>
        `;
    }).join('');
}

// ============================================
// TRADE HISTORY — FULL DETAILS TABLE
// ============================================
function renderTradeHistory() {
    const tbody = document.getElementById('trade-history-body');
    if (!tbody) return;
    const countEl = document.getElementById('history-count');
    if (countEl) countEl.textContent = `${state.trades.length} trades`;

    if (!state.trades.length) {
        tbody.innerHTML = `<tr><td colspan="18" style="text-align:center;padding:18px;color:var(--text-muted);">No trades yet.</td></tr>`;
        return;
    }

    tbody.innerHTML = state.trades.map(t => {
        const lot = CONFIG.lotSize[t.symbol] || 1;
        const active = t.status === 'ACTIVE';
        // live premium for active trades; exit price for closed
        const ltp = active ? LIVE.optPrem[`${t.strike}-${t.side}`] : t.exit;
        // live (unrealised) P&L for active trades, realised for closed
        const pnlPts = active ? (ltp != null ? ltp - t.entry : null) : t.pnl;
        const pnlRs = pnlPts != null ? pnlPts * lot - (active ? 0 : (t.charges || 0)) : null;
        const pnlClass = pnlPts > 0 ? 'profit' : pnlPts < 0 ? 'loss' : '';
        const statusClass = active ? 'active-trade' : (t.pnl >= 0 ? 'tp-hit' : 'sl-hit');
        return `
            <tr class="${t.type === 'BUY' ? 'buy-row' : 'sell-row'}">
                <td>${t.id}</td>
                <td>${t.symbol || '--'}${t.strike ? ' ' + t.strike + (t.side || '') : ''}</td>
                <td>${t.timeframe ? t.timeframe + 'm' : '--'}</td>
                <td style="font-weight:700;color:${t.type === 'BUY' ? 'var(--buy-color)' : 'var(--sell-color)'};">${t.type}</td>
                <td>${formatTimeShort(t.time)}</td>
                <td>₹${formatPrice(t.entry)}</td>
                <td style="color:var(--sell-color);">₹${formatPrice(t.initialSl)}</td>
                <td>${t.risk != null ? formatPrice(t.risk) : '--'}</td>
                <td style="color:var(--warn-color);">₹${formatPrice(t.sl)}</td>
                <td style="color:var(--buy-color);">₹${formatPrice(t.peak)}</td>
                <td style="font-weight:700;color:var(--text-primary);">${ltp != null ? '₹' + formatPrice(ltp) + ' <span style="color:var(--text-muted);font-weight:400;">(₹' + Math.round(ltp * lot).toLocaleString('en-IN') + ')</span>' : '--'}</td>
                <td>${t.exit != null ? '₹' + formatPrice(t.exit) : '--'}</td>
                <td>${t.exitTime ? formatTimeShort(t.exitTime) : '--'}</td>
                <td><span class="trade-status ${statusClass}">${t.status}</span></td>
                <td class="${pnlClass}">${pnlPts != null ? (pnlPts > 0 ? '+' : '') + formatPrice(pnlPts) : '--'}</td>
                <td class="${pnlClass}" style="font-weight:700;">${pnlRs != null ? (pnlRs > 0 ? '+' : '') + pnlRs.toFixed(0) : '--'}</td>
                <td style="color:var(--text-secondary);font-family:var(--font-primary);">${t.reason}</td>
                <td>${active ? `<button class="exit-btn" data-id="${t.id}">Exit</button>` : '--'}</td>
            </tr>
        `;
    }).join('');
}

function exportTradeHistoryCSV() {
    if (!state.trades.length) return;
    const cols = ['id', 'symbol', 'timeframe', 'type', 'entry', 'initialSl', 'risk', 'sl', 'peak', 'exit', 'status', 'pnl', 'reason'];
    const head = ['ID', 'Symbol', 'TF', 'Side', 'EntryTime', 'Entry', 'InitSL', 'Risk', 'TrailSL', 'Peak', 'Exit', 'ExitTime', 'Status', 'PnL_pts', 'PnL_Rs', 'Signal'];
    const rows = state.trades.map(t => {
        const lot = CONFIG.lotSize[t.symbol] || 1;
        return [t.id, t.symbol, t.timeframe, t.type,
            t.time ? formatTimeShort(t.time) : '', t.entry, t.initialSl, t.risk, t.sl, t.peak,
            t.exit != null ? t.exit : '', t.exitTime ? formatTimeShort(t.exitTime) : '',
            t.status, t.pnl, ((t.pnl || 0) * lot - (t.charges || 0)).toFixed(0), `"${(t.reason || '').replace(/"/g, '""')}"`].join(',');
    });
    const csv = head.join(',') + '\n' + rows.join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `trade_history_${Date.now()}.csv`;
    a.click();
}

// ============================================
// P&L SUMMARY
// ============================================
function updatePnLSummary() {
    const trades = state.trades;
    const closed = trades.filter(t => t.status !== 'ACTIVE');

    let wins = 0, losses = 0, totalProfit = 0, totalLoss = 0, buyPnl = 0, sellPnl = 0;
    for (const t of closed) {
        const rs = (t.pnl || 0) * (CONFIG.lotSize[t.symbol] || 1) - (t.charges || 0);   // rupee P&L (net of charges)
        if (rs > 0) { wins++; totalProfit += rs; }
        else if (rs < 0) { losses++; totalLoss += rs; }
        if (t.type === 'BUY') buyPnl += rs; else sellPnl += rs;
    }
    const net = totalProfit + totalLoss;
    const decided = wins + losses;
    const winRate = decided > 0 ? (wins / decided * 100) : 0;

    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set('pnl-total-trades', trades.length);
    set('pnl-wins', wins);
    set('pnl-losses', losses);
    set('pnl-winrate', winRate.toFixed(1) + '%');
    set('pnl-profit', '+' + totalProfit.toFixed(2));
    set('pnl-loss', totalLoss.toFixed(2));

    const setSigned = (id, val) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = (val >= 0 ? '+' : '') + val.toFixed(2);
        el.className = 'pnl-val ' + (val > 0 ? 'profit' : val < 0 ? 'loss' : '');
    };
    setSigned('pnl-buy', buyPnl);
    setSigned('pnl-sell', sellPnl);
    setSigned('pnl-net', net);
}

// ============================================
// CHART INITIALIZATION
// ============================================
function initChart() {
    const elIndex = document.getElementById('chart-index');
    const elCall = document.getElementById('chart-call');
    const elPut = document.getElementById('chart-put');
    if (!elIndex || !elCall || !elPut) return;

    [state.chartIndex, state.chartCall, state.chartPut].forEach(c => { try { c && c.remove(); } catch (e) {} });
    state.emaLines = {};
    state.callData = state.callData || [];
    state.putData = state.putData || [];

    const baseOpts = (el) => ({
        width: el.clientWidth, height: el.clientHeight,
        layout: { background: { type: 'solid', color: '#0a0e17' }, textColor: '#8892a6', fontFamily: "'Inter', sans-serif", fontSize: 11 },
        grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.03)' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: 'rgba(255,255,255,0.06)', scaleMargins: { top: 0.15, bottom: 0.12 }, autoScale: true },
        timeScale: { borderColor: 'rgba(255,255,255,0.06)', timeVisible: true, secondsVisible: false, rightOffset: 5, barSpacing: 8 },
        handleScroll: { vertTouchDrag: false },
    });

    state.chartIndex = LightweightCharts.createChart(elIndex, baseOpts(elIndex));
    state.chartCall = LightweightCharts.createChart(elCall, baseOpts(elCall));
    state.chartPut = LightweightCharts.createChart(elPut, baseOpts(elPut));

    const cOpts = (up, down) => ({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
    state.candleSeries = state.chartIndex.addCandlestickSeries(cOpts(CONFIG.candleUp, CONFIG.candleDown));
    state.callSeries = state.chartCall.addCandlestickSeries(cOpts('#00f5d4', '#0a6b5e'));
    state.putSeries = state.chartPut.addCandlestickSeries(cOpts('#f72585', '#7d1342'));

    // EMA lines on the INDEX chart only — lines, but NO axis badges. Four EMA labels on the
    // price scale (plus Max Pain / OI levels) buried the actual price; values live in the
    // levels panel instead. Only the last traded price keeps its label on the axis.
    for (const period of CONFIG.emaPeriods) {
        state.emaLines[period] = state.chartIndex.addLineSeries({
            color: CONFIG.emaColors[period], lineWidth: CONFIG.emaLineWidths[period],
            priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: `EMA ${period}`
        });
    }

    // keep all 3 charts time-synced (scroll/zoom together)
    syncTimeScales([state.chartIndex, state.chartCall, state.chartPut]);

    const pairs = [[elIndex, state.chartIndex], [elCall, state.chartCall], [elPut, state.chartPut]];
    const ro = new ResizeObserver(() => { for (const [el, ch] of pairs) ch.applyOptions({ width: el.clientWidth, height: el.clientHeight }); });
    pairs.forEach(([el]) => ro.observe(el));
}

function syncTimeScales(charts) {
    let syncing = false;
    charts.forEach(src => {
        src.timeScale().subscribeVisibleLogicalRangeChange(range => {
            if (syncing || !range) return;
            if (range.to - range.from < 1) return;   // ignore degenerate ranges
            syncing = true;
            charts.forEach(dst => { if (dst !== src) { try { dst.timeScale().setVisibleLogicalRange(range); } catch (e) {} } });
            syncing = false;
        });
    });
}

function setAllMarkers() {
    const mk = (state.showSignals && state.markers.length) ? state.markers.slice().sort((a, b) => a.time - b.time) : [];
    [state.candleSeries, state.callSeries, state.putSeries].forEach(s => { if (s) s.setMarkers(mk); });
}

function updateChart() {
    if (!state.candleSeries) return;

    state.candleSeries.setData(state.candleData);

    for (const period of CONFIG.emaPeriods) {
        if (state.emaData[period] && state.emaLines[period]) {
            state.emaLines[period].applyOptions({ visible: state.showEMAs });
            if (state.showEMAs) state.emaLines[period].setData(state.emaData[period]);
        }
    }

    if (state.callSeries) state.callSeries.setData(state.callData || []);
    if (state.putSeries) state.putSeries.setData(state.putData || []);

    setAllMarkers();

    // auto-adjust: keep the latest candles in view (price scales auto-scale themselves)
    if (state.autoFit && state.chartIndex) {
        try { state.chartIndex.timeScale().scrollToRealTime(); } catch (e) {}
    }
}

function addChartMarker(signal) {
    state.markers.push({
        time: signal.candleTime,
        position: signal.type === 'BUY' ? 'belowBar' : 'aboveBar',
        color: signal.type === 'BUY' ? CONFIG.candleUp : CONFIG.candleDown,
        shape: signal.type === 'BUY' ? 'arrowUp' : 'arrowDown',
        text: ''   // no text — keep the chart clean (arrow + colour shows direction)
    });

    // keep only the most recent markers so the chart doesn't get cluttered
    if (state.markers.length > 15) state.markers = state.markers.slice(-15);

    setAllMarkers();
}

// ============================================
// UI UPDATE FUNCTIONS
// ============================================
function updateStatsBar() {
    const candles = state.candleData;
    if (candles.length === 0) return;

    const current = candles[candles.length - 1];
    const price = current.close;
    const prevPrice = candles.length > 1 ? candles[candles.length - 2].close : price;
    const change = ((price - prevPrice) / prevPrice * 100);

    // LTP
    const ltpEl = document.getElementById('ltp-value');
    const changeEl = document.getElementById('ltp-change');
    ltpEl.textContent = formatPrice(price);
    changeEl.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
    changeEl.className = `stat-change ${change >= 0 ? 'positive' : 'negative'}`;

    // Flash effect
    const ltpCard = document.getElementById('stat-ltp');
    ltpCard.classList.remove('flash-buy', 'flash-sell');
    void ltpCard.offsetWidth; // reflow
    ltpCard.classList.add(change >= 0 ? 'flash-buy' : 'flash-sell');

    // EMAs
    for (const period of CONFIG.emaPeriods) {
        const emaVal = getLatestEMA(period);
        const valEl = document.getElementById(`ema${period}-value`);
        const distEl = document.getElementById(`ema${period}-dist`);

        if (emaVal && valEl) {
            valEl.textContent = formatPrice(emaVal);
            const dist = price - emaVal;
            distEl.textContent = `${dist >= 0 ? '+' : ''}${dist.toFixed(1)} pts`;
            distEl.style.color = dist >= 0 ? 'var(--buy-color)' : 'var(--sell-color)';
        }
    }
}

function updateLevelsTable() {
    const container = document.getElementById('levels-table');
    if (!container) return;

    const candles = state.candleData;
    if (candles.length === 0) return;

    const price = candles[candles.length - 1].close;

    let html = `
        <div class="level-row header-row">
            <span>EMA</span>
            <span>Value</span>
            <span>Distance</span>
            <span>Status</span>
        </div>
    `;

    for (const period of CONFIG.emaPeriods) {
        const emaVal = getLatestEMA(period);
        if (!emaVal) continue;

        const dist = price - emaVal;
        const distPct = (dist / price * 100).toFixed(2);
        const isAbove = dist > 0;
        const isNear = Math.abs(distPct) < 0.15;

        let status, statusClass;
        if (isNear) {
            status = '⚡ NEAR';
            statusClass = 'near';
        } else if (isAbove) {
            status = '🟢 SUPPORT';
            statusClass = 'support';
        } else {
            status = '🔴 RESIST';
            statusClass = 'resistance';
        }

        html += `
            <div class="level-row">
                <span class="level-ema" style="color: ${CONFIG.emaColors[period]}">EMA ${period}</span>
                <span class="level-val">${formatPrice(emaVal)}</span>
                <span class="level-dist ${isAbove ? 'above' : 'below'}">${dist >= 0 ? '+' : ''}${dist.toFixed(1)}</span>
                <span class="level-status ${statusClass}">${status}</span>
            </div>
        `;
    }

    container.innerHTML = html;
}

function updateTimeDisplay() {
    const el = document.getElementById('time-display');
    if (el) {
        el.textContent = formatTime(new Date()) + ' IST';
    }
}

function updateMarketStatus() {
    // Prefer the live feed as truth; fall back to the clock when no feed yet.
    const open = feedLive() || isMarketOpen();
    LIVE.marketOpen = open;

    const statusDot = document.querySelector('.status-dot');
    const statusText = document.querySelector('.status-text');

    // NSE market hours: Mon–Fri, 9:15 AM - 3:30 PM IST
    if (open) {
        statusDot.className = 'status-dot live';
        statusText.textContent = 'MARKET OPEN';
        statusText.style.color = 'var(--buy-color)';
    } else {
        statusDot.className = 'status-dot closed';
        statusText.textContent = 'MARKET CLOSED';
        statusText.style.color = 'var(--sell-color)';
    }

    // Keep the data-source banner in sync with market state
    updateDataBanner(state.lastAuthInfo || null);
}

// ============================================
// MAIN UPDATE LOOP
// ============================================
function startDataStream() {
    if (state.updateInterval) {
        clearInterval(state.updateInterval);
    }

    state.updateInterval = setInterval(async () => {
        if (state.candleData.length === 0) return;

        // Real data only — never simulate
        if (!LIVE.active || !LIVE.authenticated) return;

        // Work ONLY while ticks are actually flowing (market live). When the
        // feed goes quiet (market closed), freeze on the last real data —
        // no new candles, no new signals.
        const open = feedLive();
        if (!open) {
            LIVE.wasMarketOpen = false;
            return;
        }

        // Market just (re)opened -> force an immediate full candle refresh
        if (!LIVE.wasMarketOpen) {
            LIVE.wasMarketOpen = true;
            LIVE.lastCandleRefresh = 0;
        }

        await tickLive();

        // Recalculate EMAs
        state.emaData = calculateAllEMAs(state.candleData);

        // Update chart + UI
        updateChart();
        updateStatsBar();
        updateLevelsTable();

        // Detect signals on real candles (entries are taken on options)
        detectSignals();
        // 3 PM spike catcher (only active in the 2:55-3:05 window when ON)
        detectSpike();
        // (option trades are trailed/exited in pollOptions -> resolveOptionTrades)

    }, CONFIG.updateIntervalMs);
}

// --- LIVE: poll real LTP, periodically re-sync full candles ---
async function tickLive() {
    const now = Date.now();

    // Periodic full candle re-fetch keeps OHLC accurate (esp. during market hours)
    if (now - LIVE.lastCandleRefresh >= CONFIG.candleRefreshMs) {
        LIVE.lastCandleRefresh = now;
        try {
            const fresh = await fetchRealCandles(state.symbol, state.timeframe);
            if (fresh && fresh.length) {
                state.candleData = fresh;
                return;
            }
        } catch (e) { /* keep current data */ }
    }

    // If the live SSE feed is delivering ticks, it already updates the candle —
    // skip the REST LTP poll. Only poll as a fallback when SSE is silent (>6s).
    const sseAlive = LIVE.sseConnected && (now - LIVE.lastTickTime < 6000);
    if (sseAlive) return;

    // Fallback LTP poll updates the in-progress candle
    let ltp = null;
    try { ltp = await fetchLTP(state.symbol); } catch (e) { ltp = null; }
    if (ltp == null) return;

    const lastCandle = state.candleData[state.candleData.length - 1];
    const nowSec = Math.floor(now / 1000);
    const candleEnd = lastCandle.time + state.timeframe * 60;

    if (nowSec >= candleEnd) {
        // Start a new candle aligned to the timeframe boundary
        state.candleData.push({
            time: candleEnd,
            open: ltp, high: ltp, low: ltp, close: ltp
        });
        if (state.candleData.length > 500) state.candleData.shift();
    } else {
        lastCandle.close = parseFloat(ltp.toFixed(2));
        lastCandle.high = parseFloat(Math.max(lastCandle.high, ltp).toFixed(2));
        lastCandle.low = parseFloat(Math.min(lastCandle.low, ltp).toFixed(2));
    }
}

// --- SIMULATION fallback (original behaviour) ---
function tickSimulated() {
    const lastCandle = state.candleData[state.candleData.length - 1];
    const now = Math.floor(Date.now() / 1000);
    const candleEnd = lastCandle.time + state.timeframe * 60;

    if (now >= candleEnd) {
        const newCandle = generateNextCandle(lastCandle, state.symbol, state.timeframe);
        state.candleData.push(newCandle);
        if (state.candleData.length > 500) state.candleData.shift();
    } else {
        const vol = CONFIG.symbols[state.symbol].volatility * lastCandle.close * 0.3;
        const tick = (Math.random() - 0.5) * vol;
        lastCandle.close = parseFloat((lastCandle.close + tick).toFixed(2));
        lastCandle.high = parseFloat(Math.max(lastCandle.high, lastCandle.close).toFixed(2));
        lastCandle.low = parseFloat(Math.min(lastCandle.low, lastCandle.close).toFixed(2));
    }
}

// ============================================
// EVENT HANDLERS
// ============================================
function setupEventHandlers() {
    // Symbol selector
    document.querySelectorAll('.symbol-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.symbol-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            switchSymbol(btn.dataset.symbol);
        });
    });

    // Timeframe selector
    document.querySelectorAll('.tf-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            switchTimeframe(parseInt(btn.dataset.tf));
        });
    });

    // Chart controls
    document.getElementById('btn-ema-toggle')?.addEventListener('click', function () {
        state.showEMAs = !state.showEMAs;
        this.classList.toggle('active', state.showEMAs);
        updateChart();
    });

    document.getElementById('btn-signals-toggle')?.addEventListener('click', function () {
        state.showSignals = !state.showSignals;
        this.classList.toggle('active', state.showSignals);
        updateChart();
    });

    document.getElementById('btn-levels-toggle')?.addEventListener('click', function () {
        state.showLevels = !state.showLevels;
        this.classList.toggle('active', state.showLevels);
    });

    // Panel toggles
    document.querySelectorAll('.panel-toggle').forEach(btn => {
        btn.addEventListener('click', () => {
            const panel = btn.closest('.panel');
            const body = panel.querySelector('.panel-body');
            body.classList.toggle('collapsed');
            btn.textContent = body.classList.contains('collapsed') ? '+' : '−';
        });
    });

    // Trade log filters
    document.querySelectorAll('.log-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.log-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderTradeLog(btn.dataset.filter);
        });
    });

    // All-index ticker -> click to switch symbol
    document.getElementById('index-ticker')?.addEventListener('click', (e) => {
        const item = e.target.closest('.idx-item');
        if (!item) return;
        const sym = item.dataset.sym;
        if (!sym || sym === state.symbol) return;
        document.querySelectorAll('.symbol-btn').forEach(b => b.classList.toggle('active', b.dataset.symbol === sym));
        switchSymbol(sym);
        renderIndexTicker();
    });

    // Strategy selector
    document.getElementById('strategy-select')?.addEventListener('change', function () {
        state.strategy = this.value;
        state.lastSignalTime = 0;
        state.lastInsideBarTime = 0;
        state.orbDoneDay = null;
        console.log('Strategy ->', state.strategy);
    });

    // PCR chip -> toggle option-chain dropdown
    document.getElementById('pcr-chip')?.addEventListener('click', () => {
        const p = document.getElementById('oc-panel');
        if (p) p.style.display = (p.style.display === 'none' || !p.style.display) ? 'block' : 'none';
    });

    // Click a strike row -> chart that strike's CE & PE
    document.getElementById('oc-body')?.addEventListener('click', (e) => {
        const tr = e.target.closest('tr[data-strike]');
        if (tr) selectStrike(parseFloat(tr.dataset.strike));
    });

    // 3PM Spike toggle
    document.getElementById('btn-spike')?.addEventListener('click', function () {
        state.spikeMode = !state.spikeMode;
        this.classList.toggle('active', state.spikeMode);
        this.innerHTML = this.innerHTML.replace(/3PM Spike: (ON|OFF)/, '3PM Spike: ' + (state.spikeMode ? 'ON' : 'OFF'));
        this.style.background = state.spikeMode ? 'var(--warn-color)' : '';
        this.style.color = state.spikeMode ? '#000' : 'var(--warn-color)';
    });

    // Auto-Fit toggle
    document.getElementById('btn-autofit')?.addEventListener('click', function () {
        state.autoFit = !state.autoFit;
        this.classList.toggle('active', state.autoFit);
        this.innerHTML = this.innerHTML.replace(/Auto-Fit: (ON|OFF)/, 'Auto-Fit: ' + (state.autoFit ? 'ON' : 'OFF'));
        if (state.autoFit) fitAllCharts();
    });

    // ARM LIVE (real money) toggle
    document.getElementById('btn-livearm')?.addEventListener('click', toggleLiveArm);

    // Auto-Trade ON/OFF toggle (both the header master switch and the toolbar button)
    document.getElementById('btn-autotrade-main')?.addEventListener('click', () => {
        state.autoTrade = !state.autoTrade;
        syncAutoTradeUI();
    });
    document.getElementById('btn-autotrade')?.addEventListener('click', function () {
        state.autoTrade = !state.autoTrade;
        syncAutoTradeUI();
    });

    // Hard refresh button — reload the page with fresh data (bypass cache)
    document.getElementById('btn-refresh')?.addEventListener('click', function () {
        this.classList.add('active');
        // cache-busting query param forces a fresh load of files + data
        window.location.href = window.location.pathname + '?r=' + Date.now();
    });

    // Clear log
    document.getElementById('clear-log')?.addEventListener('click', () => {
        state.trades = [];
        state.tradeCounter = 0;
        renderTradeLog();
        saveSession();
    });

    // Export full trade history as CSV
    document.getElementById('export-history')?.addEventListener('click', exportTradeHistoryCSV);

    // Manual Exit buttons (delegated — works for both trade tables)
    document.body.addEventListener('click', (e) => {
        const btn = e.target.closest('.exit-btn');
        if (btn && btn.dataset.id) exitTrade(parseInt(btn.dataset.id, 10));
    });

    // Set initial active states for control buttons
    document.getElementById('btn-ema-toggle')?.classList.add('active');
    document.getElementById('btn-signals-toggle')?.classList.add('active');
    document.getElementById('btn-levels-toggle')?.classList.add('active');

    // Sync Auto-Fit button
    const af = document.getElementById('btn-autofit');
    if (af) af.classList.toggle('active', state.autoFit);

    // Sync Auto-Trade UI to current state (auto-pilot)
    syncAutoTradeUI();
}

// Keeps BOTH auto-trade controls (header master switch + toolbar button) in step with
// state.autoTrade — whichever is clicked, and whenever the kill-switch forces it off.
function syncAutoTradeUI() {
    const on = state.autoTrade;

    const main = document.getElementById('btn-autotrade-main');
    if (main) {
        main.classList.toggle('active', on);
        const st = document.getElementById('at-state');
        if (st) st.textContent = on ? 'ON' : 'OFF';
        main.title = on
            ? 'Auto-Trade ON — bot takes option trades automatically on signals (PAPER)'
            : 'Auto-Trade OFF — signals are shown but no trades are taken';
    }

    const at = document.getElementById('btn-autotrade');
    if (at) {
        at.classList.toggle('active', on);
        at.innerHTML = at.innerHTML.replace(/Auto-Trade: (ON|OFF)/, 'Auto-Trade: ' + (on ? 'ON' : 'OFF'));
        at.style.color = on ? 'var(--buy-color)' : '';
        at.style.borderColor = on ? 'var(--buy-color)' : '';
    }
}

function switchSymbol(symbol) {
    state.symbol = symbol;
    state.signals = [];
    state.markers = [];
    state.lastSignalTime = 0;
    state.lastInsideBarTime = 0;

    loadData();
}

function switchTimeframe(tf) {
    state.timeframe = tf;
    state.signals = [];
    state.markers = [];
    state.lastSignalTime = 0;
    state.lastInsideBarTime = 0;

    loadData();
}

async function loadData() {
    let candles = null;

    // reset the Call/Put charts (symbol-specific)
    state.callData = [];
    state.putData = [];
    if (state.callSeries) state.callSeries.setData([]);
    if (state.putSeries) state.putSeries.setData([]);
    LIVE.lastOptLineTime = 0;
    LIVE.optPrem = {};
    LIVE.chartCe = null;
    LIVE.chartPe = null;
    // clear OI level lines (symbol-specific)
    if (state.oiLines && state.candleSeries) {
        for (const k in state.oiLines) { try { state.candleSeries.removePriceLine(state.oiLines[k].line); } catch (e) {} }
    }
    state.oiLines = {};

    const auth = await checkAuth();
    state.lastAuthInfo = auth;

    if (LIVE.authenticated) {
        try {
            candles = await fetchRealCandles(state.symbol, state.timeframe);
        } catch (e) {
            console.warn('Real candle fetch failed:', e);
            candles = null;
        }
    }

    if (candles && candles.length) {
        // Real data loaded — this stays as the last session data when market is closed
        state.candleData = candles;
        LIVE.active = true;
        LIVE.lastCandleRefresh = Date.now();
    } else {
        // Real data only — NO simulation. Show whatever we last had (or empty).
        LIVE.active = false;
        if (!state.candleData) state.candleData = [];
    }

    state.emaData = calculateAllEMAs(state.candleData);
    updateMarketStatus();      // sets LIVE.marketOpen + banner

    updateChart();
    updateStatsBar();
    updateLevelsTable();
    renderSignalsList();
    renderTradeLog();

    // Load full-session Call/Put candle charts, then fit all 3 together
    await loadOptionCharts();
    fitAllCharts();
}

function fitAllCharts() {
    // fit the index, then apply its exact logical range to the option charts
    try {
        if (!state.chartIndex) return;
        state.chartIndex.timeScale().fitContent();
        const range = state.chartIndex.timeScale().getVisibleLogicalRange();
        if (range) {
            [state.chartCall, state.chartPut].forEach(c => {
                try { c && c.timeScale().setVisibleLogicalRange(range); } catch (e) {}
            });
        }
    } catch (e) { /* ignore */ }
}

// Fetch today's full 1-min OHLC for the ATM Call & Put, so the two option
// charts span the whole session (same time grid as the index chart).
async function loadOptionCharts() {
    if (!LIVE.authenticated) return;
    try {
        const [ce, pe] = await Promise.all([
            apiGet(`/api/atm?symbol=${state.symbol}&side=CE`),
            apiGet(`/api/atm?symbol=${state.symbol}&side=PE`)
        ]);
        if (ce.status === 'ok' && ce.instrument_key) LIVE.chartCe = { strike: ce.strike, key: ce.instrument_key };
        if (pe.status === 'ok' && pe.instrument_key) LIVE.chartPe = { strike: pe.strike, key: pe.instrument_key };

        const loadLeg = async (leg) => {
            if (!leg || !leg.key) return [];
            const r = await apiGet(`/api/optintraday?key=${encodeURIComponent(leg.key)}&interval=${state.timeframe}`);
            return (r.status === 'ok' && Array.isArray(r.candles)) ? r.candles : [];
        };
        const [callC, putC] = await Promise.all([loadLeg(LIVE.chartCe), loadLeg(LIVE.chartPe)]);
        state.callData = callC;
        state.putData = putC;
        if (state.callSeries) state.callSeries.setData(state.callData);
        if (state.putSeries) state.putSeries.setData(state.putData);
        setAllMarkers();
        const all = state.candleData;
        LIVE.lastOptLineTime = all.length ? all[all.length - 1].time : 0;
    } catch (e) { /* ignore */ }
}

// ============================================
// TIME UPDATE
// ============================================
function startClock() {
    updateTimeDisplay();
    updateMarketStatus();
    state._dayKey = new Date().toDateString();
    setInterval(() => {
        updateTimeDisplay();
        updateMarketStatus();
        // reset daily risk counters at day change
        const dk = new Date().toDateString();
        if (dk !== state._dayKey) {
            state._dayKey = dk;
            state._killSwitch = false;
            state.realOrdersToday = 0;
        }
    }, 1000);
}

// Auto-reload the SAME tab when index.html/app.js/styles.css change on disk
async function startAutoReload() {
    let base = null;
    try { const r = await apiGet('/api/version'); base = r.version; } catch (e) { return; }
    setInterval(async () => {
        try {
            const r = await apiGet('/api/version');
            if (base != null && r.version && r.version !== base) {
                location.reload();   // reload in place — no new tab
            }
        } catch (e) { /* ignore */ }
    }, 3000);
}

// ============================================
// INITIALIZATION
// ============================================
async function init() {
    initChart();
    setupEventHandlers();
    await loadSession();      // restore saved trades + option orders
    await loadData();
    startDataStream();
    startLiveFeed();      // live WebSocket ticks via SSE
    startClock();
    requestAlertPermission();
    renderPaperOrders();
    setInterval(pollOptions, 3000);   // option premiums: chart lines + trade mgmt + gamma blast
    updateAllLtp();                   // all-index ticker
    setInterval(updateAllLtp, 5000);
    startAutoReload();                 // auto-refresh THIS tab when files change (no new tab)

    // Initial empty states
    renderSignalsList();
    renderTradeLog();

    console.log('%c EMA Strategy Dashboard Initialized ', 'background: linear-gradient(135deg, #00f5d4, #7b61ff); color: #0a0e17; font-weight: bold; padding: 4px 12px; border-radius: 4px;');
}

// Wait for DOM
document.addEventListener('DOMContentLoaded', init);

// Handle window resize (3 charts)
window.addEventListener('resize', () => {
    [['chart-index', state.chartIndex], ['chart-call', state.chartCall], ['chart-put', state.chartPut]].forEach(([id, ch]) => {
        const el = document.getElementById(id);
        if (el && ch) ch.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    });
});
