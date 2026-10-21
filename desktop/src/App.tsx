import { useState, useRef, useEffect } from 'react';
import { invoke } from '@tauri-apps/api/core';
import './App.css';

interface Message {
  role: 'user' | 'assistant';
  content: string;
}

function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const [status, setStatus] = useState('Kuro Desktop v1.0.0');
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    invoke<string>('get_status').then(setStatus).catch(console.error);
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const sendMessage = async () => {
    if (!input.trim() || isStreaming) return;

    const userMsg: Message = { role: 'user', content: input };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setIsStreaming(true);

    try {
      await invoke('send_message', { text: input });
      // In a full implementation, we'd listen for streaming events here
      // For now, we add a placeholder response
      const assistantMsg: Message = { role: 'assistant', content: 'Message sent to Kuro backend...' };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err) {
      console.error(err);
      const errorMsg: Message = { role: 'assistant', content: `Error: ${err}` };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsStreaming(false);
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
          <select className="model-select">
            <option>deepseek-v4.1-flash</option>
            <option>qwen-3.8-27b</option>
            <option>gemma-4-26b</option>
          </select>
        </div>
        <div className="sidebar-section">
          <h3>Mode</h3>
          <select className="mode-select">
            <option value="auto">AUTO</option>
            <option value="edits">EDITS</option>
            <option value="plan">PLAN</option>
            <option value="zim">ZIM</option>
            <option value="danger">DANGER</option>
            <option value="uncensored">UNCENSORED</option>
          </select>
        </div>
      </div>

      <div className="main">
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
              <div className="message-role">{msg.role === 'user' ? 'You' : 'Kuro'}</div>
              <div className="message-content">{msg.content}</div>
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
          <span>{status}</span>
          <span className="status-right">Tokens: 0 | Cost: $0.00</span>
        </div>
      </div>
    </div>
  );
}

export default App;
