# Kuro Desktop — Application Specification

## Objetivo
Crear una aplicación de escritorio para Windows (y eventualmente macOS/Linux) que envuelva la funcionalidad del agente Kuro en una interfaz gráfica, similar a OpenCode, GitHub Copilot o Claude Desktop.

## Stack tecnológico propuesto

### Opción A: Electron + React (similar a VS Code, GitHub Copilot)
- **Frontend**: React + TypeScript + Tailwind CSS
- **Backend**: Node.js (Electron main process)
- **Comunicación con Kuro**: spawn del proceso Python `kuro` como subprocess, comunicar via JSON-RPC o stdin/stdout
- **Ventajas**: Ecosistema maduro, fácil de distribuir, cross-platform
- **Desventajas**: Tamaño de bundle grande (~150MB)

### Opción B: Tauri + Rust + React (similar a OpenCode)
- **Frontend**: React + TypeScript + Tailwind CSS
- **Backend**: Rust (Tauri)
- **Comunicación con Kuro**: spawn del proceso Python `kuro` como sidecar, comunicar via JSON-RPC
- **Ventajas**: Bundle pequeño (~10MB), rendimiento nativo, seguro
- **Desventajas**: Requiere Rust toolchain

### Opción C: PyQt6 / PySide6 (nativo Python)
- **Frontend**: PyQt6 (QML o Widgets)
- **Backend**: Python (mismo proceso que kuro)
- **Ventajas**: Un solo lenguaje, acceso directo al agente sin IPC
- **Desventajas**: Menos moderno, harder to distribute

## Recomendación: Opción B (Tauri)
Tauri ofrece el mejor balance entre tamaño, rendimiento y experiencia de desarrollo moderna. La UI en React permite reutilizar componentes y estilos.

## Arquitectura

```
┌─────────────────────────────────────────────────────┐
│                   Tauri Frontend                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Sidebar  │  │ Chat     │  │  Tool Output     │  │
│  │ (files,  │  │ Window   │  │  (diffs, bash    │  │
│  │  history,│  │          │  │   output, etc.)  │  │
│  │  models) │  │          │  │                  │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
│  ┌──────────────────────────────────────────────┐   │
│  │ Status Bar (model, tokens, cost, mode)        │   │
│  └──────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────┘
                       │ JSON-RPC (stdin/stdout)
┌──────────────────────┴──────────────────────────────┐
│              Kuro Python Backend (sidecar)          │
│  - Agent loop (agent.py)                           │
│  - Tool execution (tools.py)                       │
│  - Session management (session.py)                  │
│  - Config (config.py)                              │
│  - Scope guard (scope.py)                          │
└─────────────────────────────────────────────────────┘
```

## Funcionalidades de la app de escritorio

### Core (paridad con TUI)
- [ ] Chat con streaming de respuestas
- [ ] Tool cards (bash, read, write, edit, glob, grep, etc.)
- [ ] Diffs visuales para cambios de archivo
- [ ] Selector de modelo
- [ ] Selector de modo (auto, edits, plan, zim, danger, uncensored)
- [ ] Contador de tokens y costo en tiempo real
- [ ] Historial de sesión (guardar/cargar)
- [ ] Compactación de contexto
- [ ] Scope guard (allow.yaml / out-of-scope.yaml)

### Mejoras sobre TUI
- [ ] Sidebar con árbol de archivos del proyecto
- [ ] Panel de diffs lado a lado
- [ ] Historial de conversaciones persistente
- [ ] Búsqueda en el historial
- [ ] Notificaciones de escritorio
- [ ] Tray icon (minimizar a bandeja)
- [ ] Atajos de teclado (Ctrl+Enter para enviar, etc.)
- [ ] Temas (claro/oscuro, además del verde)
- [ ] Multi-ventana / tabs
- [ ] Drag & drop de archivos
- [ ] Preview de imágenes/markdown
- [ ] Integración con git (diff viewer, blame)

### Exclusivas de desktop
- [ ] Auto-update (similar al update.py pero via Tauri updater)
- [ ] Crash reporting
- [ ] Telemetría (opt-in)
- [ ] Deep linking (kuro:// URLs)

## Plan de implementación

### Fase 1: Scaffold (1-2 semanas)
- Crear proyecto Tauri con React
- Implementar sidecar para Kuro Python
- JSON-RPC bridge entre frontend y backend
- UI básica: chat window + input + status bar

### Fase 2: Paridad TUI (2-3 semanas)
- Tool cards con streaming
- Diffs visuales
- Selector de modelo/modo
- Token counter
- Session save/load

### Fase 3: Mejoras desktop (2-3 semanas)
- Sidebar con file tree
- Historial persistente
- Keyboard shortcuts
- Themes
- System tray

### Fase 4: Polish & Release (1-2 semanas)
- Auto-updater
- Packaging (MSI, DMG, AppImage)
- Testing cross-platform
- Documentación

## Estructura del repositorio (rama desktop-app)

```
kuro/
├── kuro/                    # Python backend (existing)
├── desktop/                 # New: Tauri + React app
│   ├── src/                 # React frontend
│   ├── src-tauri/           # Rust backend (Tauri)
│   ├── package.json
│   └── tauri.conf.json
├── packaging/               # Existing PS1/Bash scripts
├── docs/                    # Existing docs
├── tests/                   # Existing tests
└── ...
```

## Comunicación Frontend ↔ Backend

El frontend envía comandos JSON-RPC al proceso Python de Kuro via stdin/stdout:

```json
// Frontend -> Backend
{"jsonrpc": "2.0", "method": "send_message", "params": {"text": "fix the bug"}, "id": 1}

// Backend -> Frontend (streaming)
{"jsonrpc": "2.0", "method": "text_delta", "params": {"text": "Let me look..."}}
{"jsonrpc": "2.0", "method": "tool_use", "params": {"name": "bash", "input": {"command": "pytest"}}}
{"jsonrpc": "2.0", "method": "tool_result", "params": {"output": "5 passed", "is_error": false}}
{"jsonrpc": "2.0", "result": {"done": true}, "id": 1}
```

## Próximos pasos

1. Decidir stack final (Tauri vs Electron)
2. Crear scaffold del proyecto desktop
3. Implementar JSON-RPC bridge
4. Portar UI de TUI a React
5. Testing y packaging
