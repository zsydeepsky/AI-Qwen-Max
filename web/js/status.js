// web/js/status.js — 运行状态页：引擎状态 + 缓存池监控
'use strict';

const statusContent = document.getElementById('statusContent');
const spReadyDot = document.getElementById('spReadyDot');
const autoRefresh = document.getElementById('autoRefresh');
let autoTimer = null;

function card(title, rows, extraHtml) {
  return `
    <div class="status-card">
      <h3>${title}</h3>
      ${rows.map(([k, v]) =>
        `<div class="sp-row"><span class="sp-k">${k}</span><span class="sp-v">${v}</span></div>`
      ).join('')}
      ${extraHtml || ''}
    </div>`;
}

function ctxBar(used, total, pct) {
  const p = Math.min(100, Math.max(0, pct || 0));
  return `
    <div class="sp-row">
      <span class="sp-k">上下文</span>
      <span class="sp-v">${Common.fmtNum(used)}/${Common.fmtNum(total)} (${p}%)</span>
    </div>
    <div class="ctx-bar"><div class="fill" style="width:${p}%"></div></div>`;
}

async function loadStatus() {
  const s = await Common.apiFetchSilent('/status');
  if (!s) {
    spReadyDot.textContent = '🔴';
    statusContent.innerHTML = `<div class="status-error">无法连接后端，请检查端口和服务器是否运行。</div>`;
    return;
  }

  spReadyDot.textContent = s.backend_ready ? '🟢' : '🔴';
  const perf = s.perf || {};
  const c = s.cache || {};

  const cards = [];

  // 引擎
  const engineRows = [
    ['后端', s.backend_ready ? '就绪' : '未就绪'],
    ['模型', s.model ? s.model.split(/[\\/]/).pop() : '—'],
  ];
  let engineExtra = '';
  if (s.ctx_total) {
    engineExtra = ctxBar(s.ctx_tokens ?? 0, s.ctx_total, s.ctx_used_pct ?? 0);
  }
  cards.push(card('🧠 引擎', engineRows, engineExtra));

  // 性能
  cards.push(card('⚡ 性能', [
    ['TTFT', perf.ttft_s != null ? perf.ttft_s.toFixed(1) + 's' : '—'],
    ['预填充 PP', perf.pp_tps ? perf.pp_tps.toFixed(1) + ' t/s' : '—'],
    ['生成 TG', perf.tg_tps ? perf.tg_tps.toFixed(2) + ' t/s' : '—'],
    ['缓存命中', perf.cache_hit_pct != null ? perf.cache_hit_pct.toFixed(0) + '%' : '—'],
  ]));

  // 缓存池
  const nCache = (c.ram_entries || 0) + (c.ssd_entries || 0);
  cards.push(card('💾 KV 缓存池', [
    ['条目', nCache ? `${Common.fmtNum(nCache)}（RAM ${c.ram_entries || 0} / SSD ${c.ssd_entries || 0}）` : '0'],
    ['RAM 池', Common.fmtBytes(c.ram_bytes || 0)],
    ['SSD 池', Common.fmtBytes(c.ssd_bytes || 0)],
    ['缓存修复 heal', c.heal_requests ? `${Common.fmtNum(c.heal_requests)} 次 / ${Common.fmtNum(c.heal_tokens || 0)} tok` : '0'],
  ]));

  statusContent.innerHTML = cards.join('');
}

// 自动刷新（默认开）
function restartAutoRefresh() {
  if (autoTimer) clearInterval(autoTimer);
  autoTimer = setInterval(() => {
    if (autoRefresh.checked) loadStatus();
  }, 5000);
}

autoRefresh.addEventListener('change', () => {
  if (autoRefresh.checked) loadStatus();
});

// 端口变更：立即重新加载
Common.onPortChange(loadStatus);

// ===== 初始化 =====
loadStatus();
restartAutoRefresh();
