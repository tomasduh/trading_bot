let pnlChart   = null;
let priceChart = null;
let ws         = null;
let reconnectTimer  = null;
let countdownTimer  = null;
let nextCycleAt     = null;
let lastHeartbeat   = 0;
let staleCheckTimer = null;
let allTrades       = [];
let tradeFilter     = "";
let tradePage       = 1;
const TRADES_PAGE_SIZE = 10;

const STALE_THRESHOLD_MS = 30_000;  // sin heartbeat 30s → mostrar "stale"

// Token de auth — se acepta vía:
//   1) URL: ?token=XXX  (se guarda en localStorage y se limpia de la URL)
//   2) localStorage.setItem('dashboard_token', 'XXX')
//   3) prompt si no hay token
function _resolveToken() {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get('token');
    if (fromUrl) {
      localStorage.setItem('dashboard_token', fromUrl);
      // Limpiar el token de la URL para no dejarlo en historial
      params.delete('token');
      const newUrl = window.location.pathname +
        (params.toString() ? '?' + params.toString() : '') +
        window.location.hash;
      window.history.replaceState({}, '', newUrl);
      return fromUrl;
    }
    const fromStorage = localStorage.getItem('dashboard_token');
    if (fromStorage) return fromStorage;
    // Si no hay token, pedirlo (solo una vez por sesión)
    const entered = window.prompt('Dashboard token requerido:');
    if (entered) {
      localStorage.setItem('dashboard_token', entered.trim());
      return entered.trim();
    }
  } catch (e) { console.warn('token resolver error', e); }
  return "";
}
const DASHBOARD_TOKEN = _resolveToken();

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Escapa caracteres HTML especiales para evitar XSS al insertar texto
 * de origen externo (log lines del servidor) en innerHTML.
 */
function escapeHtml(str) {
  return String(str)
    .replace(/&/g,  '&amp;')
    .replace(/</g,  '&lt;')
    .replace(/>/g,  '&gt;')
    .replace(/"/g,  '&quot;')
    .replace(/'/g,  '&#39;');
}

const fmt = (n, d = 2) => n == null ? '—'
  : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });

const fmtPct = n => n == null ? '—'
  : (Number(n) >= 0 ? '+' : '') + Number(n).toFixed(2) + '%';

const pnlClass = v => v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : 'text-slate-400';

const signalBadge = s => {
  const cls = s === 'BUY' ? 'badge-buy' : s === 'SELL' ? 'badge-sell' : 'badge-none';
  return `<span class="px-2 py-0.5 rounded text-xs font-semibold ${cls}">${s}</span>`;
};

const timeAgo = iso => {
  if (!iso) return '—';
  const d = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (d < 60) return `${d}s ago`;
  if (d < 3600) return `${Math.floor(d/60)}m ago`;
  return `${Math.floor(d/3600)}h ago`;
};

// ── Renders ───────────────────────────────────────────────────────────────────

function renderStatus(d) {
  const pnl = d.total_pnl_usdt;
  document.getElementById('total-pnl').innerHTML =
    `<span class="${pnlClass(pnl)}">${pnl >= 0 ? '+' : ''}$${fmt(pnl)}</span>`;
  document.getElementById('win-rate').textContent     = d.win_rate + '%';
  document.getElementById('total-trades').textContent = d.total_trades;
  document.getElementById('wins-losses').innerHTML    =
    `<span class="pnl-pos">${d.wins}W</span> / <span class="pnl-neg">${d.losses}L</span>`;
  document.getElementById('total-cycles').textContent = d.total_cycles;
  document.getElementById('last-update').textContent  = 'Último ciclo: ' + timeAgo(d.last_cycle);
  document.getElementById('timeframe-badge').textContent = d.timeframe;

  // Pause state
  const pauseBadge  = document.getElementById('pause-badge');
  const pauseBtn    = document.getElementById('btn-pause');
  const resumeBtn   = document.getElementById('btn-resume');
  if (d.paused) {
    pauseBadge.classList.remove('hidden');
    pauseBtn.classList.add('hidden');
    resumeBtn.classList.remove('hidden');
  } else {
    pauseBadge.classList.add('hidden');
    pauseBtn.classList.remove('hidden');
    resumeBtn.classList.add('hidden');
  }

  // Countdown usa el intervalo del server (no asume 30 min)
  const interval = d.loop_interval_s || 1800;
  startCountdown(d.last_cycle, interval);

  const posEl = document.getElementById('open-positions');
  if (!d.open_trades.length) {
    posEl.innerHTML = '<p class="text-center py-6 text-slate-500 text-sm">Sin posiciones abiertas</p>';
  } else {
    posEl.innerHTML = d.open_trades.map(t => {
      const mtmClass = t.mtm_pnl_usdt >= 0 ? 'pnl-pos' : 'pnl-neg';
      const arrow    = t.mtm_pnl_usdt >= 0 ? '▲' : '▼';
      return `
      <div class="bg-slate-800 rounded-lg p-3 border border-brand-800">
        <div class="flex justify-between items-center mb-2">
          <span class="text-white font-semibold text-sm">${t.symbol}</span>
          <span class="badge-open px-2 py-0.5 rounded text-xs font-semibold">ABIERTO</span>
        </div>
        <div class="grid grid-cols-2 gap-1 text-xs text-slate-400">
          <span>Entrada</span><span class="text-right text-white">$${fmt(t.entry_price)}</span>
          <span>Actual</span><span class="text-right ${mtmClass} font-semibold">$${fmt(t.current_price)}</span>
          <span>P&L vivo</span><span class="text-right ${mtmClass} font-semibold">${arrow} ${t.mtm_pnl_usdt >= 0 ? '+' : ''}$${fmt(t.mtm_pnl_usdt)} (${fmtPct(t.mtm_pnl_pct)})</span>
          <span>Cantidad</span><span class="text-right text-white">${t.quantity}</span>
          <span>Stop Loss</span><span class="text-right pnl-neg">$${fmt(t.stop_loss)}</span>
          <span>Take Profit</span><span class="text-right pnl-pos">$${fmt(t.take_profit)}</span>
        </div>
      </div>`;
    }).join('');
  }
}

function renderSignals(data) {
  const tbody = document.getElementById('signals-table');
  tbody.innerHTML = data.map(s => {
    const rsiColor  = s.rsi > 70 ? 'text-red-400' : s.rsi < 30 ? 'text-emerald-400' : 'text-slate-300';
    const macdColor = s.macd_hist > 0 ? 'pnl-pos' : s.macd_hist < 0 ? 'pnl-neg' : 'text-slate-400';
    const emaColor  = s.ema_fast > s.ema_slow ? 'pnl-pos' : 'pnl-neg';
    const emaArrow  = s.ema_fast > s.ema_slow ? '↑' : '↓';
    return `
      <tr class="hover:bg-slate-800/50 transition-colors">
        <td class="py-2.5 font-medium text-white">${s.symbol}</td>
        <td class="py-2.5 text-right font-mono">$${fmt(s.price)}</td>
        <td class="py-2.5 text-right font-mono ${rsiColor}">${fmt(s.rsi, 1)}</td>
        <td class="py-2.5 text-right font-mono ${macdColor}">${s.macd_hist >= 0 ? '+' : ''}${fmt(s.macd_hist, 2)}</td>
        <td class="py-2.5 text-right text-xs ${emaColor}">${emaArrow} ${fmt(s.ema_fast, 0)}</td>
        <td class="py-2.5 text-center">${signalBadge(s.signal)}</td>
      </tr>`;
  }).join('');
}

function renderTrades(data) {
  allTrades = data;
  refreshTradesView();
}

function refreshTradesView() {
  const tbody     = document.getElementById('trades-table');
  const noTrades  = document.getElementById('no-trades');
  const filtered  = tradeFilter
    ? allTrades.filter(t =>
        (t.symbol || '').toLowerCase().includes(tradeFilter) ||
        (t.exit_reason || '').toLowerCase().includes(tradeFilter))
    : allTrades;

  const pagination = document.getElementById('trades-pagination');

  if (!filtered.length) {
    tbody.innerHTML = '';
    noTrades.style.display = 'block';
    noTrades.textContent = allTrades.length
      ? 'Ningún trade coincide con el filtro'
      : 'Sin trades aún — el bot está buscando señales';
    pagination.style.display = 'none';
    return;
  }
  noTrades.style.display = 'none';

  const totalPages = Math.max(1, Math.ceil(filtered.length / TRADES_PAGE_SIZE));
  if (tradePage > totalPages) tradePage = totalPages;
  if (tradePage < 1) tradePage = 1;
  const start = (tradePage - 1) * TRADES_PAGE_SIZE;
  const pageItems = filtered.slice(start, start + TRADES_PAGE_SIZE);

  pagination.style.display = filtered.length > TRADES_PAGE_SIZE ? 'flex' : 'none';
  document.getElementById('trades-page-info').textContent =
    `Página ${tradePage} de ${totalPages} (${filtered.length} trades)`;
  document.getElementById('trades-prev').disabled = tradePage <= 1;
  document.getElementById('trades-next').disabled = tradePage >= totalPages;

  tbody.innerHTML = pageItems.map(t => {
    const pnl    = t.pnl_usdt;
    const status = t.status === 'OPEN'
      ? '<span class="badge-open px-2 py-0.5 rounded text-xs">ABIERTO</span>'
      : '<span class="badge-closed px-2 py-0.5 rounded text-xs">CERRADO</span>';
    const rColor = { STOP_LOSS:'text-red-400', TAKE_PROFIT:'pnl-pos',
                     SIGNAL:'text-brand-400', TRAILING_STOP:'text-amber-400'
                   }[t.exit_reason] || 'text-slate-500';
    return `
      <tr class="hover:bg-slate-800/50 transition-colors">
        <td class="py-2.5 text-slate-500">#${t.id}</td>
        <td class="py-2.5 font-medium text-white">${t.symbol}</td>
        <td class="py-2.5 text-right font-mono">$${fmt(t.entry_price)}</td>
        <td class="py-2.5 text-right font-mono">${t.exit_price ? '$'+fmt(t.exit_price) : '—'}</td>
        <td class="py-2.5 text-right text-slate-400">${t.quantity}</td>
        <td class="py-2.5 text-right font-mono font-semibold ${pnlClass(pnl)}">
          ${pnl != null ? (pnl >= 0 ? '+' : '')+'$'+fmt(pnl) : '—'}
        </td>
        <td class="py-2.5 text-right font-mono ${pnlClass(t.pnl_pct)}">${fmtPct(t.pnl_pct)}</td>
        <td class="py-2.5 text-center">${status}</td>
        <td class="py-2.5 text-center text-xs ${rColor}">${t.exit_reason || '—'}</td>
      </tr>`;
  }).join('');

  const closed = allTrades.filter(t => t.status === 'CLOSED').reverse();
  if (closed.length) {
    let acc = 0;
    const cumPnl = closed.map(t => +(acc += (t.pnl_usdt || 0)).toFixed(2));
    renderPnlChart(closed.map(t => '#'+t.id), cumPnl);
  }
}

async function loadPerSymbolStats() {
  try {
    const data = await fetch('/api/per-symbol').then(r => r.json());
    const tbody = document.getElementById('per-symbol-table');
    const empty = document.getElementById('no-per-symbol');
    if (!data.length) {
      tbody.innerHTML = '';
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';
    tbody.innerHTML = data.map(d => `
      <tr class="hover:bg-slate-800/50 transition-colors">
        <td class="py-2.5 font-medium text-white">${d.symbol}</td>
        <td class="py-2.5 text-right">${d.trades}</td>
        <td class="py-2.5 text-right"><span class="pnl-pos">${d.wins}</span> / <span class="pnl-neg">${d.losses}</span></td>
        <td class="py-2.5 text-right text-brand-400 font-semibold">${d.win_rate}%</td>
        <td class="py-2.5 text-right font-mono font-semibold ${pnlClass(d.pnl_usdt)}">${d.pnl_usdt >= 0 ? '+' : ''}$${fmt(d.pnl_usdt)}</td>
        <td class="py-2.5 text-right font-mono ${pnlClass(d.avg_pnl_pct)}">${fmtPct(d.avg_pnl_pct)}</td>
      </tr>`).join('');
  } catch (e) { console.warn('per-symbol error', e); }
}

function renderLog(lines) {
  const container = document.getElementById('log-container');
  container.innerHTML = lines.map(formatLogLine).join('');
  container.scrollTop = container.scrollHeight;
}

function appendLogLine(line) {
  const container = document.getElementById('log-container');
  const atBottom  = container.scrollHeight - container.scrollTop <= container.clientHeight + 40;
  const p = document.createElement('div');
  p.innerHTML = formatLogLine(line);
  container.appendChild(p.firstChild);
  while (container.children.length > 200) container.removeChild(container.firstChild);
  if (atBottom) container.scrollTop = container.scrollHeight;
}

function formatLogLine(line) {
  let cls = 'log-line';
  if (line.includes('[ERROR]'))   cls += ' error';
  else if (line.includes('[WARNING]') || line.includes('[CRITICAL]')) cls += ' warn';
  else if (line.includes('[INFO]')) cls += ' info';

  // Escapar HTML ANTES de inyectar en el DOM para evitar XSS.
  // Las regex de coloreado solo añaden spans con clases hardcodeadas — seguro.
  const safe = escapeHtml(line);

  const h = safe
    .replace(/(Abriendo BUY|Trade.*abierto)/g, '<span class="text-emerald-400 font-semibold">$1</span>')
    .replace(/(Cerrando|STOP_LOSS|TAKE_PROFIT|TRAILING_STOP)/g, '<span class="text-amber-400">$1</span>')
    .replace(/(\+[\d.]+\s*USDT)/g, '<span class="pnl-pos">$1</span>')
    .replace(/(−[\d.]+\s*USDT|-[\d.]+\s*USDT)/g, '<span class="pnl-neg">$1</span>')
    .replace(/(\[BUY\]|\[SELL\])/g, '<span class="text-brand-400 font-semibold">$1</span>')
    .replace(/(KILL-SWITCH)/g, '<span class="text-red-400 font-bold">$1</span>');

  return `<p class="${cls}">${h}</p>`;
}

// ── Charts ────────────────────────────────────────────────────────────────────

function renderPnlChart(labels, data) {
  const ctx = document.getElementById('pnl-chart').getContext('2d');
  if (pnlChart) pnlChart.destroy();
  const pos = data[data.length - 1] >= 0;
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets: [{
      label: 'PnL acumulado (USDT)', data,
      borderColor: pos ? '#34d399' : '#f87171',
      backgroundColor: pos ? 'rgba(52,211,153,0.08)' : 'rgba(248,113,113,0.08)',
      borderWidth: 2, fill: true, tension: 0.3,
      pointRadius: 3, pointBackgroundColor: pos ? '#34d399' : '#f87171',
    }]},
    options: {
      responsive: true, plugins: { legend: { display: false } },
      scales: {
        x: { ticks:{ color:'#64748b', font:{size:10} }, grid:{color:'#1e293b'} },
        y: { ticks:{ color:'#64748b', font:{size:10} }, grid:{color:'#1e293b'} },
      }
    }
  });
}

async function loadPriceChart() {
  const symbol = document.getElementById('chart-symbol').value;
  try {
    const data = await fetch(`/api/candles/${encodeURIComponent(symbol)}?limit=80`).then(r => r.json());
    if (!data.length) return;
    const ctx = document.getElementById('price-chart').getContext('2d');
    if (priceChart) priceChart.destroy();
    priceChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: data.map(c => c.t.slice(11,16)),
        datasets: [
          { label:'Precio',   data: data.map(c=>c.c), borderColor:'#3b82f6', borderWidth:2, pointRadius:0, fill:false, tension:0.2 },
          { label:'BB Upper', data: data.map(c=>c.bb_upper), borderColor:'rgba(148,163,184,0.3)', borderWidth:1, borderDash:[4,4], pointRadius:0, fill:false },
          { label:'BB Lower', data: data.map(c=>c.bb_lower), borderColor:'rgba(148,163,184,0.3)', borderWidth:1, borderDash:[4,4], pointRadius:0, fill:'-1', backgroundColor:'rgba(59,130,246,0.04)' },
          { label:'EMA 9',    data: data.map(c=>c.ema_fast), borderColor:'#f59e0b', borderWidth:1, pointRadius:0, fill:false },
        ]
      },
      options: {
        responsive: true, interaction: { mode:'index', intersect:false },
        plugins: { legend: { labels:{ color:'#64748b', font:{size:10}, boxWidth:12 } } },
        scales: {
          x: { ticks:{ color:'#64748b', font:{size:9}, maxTicksLimit:8 }, grid:{color:'#1e293b'} },
          y: { ticks:{ color:'#64748b', font:{size:10} }, grid:{color:'#1e293b'} },
        }
      }
    });
  } catch (e) { console.warn('price chart error', e); }
}

// ── Stale data detection ──────────────────────────────────────────────────────

function setStale(stale) {
  const banner = document.getElementById('stale-banner');
  if (stale) banner.classList.remove('hidden');
  else banner.classList.add('hidden');
}

function startStaleWatcher() {
  if (staleCheckTimer) clearInterval(staleCheckTimer);
  staleCheckTimer = setInterval(() => {
    if (lastHeartbeat === 0) return;  // aún no llegó ningún heartbeat
    setStale(Date.now() - lastHeartbeat > STALE_THRESHOLD_MS);
  }, 5000);
}

// ── Countdown ─────────────────────────────────────────────────────────────────

function startCountdown(lastCycleIso, intervalSeconds) {
  if (countdownTimer) clearInterval(countdownTimer);
  if (!lastCycleIso) return;
  nextCycleAt = new Date(lastCycleIso).getTime() + intervalSeconds * 1000;
  function tick() {
    const remaining = Math.max(0, Math.floor((nextCycleAt - Date.now()) / 1000));
    const mm = String(Math.floor(remaining / 60)).padStart(2, '0');
    const ss = String(remaining % 60).padStart(2, '0');
    const el = document.getElementById('countdown');
    if (!el) return;
    el.textContent = `${mm}:${ss}`;
    if (remaining === 0) el.classList.add('animate-pulse');
    else                 el.classList.remove('animate-pulse');
  }
  tick();
  countdownTimer = setInterval(tick, 1000);
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

function setConnStatus(connected) {
  const dot = document.getElementById('conn-dot');
  const lbl = document.getElementById('conn-label');
  if (connected) {
    dot.className = 'w-2 h-2 rounded-full bg-emerald-400 pulse';
    lbl.textContent = 'En vivo';
    lbl.className = 'text-emerald-400 text-xs';
  } else {
    dot.className = 'w-2 h-2 rounded-full bg-red-400';
    lbl.textContent = 'Reconectando...';
    lbl.className = 'text-red-400 text-xs';
  }
}

function connect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const tokenParam = DASHBOARD_TOKEN ? `?token=${encodeURIComponent(DASHBOARD_TOKEN)}` : '';
  ws = new WebSocket(`${proto}://${location.host}/ws${tokenParam}`);

  ws.onopen = () => {
    setConnStatus(true);
    setStale(false);
    lastHeartbeat = Date.now();
    loadPriceChart();
    loadPerSymbolStats();
  };

  ws.onmessage = ({ data }) => {
    let msg;
    try { msg = JSON.parse(data); }
    catch (e) { console.warn('WS bad JSON:', data); return; }

    lastHeartbeat = Date.now();
    setStale(false);

    switch (msg.type) {
      case 'status':    renderStatus(msg.data); break;
      case 'signals':   renderSignals(msg.data); break;
      case 'trades':    renderTrades(msg.data); loadPerSymbolStats(); break;
      case 'log':
        if (msg.lines) renderLog(msg.lines);
        else if (msg.line) appendLogLine(msg.line);
        break;
      case 'heartbeat': /* solo refresca lastHeartbeat */ break;
      default: console.warn('WS unknown type:', msg.type);
    }
  };

  ws.onclose = () => {
    setConnStatus(false);
    reconnectTimer = setTimeout(connect, 3000);
  };

  ws.onerror = () => ws.close();
}

// ── Pause / Resume ────────────────────────────────────────────────────────────

function _authHeaders() {
  const h = { 'Content-Type': 'application/json' };  // requerido por backend (anti-CSRF)
  if (DASHBOARD_TOKEN) h['Authorization'] = `Bearer ${DASHBOARD_TOKEN}`;
  return h;
}

async function pauseBot() {
  const r = await fetch('/api/pause', { method: 'POST', headers: _authHeaders(), body: '{}' });
  if (!r.ok) alert('Error al pausar: ' + r.status);
}

async function resumeBot() {
  const r = await fetch('/api/resume', { method: 'POST', headers: _authHeaders(), body: '{}' });
  if (!r.ok) alert('Error al reanudar: ' + r.status);
}

// ── Init ──────────────────────────────────────────────────────────────────────

document.getElementById('chart-symbol').addEventListener('change', loadPriceChart);
document.getElementById('btn-pause').addEventListener('click', pauseBot);
document.getElementById('btn-resume').addEventListener('click', resumeBot);
document.getElementById('trade-filter').addEventListener('input', e => {
  tradeFilter = e.target.value.toLowerCase().trim();
  tradePage = 1;
  refreshTradesView();
});
document.getElementById('trades-prev').addEventListener('click', () => {
  tradePage--;
  refreshTradesView();
});
document.getElementById('trades-next').addEventListener('click', () => {
  tradePage++;
  refreshTradesView();
});
startStaleWatcher();
connect();
