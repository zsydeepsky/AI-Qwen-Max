// web/js/apilog.js — API 日志页：SSE 实时监听 /api/events
// 列表反向增长（最新在最前），最多保留 100 张卡片。
'use strict';

const apiList = document.getElementById('apiList');
const apiLiveDot = document.getElementById('apiLiveDot');
let apiEvSource = null;
let apiCards = {};      // req_id -> {el, headEl, bodyEl, respEl, reasonEl}
let apiCardOrder = [];  // 渲染顺序（新 → 旧），用于裁剪上限

function connectApiEvents() {
  if (apiEvSource) { apiEvSource.close(); apiEvSource = null; }
  apiEvSource = new EventSource(Common.getBaseUrl() + '/api/events');
  apiEvSource.onopen = () => {
    apiLiveDot.textContent = '● 实时';
    apiLiveDot.className = 'api-live on';
  };
  apiEvSource.onmessage = (ev) => {
    try { upsertApiCard(JSON.parse(ev.data)); }
    catch (e) { console.error('bad api event:', e); }
  };
  apiEvSource.onerror = () => {
    // 服务未启动 / 断线：EventSource 会自动重连
    apiLiveDot.textContent = '◐ 重连中';
    apiLiveDot.className = 'api-live';
  };
}

function trimApiCards() {
  const MAX = 100;   // 最多保留 100 张卡片，超出裁掉最旧的
  while (apiCardOrder.length > MAX) {
    const id = apiCardOrder.shift();
    const c = apiCards[id];
    if (c && c.el) c.el.remove();
    delete apiCards[id];
  }
}

function upsertApiCard(rec) {
  let card = apiCards[rec.id];
  const isNew = !card;
  if (isNew) {
    card = { id: rec.id, el: null, headEl: null, bodyEl: null, respEl: null, reasonEl: null };
    card.el = document.createElement('div');
    card.el.className = 'api-card';
    card.headEl = document.createElement('div');
    card.headEl.className = 'api-card-head';
    card.el.appendChild(card.headEl);
    card.bodyEl = document.createElement('div');
    card.bodyEl.className = 'api-card-body';
    card.el.appendChild(card.bodyEl);
    apiList.prepend(card.el);      // 新请求加在最前（反向增长）
    apiCards[rec.id] = card;
    apiCardOrder.push(rec.id);
    trimApiCards();
  }
  // 头部：API request | 时间 — 方法 路径 — 状态（耗时）[cache] [perf]
  const st = rec.status;
  let head = `API request | ${rec.ts || ''} — <b>${Common.escHtml(rec.method || '')} ${Common.escHtml(rec.path || '')}</b>`;
  if (st !== undefined && st !== null) {
    const cls = st >= 200 && st < 300 ? 's2xx' : (st === 0 || st >= 400 ? 's0' : '');
    head += ` <span class="api-status ${cls}">HTTP ${st}</span>`;
    if (rec.dur_s !== undefined) head += ` <span>(${rec.dur_s}s)</span>`;
  }
  if (rec.cache_n != null && rec.prompt_n) {
    const pct = Math.round(Math.min(rec.cache_n, rec.prompt_n) / rec.prompt_n * 100);
    head += ` <span class="api-cache">cache ${pct}%</span>`;
  }
  if (rec.perf) {
    const pf = rec.perf;
    const s = [];
    if (pf.ttft_s != null) s.push(`TTFT ${pf.ttft_s}s`);
    if (pf.pp_tps) s.push(`PP ${pf.pp_tps}`);
    if (pf.tg_tps) s.push(`TG ${pf.tg_tps}`);
    if (s.length) head += ` <span class="api-perf">${s.join(' ')}</span>`;
  }
  if (rec.error) head += ` <span class="api-error">${Common.escHtml(rec.error)}</span>`;
  card.headEl.innerHTML = head;

  // 空态提示移除（出现第一张卡片后）
  const empty = apiList.querySelector('.api-empty');
  if (empty) empty.remove();

  // 请求体（仅创建时写入一次）
  if (isNew) {
    const label = document.createElement('div');
    label.className = 'api-label';
    label.textContent = 'Request body:';
    card.bodyEl.appendChild(label);
    const pre = document.createElement('pre');
    pre.textContent = rec.body || '（无请求体）';
    card.bodyEl.appendChild(pre);
  }

  // 思考（reasoning）与输出（content）：流式过程逐次替换为最新累计文本
  if (rec.reasoning) {
    if (!card.reasonEl) {
      const label = document.createElement('div');
      label.className = 'api-label';
      label.textContent = 'Thinking:';
      card.bodyEl.appendChild(label);
      card.reasonEl = document.createElement('div');
      card.reasonEl.className = 'api-reasoning';
      card.bodyEl.appendChild(card.reasonEl);
    }
    card.reasonEl.textContent = rec.reasoning;
  }
  if (rec.text || rec.error) {
    if (!card.respEl) {
      const label = document.createElement('div');
      label.className = 'api-label';
      label.textContent = 'Response:';
      card.bodyEl.appendChild(label);
      card.respEl = document.createElement('div');
      card.respEl.className = 'api-response';
      card.bodyEl.appendChild(card.respEl);
    }
    card.respEl.textContent = rec.text || (rec.error ? `（${rec.error}）` : '');
  } else if (rec.status !== undefined && rec.status !== null && !rec.live) {
    if (!card.respEl) {
      const label = document.createElement('div');
      label.className = 'api-label';
      label.textContent = 'Response:';
      card.bodyEl.appendChild(label);
      card.respEl = document.createElement('div');
      card.respEl.className = 'api-response';
      card.bodyEl.appendChild(card.respEl);
    }
    card.respEl.textContent = '（空响应）';
  }
}

// 端口变更：清空并重连
Common.onPortChange(() => {
  apiCards = {};
  apiCardOrder = [];
  apiList.innerHTML = '<div class="api-empty">等待 API 请求…</div>';
  connectApiEvents();
});

// ===== 初始化 =====
connectApiEvents();
