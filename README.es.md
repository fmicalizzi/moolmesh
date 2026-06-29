> [English version](README.md)

<p align="center">
  <img src="docs/cli-banner.svg" alt="MoolMesh CLI" width="560">
</p>

# MoolMesh

**La malla de contexto para agentes autónomos.**

Observabilidad unificada, telemetría y coordinación entre agentes — ejecutándose completamente en tu máquina.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://python.org)
[![Tests: 593 passing](https://img.shields.io/badge/Tests-593%20passing-green.svg)](#desarrollo)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero-brightgreen.svg)](#)
[![PyPI](https://img.shields.io/pypi/v/moolmesh.svg)](https://pypi.org/project/moolmesh/)

---

## ¿Por qué MoolMesh?

El desarrollo de software moderno ya no es humano-contra-teclado. Es un ecosistema de agentes de IA trabajando en paralelo — cada uno con sus propios logs, contadores de tokens y trazas de razonamiento, todos encerrados en silos separados.

Cuando Claude Code se queda atrapado en un bucle, tus otros agentes no lo saben. Cuando gastas tokens en cuatro proveedores, no puedes ver qué commit lo justificó. Cuando tu equipo usa diferentes herramientas de IA en el mismo repositorio, nadie tiene la imagen completa.

**MoolMesh congrega lo que está disperso.** Auto-descubre sesiones de todos los agentes de programación con IA principales, las normaliza en una única base de datos consultable y expone ese estado tanto a humanos (vía dashboard) como a máquinas (vía MCP).

Lee nuestra [Filosofía](PHILOSOPHY.md) para entender el doble axioma detrás de MoolMesh: **Human-First y Agent-First**.

---

## Qué obtienes

Cuatro vistas en una sola pestaña del navegador:

| Vista | Qué muestra |
|-------|-------------|
| **AI Sessions** | Feed de eventos en vivo de todos los agentes — mensajes, llamadas a herramientas, uso de tokens, modelos |
| **Analytics** | Consumo de tokens por proveedor, actividad por hora, herramientas más usadas, proyectos principales |
| **Project Pulse** | Kanban de PRs, lista de issues, milestones, tablero de GitHub Projects v2 |
| **Code Timeline** | Feed de commits, estadisticas por autor, archivos calientes, resumenes diarios/semanales |

Además, un **servidor MCP** que permite a otros agentes de IA consultar los datos de sesión programáticamente — habilitando supervisión y orquestación entre agentes.

---

## Inicio rápido

```bash
# Instalar
pip install moolmesh

# Iniciar el dashboard
mool dashboard
# → abrir http://localhost:5200
```

Eso es todo. MoolMesh auto-descubre tus sesiones de IA inmediatamente. No requiere configuración.

> **Ejecutar desde el código fuente:**
> ```bash
> git clone https://github.com/fmicalizzi/moolmesh.git
> cd moolmesh
> python -m venv .venv && source .venv/bin/activate
> pip install -e ".[dev]"
> mool dashboard
> ```

---

## Instalación en producción

Para acceso global (ejecutar `mool` desde cualquier directorio):

```bash
# Recomendado — venv aislado, binario global
pipx install moolmesh

# O con pip (requiere venv en Python moderno)
pip install moolmesh
```

### Servicio systemd (Linux)

Usa `mool dashboard` (foreground) como punto de entrada — MoolMesh auto-detecta systemd y se queda en primer plano:

```ini
# ~/.config/systemd/user/moolmesh.service
[Unit]
Description=MoolMesh Dashboard
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/mool daemon start --port 5200
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now moolmesh
systemctl --user status moolmesh
```

> **Nota:** `mool daemon start` auto-detecta systemd (`$INVOCATION_ID`) y se queda en primer plano, así `Type=simple` funciona correctamente. Fuera de systemd, hace double-fork como siempre.

---

## Agentes soportados

| Proveedor | Fuente de sesión | Formato |
|-----------|-----------------|---------|
| **Claude Code** | `~/.claude/projects/` | JSONL por sesión + logs de subagentes |
| **Codex (GPT-5)** | `~/.codex/sessions/` + `state_5.sqlite` | Rollout JSONL + metadatos SQLite |
| **Qwen CLI** | `~/.qwen/projects/` | JSONL por chat |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | SQLite (sesion → mensaje → parte) |
| **Cursor** | `~/Library/Application Support/Cursor/User/` (macOS) | SQLite (`state.vscdb` clave-valor: bubbles del composer) |

Las sesiones se auto-descubren al iniciar. Sin configuración, sin claves API, sin servicios en la nube.

---

## Integracion con Git y GitHub

Registra un repositorio git para desbloquear Project Pulse y Code Timeline:

```bash
cd /path/to/your/repo
mool repo add                            # Registra el directorio actual
```

Esto ingesta el historial de commits y comienza a consultar GitHub para issues, PRs, milestones y Projects v2.

```bash
mool repo list                           # Mostrar repos registrados
mool repo remove                         # Desregistrar el repo actual
mool repo sync --all                     # Re-ingestar historial completo
```

Todos los subcomandos de `repo` usan el directorio actual cuando no se especifica una ruta.

### Token de GitHub

El token se resuelve automáticamente en este orden:

1. `gh auth token` (GitHub CLI — recomendado)
2. Variable de entorno `GITHUB_TOKEN`
3. `~/.moolmesh/config.toml` → `[github] token = "..."`

Para **repos públicos**, no se necesita token — el historial de commits funciona sin acceso a la API de GitHub.

Para **repos privados**, se requiere un token con scope `repo`. La forma más fácil:

```bash
gh auth login                            # Seguir los prompts, seleccionar scope repo
```

Si no tenés GitHub CLI, configurá la variable de entorno o agregalo al config:

```toml
# ~/.moolmesh/config.toml
[github]
token = "ghp_xxxxxxxxxxxxxxxxxxxx"
```

Sin un token válido, `mool repo add` funciona igual — ingesta el historial local de git, pero Project Pulse (issues, PRs, milestones) no tendrá datos de GitHub.

---

## Servidor MCP (API inter-agentes)

MoolMesh expone un servidor MCP de solo lectura vía stdio, permitiendo que cualquier agente compatible con MCP consulte los datos de sesión.

El servidor MCP usa [PEP 723](https://peps.python.org/pep-0723/) (inline script metadata) para sus dependencias (el paquete `mcp`). Esto mantiene MoolMesh con cero dependencias externas mientras permite que el servidor MCP funcione de forma independiente.

### Configuración rápida

```bash
mool mcp setup                  # Claude Code (global, scope usuario)
mool mcp setup claude-desktop   # Claude Desktop (macOS/Linux/Windows)
mool mcp setup json             # Imprimir JSON para cualquier cliente MCP
```

El comando auto-detecta el método de instalación (pipx/pip/source), encuentra las rutas correctas de Python y del servidor, y verifica la dependencia `mcp`. Si `mcp` no está instalado, muestra el comando exacto, o usa `--install-deps` para instalarlo automáticamente:

```bash
mool mcp setup --install-deps   # También ejecuta: pipx inject moolmesh mcp
```

Usa `--dry-run` para previsualizar cambios sin modificar archivos.

### Configuración manual

Si preferís configurar manualmente, estos son los dos escenarios comunes:

**Desde el código fuente** (requiere [uv](https://docs.astral.sh/uv/)):

```json
{
  "mcpServers": {
    "moolmesh": {
      "command": "uv",
      "args": ["run", "/path/to/moolmesh/hub/mcp_server.py"]
    }
  }
}
```

**Desde pipx/pip** (requiere `pipx inject moolmesh mcp`):

```json
{
  "mcpServers": {
    "moolmesh": {
      "command": "/ruta/a/pipx/venvs/moolmesh/bin/python",
      "args": ["/ruta/a/pipx/venvs/moolmesh/lib/.../hub/mcp_server.py"]
    }
  }
}
```

### Herramientas disponibles

| Herramienta | Descripción |
|-------------|-------------|
| `get_recent_events` | Últimos N eventos de todos los proveedores |
| `get_active_sessions` | Sesiones activas en las últimas N horas |
| `get_token_usage` | Consumo de tokens por proveedor |
| `get_tool_stats` | Herramientas más usadas por los agentes de IA |
| `search_events` | Búsqueda de texto completo en resúmenes de eventos |
| `get_project_activity` | Resumen completo del proyecto con estadísticas |

Recursos: `hub://schema` (esquema de base de datos), `hub://projects` (lista de proyectos con estadísticas).

El servidor abre SQLite en modo solo lectura (`?mode=ro`). Se ejecuta como un proceso separado (~15-20 MB RAM), independiente del dashboard.

---

## Resúmenes narrativos (Digests)

Code Timeline genera resúmenes diarios y semanales para cada repositorio registrado:

| Nivel | Qué contiene | Cuándo |
|-------|-------------|--------|
| **L1** | Estadísticas SQL crudas (commits, PRs, issues, LOC) | Siempre disponible |
| **L2** | Plantilla estructurada con puntos clave | Siempre disponible |
| **L3** | Párrafo narrativo generado por LLM | Cuando hay un proveedor LLM configurado |

L3 funciona con cualquier API compatible con OpenAI. Configura en `~/.moolmesh/config.toml`:

```toml
[llm]
provider = "openrouter"
api_url  = "https://openrouter.ai/api/v1"
model    = "google/gemma-4-31b-it:free"
api_key  = "sk-or-v1-..."
```

Proveedores soportados: OpenRouter, OpenAI, Together, Groq, Ollama. Si el LLM no está disponible, los resúmenes caen automáticamente a L2.

---

## Reportes por lotes

Genera reportes de análisis en Markdown desde la línea de comandos:

```bash
# Reporte automático — escribe en ~/.moolmesh/reports/
mool report auto

# Contenido completo (sin truncar)
mool report auto --complete

# Filtrar por proyecto o proveedor
mool report --project myapp --provider claude --output ./exports
```

---

## Referencia del CLI

```
mool <comando> [opciones]

Comandos:
  dashboard              Iniciar el dashboard de monitoreo en vivo
  daemon start           Ejecutar el dashboard como servicio en background
  daemon stop            Detener el servicio en background
  daemon status          Mostrar PID, uptime, tamaño de log
  daemon restart         Reiniciar el servicio en background
  status [--json]        Atajo rápido para daemon status
  mcp setup [TARGET]     Configurar servidor MCP (claude-code|claude-desktop|json)
  doctor                 Ejecutar diagnóstico del sistema
  install                Instalar comando mool globalmente (~/.local/bin)
  report                 Generar reportes de análisis en Markdown por lotes
  discover [--json]      Listar todos los proyectos de agentes de IA descubiertos
  repo add [PATH]        Registrar un repositorio git (por defecto: directorio actual)
  repo list              Listar repos registrados con conteo de commits
  repo remove [PATH]     Desregistrar un repositorio (por defecto: directorio actual)
  repo sync [PATH]       Re-ingestar historial de commits
  query events           Eventos recientes como JSON
  query sessions         Sesiones activas como JSON
  query tokens           Uso de tokens por proveedor como JSON
  query tools            Herramientas más usadas como JSON
  query search TEXTO     Buscar eventos por texto como JSON
  query project NOMBRE   Resumen de actividad de un proyecto como JSON

Opciones globales:
  --version              Mostrar versión y salir

Opciones del dashboard / daemon:
  --port PORT            Puerto del servidor (por defecto: 5200)
  --host HOST            Host del servidor (por defecto: localhost)
  --project NAME         Filtrar por nombre de proyecto
  --providers LIST       Separados por coma: claude,codex,qwen,opencode

Opciones de reportes:
  --complete             Modo contenido completo: sin truncar
  --output DIR           Directorio de salida
  --provider PROVIDER    Filtrar por proveedor
```

### CLI para agentes (`mool query`)

Para agentes sin soporte MCP, `mool query` expone los mismos datos del servidor MCP via JSON en stdout:

```bash
# Últimos 10 eventos
mool query events -n 10

# Sesiones activas en las últimas 2 horas
mool query sessions --hours 2

# Consumo de tokens por proveedor desde una fecha
mool query tokens --since 2026-06-01

# Herramientas más usadas en un proyecto
mool query tools --project moolmesh -n 5

# Buscar eventos que mencionen "daemon"
mool query search "daemon" --provider claude

# Resumen completo de actividad de un proyecto
mool query project moolmesh
```

Toda la salida es JSON válido — compatible con `jq`, parseable en cualquier lenguaje, o usable desde llamadas subprocess de agentes. También: `mool status --json` y `mool discover --json` para salida parseable por máquinas.

### Endpoint de salud

Cuando el dashboard está corriendo, `GET /health` retorna:

```json
{"status": "healthy", "version": "1.4.0", "uptime_seconds": 3600, "events_count": 45231}
```

---

## Arquitectura

```
hub/
  parsers/         Parsers JSONL + SQLite para cada proveedor
  adapters/        Normalizar entradas del proveedor → eventos unificados
  watchers/        Cosechadores de archivos: descubrir → offset → parsear → almacenar → SSE
  harvesters/      GitHarvester (120s) + GitHubHarvester (15s/60s)
  integrations/    GitHubClient (REST + GraphQL) + clientes LLM
  digests/         L1 Stats → L2 Plantilla → L3 Narrativa LLM
  correlation/     Enlaces AI ↔ Git: Co-Author, refs a issues, timestamps
  dashboard/       Servidor HTTP + SSE + 4 paginas HTML
  cache/           EventStore (events.db) + GitStore (github.db)
  mcp_server.py    Servidor MCP stdio (solo lectura, PEP 723 deps inline)
  cli.py           Punto de entrada del CLI
```

### Flujo de datos

1. **Discovery** escanea los directorios de cada proveedor buscando archivos de sesion
2. **Parsers** leen JSONL o consultan SQLite generando entradas tipadas
3. **Adapters** normalizan a `UnifiedEvent` con campos comunes
4. **Watchers** hacen polling incremental, almacenan atomicamente en SQLite y envian via SSE
5. **Dashboard** sirve el feed en vivo + analytics via HTTP + Server-Sent Events

Todo el estado se persiste en SQLite. Seguro ante crashes, semantica exactly-once via offsets transaccionales.

---

## Persistencia

| Base de datos | Ruta | Contenido |
|---------------|------|-----------|
| `events.db` | `~/.moolmesh/events.db` | Eventos de sesiones de IA, offsets de archivos, búfer de replay SSE |
| `github.db` | `~/.moolmesh/github.db` | Repos, commits, issues, PRs, milestones, resúmenes |

Ambas bases de datos se crean automáticamente. El esquema migra al iniciar.

---

## Confiabilidad

- **SSE sin brechas** — los campos `id:` habilitan reconexion del navegador con replay desde SQLite
- **Offsets transaccionales** — eventos y posiciones de archivo se actualizan en una sola transaccion
- **Seguridad ante crashes de Git** — excepciones capturadas por repositorio, timeout de 60s en `git fetch`
- **ETags de GitHub** — las respuestas 304 no consumen rate limit
- **Fallback de resúmenes** — LLM no disponible → plantilla L2, sin repos → estadísticas L1
- **Seguridad WAL de OpenCode** — SQLite solo lectura con timeout, nunca bloquea escrituras de OpenCode

---

## Hoja de ruta

MoolMesh empezó con agentes de programación, pero la visión es más amplia — cualquier agente autónomo que genere señales observables pertenece a la malla.

| Estado | Versión | Alcance |
|--------|---------|---------|
| **Entregado** | v1.6 | 4 proveedores (Claude, Codex, Qwen, OpenCode), metadata de sesiones, export de transcripts, búsqueda full-text, correlación por branch, vinculación cruzada de sesiones |
| **Planeado** | v1.7 | Nuevos proveedores: Aider, GitHub Copilot CLI, Pi |
| **Planeado** | v1.8 | Template de proveedor y guía de contribución |
| **Futuro** | v2.0 | Soporte para agentes autónomos: Hermes, Odyssey, Goose |
| **Visión** | v2.x | Observabilidad a nivel organización, multi-usuario, analytics cross-repo |

Consulta [ROADMAP.md](ROADMAP.md) para planes detallados, preguntas abiertas y principios de diseño.

---

## Limitaciones

- **Óptimo en macOS, compatible con Linux** — macOS usa `kqueue` para detección instantánea; Linux usa polling (~1s)
- **Sin autenticación** — el dashboard se vincula a localhost. Usa un proxy inverso para acceso remoto
- **Diseño mono-usuario** — no está pensado para despliegues multi-usuario o en servidor
- **Python 3.11+** — utiliza `tomllib` de la biblioteca estandar
- **Solo GitHub Projects v2** — Projects clasicos (v1) no estan soportados
- **Limitaciones de Cursor** — Cursor no guarda timestamps por mensaje localmente (MoolMesh los aproxima desde la metadata del composer) y su esquema en disco es reverse-engineered, por lo que una actualización de Cursor puede reducir temporalmente la ingesta hasta ajustar el parser

---

## Desarrollo

```bash
# Ejecutar todos los tests
pytest tests/ -v

# Ejecutar con cobertura
pytest tests/ -v --cov=hub
```

593 tests. Cero dependencias externas. Python stdlib + SQLite.

Consulta [CONTRIBUTING.md](CONTRIBUTING.md) para las guías de contribución.

---

## Licencia

[MIT](LICENSE) — Tu telemetria es tuya.
