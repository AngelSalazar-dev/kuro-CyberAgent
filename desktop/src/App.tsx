import { useState, useRef, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import './App.css';

interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

interface ToolEntry {
  id: string;
  name: string;
  preview: string;
  output: string;
  isError: boolean;
  done: boolean;
}

interface PermissionRequest {
  id: number;
  name: string;
  preview: string;
}

interface BackendStatus {
  ready: boolean;
  detail: string;
  model: string;
  mode: string;
  python: string;
  script: string;
}

const MODELS = [
  'deepseek-v4.1-flash',
  'qwen-3.8-27b',
  'gemma-4-26b',
];

const MODES = ['auto', 'edits', 'plan', 'zim', 'danger', 'uncensored'];

function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [tools, setTools] = useState<ToolEntry[]>([]);
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [status, setStatus] = useState<BackendStatus | null>(null);
  const [banner, setBanner] = useState('');
  const [permission, setPermission] = useState<PermissionRequest | null>(null);
  const [model, setModel] = useState(MODELS[0]);
  const [mode, setMode] = useState('auto');
  const [cost, setCost] = useState('$0.00');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const streamRef = useRef('');

  const refreshStatus = useCallback(async () => {
    try {
      const next = await invoke<BackendStatus>('get_status');
      setStatus(next);
      if (!next.ready) {
        setBanner(next.detail || 'the Kuro backend is not running');
      }
    } catch (err) {
      setBanner(String(err));
    }
  }, []);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, tools]);

  useEffect(() => {
    const unlisten = listen<{ method?: string; params?: Record<string, unknown> }>(
      'kuro-rpc',
      (event) => {
        const method = event.payload.method || '';
        const params = (event.payload.params || {}) as Record<string, unknown>;

        switch (method) {
          case 'status': {
            const notice = String(params.notice ?? '');
            setBanner(notice);
            if (typeof params.model === 'string') setModel(params.model);
            if (typeof params.mode === 'string') setMode(params.mode);
            break;
          }
          case 'text_delta': {
            const text = String(params.text ?? '');
            streamRef.current += text;
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === 'assistant' && last.content === streamRef.current) {
                next[next.length - 1] = { ...last, content: streamRef.current };
              } else {
                next.push({ role: 'assistant', content: streamRef.current });
              }
              return next;
            });
            break;
          }
          case 'reasoning_delta': {
            break;
          }
          case 'tool_call': {
            const key = String(params.id ?? params.name ?? Date.now());
            setTools((prev) => [
              ...prev,
              {
                id: key,
                name: String(params.name ?? 'tool'),
                preview: String(params.preview ?? ''),
                output: '',
                isError: false,
                done: false,
              },
            ]);
            break;
          }
          case 'tool_result': {
            const key = String(params.id ?? params.name ?? '');
            setTools((prev) =>
              prev.map((tool) =>
                tool.id === key
                  ? {
                      ...tool,
                      done: true,
                      isError: Boolean(params.is_error),
                      output: String(params.output ?? ''),
                    }
                  : tool
              )
            );
            break;
          }
          case 'permission_request': {
            setPermission({
              id: Number(params.id ?? 0),
              name: String(params.name ?? 'tool'),
              preview: String(params.preview ?? ''),
            });
            break;
          }
          case 'error': {
            const text = String(params.message ?? 'unknown error');
            setMessages((prev) => [...prev, { role: 'system', content: text }]);
            setIsStreaming(false);
            break;
          }
          case 'done': {
            setIsStreaming(false);
            const value = Number(params.cost ?? 0);
            if (value) setCost(`$${value.toFixed(4)}`);
            break;
          }
          default:
            break;
        }
      }
    );

    const unlistenErr = listen<string>('kuro-stderr', (event) => {
      setBanner(String(event.payload));
    });

    return () => {
      unlisten.then((off) => off());
      unlistenErr.then((off) => off());
    };
  }, []);

  const sendMessage = async () => {
    if (!input.trim() || isStreaming) return;

    const text = input.trim();
    setMessages((prev) => [...prev, { role: 'user', content: text }]);
    setInput('');
    setIsStreaming(true);
    streamRef.current = '';

    try {
      await invoke('send_message', { text, model, mode });
    } catch (err) {
      setMessages((prev) => [...prev, { role: 'system', content: `Error: ${err}` }]);
      setIsStreaming(false);
    }
  };

  const answerPermission = async (decision: 'yes' | 'no') => {
    if (!permission) return;
    const current = permission;
    setPermission(null);
    try {
      await invoke('permission_response', { id: current.id, decision });
    } catch (err) {
      setMessages((prev) => [...prev, { role: 'system', content: `Error: ${err}` }]);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <div className="app">
      <div className="sidebar">
        <div className="sidebar-header">
          <div className="kuro-avatar"></div>
          <h1>KURO</h1>
        </div>
        <div className="sidebar-section">
          <h3>Model</h3>
          <select
            className="model-select"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          >
            {MODELS.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </div>
        <div className="sidebar-section">
          <h3>Mode</h3>
          <select
            className="mode-select"
            value={mode}
            onChange={(e) => setMode(e.target.value)}
          >
            {MODES.map((name) => (
              <option key={name} value={name}>
                {name.toUpperCase()}
              </option>
            ))}
          </select>
        </div>
        <div className="sidebar-section sidebar-info">
          <h3>Backend</h3>
          <div className="backend-state">
            {status?.ready ? 'online' : 'offline'}
          </div>
          {status?.python && <div className="backend-path">{status.python}</div>}
        </div>
      </div>

      <div className="main">
        {banner && <div className="banner">{banner}</div>}

        {permission && (
          <div className="permission">
            <div className="permission-title">
              Kuro wants to run <strong>{permission.name}</strong>
            </div>
            {permission.preview && (
              <pre className="permission-preview">{permission.preview}</pre>
            )}
            <div className="permission-actions">
              <button className="approve" onClick={() => answerPermission('yes')}>
                Approve
              </button>
              <button className="deny" onClick={() => answerPermission('no')}>
                Deny
              </button>
            </div>
          </div>
        )}

        <div className="chat-window">
          {messages.length === 0 && (
            <div className="welcome">
              <div className="kuro-avatar-large"></div>
              <h2>KURO</h2>
              <p>An autonomous coding agent that lives in your terminal — and now on your desktop.</p>
              <p className="hint">Type a message to get started.</p>
            </div>
          )}
          {messages.map((msg, i) => (
            <div key={i} className={`message ${msg.role}`}>
              <div className="message-role">
                {msg.role === 'user' ? 'You' : msg.role === 'assistant' ? 'Kuro' : 'Error'}
              </div>
              <div className="message-content">{msg.content}</div>
            </div>
          ))}
          {tools.map((tool) => (
            <div key={tool.id} className={`tool ${tool.isError ? 'tool-error' : ''}`}>
              <div className="tool-name">
                {tool.done ? '✓' : '…'} {tool.name}
              </div>
              {tool.preview && <pre className="tool-preview">{tool.preview}</pre>}
              {tool.output && <pre className="tool-output">{tool.output}</pre>}
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>

        <div className="input-area">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask Kuro anything..."
            disabled={isStreaming}
            rows={3}
          />
          <button onClick={sendMessage} disabled={isStreaming || !input.trim()}>
            {isStreaming ? '...' : 'Send'}
          </button>
        </div>

        <div className="status-bar">
          <span>{status?.ready ? 'Kuro Desktop v1.0.0' : 'Kuro Desktop v1.0.0 — starting'}</span>
          <span className="status-right">Cost: {cost}</span>
        </div>
      </div>
    </div>
  );
}

export default App;
