"""ACP agent bridge server for `toad serve --new-ui <agent>`.

Spawns an ACP agent subprocess and serves a web chat UI over HTTP/WebSocket.
The aiohttp server duck-types as a proc for heroku_tunnel.SessionData so
the existing tunnel machinery needs no changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("toad.heroku_agent_bridge")


# ---------------------------------------------------------------------------
# Inline chat HTML/CSS/JS (no build step — served directly)
# ---------------------------------------------------------------------------

CHAT_UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Toad</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #1a1a2e;
  --surface: #16213e;
  --surface2: #0f3460;
  --accent: #e94560;
  --accent2: #533483;
  --text: #eaeaea;
  --text-muted: #888;
  --user-bg: #0f3460;
  --agent-bg: #16213e;
  --tool-bg: #1e1e3a;
  --border: #2a2a4a;
  --green: #4ade80;
  --red: #f87171;
  --yellow: #fbbf24;
  --blue: #60a5fa;
}
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 15px; }
#app { display: flex; flex-direction: column; height: 100%; max-width: 900px; margin: 0 auto; }
#status-bar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 8px 16px; display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-muted); flex-shrink: 0; }
#status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); transition: background 0.3s; }
#status-dot.connected { background: var(--green); }
#status-dot.error { background: var(--red); }
#messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
.msg { max-width: 85%; }
.msg.user { align-self: flex-end; }
.msg.assistant { align-self: flex-start; }
.msg.system { align-self: center; }
.bubble { padding: 10px 14px; border-radius: 12px; line-height: 1.5; }
.user .bubble { background: var(--user-bg); border-bottom-right-radius: 4px; }
.assistant .bubble { background: var(--agent-bg); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
.system .bubble { background: transparent; color: var(--text-muted); font-size: 13px; font-style: italic; }
.bubble p { margin: 0 0 8px; } .bubble p:last-child { margin-bottom: 0; }
.bubble code { background: rgba(0,0,0,0.3); padding: 1px 4px; border-radius: 3px; font-family: monospace; font-size: 13px; }
.bubble pre { background: rgba(0,0,0,0.3); padding: 10px; border-radius: 6px; overflow-x: auto; margin: 8px 0; }
.bubble pre code { background: none; padding: 0; }
details.thinking { margin: 6px 0; }
details.thinking summary { cursor: pointer; color: var(--text-muted); font-size: 13px; user-select: none; }
details.thinking summary:hover { color: var(--text); }
.thinking-content { padding: 8px 12px; margin-top: 4px; border-left: 2px solid var(--accent2); color: var(--text-muted); font-size: 13px; font-style: italic; white-space: pre-wrap; }
.tool-card { background: var(--tool-bg); border: 1px solid var(--border); border-radius: 8px; margin: 6px 0; overflow: hidden; font-size: 13px; }
.tool-header { display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; user-select: none; }
.tool-header:hover { background: rgba(255,255,255,0.04); }
.tool-icon { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.status-pending .tool-icon { background: var(--yellow); }
.status-in_progress .tool-icon { background: var(--blue); animation: pulse 1s infinite; }
.status-completed .tool-icon { background: var(--green); }
.status-failed .tool-icon { background: var(--red); }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
.tool-title { flex: 1; color: var(--text); font-weight: 500; }
.tool-status-badge { font-size: 11px; color: var(--text-muted); text-transform: uppercase; }
.tool-body { padding: 0 12px 10px; display: none; }
.tool-card.open .tool-body { display: block; }
.diff-file { font-size: 12px; color: var(--text-muted); margin-bottom: 6px; }
.diff-block { font-family: monospace; font-size: 12px; line-height: 1.6; overflow-x: auto; }
.diff-line { white-space: pre; padding: 0 4px; }
.diff-add { background: rgba(74,222,128,0.12); color: var(--green); }
.diff-remove { background: rgba(248,113,113,0.12); color: var(--red); }
.diff-meta { color: var(--text-muted); }
.plan-list { list-style: none; padding: 0; }
.plan-entry { display: flex; gap: 8px; align-items: flex-start; padding: 4px 0; font-size: 13px; }
.plan-bullet { flex-shrink: 0; margin-top: 2px; }
.plan-pending .plan-bullet::before { content: '○'; color: var(--text-muted); }
.plan-in_progress .plan-bullet::before { content: '◉'; color: var(--blue); }
.plan-completed .plan-bullet::before { content: '●'; color: var(--green); }
.permission-card { background: var(--tool-bg); border: 1px solid var(--accent); border-radius: 8px; padding: 12px; margin: 6px 0; }
.tool-result { background: var(--tool-bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin: 6px 0; font-size: 13px; }
.tool-result pre { margin: 0; font-family: monospace; font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-all; color: var(--text); }
.permission-title { font-size: 13px; font-weight: 600; margin-bottom: 10px; }
.permission-buttons { display: flex; flex-wrap: wrap; gap: 8px; }
.perm-btn { padding: 6px 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface2); color: var(--text); font-size: 13px; cursor: pointer; transition: background 0.15s; }
.perm-btn:hover { background: var(--accent2); border-color: var(--accent2); }
.perm-btn.allow { border-color: var(--green); }
.perm-btn.allow:hover { background: rgba(74,222,128,0.2); }
.perm-btn.reject { border-color: var(--red); }
.perm-btn.reject:hover { background: rgba(248,113,113,0.2); }
#input-area { border-top: 1px solid var(--border); padding: 12px 16px; background: var(--surface); flex-shrink: 0; }
#input-row { display: flex; gap: 8px; align-items: flex-end; }
#msg-input { flex: 1; background: var(--tool-bg); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 14px; padding: 10px 14px; resize: none; outline: none; min-height: 44px; max-height: 200px; line-height: 1.5; font-family: inherit; }
#msg-input:focus { border-color: var(--accent2); }
#msg-input:disabled { opacity: 0.5; }
.btn { padding: 10px 18px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 500; transition: background 0.15s; }
#send-btn { background: var(--accent2); color: var(--text); }
#send-btn:hover:not(:disabled) { background: #6040a0; }
#send-btn:disabled { opacity: 0.4; cursor: default; }
#cancel-btn { background: transparent; color: var(--text-muted); border: 1px solid var(--border); display: none; }
#cancel-btn:hover { color: var(--text); border-color: var(--text-muted); }
#cancel-btn.active { display: inline-block; }
.error-msg { color: var(--red); font-size: 13px; }
@media (max-width: 600px) {
  .msg { max-width: 95%; }
  #input-area { padding: 8px 12px; }
  .btn { padding: 9px 14px; }
}
</style>
</head>
<body>
<div id="app">
  <div id="status-bar">
    <div id="status-dot"></div>
    <span id="status-text">Connecting…</span>
  </div>
  <div id="messages"></div>
  <div id="input-area">
    <div id="input-row">
      <textarea id="msg-input" placeholder="Type a message… (Enter to send, Shift+Enter for newline)" rows="1" disabled></textarea>
      <button class="btn" id="cancel-btn">Cancel</button>
      <button class="btn" id="send-btn" disabled>Send</button>
    </div>
  </div>
</div>
<script>
(function() {
'use strict';
const $ = id => document.getElementById(id);
const messagesEl = $('messages');
const inputEl = $('msg-input');
const sendBtn = $('send-btn');
const cancelBtn = $('cancel-btn');
const statusDot = $('status-dot');
const statusText = $('status-text');

let ws = null;
let retryDelay = 1000;
let everConnected = false;    // true after the very first agent_ready
let currentAssistantBubble = null;
let currentThinkingEl = null;
let currentAssistantText = '';
let currentThinkingText = '';
let isAgentBusy = false;
const toolCards = {};         // toolCallId → card element
const permissionCards = {};   // id → card element

function setStatus(label, cls) {
  statusText.textContent = label;
  statusDot.className = cls || '';
}

function scrollBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function mkEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}

function addMessage(role, html, isHtml) {
  const wrap = mkEl('div', `msg ${role}`);
  const bubble = mkEl('div', 'bubble');
  if (isHtml) bubble.innerHTML = html;
  else bubble.textContent = html;
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  scrollBottom();
  return bubble;
}

function startAssistantBubble() {
  if (currentAssistantBubble) return;
  const wrap = mkEl('div', 'msg assistant');
  const bubble = mkEl('div', 'bubble');
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  currentAssistantBubble = bubble;
  currentAssistantText = '';
  scrollBottom();
}

function ensureThinking() {
  if (currentThinkingEl) return;
  startAssistantBubble();
  const details = mkEl('details', 'thinking');
  details.innerHTML = '<summary>Thinking…</summary>';
  const content = mkEl('div', 'thinking-content');
  details.appendChild(content);
  currentAssistantBubble.appendChild(details);
  currentThinkingEl = content;
  currentThinkingText = '';
}

function finalizeAssistant() {
  if (currentAssistantBubble && currentAssistantText) {
    try {
      currentAssistantBubble.innerHTML = marked.parse(currentAssistantText);
    } catch(e) {
      currentAssistantBubble.textContent = currentAssistantText;
    }
  }
  currentAssistantBubble = null;
  currentAssistantText = '';
  currentThinkingEl = null;
  currentThinkingText = '';
}

function renderDiff(content) {
  const div = mkEl('div');
  for (const item of content) {
    if (item.type === 'diff') {
      const fileEl = mkEl('div', 'diff-file', item.path || '');
      div.appendChild(fileEl);
      const blockEl = mkEl('div', 'diff-block');
      const oldLines = (item.oldText || '').split('\n');
      const newLines = (item.newText || '').split('\n');
      // Simple diff: show removed lines then added lines
      for (const l of oldLines) {
        const ln = mkEl('div', 'diff-line diff-remove', '- ' + l);
        blockEl.appendChild(ln);
      }
      for (const l of newLines) {
        const ln = mkEl('div', 'diff-line diff-add', '+ ' + l);
        blockEl.appendChild(ln);
      }
      div.appendChild(blockEl);
    } else if (item.type === 'content' && item.content) {
      const textEl = mkEl('div');
      textEl.style.cssText = 'font-size:12px;color:var(--text-muted);margin-top:6px;white-space:pre-wrap;';
      textEl.textContent = item.content.text || '';
      div.appendChild(textEl);
    }
  }
  return div;
}

function extractRawText(rawOutput) {
  if (!rawOutput) return '';
  if (typeof rawOutput === 'string') return rawOutput;
  if (Array.isArray(rawOutput)) {
    return rawOutput.filter(x => x && x.type === 'text' && x.text).map(x => x.text).join('');
  }
  if (typeof rawOutput === 'object' && rawOutput.output) return String(rawOutput.output);
  return '';
}

function upsertToolCard(id, title, kind, status, content, rawOutput) {
  let card = toolCards[id];
  if (!card) {
    card = mkEl('div', 'tool-card');
    card.dataset.id = id;
    const header = mkEl('div', 'tool-header');
    header.innerHTML = `<div class="tool-icon"></div><div class="tool-title"></div><div class="tool-status-badge"></div><div style="font-size:11px;color:var(--text-muted);margin-left:4px">▾</div>`;
    const body = mkEl('div', 'tool-body');
    card.appendChild(header);
    card.appendChild(body);
    header.addEventListener('click', () => card.classList.toggle('open'));
    toolCards[id] = card;
    startAssistantBubble();
    currentAssistantBubble.appendChild(card);
    scrollBottom();
  }
  const s = status || 'pending';
  card.className = `tool-card status-${s}`;
  const titleEl = card.querySelector('.tool-title');
  const badgeEl = card.querySelector('.tool-status-badge');
  if (title) titleEl.textContent = title;
  badgeEl.textContent = s;

  if (s === 'completed' || s === 'failed') {
    const permCard = permissionCards[id];
    if (permCard) {
      // Keep tool card collapsed, show result BELOW the permission card.
      const body = card.querySelector('.tool-body');
      body.innerHTML = '';
      card.classList.remove('open');

      let resultDiv = document.querySelector(`.tool-result[data-id="${id}"]`);
      if (!resultDiv) {
        resultDiv = mkEl('div', 'tool-result');
        resultDiv.dataset.id = id;
        permCard.insertAdjacentElement('afterend', resultDiv);
      }
      resultDiv.innerHTML = '';
      const text = extractRawText(rawOutput);
      if (text) {
        const pre = mkEl('pre');
        pre.textContent = text;
        resultDiv.appendChild(pre);
      } else if (content && content.length) {
        resultDiv.appendChild(renderDiff(content));
      }
    } else {
      if (content && content.length) {
        const body = card.querySelector('.tool-body');
        body.innerHTML = '';
        body.appendChild(renderDiff(content));
        card.classList.add('open');
      }
    }
  } else if (content && content.length) {
    const body = card.querySelector('.tool-body');
    body.innerHTML = '';
    body.appendChild(renderDiff(content));
  }
  scrollBottom();
}

function showPermission(id, title, options) {
  if (permissionCards[id]) return;
  // Close the current assistant bubble so any post-permission agent text
  // starts a new bubble that appears AFTER the permission card.
  finalizeAssistant();
  const card = mkEl('div', 'permission-card');
  const titleEl = mkEl('div', 'permission-title', title || 'Permission required');
  card.appendChild(titleEl);
  const btns = mkEl('div', 'permission-buttons');
  for (const opt of options) {
    const btn = mkEl('button', 'perm-btn', opt.name);
    const k = opt.kind || '';
    if (k.startsWith('allow')) btn.classList.add('allow');
    else if (k.startsWith('reject')) btn.classList.add('reject');
    btn.addEventListener('click', () => {
      resolvePermission(id, opt.optionId, opt.name, opt.kind);
      card.style.opacity = '0.5';
      card.querySelectorAll('button').forEach(b => b.disabled = true);
    });
    btns.appendChild(btn);
  }
  card.appendChild(btns);
  permissionCards[id] = card;
  messagesEl.appendChild(card);
  scrollBottom();
}

function resolvePermission(id, optionId, name, kind) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'permission_response', id, option_id: optionId, text: name }));
  }
}

function setAgentBusy(busy) {
  isAgentBusy = busy;
  inputEl.disabled = busy;
  sendBtn.disabled = busy;
  if (busy) {
    cancelBtn.classList.add('active');
  } else {
    cancelBtn.classList.remove('active');
  }
}

function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN || isAgentBusy) return;
  ws.send(JSON.stringify({ type: 'message', text }));
  inputEl.value = '';
  inputEl.style.height = 'auto';
  setAgentBusy(true);
}

function handleMessage(data) {
  switch (data.type) {
    case 'agent_ready':
      everConnected = true;
      setStatus('Connected', 'connected');
      setAgentBusy(false);
      break;

    case 'user_text':
      finalizeAssistant();
      addMessage('user', data.text);
      break;

    case 'text_chunk':
      if (data.kind === 'thinking') {
        ensureThinking();
        currentThinkingText += data.text;
        currentThinkingEl.textContent = currentThinkingText;
      } else {
        startAssistantBubble();
        currentAssistantText += data.text;
        // Show streaming text raw, will render markdown on done
        currentAssistantBubble.textContent = currentAssistantText;
      }
      scrollBottom();
      break;

    case 'tool_call':
      upsertToolCard(data.id, data.title, data.kind, data.status || 'pending', data.content, data.rawOutput);
      break;

    case 'tool_call_update':
      upsertToolCard(data.id, data.title, data.kind, data.status, data.content, data.rawOutput);
      break;

    case 'plan': {
      const ul = mkEl('ul', 'plan-list');
      for (const e of (data.entries || [])) {
        const li = mkEl('li', `plan-entry plan-${e.status || 'pending'}`);
        li.innerHTML = `<span class="plan-bullet"></span><span>${e.content || ''}</span>`;
        ul.appendChild(li);
      }
      startAssistantBubble();
      currentAssistantBubble.appendChild(ul);
      scrollBottom();
      break;
    }

    case 'permission_request':
      showPermission(data.id, data.title, data.options || []);
      break;

    case 'done':
      finalizeAssistant();
      setAgentBusy(false);
      break;

    case 'error':
      finalizeAssistant();
      addMessage('system', data.message || 'Unknown error');
      setAgentBusy(false);
      break;
  }
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // Build the correct WS path relative to the current page.
  // The relay routes /{endpoint}/{session_id}/ws as the WebSocket endpoint.
  // location.pathname is /{endpoint}/{session_id}/ (with trailing slash).
  const base = location.pathname.replace(/\/?$/, '/');
  ws = new WebSocket(`${proto}//${location.host}${base}ws`);

  ws.addEventListener('open', () => {
    retryDelay = 1000;
    if (everConnected) {
      setStatus('Reconnecting…', '');
    } else {
      setStatus('Starting agent…', '');
    }
    // Always send reconnect so the server can immediately confirm agent state
    // if the agent is already running (e.g. after a page refresh).
    ws.send(JSON.stringify({ type: 'reconnect' }));
  });

  ws.addEventListener('message', e => {
    try {
      handleMessage(JSON.parse(e.data));
    } catch(err) {
      console.error('Bad message', err);
    }
  });

  ws.addEventListener('close', () => {
    const delay = retryDelay;
    retryDelay = Math.min(retryDelay * 2, 30000);
    setStatus(`Disconnected — reconnecting in ${delay/1000}s…`, 'error');
    setAgentBusy(true);
    setTimeout(connect, delay);
  });

  ws.addEventListener('error', () => {
    ws.close();
  });
}

// Input auto-resize
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
});

inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

sendBtn.addEventListener('click', sendMessage);

cancelBtn.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'cancel' }));
  }
});

connect();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# ToolState-compatible dataclass (mirrors toad.widgets.terminal_tool.ToolState)
# ---------------------------------------------------------------------------


@dataclass
class TerminalState:
    output: str
    truncated: bool
    return_code: int | None = None
    signal: str | None = None


# ---------------------------------------------------------------------------
# TerminalProcess — wraps an asyncio subprocess, collects output
# ---------------------------------------------------------------------------


class TerminalProcess:
    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        output_byte_limit: int,
    ) -> None:
        self._proc = proc
        self._output_byte_limit = output_byte_limit
        self._buffer = bytearray()
        self._truncated = False
        self._exit_event = asyncio.Event()
        self.return_code: int | None = None
        self.signal: str | None = None
        self._read_task = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        async def _drain(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                remaining = self._output_byte_limit - len(self._buffer)
                if remaining <= 0:
                    self._truncated = True
                else:
                    self._buffer.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        self._truncated = True

        await asyncio.gather(
            _drain(self._proc.stdout),
            _drain(self._proc.stderr),
        )
        self.return_code = await self._proc.wait()
        self._exit_event.set()

    @property
    def output(self) -> str:
        return self._buffer.decode("utf-8", errors="replace")

    @property
    def truncated(self) -> bool:
        return self._truncated

    def get_state(self) -> TerminalState:
        return TerminalState(
            output=self.output,
            truncated=self.truncated,
            return_code=self.return_code,
            signal=self.signal,
        )

    async def wait_for_exit(self) -> tuple[int, str | None]:
        await self._exit_event.wait()
        return (self.return_code or 0, self.signal)

    def kill(self) -> None:
        try:
            self._proc.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TerminalManager — dict[str, TerminalProcess] + helpers
# ---------------------------------------------------------------------------


class TerminalManager:
    def __init__(self) -> None:
        self._terminals: dict[str, TerminalProcess] = {}

    async def create(
        self,
        terminal_id: str,
        command: str,
        args: list[str] | None,
        cwd: str | None,
        env: dict[str, str] | None,
        output_byte_limit: int | None,
    ) -> bool:
        import shlex
        PIPE = asyncio.subprocess.PIPE
        merged_env = os.environ.copy()
        # Ensure standard system paths are always present even if the agent
        # sends a minimal env with no PATH.
        standard_paths = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env_path = merged_env.get("PATH", "")
        for path in standard_paths.split(":"):
            if path not in env_path.split(":"):
                env_path = f"{env_path}:{path}" if env_path else path
        merged_env["PATH"] = env_path
        if env:
            merged_env.update(env)

        # Build argv: when the agent sends "ls -la" as a single command string
        # with no separate args, shlex.split parses it into ["ls", "-la"] so
        # we can exec directly without a shell wrapper.
        if args:
            argv = [command] + list(args)
        else:
            argv = shlex.split(command)

        log.debug("TerminalManager.create argv=%r cwd=%r", argv, cwd)

        limit = output_byte_limit or (5 * 1024 * 1024)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=PIPE,
                stderr=PIPE,
                cwd=cwd,
                env=merged_env,
            )
        except Exception as exc:
            log.warning("TerminalManager.create failed: %s", exc)
            return False

        self._terminals[terminal_id] = TerminalProcess(proc, limit)
        return True

    def get_state(self, terminal_id: str) -> TerminalState:
        tp = self._terminals.get(terminal_id)
        if tp is None:
            return TerminalState(output="", truncated=False, return_code=None)
        return tp.get_state()

    async def wait_for_exit(self, terminal_id: str) -> tuple[int, str | None]:
        tp = self._terminals.get(terminal_id)
        if tp is None:
            return (0, None)
        return await tp.wait_for_exit()

    def kill(self, terminal_id: str) -> None:
        tp = self._terminals.get(terminal_id)
        if tp is not None:
            tp.kill()

    def release(self, terminal_id: str) -> None:
        self._terminals.pop(terminal_id, None)


# ---------------------------------------------------------------------------
# WebMessageTarget — duck-typed substitute for Textual MessagePump
# ---------------------------------------------------------------------------


class WebMessageTarget:
    """Routes ACP agent messages to either the TerminalManager or the event queue."""

    def __init__(self, terminal_manager: TerminalManager) -> None:
        self._tm = terminal_manager
        self._event_queue: asyncio.Queue[Any] = asyncio.Queue()
        # tool_call_id → (future, {option_id: kind})
        self._permission_futures: dict[str, tuple[asyncio.Future, dict[str, str]]] = {}

    def post_message(self, message: Any) -> bool:
        asyncio.ensure_future(self._handle_message(message))
        return True

    def call_later(self, callback: Any, *args: Any) -> None:
        result = callback(*args)
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)

    async def _handle_message(self, msg: Any) -> None:
        # Import here to avoid circular imports at module load time
        from toad.acp import messages

        if isinstance(msg, messages.CreateTerminal):
            ok = await self._tm.create(
                msg.terminal_id,
                msg.command,
                msg.args,
                msg.cwd,
                dict(msg.env) if msg.env else None,
                msg.output_byte_limit,
            )
            if not msg.result_future.done():
                msg.result_future.set_result(ok)

        elif isinstance(msg, messages.GetTerminalState):
            state = self._tm.get_state(msg.terminal_id)
            if not msg.result_future.done():
                msg.result_future.set_result(state)

        elif isinstance(msg, messages.WaitForTerminalExit):
            result = await self._tm.wait_for_exit(msg.terminal_id)
            if not msg.result_future.done():
                msg.result_future.set_result(result)

        elif isinstance(msg, messages.KillTerminal):
            self._tm.kill(msg.terminal_id)

        elif isinstance(msg, messages.ReleaseTerminal):
            self._tm.release(msg.terminal_id)

        elif isinstance(msg, messages.RequestPermission):
            tool_call_id = msg.tool_call.get("toolCallId", "")
            kinds = {opt["optionId"]: opt.get("kind") for opt in msg.options}
            self._permission_futures[tool_call_id] = (msg.result_future, kinds)
            # Put event on queue (without the future itself)
            await self._event_queue.put(msg)

        else:
            await self._event_queue.put(msg)


# ---------------------------------------------------------------------------
# AgentBridgeSession — one per WebSocket connection
# ---------------------------------------------------------------------------


class AgentBridgeSession:
    # How long to keep the agent alive after the browser disconnects.
    DETACH_TIMEOUT = 300.0  # seconds

    def __init__(
        self,
        ws: Any,
        agent_name: str,
        project_root: Path,
        session_id: str = "",
        on_expire: Any = None,
    ) -> None:
        self._ws = ws
        self._agent_name = agent_name
        self._project_root = project_root
        self._session_id = session_id
        self._on_expire = on_expire  # callable(session_id) when agent is stopped
        self._agent: Any = None
        self._agent_started = False  # True after AgentReady received
        self._message_target: WebMessageTarget | None = None
        self._tm = TerminalManager()
        self._prompt_tasks: set[asyncio.Task] = set()
        self._expiry_task: asyncio.Task | None = None
        self._expired = False

    @property
    def is_expired(self) -> bool:
        return self._expired

    async def run(self) -> None:
        """Start the agent and service the first WS connection."""
        started = await self._start_agent()
        if not started:
            return
        await self._run_loops()
        # WS closed — keep agent alive, start expiry countdown.
        self._expiry_task = asyncio.ensure_future(self._expire_after(self.DETACH_TIMEOUT))

    async def reconnect(self, ws: Any) -> None:
        """Attach a new WebSocket to this running session (browser reconnected)."""
        # Cancel any pending expiry timer.
        if self._expiry_task and not self._expiry_task.done():
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
        self._expiry_task = None

        self._ws = ws

        # Drain messages that piled up while disconnected — start fresh.
        if self._message_target is not None:
            q = self._message_target._event_queue
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

        # Tell the browser the agent is ready.
        if self._agent_started:
            try:
                await ws.send_json({"type": "agent_ready"})
            except Exception:
                pass

        await self._run_loops()
        # WS closed again — restart expiry.
        self._expiry_task = asyncio.ensure_future(self._expire_after(self.DETACH_TIMEOUT))

    async def _run_loops(self) -> None:
        """Run the read/send loops until the current WS closes."""
        assert self._message_target is not None

        read_task = asyncio.ensure_future(self._read_from_ws())
        send_task = asyncio.ensure_future(self._send_events_to_ws())

        done, pending = await asyncio.wait(
            [read_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Cancel any in-flight _do_prompt tasks so they don't run as zombies.
        for task in list(self._prompt_tasks):
            task.cancel()
        if self._prompt_tasks:
            await asyncio.gather(*self._prompt_tasks, return_exceptions=True)

    async def _expire_after(self, seconds: float) -> None:
        """Stop the agent after `seconds` of inactivity (no browser reconnect)."""
        await asyncio.sleep(seconds)
        self._expired = True
        log.info("[bridge] session %s expired after %.0fs with no reconnect", self._session_id, seconds)
        if self._agent is not None:
            try:
                await self._agent.stop()
            except Exception:
                pass
        if self._on_expire:
            self._on_expire(self._session_id)

    async def _start_agent(self) -> bool:
        from toad.cli import get_agent_data
        from toad.agent import AgentReady, AgentFail
        # messages must be imported before agent to resolve the circular import:
        # messages→agent works (agent sees messages already in sys.modules);
        # agent→messages fails (messages tries to import Mode from unfinished agent).
        from toad.acp import messages as _  # noqa: F401
        from toad.acp.agent import Agent

        agent_data = await get_agent_data(self._agent_name)
        if agent_data is None:
            try:
                await self._ws.send_json({
                    "type": "error",
                    "message": f"Agent '{self._agent_name}' not found. "
                               "Make sure it is installed and visible to `toad`.",
                })
            except Exception:
                pass
            return False

        self._message_target = WebMessageTarget(self._tm)
        self._agent = Agent(self._project_root, agent_data, session_id=None)

        try:
            await self._agent.start(message_target=self._message_target)
        except Exception as exc:
            print(f"[bridge] failed to start agent: {exc}", file=sys.stderr)
            try:
                await self._ws.send_json({
                    "type": "error",
                    "message": f"Failed to start agent: {exc}",
                })
            except Exception:
                pass
            return False

        # Wait for AgentReady or AgentFail
        while True:
            try:
                msg = await asyncio.wait_for(
                    self._message_target._event_queue.get(), timeout=30
                )
            except asyncio.TimeoutError:
                print("[bridge] timed out waiting for agent ready", file=sys.stderr)
                try:
                    await self._ws.send_json({
                        "type": "error",
                        "message": "Timed out waiting for agent to start.",
                    })
                except Exception:
                    pass
                return False

            if isinstance(msg, AgentReady):
                self._agent_started = True
                try:
                    await self._ws.send_json({"type": "agent_ready"})
                except Exception:
                    pass
                return True
            elif isinstance(msg, AgentFail):
                print(f"[bridge] agent failed: {msg.message}", file=sys.stderr)
                try:
                    await self._ws.send_json({
                        "type": "error",
                        "message": msg.message,
                    })
                except Exception:
                    pass
                return False
            # Other messages (e.g. SetModes) silently dropped during startup

    async def _read_from_ws(self) -> None:
        import aiohttp

        assert self._message_target is not None

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self._handle_browser_msg(data)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    async def _handle_browser_msg(self, data: dict) -> None:
        msg_type = data.get("type")
        if msg_type == "message":
            text = data.get("text", "")
            task = asyncio.ensure_future(self._do_prompt(text))
            self._prompt_tasks.add(task)
            task.add_done_callback(self._prompt_tasks.discard)
        elif msg_type == "cancel":
            if self._agent is not None:
                asyncio.ensure_future(self._agent.cancel())
        elif msg_type == "permission_response":
            await self._resolve_permission(data)
        elif msg_type == "reconnect":
            # Browser reconnected to a live session — re-send agent_ready so the
            # UI transitions from "Starting agent…" to "Connected".
            if self._agent_started:
                try:
                    await self._ws.send_json({"type": "agent_ready"})
                except Exception:
                    pass

    async def _do_prompt(self, text: str) -> None:
        if self._agent is None:
            return

        # Echo the user message immediately
        try:
            await self._ws.send_json({"type": "user_text", "text": text})
        except Exception:
            return

        try:
            stop_reason = await self._agent.send_prompt(text)
        except asyncio.CancelledError:
            raise  # let cancellation propagate normally
        except Exception as exc:
            print(f"[bridge] send_prompt error: {exc}", file=sys.stderr)
            try:
                await self._ws.send_json({
                    "type": "error",
                    "message": f"Agent error: {exc}",
                })
            except Exception:
                pass
            return
        try:
            await self._ws.send_json({
                "type": "done",
                "stop_reason": stop_reason or "end_turn",
            })
        except Exception:
            pass

    async def _resolve_permission(self, data: dict) -> None:
        from toad.answer import Answer

        assert self._message_target is not None

        tool_call_id = data.get("id", "")
        option_id = data.get("option_id", "")
        text = data.get("text", "")

        entry = self._message_target._permission_futures.pop(tool_call_id, None)
        if entry is None:
            return
        future, kinds = entry
        kind = kinds.get(option_id)
        if not future.done():
            future.set_result(Answer(text=text, id=option_id, kind=kind))

    async def _send_events_to_ws(self) -> None:
        assert self._message_target is not None
        while True:
            msg = await self._message_target._event_queue.get()
            json_data = self._event_to_json(msg)
            if json_data is not None:
                try:
                    await self._ws.send_json(json_data)
                except Exception:
                    return

    def _event_to_json(self, msg: Any) -> dict | None:
        from toad.agent import AgentReady, AgentFail
        from toad.acp import messages

        if isinstance(msg, AgentReady):
            return {"type": "agent_ready"}

        if isinstance(msg, AgentFail):
            return {"type": "error", "message": msg.message}

        if isinstance(msg, messages.Update):
            return {"type": "text_chunk", "text": msg.text, "kind": msg.type or "message"}

        if isinstance(msg, messages.Thinking):
            return {"type": "text_chunk", "text": msg.text, "kind": "thinking"}

        if isinstance(msg, messages.UserMessage):
            # Suppress agent echo — we already sent user_text in _do_prompt
            return None

        if isinstance(msg, messages.ToolCall):
            tc = msg.tool_call
            return {
                "type": "tool_call",
                "id": tc.get("toolCallId", ""),
                "title": tc.get("title", "Tool call"),
                "kind": tc.get("kind"),
                "status": tc.get("status", "pending"),
                "content": self._serialize_content(tc.get("content")),
                "rawOutput": tc.get("rawOutput"),
            }

        if isinstance(msg, messages.ToolCallUpdate):
            tc = msg.tool_call
            return {
                "type": "tool_call_update",
                "id": tc.get("toolCallId", ""),
                "title": tc.get("title"),
                "kind": tc.get("kind"),
                "status": tc.get("status"),
                "content": self._serialize_content(tc.get("content")),
                "rawOutput": tc.get("rawOutput"),
            }

        if isinstance(msg, messages.Plan):
            return {
                "type": "plan",
                "entries": list(msg.entries),
            }

        if isinstance(msg, messages.RequestPermission):
            tc = msg.tool_call
            tool_call_id = tc.get("toolCallId", "")
            return {
                "type": "permission_request",
                "id": tool_call_id,
                "title": tc.get("title", "Permission required"),
                "options": [
                    {
                        "optionId": opt["optionId"],
                        "name": opt["name"],
                        "kind": opt.get("kind"),
                    }
                    for opt in msg.options
                ],
            }

        # Silently drop messages we don't know how to render
        return None

    def _serialize_content(self, content: Any) -> list:
        if not content:
            return []
        result = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "diff":
                result.append({
                    "type": "diff",
                    "path": item.get("path", ""),
                    "oldText": item.get("oldText", ""),
                    "newText": item.get("newText", ""),
                })
            elif item_type == "content":
                c = item.get("content", {})
                result.append({
                    "type": "content",
                    "content": {"text": c.get("text", "") if isinstance(c, dict) else ""},
                })
            elif item_type == "terminal":
                result.append({
                    "type": "terminal",
                    "terminalId": item.get("terminalId", ""),
                })
        return result


# ---------------------------------------------------------------------------
# AgentBridgeServer — aiohttp app, duck-typed as a proc for SessionData
# ---------------------------------------------------------------------------


class AgentBridgeServer:
    """Serves the web chat UI and bridges WebSocket connections to ACP agent.

    Duck-types asyncio.subprocess.Process: has .returncode and .terminate().
    """

    returncode: int | None = None

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._agent_name: str = ""
        self._project_root: Path = Path(".").absolute()
        # session_id → AgentBridgeSession (kept alive across WS reconnects)
        self._sessions: dict[str, AgentBridgeSession] = {}

    def terminate(self) -> None:
        """Called by HerokuTunnel cleanup — signal the server to stop."""
        self._stop_event.set()
        self.returncode = 0

    async def start(
        self,
        port: int,
        agent_name: str,
        project_root: Path,
    ) -> None:
        from aiohttp import web

        self._agent_name = agent_name
        self._project_root = project_root

        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", port)
        await site.start()

        log.info("AgentBridgeServer listening on http://localhost:%d", port)

        await self._stop_event.wait()

        await runner.cleanup()
        self.returncode = 0

    async def _handle_index(self, request: Any) -> Any:
        from aiohttp import web

        return web.Response(
            text=CHAT_UI_HTML,
            content_type="text/html",
            charset="utf-8",
        )

    async def _handle_ws(self, request: Any) -> Any:
        from aiohttp.web_ws import WebSocketResponse

        ws = WebSocketResponse()
        await ws.prepare(request)

        session_id = request.rel_url.query.get("session_id", "")
        existing = self._sessions.get(session_id) if session_id else None

        if existing is not None and not existing.is_expired:
            # Reconnect browser to the still-running agent session.
            try:
                await existing.reconnect(ws)
            except Exception as exc:
                print(f"[bridge] reconnect error: {exc}", file=sys.stderr)
        else:
            def _on_expire(sid: str) -> None:
                self._sessions.pop(sid, None)

            session = AgentBridgeSession(
                ws,
                self._agent_name,
                self._project_root,
                session_id=session_id,
                on_expire=_on_expire,
            )
            if session_id:
                self._sessions[session_id] = session
            try:
                await session.run()
            except Exception as exc:
                print(f"[bridge] session error: {exc}", file=sys.stderr)
                import traceback
                traceback.print_exc()

        return ws
