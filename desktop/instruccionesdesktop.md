# Kuro Desktop — Guía de instalación y uso

## Requisitos previos

### Windows
- **Node.js 18+** — https://nodejs.org/
- **Rust toolchain** — https://rustup.rs/
- **Python 3.11+** — https://www.python.org/downloads/
- **Git** — https://git-scm.com/download/win
- **Visual Studio Build Tools** (para compilar Rust en Windows) — https://visualstudio.microsoft.com/visual-cpp-build-tools/

### macOS
- **Node.js 18+** — `brew install node`
- **Rust toolchain** — `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- **Python 3.11+** — `brew install python@3.12`
- **Xcode Command Line Tools** — `xcode-select --install`

### Linux
- **Node.js 18+** — `sudo apt install nodejs npm` o `sudo dnf install nodejs`
- **Rust toolchain** — `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- **Python 3.11+** — `sudo apt install python3.12 python3.12-venv`
- **Dependencias del sistema** — `sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev`

## Instalación desde el código fuente

### 1. Clonar el repositorio

```bash
git clone https://github.com/AngelSalazar-dev/kuro-CyberAgent.git
cd kuro-CyberAgent
git checkout desktop-app
```

### 2. Instalar dependencias del frontend

```bash
cd desktop
npm install
```

### 3. Instalar dependencias de Python

```bash
cd ..
python -m venv .venv
# Windows:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 4. Instalar Tauri CLI

```bash
npm install -g @tauri-apps/cli
```

### 5. Ejecutar en modo desarrollo

```bash
cd desktop
npm run dev
```

Esto va a:
- Iniciar el servidor de desarrollo de Vite en `http://localhost:1420`
- Compilar el backend de Rust
- Lanzar la app de escritorio con hot-reload

### 6. Compilar para producción

```bash
cd desktop
npm run build
```

Los instaladores se generan en `desktop/src-tauri/target/release/bundle/`:
- **Windows**: `.msi` y `.exe` (NSIS)
- **macOS**: `.dmg` y `.app`
- **Linux**: `.deb`, `.AppImage`, `.rpm`

## Estructura del proyecto desktop

```
desktop/
├── src/                    # Frontend React
│   ├── App.tsx             # Componente principal (chat, sidebar, input)
│   ├── App.css             # Estilos Kuro (verde-on-black)
│   └── main.tsx            # Entry point de React
├── src-tauri/              # Backend Rust (Tauri)
│   ├── src/
│   │   └── main.rs         # Sidecar management, JSON-RPC commands
│   ├── Cargo.toml          # Dependencias Rust
│   └── tauri.conf.json     # Configuración de Tauri
├── kuro-server.py          # Wrapper Python JSON-RPC para el agente Kuro
├── index.html              # HTML base
├── package.json            # Dependencias Node.js
├── vite.config.ts          # Configuración de Vite
├── tsconfig.json           # Configuración de TypeScript
└── README.md               # Spec y roadmap
```

## Arquitectura de comunicación

```
┌─────────────────────────────────────────────────────┐
│              Tauri Frontend (React)                  │
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
                       │ Tauri IPC (invoke)
┌──────────────────────┴──────────────────────────────┐
│              Rust Backend (Tauri)                    │
│  - Sidecar management (spawn kuro-server.py)        │
│  - JSON-RPC bridge (stdin/stdout)                   │
│  - Window management                                │
└──────────────────────┬──────────────────────────────┘
                       │ JSON-RPC (stdin/stdout)
┌──────────────────────┴──────────────────────────────┐
│              Kuro Python Backend (sidecar)          │
│  - Agent loop (kuro/agent.py)                       │
│  - Tool execution (kuro/tools.py)                  │
│  - Session management (kuro/session.py)             │
│  - Config (kuro/config.py)                         │
│  - Scope guard (kuro/scope.py)                     │
└─────────────────────────────────────────────────────┘
```

## Protocolo JSON-RPC

### Frontend → Backend

```json
{"jsonrpc": "2.0", "method": "send_message", "params": {"text": "fix the bug"}, "id": 1}
{"jsonrpc": "2.0", "method": "get_status", "params": {}, "id": 2}
```

### Backend → Frontend (streaming)

```json
{"jsonrpc": "2.0", "method": "text_delta", "params": {"text": "Let me look..."}}
{"jsonrpc": "2.0", "method": "tool_use", "params": {"name": "bash", "input": {"command": "pytest"}}}
{"jsonrpc": "2.0", "method": "tool_result", "params": {"output": "5 passed", "is_error": false}}
{"jsonrpc": "2.0", "result": {"done": true}, "id": 1}
```

## Funcionalidades implementadas (v1.0)

- [x] Chat window con streaming de respuestas
- [x] Sidebar con selector de modelo y modo
- [x] Input area con Enter para enviar
- [x] Status bar con estado del agente
- [x] Welcome screen con avatar de Kuro
- [x] Tema verde-on-black (Kuro theme)
- [x] JSON-RPC bridge (stdin/stdout)
- [x] Sidecar management (Rust → Python)

## Funcionalidades pendientes

- [ ] Tool cards con streaming en tiempo real
- [ ] Diffs visuales para cambios de archivo
- [ ] Contador de tokens y costo en tiempo real
- [ ] Historial de sesión persistente
- [ ] Sidebar con árbol de archivos del proyecto
- [ ] Notificaciones de escritorio
- [ ] Tray icon (minimizar a bandeja)
- [ ] Atajos de teclado (Ctrl+Enter, etc.)
- [ ] Temas (claro/oscuro)
- [ ] Multi-ventana / tabs
- [ ] Auto-update (Tauri updater)
- [ ] Crash reporting

## Solución de problemas

### "npm install" falla
Verifica que Node.js 18+ esté instalado: `node --version`

### "cargo build" falla
Verifica que Rust esté instalado: `rustc --version`
En Windows, asegúrate de tener Visual Studio Build Tools instalado.

### "kuro-server.py" no se encuentra
El sidecar debe estar en el mismo directorio que el ejecutable de Tauri. Verifica que `kuro-server.py` esté en `desktop/`.

### La app no se conecta al backend
Verifica que Python y las dependencias estén instaladas:
```bash
python -c "import anthropic, textual, rich, yaml; print('OK')"
```

### Error de compilación de Tauri
```bash
cd desktop
npm run tauri build -- --debug
```

## Notas

- La rama `desktop-app` contiene el código de la app de escritorio
- La rama `main` contiene solo la versión de consola
- Para cambiar entre ramas: `git checkout main` o `git checkout desktop-app`
- El backend Python (`kuro/`) se comparte entre ambas versiones
