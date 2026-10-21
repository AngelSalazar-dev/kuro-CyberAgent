# Kuro — Guía de instalación y uso en Windows

## Requisitos previos

- **Python 3.11+** — descárgalo desde https://www.python.org/downloads/ (marca "Add Python to PATH" al instalar)
- **Git** — descárgalo desde https://git-scm.com/download/win
- **PowerShell 5.1+** (viene preinstalado en Windows 10/11)

Opcional (para la feature `--panes` con tmux):
- `winget install tmux` o `choco install tmux`

## Instalación rápida (un solo comando)

Abre **PowerShell** (no CMD) y ejecuta:

```powershell
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force
git clone https://github.com/AngelSalazar-dev/kuro-CyberAgent.git
cd kuro-CyberAgent
.\install.ps1
```

Eso va a:
1. Clonar el repositorio en `%LOCALAPPDATA%\kuro`
2. Crear un virtualenv en `%LOCALAPPDATA%\kuro\.venv`
3. Instalar todas las dependencias
4. Instalar el launcher `kuro.ps1` en `%USERPROFILE%\bin`
5. Configurar la ruta de Logfare (sin clave por defecto)

## Configurar la clave de Logfare

Kuro necesita una clave de Logfare para hacer peticiones. Consigue una en https://logfare.ai (empiezan con `lfu_`).

**Opción A — desde el TUI de Kuro:**
```powershell
kuro
# Dentro del TUI, escribe:
# /logfare-api-setup lfu_tu_clave_aqui
```

**Opción B — manualmente:**
```powershell
# Crear el directorio si no existe
New-Item -ItemType Directory -Path "$env:USERPROFILE\.kuro\logfare" -Force | Out-Null

# Crear el perfil con tu clave
@"
export ANTHROPIC_BASE_URL="http://localhost:4001"
export ANTHROPIC_AUTH_TOKEN="lfu_tu_clave_aqui"
export ANTHROPIC_MODEL="deepseek-v4.1-flash"
export ANTHROPIC_API_KEY=""
"@ | Set-Content -Path "$env:USERPROFILE\.kuro\logfare\source" -Encoding UTF8
```

## Uso básico

```powershell
kuro                          # lanza Kuro en el directorio actual
kuro -C C:\mi\proyecto        # ejecuta contra otro directorio
kuro --mode zim               # modo autónomo (sigue AGENTS.md)
kuro --allow allow.yaml       # declara targets autorizados (sesión armada)
kuro --deny out-of-scope.yaml # hosts bloqueados
kuro --help                   # ver todas las opciones
kuro --version                # ver versión
```

## Comandos dentro del TUI

| Comando | Descripción |
|---------|-------------|
| `/model` | Cambiar modelo (lista los disponibles) |
| `/mode <nombre>` | Cambiar modo (auto, edits, plan, zim, danger, uncensored) |
| `/logfare-models` | Ver modelos disponibles en Logfare con uptime |
| `/logfare-api-setup <key>` | Configurar clave de Logfare |
| `/zim-logfare` | Cambiar a la ruta de Logfare |
| `/zim-openrouter` | Cambiar a OpenRouter |
| `/zim-unorouter` | Cambiar a UnoRouter |
| `/compact` | Compactar historial cuando el contexto se llena |
| `/think` | Activar/desactivar mostrar el razonamiento del modelo |
| `/rain` | Activar/desactivar matrix rain |
| `/panes` | Re-abrir bajo tmux (para multi-pane) |
| `/limits` | Ver límites del modelo |
| `/protocol` | Ver/pin protocolo de conexión |

## Modos de operación

| Modo | Comportamiento |
|------|----------------|
| `auto` (default) | Full auto — escritura de archivos y comandos shell sin confirmación |
| `edits` | Solo edición de archivos — shell deshabilitado |
| `plan` | Solo lectura — investiga y propone, no cambia nada |
| `zim` | Full auto gobernado por AGENTS.md |
| `danger` | Full auto, dirigido por el operador — sin preguntas |
| `uncensored` | Full auto, prompt por defecto reemplazado — el modelo no puede rechazar |

## Variables de entorno

| Variable | Descripción |
|----------|-------------|
| `KURO_HOME` | Directorio de datos de Kuro (default: `%USERPROFILE%\.kuro`) |
| `KURO_NO_PROXY=1` | Nunca tocar el proxy local |
| `KURO_REQUIRE_PROXY=1` | Fallar si el proxy no está sano |
| `ZIMZILLA_MODEL` / `ANTHROPIC_MODEL` | Modelo por defecto |
| `ANTHROPIC_BASE_URL` | Endpoint de API |
| `ANTHROPIC_AUTH_TOKEN` | Clave de API |
| `ZIMZILLA_NO_UPDATE=1` | Desactivar actualización automática en launch |

## Instalación desde el código fuente

```powershell
git clone https://github.com/AngelSalazar-dev/kuro-CyberAgent.git
cd kuro-CyberAgent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
.\setup.ps1                          # verificar la ruta end-to-end
.\run.ps1                            # lanzar
```

## Actualización

Kuro se actualiza automáticamente en cada launch (si hay nuevos commits en el remote). Para desactivar:

```powershell
kuro --no-update
# o permanentemente:
$env:ZIMZILLA_NO_UPDATE = '1'
```

## Solución de problemas

### "no python on PATH"
Instala Python 3.11+ desde https://www.python.org/downloads/ y marca "Add Python to PATH" durante la instalación.

### "git is required"
Instala Git desde https://git-scm.com/download/win

### "profile not found"
Ejecuta `.\setup.ps1` o configura la clave manualmente (ver sección "Configurar la clave de Logfare").

### "proxy did not start"
Verifica que el puerto 4001 no esté ocupado. Revisa los logs en `%TEMP%\litellm-zim.log`.

### "tmux is not installed"
Para usar `--panes`, instala tmux: `winget install tmux` o `choco install tmux`.

### La clave no funciona
- La clave debe empezar con `lfu_`
- Verifica que Logfare esté disponible en https://logfare.ai
- Prueba con `/logfare-api-setup <key>` dentro del TUI

## Notas sobre Windows

- Los scripts de PowerShell son compatibles con PowerShell 5.1+ (viene con Windows 10/11)
- El file locking para el buzón de panes usa `msvcrt.locking` en Windows y `fcntl.flock` en Linux
- Las rutas usan backslashes (`\`) nativamente, pero forward slashes (`/`) también funcionan en la mayoría de los casos
- `--panes` / tmux requiere tmux instalado por separado
- El venv propio está en `.venv\` dentro del checkout; no necesitas activarlo manualmente
