// web/js/common.js — 外壳与子页面共享工具
// 端口协议：外壳负责读写 localStorage['max_port']，并通过 postMessage 广播给 iframe 子页面；
// 子页面监听 message（source==='max-shell'）与 storage 事件同步端口。
'use strict';

const Common = (() => {
  // 页面由后端自身提供：location.port 即后端真实端口，作为默认值，用户无需手动改端口。
  // 仅当非 http(s) 打开（如 file://）时才退回 localStorage / 8080。
  function detectDefaultPort() {
    if (window.location.protocol === 'http:' || window.location.protocol === 'https:') {
      const served = parseInt(window.location.port, 10);
      if (served) return served;
    }
    const saved = parseInt(localStorage.getItem('max_port'), 10);
    if (saved) return saved;
    return 8080;
  }

  let port = detectDefaultPort();
  const portChangeListeners = [];

  const getPort = () => port;
  const getBaseUrl = () => `http://127.0.0.1:${port}`;

  function setPort(p) {
    const n = parseInt(p, 10);
    if (!n || n === port) return;
    port = n;
    localStorage.setItem('max_port', String(n));
    portChangeListeners.forEach(fn => fn(n));
  }

  // 子页面注册端口变更回调（重新连接/刷新数据）
  function onPortChange(fn) {
    portChangeListeners.push(fn);
  }

  // 需要 toast 提示的请求（用户操作类）；失败时抛错并提示
  async function apiFetch(path, options = {}) {
    try {
      const resp = await fetch(getBaseUrl() + path, options);
      if (!resp.ok) {
        const errText = await resp.text().catch(() => '');
        throw new Error(`HTTP ${resp.status}: ${errText}`);
      }
      return await resp.json();
    } catch (e) {
      console.error('API error:', path, e);
      showToast('连接失败，请检查端口和后端是否运行');
      throw e;
    }
  }

  // 静默请求（轮询类）：失败返回 null，不弹 toast
  async function apiFetchSilent(path, options = {}) {
    try {
      const resp = await fetch(getBaseUrl() + path, options);
      if (!resp.ok) return null;
      return await resp.json();
    } catch (e) {
      return null;
    }
  }

  function showToast(msg) {
    let container = document.querySelector('.toast-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container';
      document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = msg;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
  }

  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function escapeHtml(text) {
    if (text == null) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function fmtBytes(n) {
    if (!n) return '0B';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + units[i];
  }

  function fmtNum(n) {
    if (n == null) return '—';
    return n.toLocaleString();
  }

  // 外壳广播端口变更 → 子页面
  window.addEventListener('message', (e) => {
    const d = e.data || {};
    if (d.source === 'max-shell' && d.type === 'port') {
      setPort(d.port);
    }
  });

  // 多标签页兜底（外壳与子页面在同一浏览器）
  window.addEventListener('storage', (e) => {
    if (e.key === 'max_port' && e.newValue) {
      setPort(parseInt(e.newValue, 10));
    }
  });

  return { getPort, getBaseUrl, onPortChange, apiFetch, apiFetchSilent, showToast, escHtml, escapeHtml, fmtBytes, fmtNum };
})();
