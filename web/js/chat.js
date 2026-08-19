// web/js/chat.js — 对话页：会话管理 + 流式对话 + 多模态上传
'use strict';

// ===== State =====
let currentSessionId = null;
let currentMessages = [];
let isGenerating = false;
let pendingMediaFiles = []; // {file, type, size, data}

const messagesContainer = document.getElementById('messagesContainer');
const messageInput = document.getElementById('messageInput');
const sendBtn = document.getElementById('sendBtn');
const mediaPreview = document.getElementById('mediaPreview');

// ===== 会话列表 =====
async function loadConversations() {
  try {
    const data = await Common.apiFetchSilent('/chat/all');
    renderConversationList(data);
  } catch (e) {
    console.error('Failed to load conversations:', e);
  }
}

function renderConversationList(convs) {
  const list = document.getElementById('convList');
  if (!convs || convs.length === 0) {
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-secondary);font-size:13px;">暂无对话</div>';
    return;
  }

  list.innerHTML = convs.map(c => `
    <div class="conv-item ${c.session_id === currentSessionId ? 'active' : ''}" onclick="loadConversation('${c.session_id}')">
      <span class="conv-title">${Common.escapeHtml(c.title || '新对话')}</span>
      <button class="conv-delete" onclick="event.stopPropagation(); deleteConversation('${c.session_id}')" title="删除">×</button>
    </div>
  `).join('');
}

async function createNewChat() {
  try {
    const data = await Common.apiFetch('/chat/new', { method: 'POST' });
    currentSessionId = data.session_id;
    currentMessages = [];
    document.getElementById('sessionTitle').textContent = '新对话';
    renderEmptyChat();
    loadConversations();
    closeChatSideMobile();
  } catch (e) {
    console.error('Failed to create chat:', e);
  }
}

async function loadConversation(sid) {
  try {
    const data = await Common.apiFetch(`/chat/get/${sid}`);
    if (!data) return;
    currentSessionId = sid;
    currentMessages = data.messages || [];
    document.getElementById('sessionTitle').textContent = data.title || '对话';
    renderMessages(currentMessages);
    loadConversations(); // refresh active state
    closeChatSideMobile();
  } catch (e) {
    console.error('Failed to load conversation:', e);
  }
}

async function deleteConversation(sid) {
  try {
    await Common.apiFetch(`/chat/delete/${sid}`, { method: 'DELETE' });
    if (currentSessionId === sid) {
      currentSessionId = null;
      currentMessages = [];
      document.getElementById('sessionTitle').textContent = '选择或新建对话';
      renderEmptyChat();
    }
    loadConversations();
  } catch (e) {
    console.error('Failed to delete:', e);
  }
}

// ===== 移动端会话侧栏 =====
function toggleChatSide() {
  document.getElementById('chatSide').classList.toggle('open');
}

function closeChatSideMobile() {
  document.getElementById('chatSide').classList.remove('open');
}

// ===== 消息渲染 =====
function renderEmptyChat() {
  messagesContainer.innerHTML = `
    <div class="empty-state">
      <div class="icon">💬</div>
      <h2>欢迎使用 AI-Qwen-Max</h2>
      <p>从左侧选择一个对话，或点击「新建对话」开始聊天。<br>支持上传图片和文档附件。</p>
    </div>`;
}

function renderMessages(msgs) {
  if (!msgs || msgs.length === 0) {
    renderEmptyChat();
    return;
  }

  let html = '';
  for (const msg of msgs) {
    const role = msg.role || 'user';
    const content = msg.content || '';
    const reasoningContent = msg.reasoning_content || '';

    if (role === 'user') {
      html += `
        <div class="message-row">
          <div class="msg-role user">用户</div>
          <div class="user-bubble">${renderUserContent(content)}</div>
        </div>`;
    } else if (role === 'assistant') {
      let thinkingHtml = '';
      if (reasoningContent) {
        const thinkingId = 'thinking_' + Math.random().toString(36).slice(2, 8);
        thinkingHtml = `
          <div class="thinking-block">
            <div class="thinking-header" onclick="toggleThinking('${thinkingId}')">
              <span class="arrow collapsed">▼</span>
              🧠 思考过程
            </div>
            <div class="thinking-content collapsed" id="${thinkingId}">${Common.escapeHtml(reasoningContent)}</div>
          </div>`;
      }
      html += `
        <div class="message-row">
          <div class="msg-role assistant">AI 助手</div>
          <div class="assistant-bubble">
            ${thinkingHtml}
            <div class="md-content">${renderMarkdown(content)}</div>
            ${formatMsgMeta(msg)}
          </div>
        </div>`;
    } else if (role === 'system') {
      html += `
        <div class="message-row">
          <div class="msg-role system">系统</div>
          <div class="assistant-bubble" style="max-width:100%;opacity:0.7;">${Common.escapeHtml(content)}</div>
        </div>`;
    }
  }

  messagesContainer.innerHTML = html;
  scrollToBottom();
}

function renderUserContent(content) {
  // OpenAI 多模态数组 content：text 段 + image_url（data URI）段
  if (Array.isArray(content)) {
    let html = '';
    for (const p of content) {
      if (!p || typeof p !== 'object') continue;
      if (p.type === 'text' && p.text) {
        html += Common.escapeHtml(p.text);
      } else if (p.type === 'image_url' && p.image_url && p.image_url.url && p.image_url.url.startsWith('data:')) {
        html += `<img src="${p.image_url.url}" alt="图片" style="max-width:100%;max-height:300px;border-radius:8px;margin-top:8px;display:block;">`;
      }
    }
    return html;
  }
  return Common.escapeHtml(content || '');
}

// ===== 回复元数据（发起时间/模型/性能/缓存命中，风格对齐 CLI 性能行） =====
function formatMsgMeta(msg) {
  const bits = [];
  if (msg.created_at) bits.push('⏱ ' + msg.created_at);
  if (msg.model) bits.push('🤖 ' + msg.model);
  const p = msg.perf || {};
  if (p.ttft_s != null) bits.push('TTFT ' + p.ttft_s + 's');
  if (p.pp_tps) bits.push('PP ' + p.pp_tps + ' t/s');
  if (p.tg_tps) bits.push('TG ' + p.tg_tps + ' t/s');
  if (p.dur_s != null) bits.push('总耗时 ' + p.dur_s + 's');
  const c = msg.cache;
  if (c && c.total) {
    bits.push('cache ' + c.pct + '% (' + Common.fmtNum(c.hits) + '/' + Common.fmtNum(c.total) + ')');
  }
  return bits.length ? `<div class="msg-meta">${Common.escHtml(bits.join('  │  '))}</div>` : '';
}

// ===== Thinking 折叠（渲染结果经 onclick 调用，保持全局） =====
function toggleThinking(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const arrow = el.previousElementSibling.querySelector('.arrow');
  if (el.classList.contains('collapsed')) {
    el.classList.remove('collapsed');
    arrow.classList.remove('collapsed');
  } else {
    el.classList.add('collapsed');
    arrow.classList.add('collapsed');
  }
}

// ===== Markdown 渲染 =====
function renderMarkdown(text) {
  if (!text) return '';
  let html = Common.escapeHtml(text);

  // Code blocks (``` ... ```)
  html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    const langLabel = lang ? `<span style="color:#666;font-size:12px;">${lang}</span>\n` : '';
    return `<pre>${langLabel}<code>${code.trim()}</code></pre>`;
  });

  // Inline code (` ... `)
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Headers
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Bold and italic
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

  // Blockquotes
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');

  // Line breaks into paragraphs
  html = html.replace(/\n\n/g, '</p><p>');
  html = '<p>' + html + '</p>';
  html = html.replace(/<\/p>\s*<h([1-3])>/g, '</p><h$1>');
  html = html.replace(/<\/h([1-3]>)\s*<p>/g, '</h$1><p>');

  return html;
}

// ===== 滚动 =====
function scrollToBottom() {
  requestAnimationFrame(() => {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  });
}

// ===== 文件上传 =====
async function handleFileSelect(event) {
  const files = Array.from(event.target.files);
  if (!files.length) return;

  for (const file of files) {
    const reader = new FileReader();
    const data = await new Promise((resolve, reject) => {
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });

    pendingMediaFiles.push({
      name: file.name,
      type: file.type,
      size: file.size,
      data: data
    });
  }

  renderMediaPreview();
  event.target.value = ''; // reset input
}

function renderMediaPreview() {
  if (!pendingMediaFiles.length) {
    mediaPreview.innerHTML = '';
    return;
  }

  mediaPreview.innerHTML = pendingMediaFiles.map((f, i) => `
    <div class="media-preview-item">
      ${f.type.startsWith('image/') ? `<img src="${f.data}" alt="${Common.escapeHtml(f.name)}">` : '📄'}
      <span>${Common.escapeHtml(f.name)}</span>
      <span class="remove-media" onclick="removePendingMedia(${i})">×</span>
    </div>
  `).join('');
}

function removePendingMedia(index) {
  pendingMediaFiles.splice(index, 1);
  renderMediaPreview();
}

// ===== 发送消息（流式） =====
async function sendMessage() {
  if (!currentSessionId || isGenerating) return;

  const text = messageInput.value.trim();
  if (!text && !pendingMediaFiles.length) return;

  // 先上传媒体文件（存到会话的 media/ 目录）
  for (const mf of pendingMediaFiles) {
    try {
      await Common.apiFetch(`/chat/${currentSessionId}/media?filename=${encodeURIComponent(mf.name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: dataUrlToBlob(mf.data)
      });
    } catch (e) {
      console.error('Media upload failed:', e);
    }
  }

  // 清空输入框（媒体保留到下方 parts 构建完）
  messageInput.value = '';
  messageInput.style.height = 'auto';

  // 多模态：content 用数组（text + image_url data URI，OpenAI 格式），后端落盘保留数组，
  // 多轮对话中图片留在历史里、模型可见；非图片附件仅存档（模型不可见）。
  const parts = [];
  if (text) parts.push({ type: 'text', text: text });
  for (const mf of pendingMediaFiles) {
    if (mf.type && mf.type.startsWith('image/')) {
      parts.push({ type: 'image_url', image_url: { url: mf.data } });
    }
  }
  // parts 消费完后再清空待发媒体
  pendingMediaFiles = [];
  renderMediaPreview();
  const userMsg = { role: 'user', content: parts.length ? parts : text };
  currentMessages.push(userMsg);
  renderMessages(currentMessages);

  // 调流式 API（后端通过 X-Conversation-Id 自动落盘）
  await streamResponse(userMsg);
}

async function streamResponse(userText) {
  isGenerating = true;
  sendBtn.disabled = true;
  sendBtn.textContent = '生成中...';

  // 创建 assistant 占位气泡用于流式更新
  const assistantMsgDiv = document.createElement('div');
  assistantMsgDiv.className = 'message-row';
  assistantMsgDiv.innerHTML = `
    <div class="msg-role assistant">AI 助手</div>
    <div class="assistant-bubble">
      <div id="streamContent"></div>
    </div>`;
  messagesContainer.appendChild(assistantMsgDiv);

  let reasoningContent = '';
  let content = '';
  const thinkingId = 'thinking_' + Math.random().toString(36).slice(2, 8);

  try {
    // 请求体 = 完整历史（含用户最新消息）；后端凭 X-Conversation-Id 自动保存。
    // 全量回传消息对象（含 created_at/model/perf/cache 元数据），
    // 后端 merge_request 以本历史为基准，元数据借此在跨轮对话中留存。
    const reqMessages = currentMessages.map(m => ({ ...m }));

    const resp = await fetch(Common.getBaseUrl() + '/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Conversation-Id': currentSessionId
      },
      body: JSON.stringify({
        model: 'default',
        messages: reqMessages,
        stream: true
      })
    });

    if (!resp.ok) {
      const errBody = await resp.text().catch(() => '');
      throw new Error(`HTTP ${resp.status}: ${errBody}`);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let streamDone = false;

    while (!streamDone) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const dataStr = line.slice(6).trim();
        if (dataStr === '[DONE]') { streamDone = true; break; }

        try {
          const json = JSON.parse(dataStr);
          const delta = json.choices?.[0]?.delta || {};

          if (delta.reasoning_content) {
            reasoningContent += delta.reasoning_content;
          }
          if (delta.content) {
            content += delta.content;
          }

          updateStreamingDisplay(content, reasoningContent, thinkingId);
          scrollToBottom();
        } catch (e) {
          // Skip malformed SSE lines
        }
      }
    }

  } catch (e) {
    console.error('Stream error:', e);
    content += '\n\n[连接错误: ' + e.message + ']';
    updateStreamingDisplay(content, reasoningContent, thinkingId);
  } finally {
    isGenerating = false;
    sendBtn.disabled = false;
    sendBtn.textContent = '发送';

    // 从服务端重载（后端已通过 X-Conversation-Id 保存）
    try {
      const data = await Common.apiFetchSilent(`/chat/get/${currentSessionId}`);
      if (data) {
        currentMessages = data.messages || [];
        renderMessages(currentMessages);
      } else {
        throw new Error('empty');
      }
    } catch (e) {
      // 兜底：本地更新
      currentMessages.push({
        role: 'assistant',
        content: content,
        ...(reasoningContent ? { reasoning_content: reasoningContent } : {})
      });
      renderMessages(currentMessages);
    }
  }
}

function updateStreamingDisplay(content, reasoningContent, thinkingId) {
  const streamEl = document.getElementById('streamContent');
  if (!streamEl) return;

  let html = '';

  // 思考块（可折叠）
  if (reasoningContent) {
    html += `
      <div class="thinking-block">
        <div class="thinking-header" onclick="toggleThinking('${thinkingId}')">
          <span class="arrow collapsed">▼</span>
          🧠 思考过程 (${reasoningContent.length} chars)
        </div>
        <div class="thinking-content collapsed" id="${thinkingId}">${Common.escapeHtml(reasoningContent)}</div>
      </div>`;
  }

  // Markdown 正文
  if (content) {
    html += `<div class="md-content">${renderMarkdown(content)}</div>`;
  }

  // 首 token 未到时的打字指示器
  if (!content && !reasoningContent) {
    html += `<div class="typing-indicator"><span></span><span></span><span></span></div>`;
  }

  // 用 innerHTML 更新容器内容（保留 id="streamContent" 容器本身）
  streamEl.innerHTML = html;
}

// ===== 工具 =====
function dataUrlToBlob(dataUrl) {
  const parts = dataUrl.split(',');
  const header = parts[0];
  const base64 = parts[1];
  const mimeMatch = header.match(/data\/([^;]+)/);
  const mimeType = mimeMatch ? mimeMatch[1] : 'application/octet-stream';

  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}

// ===== 输入框行为 =====
messageInput.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 200) + 'px';
});

messageInput.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// ===== 端口变更：重新加载会话 =====
Common.onPortChange(() => {
  currentSessionId = null;
  currentMessages = [];
  pendingMediaFiles = [];
  document.getElementById('sessionTitle').textContent = '选择或新建对话';
  renderEmptyChat();
  loadConversations();
});

// ===== 初始化 =====
(function init() {
  loadConversations();
})();
