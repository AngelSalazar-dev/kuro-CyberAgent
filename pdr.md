# PDR — Product Decision Records

Este archivo documenta las decisiones de producto tomadas durante el desarrollo de Kuro.

---

## PDR-001: Migración de ZimZilla a Kuro

**Fecha:** 2026-10-20
**Estado:** Aprobado

### Contexto
El proyecto original se llamaba ZimZilla (por Mr-Destroyer / ZIM). Se decidió renombrarlo a **Kuro** con un avatar estilo Claude (círculo con ojo central y estética verde-on-black).

### Decisión
- Renombrar todo: paquete Python `zimzilla` → `kuro`, binario `zimzilla` → `kuro`, directorio `.zimzilla` → `.kuro`
- Eliminar toda referencia a "ZimZilla", "ZIM", "Mr-Destroyer" en código, documentación e imágenes
- Crear avatar `docs/kuro-avatar.svg` (estilo Claude, verde-on-black)
- Reemplazar hero.svg con wordmark "KURO" + avatar

### Consecuencias
- Todos los paths, env vars y referencias internas actualizadas
- Los tests que referenciaban "ZIM (Mr-Destroyer)" se actualizaron
- El repo se subió a https://github.com/AngelSalazar-dev/kuro-CyberAgent

---

## PDR-002: Soporte nativo para Windows

**Fecha:** 2026-10-20
**Estado:** Aprobado

### Contexto
ZimZilla era exclusivamente Linux/macOS (bash scripts, fcntl, tmux). El objetivo es soportar Windows nativamente.

### Decisión
- Crear scripts PowerShell equivalentes: `install.ps1`, `setup.ps1`, `run.ps1`, `packaging/kuro.ps1`, `packaging/*/start-litellm.ps1`
- Reemplazar `fcntl.flock` con `_file_lock` multiplataforma (`msvcrt.locking` en Windows, `fcntl.flock` en POSIX)
- Agregar mensajes de error de tmux con instrucciones para Windows (`winget install tmux` / `choco install tmux`)
- Usar `& $PY @('-m', 'pip', ...)` en lugar de `& $PY -m pip ...` para evitar problemas de parsing en PS 5.1
- Eliminar redirecciones `< $null` (no soportadas en PS 5.1), usar `2>$null` en su lugar
- Eliminar BOM UTF-8 de todos los archivos (causaba `tomllib.TOMLDecodeError` en pyproject.toml)
- Reemplazar caracteres Unicode/box-drawing con ASCII puro en scripts PS

### Consecuencias
- Todos los scripts `.ps1` son compatibles con PowerShell 5.1+
- El shim `kuro.ps1` se genera con sustitución de placeholder simple para evitar problemas de here-strings
- El setup detecta el venv por `$env:VIRTUAL_ENV` o `$KURO_ROOT\.venv`

---

## PDR-003: Eliminación de rastros de ZimZilla en imágenes

**Fecha:** 2026-10-20
**Estado:** Aprobado

### Contexto
Las imágenes PNG originales (`docs/screenshots/*.png`) contenían el banner "ZIMZILLA" renderizado, lo cual contradecía el rebranding a Kuro.

### Decisión
- Eliminar todos los `.png` de screenshots
- Crear SVGs de reemplazo limpios con solo "KURO"
- Reemplazar `docs/hero.svg` con wordmark "KURO" + avatar de Kuro
- Verificar con grep que no queda ningún rastro de "ZIMZILLA" en ningún asset

### Consecuencias
- Las imágenes ahora son SVGs vectoriales (más livianas y escalables)
- No queda ninguna referencia a ZimZilla en ningún archivo del proyecto

---

## PDR-004: Estructura del shim de lanzamiento

**Fecha:** 2026-10-20
**Estado:** Aprobado

### Contexto
El shim `kuro.ps1` generado por `install.ps1` originalmente usaba here-strings de PowerShell (`@"..."@`) con interpolación de variables, lo que causaba que el contenido completo de `install.ps1` se incrustara literalmente en el shim.

### Decisión
- Usar sustitución simple de placeholder (`__INSTALL_DIR__` → `$INSTALL_DIR`) en un here-string simple (`@'...'@`)
- Generar el shim con `$env:KURO_ROOT = 'PATH'` y `& 'PATH\packaging\kuro.ps1' @args`
- Verificar que el shim funciona correctamente con `kuro --version`

### Consecuencias
- El shim ahora es limpio y funcional
- El launcher `kuro.ps1` se ejecuta correctamente desde cualquier directorio

---

## PDR-005: Dependencias de Python y pyproject.toml

**Fecha:** 2026-10-20
**Estado:** Aprobado

### Contexto
El `pyproject.toml` original tenía BOM UTF-8 que causaba `tomllib.TOMLDecodeError` al instalar con pip. Además, el nombre del paquete era `zimzilla`.

### Decisión
- Eliminar BOM UTF-8 de `pyproject.toml` y todos los archivos del proyecto
- Cambiar `name = "kuro"`, `kuro = "kuro.__main__:main"`, `include = ["kuro*"]`
- Filtrar la línea `-e .` de requirements.txt durante el install y agregar el paquete por separado con `pip install -e .` desde el directorio correcto

### Consecuencias
- La instalación con pip funciona correctamente
- El paquete se instala en modo editable desde el checkout

---

## PDR-006: Configuración de KURO_HOME en setup.ps1

**Fecha:** 2026-10-20
**Estado:** Aprobado

### Contexto
En PowerShell 5.1, el operador `-or` puede devolver valores inesperados cuando el primer operando es `$null` o una cadena vacía, causando que `$KURO_HOME` se convierta en `True` (booleano) en lugar de la ruta por defecto.

### Decisión
- Reemplazar `$env:KURO_HOME -or (Join-Path $env:USERPROFILE '.kuro')` con un bloque `if` explícito:
  ```powershell
  $KURO_HOME = $env:KURO_HOME
  if (-not $KURO_HOME) { $KURO_HOME = Join-Path $env:USERPROFILE '.kuro' }
  ```
- Aplicar el mismo patrón en `kuro.ps1` para `$env:KURO_HOME`

### Consecuencias
- `$KURO_HOME` siempre contiene una ruta válida
- Elimina el problema de rutas `True\logfare\source`

---

## PDR-007: Instrucciones de Windows

**Fecha:** 2026-10-20
**Estado:** Aprobado

### Contexto
Los usuarios de Windows necesitan una guía completa de instalación y uso, incluyendo solución de problemas.

### Decisión
- Crear `instruccioneswindows.md` con:
  - Requisitos previos
  - Instalación rápida
  - Configuración de clave de Logfare (2 opciones)
  - Uso básico y comandos del TUI
  - Modos de operación
  - Variables de entorno
  - Instalación desde código fuente
  - Actualización
  - Solución de problemas
  - Notas sobre Windows

### Consecuencias
- Los usuarios de Windows tienen una guía completa y autónoma
- Se documentan los problemas conocidos y sus soluciones
