> [English version](https://github.com/fmicalizzi/moolmesh/blob/main/README.md)

<p align="center">
  <img src="https://raw.githubusercontent.com/fmicalizzi/moolmesh/main/docs/cli-banner.svg" alt="MoolMesh CLI" width="560">
</p>

# MoolMesh

**La malla de contexto para agentes autónomos.**

Observabilidad unificada, telemetría y coordinación entre agentes — ejecutándose completamente en tu máquina.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/fmicalizzi/moolmesh/blob/main/LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://python.org)
[![Tests: 2523 passing](https://img.shields.io/badge/Tests-2523%20passing-green.svg)](#desarrollo)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero-brightgreen.svg)](#)
[![PyPI](https://img.shields.io/pypi/v/moolmesh.svg)](https://pypi.org/project/moolmesh/)

---

## ¿Por qué MoolMesh?

El desarrollo de software moderno ya no es humano-contra-teclado. Es un ecosistema de agentes de IA trabajando en paralelo — cada uno con sus propios logs, contadores de tokens y trazas de razonamiento, todos encerrados en silos separados.

Cuando Claude Code se queda atrapado en un bucle, tus otros agentes no lo saben. Cuando gastas tokens en cuatro proveedores, no puedes ver qué commit lo justificó. Cuando tu equipo usa diferentes herramientas de IA en el mismo repositorio, nadie tiene la imagen completa.

**MoolMesh congrega lo que está disperso.** Auto-descubre sesiones de todos los agentes de programación con IA principales, las normaliza en una única base de datos consultable y expone ese estado tanto a humanos (vía dashboard) como a máquinas (vía MCP).

Lee nuestra [Filosofía](https://github.com/fmicalizzi/moolmesh/blob/main/PHILOSOPHY.md) para entender el doble axioma detrás de MoolMesh: **Human-First y Agent-First**.

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

### Instalar

**Requiere Python 3.11 o superior.** MoolMesh usa `match/case`, `tomllib` y otras funcionalidades que no existen en versiones anteriores. Python 3.10 llegó a fin de vida en octubre 2026.

```bash
# Recomendado — instalación aislada, comando global
pipx install moolmesh

# O con pip (dentro de un entorno virtual)
pip install moolmesh
```

> **Nota:** En Linux moderno (Ubuntu 22.04+, Debian 12+, Fedora 38+),
> `pip install` fuera de un entorno virtual está bloqueado por
> [PEP 668](https://peps.python.org/pep-0668/). Usa `pipx` en su lugar,
> o crea un venv primero:
> ```bash
> python3 -m venv ~/.venvs/moolmesh && source ~/.venvs/moolmesh/bin/activate
> pip install moolmesh
> ```

### Windows

```powershell
# Recomendado — maneja PATH y dependencias automáticamente
uv tool install moolmesh

# Alternativa
pipx install moolmesh

# O con pip (puede requerir agregar Scripts al PATH)
pip install moolmesh
```

Si `mool` no se encuentra después de instalar con pip, agrega el directorio Scripts a tu PATH:

```powershell
pip show -f moolmesh | findstr Scripts
$env:PATH += ";C:\Users\TuUsuario\AppData\Local\...\Scripts"
```

### Iniciar

```bash
# Primer plano (bloquea la terminal)
mool dashboard

# Daemon en background (recomendado — sigue corriendo al cerrar la terminal)
mool daemon start
# → abrir http://localhost:5200
```

El daemon funciona en todas las plataformas (macOS, Linux, Windows). Usá `mool daemon stop` para detenerlo y `mool daemon status` para verificarlo (sale con código 1 si un watcher está degradado).

`mool daemon stop` vuelve sólo cuando el proceso terminó; a los 30 s informa "still shutting down (PID N)" y sale con código 1. `mool daemon start`/`restart` esperan a un daemon que todavía se está apagando y a uno que todavía está arrancando (por ejemplo, mientras una migración respalda una base: un aviso único `starting (migrating databases?) …` pasados 10 s, hasta 180 s), así que un stop seguido de un start nunca lo deja caído.

Eso es todo. MoolMesh auto-descubre tus sesiones de IA inmediatamente. No requiere configuración.

> **Ejecutar desde el código fuente:**
> ```bash
> git clone https://github.com/fmicalizzi/moolmesh.git
> cd moolmesh
>
> # macOS / Linux
> python3 -m venv .venv && source .venv/bin/activate
>
> # Windows (PowerShell)
> python -m venv .venv; .venv\Scripts\Activate.ps1
>
> pip install -e ".[dev]"
> mool dashboard
> ```

### Arrancar al iniciar sesión

`mool daemon start` no sobrevive a un reinicio. Para que el daemon vuelva solo,
registralo en el supervisor del sistema (opt-in: no se instala nada por defecto):

```bash
mool daemon install            # registra y arranca ya (localhost:5200)
mool daemon install --host 0.0.0.0 --port 5200
mool daemon install --dry-run  # muestra el archivo y los comandos, no cambia nada
mool daemon uninstall          # borra exactamente lo que creó install
```

| SO | Mecanismo | Archivo |
|----|-----------|---------|
| macOS | LaunchAgent, label `dev.moolmesh.daemon` (`launchctl bootstrap gui/<uid>`; si falla, `load -w`) | `~/Library/LaunchAgents/dev.moolmesh.daemon.plist` |
| Linux | unit de systemd `--user` (`Restart=on-failure`, logs en `journalctl --user -u moolmesh.service`) | `~/.config/systemd/user/moolmesh.service` |
| Windows | tarea `MoolMesh` del Programador de tareas al iniciar sesión, con `pythonw.exe` (sin consola) | registro en `~/.moolmesh/autostart-task.json` |

- El proceso supervisado corre siempre `python -m hub.cli daemon start --foreground`
  con el intérprete desde el que instalaste y el `PATH` del momento de instalar
  (launchd y systemd arrancan con uno mínimo; sin `gh` el sync de GitHub se
  queda sin token). ¿Reinstalaste o moviste MoolMesh? Corré `mool daemon install`
  de nuevo — `mool doctor` marca un registro cuyo intérprete ya no existe.
- Con el autoarranque instalado, `mool daemon start|stop|restart` pasan por el
  supervisor (`systemctl --user …`, `launchctl kickstart/kill`, `schtasks /Run`):
  nunca queda un segundo daemon al lado del supervisado.
- `install` nunca pisa un archivo que no creó (por ejemplo una `moolmesh.service`
  escrita a mano): sale con código 1 y muestra el diff. `--force` lo reemplaza
  después de guardar una copia `.bak-<fecha>` (en launchd la copia va a `~/.moolmesh/`).
- **Linux:** sin *linger* una unit de usuario solo arranca cuando iniciás sesión.
  `install` consulta `loginctl show-user "$USER" -p Linger` y, si está apagado,
  te indica `loginctl enable-linger "$USER"` (nunca lo ejecuta por vos).

**Datos viejos.** Las tools MCP que leen el portafolio (`get_portfolio_grouped`,
`get_portfolio_production`) y `get_server_info` traen un bloque `freshness` —
`daemon_running`, `last_git_sync_at`, `last_github_sync_at`, `repos_stale`,
`last_ingest_at`, `data_age_seconds`, `stale` — y `stale: true` arriba cuando el
daemon no corre o el último sync de git / API de GitHub es más viejo que
`[daemon] stale_after_minutes` (default 60) en `~/.moolmesh/config.toml`. En ese
caso el dashboard muestra un banner en todas las páginas y `mool doctor` imprime
una línea de frescura. `daemon.log` rota a `daemon.log.1` al pasar los 10 MB.

**Límites de almacenamiento.** El texto completo que se guarda por evento (la
salida de una herramienta, un mensaje largo) se recorta a
`[daemon] full_text_max_chars` caracteres (default 256000; `0` = sin tope): por
encima, MoolMesh guarda el principio y el final alrededor de una marca
`[… N caracteres omitidos por MoolMesh …]`, así una salida de 20 MB ya no puede
inflar `events.db`. El tope aplica a filas nuevas; el texto guardado antes queda
como está.

**La historia es historia por cualquier camino.** Un evento cuya hora propia es
anterior a la ventana en vivo de 12 h cuando MoolMesh lo lee por primera vez se
guarda como historia (`events.historical = 1`), venga del watcher en vivo, de la
recuperación al arrancar o de `mool backfill`: la primera lectura de una base de
OpenCode o Cursor (meses de sesiones) ya no inunda el feed en vivo ni el SSE, y
las líneas que una sesión de Claude retomada repite con sus fechas originales
quedan fuera de "recientes"; sus mensajes nuevos son en vivo como siempre.

**Respaldos.** Antes de reescribir filas existentes, las migraciones y
`mool backfill --reparse` copian la base a `~/.moolmesh/backups/`; sólo se
conservan las 5 copias más nuevas de `events-*` y las 5 de `workspace-*` (las más
viejas se borran después de cada respaldo nuevo, nunca la que se acaba de hacer).

**Día local.** Los digests, el rollup de workspaces y `get_portfolio_production`
comparten una sola definición de "qué día": la zona de `[user] timezone` (nombre
IANA como `"America/Costa_Rica"`; sin definir = la zona del sistema; un nombre
desconocido cae a la zona del sistema con un aviso en el log). Un PR mergeado a
las 01:00 UTC aparece en el digest del día anterior a UTC-6.
`get_portfolio_production(days)` devuelve `merged_prs_window` y
`closed_issues_window` (mergeado / cerrado en un día local de la ventana) junto a
los totales históricos `merged_prs` y `closed_issues`.

**Días trabajados.** La tira de producción cuenta una sesión en cada día local en
que tuvo eventos en un proyecto, no sólo en el último: una sesión retomada
durante tres días enciende tres días. Cada día cuenta sesiones distintas;
`sessions` cuenta sesiones distintas en la ventana (nunca la suma de los días) y
`active_days` los días distintos. Los días viven en `workspace.db`
(`edge_active_days`). La primera pasada del daemon después de actualizar los
llena desde `events.db`, tras un respaldo de `workspace.db`; cambiar
`[user] timezone` los recalcula igual. Una sesión cuyos eventos ya no están en
`events.db` se fecha por su último evento, como antes.

Las vistas del rollup (`get_portfolio`, `get_portfolio_grouped` y su nivel de
clientes, `mool workspace portfolio`, las filas de `/portfolio`) traen las
mismas cifras junto a los toques, de forma aditiva: `sessions_worked` (sesiones
distintas) y `days_worked` (días locales distintos) en la ventana — un cliente
une las sesiones y días de sus proyectos, así que una sesión compartida por dos
proyectos cuenta una vez. `days_worked_basis` dice de dónde salen:
`edge_active_days`, o `rollup` si la tabla falta o está en otra zona (entonces
`days_worked` es `active_days` y `sessions_worked` es `null`).
`session_touches` y `active_days` no cambian de significado. La CLI muestra
`Nd (Nd/Mses worked)`.

**Worktrees borrados.** El trabajo hecho en un worktree de git sigue contando
para su repo después de borrar el worktree. MoolMesh registra cada raíz git que
ve en disco (`known_git_roots` en `workspace.db`; un worktree vinculado registra
también su checkout principal), y un directorio bajo una raíz registrada que ya
no existe conserva esa identidad en vez de aparecer como un proyecto suelto sin
repo. Una sesión que tocó el checkout y un worktree cuenta una vez para el
proyecto. La primera pasada del daemon después de actualizar, tras un respaldo
de `workspace.db`, conserva lo que ya estaba atribuido a un repo y repliega los
worktrees borrados de los que hay evidencia — la entrada del propio repo en
`.git/worktrees` o un `git worktree add` que una sesión corrió desde el repo —,
nunca por el nombre de una carpeta: una carpeta borrada sin esa evidencia queda
como está. Los conteos van a `hub.log`.
Un `git worktree add` cuenta solo por su comando completo: cada proveedor cuya
llamada de herramienta trae el comando (Claude, Codex, Qwen, OpenCode, Pi,
Antigravity) lo guarda entero en `events.db` cuando la llamada de shell agrega
un worktree, así que un worktree creado y borrado entre dos pasadas igual se
pliega en su repo. Para la historia anterior, una pasada más después de
actualizar —también tras un respaldo de `workspace.db`, y reanudable— lee los
comandos de los propios archivos de sesión de los agentes (solo lectura; solo se
procesan los archivos que mencionan `worktree`). Un comando cuya ruta usa una
variable, o que va con un pipe, no cuenta, y una evidencia que nombra dos repos
para la misma ruta no pliega nada. No hace falta tocar la configuración;
`project_aliases` sigue siendo el respaldo manual para la historia sin
evidencia. El scratchpad de una sesión que corrió dentro de un worktree borrado
también se pliega en el repo del worktree.

**Scratchpads de agentes y repos descartables.** El scratchpad de un agente se
pliega en el proyecto de la sesión que lo usó, en macOS
(`/private/tmp/claude-<uid>/…`), en Linux (`/tmp/claude-<uid>/…`) o bajo otra
raíz temporal (`$TMPDIR`): cuenta la forma
`<tmp>/claude-<uid>/<cwd-codificado>/<uuid-de-sesión>/`. Las demás carpetas del
harness bajo `<tmp>/claude-<uid>/` son herramientas, nunca un proyecto. Un repo
git sin remoto que un agente creó (`git init`) en un scratchpad se pliega igual
en el proyecto de la sesión, y uno bajo una raíz temporal del sistema (`/tmp`,
`/private/tmp`, `/var`, `/private/var`, `/usr`) se muestra como carpeta temporal
en vez de proyecto. Un repo con remoto se identifica por ese remoto y sigue
siendo su proyecto donde sea que se haya clonado. No hay nada que configurar; la
siguiente pasada de clasificación lo aplica.

---

## Actualizar

```bash
pipx upgrade moolmesh --pip-args=--no-cache-dir         # pipx (recomendado)
uv tool upgrade moolmesh --no-cache                      # uv tool
python -m pip install --upgrade --no-cache-dir moolmesh  # pip dentro de un entorno virtual

mool daemon restart                                      # corre la versión nueva
```

`--no-cache-dir` / `--no-cache` omiten la caché local: justo después de un
release, un wheel cacheado (o un mirror atrasado) puede hacer que la
actualización resuelva la versión anterior. Con un gestor como Scoop, usá su
propia actualización (`scoop update moolmesh`).

Después **reiniciá las sesiones de agentes abiertas** (o sus servidores MCP) para
que carguen el código nuevo. Cada sesión de agente mantiene vivo su propio
`hub.mcp_server` hasta que se cierra, así que sigue sirviendo la versión vieja
después de actualizar y de `mool daemon restart`; `mool doctor` lista los que
corren código viejo.

**Qué hace una actualización por su cuenta.** Una migración que reescribe filas
existentes primero copia la base a `~/.moolmesh/backups/` (se conservan las 5
copias más nuevas de `events-*` y las 5 de `workspace-*`), y cada migración
aplicada queda en el log (`Migration N applied: …`): en `daemon.log`, o en
`hub.log` si la aplicó la CLI o un servidor MCP. Un primer arranque ocupado con
ese respaldo puede tardar; `daemon start`/`restart` lo esperan (ver
[Iniciar](#iniciar)). Las notas de actualización de cada versión están en
[CHANGELOG.md](https://github.com/fmicalizzi/moolmesh/blob/main/CHANGELOG.md).

**Si venís de una versión anterior** — sólo lo que todavía requiere una acción.
Cada punto aplica si la versión *desde* la que actualizás es anterior a la
indicada:

- **Anterior a 1.26.2:** los servidores MCP iniciados por 1.26.1 o anteriores no
  pueden avisar que están desactualizados, así que `mool doctor` no los lista.
  Reiniciá una vez todas las sesiones de agentes abiertas.
- **Anterior a 1.27.2:** `mool daemon start` no sobrevive a un reinicio.
  Registrá el daemon con `mool daemon install` (ver
  [Arrancar al iniciar sesión](#arrancar-al-iniciar-sesión)), desde la
  instalación de pipx / uv tool, no desde un virtualenv de desarrollo.
- **Anterior a 1.28.0, usuarios de OpenCode:** corré
  `mool backfill --reparse opencode` (una simulación que no escribe nada) y
  luego `mool backfill --reparse opencode --yes` (antes se respalda events.db).
  Recupera las respuestas, el razonamiento y el detalle de herramientas que las
  versiones anteriores guardaron sin texto, y vuelve a guardar los eventos de
  OpenCode con los timestamps UTC (y las huellas) que introdujo 1.28.0.
- **Anterior a 1.28.1:** los respaldos ahora se podan solos a los 5 más nuevos de
  cada tipo; copiá aparte cualquier respaldo viejo de `~/.moolmesh/backups/` que
  quieras conservar. Los archivos `-wal` / `-shm` vacíos que versiones anteriores
  dejaron junto a la base de un proveedor no se borran; borralos a mano con el
  proveedor cerrado.
- **Anterior a 1.29.0:** las PRs cacheadas antes de actualizar obtienen sus stats
  con `mool repo backfill-pr-stats --all-registered` (200 PRs por corrida; volvé
  a correrlo para continuar).
- **Anterior a 1.35.0, usuarios de Antigravity de escritorio / IDE:** corré
  `mool backfill` para traer la historia más vieja que la ventana de recuperación
  al arrancar.

**Volver a una versión anterior** no está soportado una vez que una versión más
nueva migró tus bases. En particular, las versiones anteriores a 1.30.0 leen los
repos `kind = "remote"` y `kind = "board"` como repos de código: no vuelvas atrás
con repos así registrados.

**Windows:** un daemon en ejecución puede bloquear archivos dentro del venv de
pipx y hacer que `pipx upgrade` falle o se aplique a medias: detenelo antes
(`mool daemon stop`), actualizá y luego `mool daemon start`.

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

`mool daemon install` (ver [Arrancar al iniciar sesión](#arrancar-al-iniciar-sesión)) escribe y administra esta unit por vos; la unit manual de abajo sigue siendo válida.

`mool daemon start` funciona como punto de entrada en primer plano — MoolMesh auto-detecta systemd y se saltea el double-fork:

```ini
# ~/.config/systemd/user/moolmesh.service
[Unit]
Description=MoolMesh Dashboard
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/mool daemon start --port 5200
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now moolmesh
systemctl --user status moolmesh
```

> **Por qué `Restart=always`:** según `systemd.service(5)`, `SIGHUP`, `SIGINT`, `SIGTERM` y `SIGPIPE` cuentan como una salida *limpia*, y `Restart=on-failure` sólo revive salidas distintas de cero o señales no limpias. Así que con `Restart=on-failure` un `kill -TERM` — incluido un `mool daemon stop` desde afuera — nunca se reviviría, sea cual sea el código de salida. `Restart=always` revive el dashboard después de cualquier salida; `systemctl --user stop moolmesh` igual lo detiene para siempre, porque systemd nunca reinicia un servicio que detuvo él mismo.
>
> `Restart=on-failure` sigue siendo una opción válida si sólo querés revivir fallas reales: `mool daemon start`/`mool dashboard` salen con código 1 cuando no pueden arrancar (puerto ocupado, otro MoolMesh en el puerto), y una ejecución supervisada nunca se muda en silencio al puerto siguiente. Pero un proceso terminado queda caído hasta que lo arranques de nuevo.
>
> **Nota:** `mool daemon start` auto-detecta systemd (`$INVOCATION_ID`) y se queda en primer plano, así `Type=simple` funciona correctamente. Fuera de systemd hace double-fork como siempre; `--foreground` fuerza el mismo comportamiento sin la marca de entorno. Bajo systemd, usá `systemctl --user stop/restart moolmesh` en lugar de `mool daemon stop/restart` — MoolMesh avisa, pero no puede reiniciar la unit por vos.

---

## Agentes soportados

| Proveedor | Fuente de sesión | Formato |
|-----------|-----------------|---------|
| **Claude Code** | `~/.claude/projects/` | JSONL por sesión + logs de subagentes |
| **Codex (GPT-5)** | `~/.codex/sessions/` + `state_5.sqlite` | Rollout JSONL + metadatos SQLite |
| **Qwen CLI** | `~/.qwen/projects/` | JSONL por chat |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | SQLite (sesion → mensaje → parte) |
| **Cursor** | `~/Library/Application Support/Cursor/User/` (macOS) | SQLite (`state.vscdb` clave-valor: bubbles del composer) |
| **Pi** | `~/.pi/agent/sessions/` (`$PI_CODING_AGENT_DIR`) | Árbol de sesión JSONL por sesión |
| **Antigravity** (CLI, app de escritorio / IDE) | `~/.gemini/antigravity-cli/` (`$ANTIGRAVITY_APP_DATA_DIR`) y `~/.gemini/antigravity/` | JSONL de transcript por conversación + índice SQLite de sesiones (solo lectura) |

Las sesiones se auto-descubren al iniciar. Sin configuración, sin claves API, sin servicios en la nube.

---

## Integración con Git y GitHub

Registra un repositorio git para desbloquear Project Pulse y Code Timeline:

```bash
cd /path/to/your/repo
mool repo add                            # Registra el directorio actual
mool repo add --remote acme/widgets      # Sin clon: sólo issues, PRs y stats de PR
mool repo add --owner acme --dry-run     # Todos los repos de una org o cuenta, como remotos
```

Esto ingesta el historial de commits y comienza a consultar GitHub para issues, PRs, milestones y Projects v2.

```bash
mool repo list                           # Mostrar repos registrados
mool repo remove                         # Desregistrar el repo actual
mool repo sync --all                     # Re-ingestar historial completo
mool repo backfill-pr-stats --all-registered --limit 200   # +/-, archivos e issues cerradas de PRs viejas
```

Todos los subcomandos de `repo` usan el directorio actual cuando no se especifica una ruta.

**Stats por PR:** el poll de GitHub guarda también, para cada PR nueva o actualizada, sus líneas agregadas y borradas, los archivos cambiados y las issues que cierra (`closingIssuesReferences`, calificadas como `owner/repo#n` — una PR puede cerrar issues de otro repo). Una llamada GraphQL cubre hasta 25 PRs, con un tope de 50 PRs por poll; si la llamada falla, el sync sigue y la PR queda sin stats. Las PRs cacheadas antes del upgrade (o que pasaron el tope) se completan con `mool repo backfill-pr-stats [PATH | --all-registered] [--limit N]` — acotado y reanudable: solo pide las PRs que todavía no tienen stats. El pipeline de PRs del dashboard (`/api/github/prs`) incluye `additions`, `deletions`, `changed_files` y `closes`.

**Export versionado de snapshots:** `mool snapshot export --repo OWNER/REPO --since AAAA-MM-DD --until AAAA-MM-DD [--format json] [--output ARCHIVO]` imprime el snapshot de un repo para un rango de días locales (`[user] timezone`): issues abiertas y cerradas en el período, totales al corte, PRs mergeadas con sus stats (+/-, archivos, `closes` como `owner/repo#n`) y un campo `freshness` — un contrato estable y documentado ([`docs/schema/snapshot.v1.md`](https://github.com/fmicalizzi/moolmesh/blob/main/docs/schema/snapshot.v1.md), `schema_version` `1.0`) para herramientas que se apoyan en MoolMesh. Dos exports del mismo rango son idénticos byte a byte (sin hora de generación en el cuerpo); `--output` escribe de forma atómica. Un rango que el último sync de GitHub del repo no cubre (p. ej. uno que incluye hoy) se rechaza con exit 3 salvo `--allow-incomplete`, que lo marca `freshness.complete = false`. Los agentes obtienen el mismo payload con la herramienta MCP `get_snapshot(repo, since, until, allow_incomplete)`. Sólo lee `github.db` (ninguna llamada nueva a la API) y los repos excluidos responden como no registrados.

**Export de esfuerzo por dispositivo (opt-in):** `mool device init [--label TEXTO]` crea `~/.moolmesh/device.json` (permisos 600: un secreto aleatorio que nunca sale de la máquina, tu etiqueta y un hash del id de la máquina — dejalo fuera de la sincronización de dotfiles). Después `mool effort export --org ORG --timezone IANA --since AAAA-MM-DD --until AAAA-MM-DD --repo OWNER/NAME [--repo …] | --repos-from ARCHIVO [--output ARCHIVO]` escribe, **sólo para los repos listados**, una fila por `(repo, día local, proveedor)` con `sessions` y `touches` ([`docs/schema/effort.v1.md`](https://github.com/fmicalizzi/moolmesh/blob/main/docs/schema/effort.v1.md), `kind` `moolmesh.effort`). Sin lista de repos o sin `device.json` se rechaza; un `device.json` copiado de otra máquina se rechaza hasta `mool device init --reset`. El id de dispositivo del archivo es `HMAC(secreto, org)` — un id distinto por organización. Los repos no listados, `exclude_repos`/`exclude_owners` y las carpetas sin remote no aportan nada (ni fila, ni conteo, ni total). `day` es el día local en `--timezone` (la zona de la organización, no la de esta máquina); `repo_id` es el id numérico de GitHub, así un repo transferido conserva su serie. La medida principal son los días trabajados (`day` distintos); `sessions`/`touches` no se suman entre repos. El encabezado trae `generated_at`, `data_through` y `stale` (el registro del propio daemon no cubre el período); re-exportar un período lo reemplaza. Mismos datos y argumentos dan `rows` idénticas. Sólo lectura, sin llamadas a la API de GitHub, sin identidades de autor. `mool device show [--org ORG]` muestra la etiqueta, el id por org y si el archivo pertenece a esta máquina (nunca el secreto).

**Repos board (backlogs sólo de issues):** un repo que se usa sólo como backlog de issues, sin código, se registra como board — `mool repo add --board acme/backlog` (no hace falta un clon local; también acepta la ruta de un clon). En `config.toml` es `kind = "board"` en su entrada `[[repos]]` (por defecto `"code"`, el comportamiento de siempre; una edición a mano se toma al reiniciar el daemon). Un board no corre `git fetch` ni ingesta commits, sólo el sync de issues de GitHub (las mismas llamadas que cualquier repo — ninguna llamada nueva a la API). El portfolio lo muestra como `board · N abiertas · M cerradas` en vez de "0 entregado" y lo deja fuera del ranking de PR mergeados. Los agentes leen el backlog con la herramienta MCP `get_board_issues(repo, state, label, assignee, min_age_days, max_age_days, days, limit)`: issues filtradas por label, assignee y antigüedad, más las issues abiertas y cerradas en un día local de los últimos `days` días (`[user] timezone`) y el backlog abierto por label. Sirve también para repos de código.

**Repos sólo por remoto (sin clon local):** un repo de código que seguís sólo por GitHub se registra con `mool repo add --remote acme/widgets` — sin clon ni historia git; sus issues, PRs y stats por PR se sincronizan como en cualquier repo de código (`kind = "remote"` en `config.toml`, clave `remote:github.com/acme/widgets`; con `--all` además completa ya todas las issues/PRs). Un board sigue siendo distinto: un board es un backlog, un repo remoto es código cuyas PRs mergeadas son la entrega. Para seguir una organización entera **o una cuenta personal**: `mool repo add --owner acme [--include 'widget*' ...] [--dry-run] [--limit N]` lista sus repos (paginado, hasta `--limit`, 300 por defecto; el listado corta antes si quedan menos de 100 llamadas a la API en la hora) y registra cada uno como remoto; `--dry-run` sólo lista. El portfolio muestra un repo remoto con actividad de GitHub en la ventana como `sin clon` — nunca "0 entregado" por falta de commits; sus PRs mergeadas cuentan como entrega. **La identidad es el id numérico de GitHub** (migración 8 de github.db, columna aditiva `repos.github_id`): el daemon chequea cada repo registrado con un `GET /repos/{owner}/{repo}` cada 6 h como mucho (con ETag, un repo sin cambios es un 304, que no cuenta contra el rate limit; un repo todavía sin id se reintenta cada 10 min), y cuando GitHub responde otro `owner/name` para el mismo id — una transferencia o un rename — se actualizan la fila y la entrada de config existentes, así la serie de issues/PRs conserva su historia; `repo add --remote`/`--owner` con el nombre nuevo renombra en vez de duplicar. `repo remove`, `repo sync` y `get_board_issues` aceptan `owner/repo` para un repo sin clon.

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

## Workspace (observar el trabajo, no sólo las sesiones)

El nombre del directorio de una sesión no es el proyecto en el que trabajó. El **eje Workspace** atribuye cada toque de archivo al proyecto *dueño del archivo* — recupera la imagen real multi-proyecto (M:N) y hace visible una carpeta incluso antes o sin ningún agente (VISION §6).

**Atribuir el trabajo de agentes que ya capturaste** — una pasada de sólo lectura sobre `events.db` que asigna cada `file_path` absoluto a su workspace dueño (escalera de identidad `git-remote → git-root → path-hash`, así sobrevive a renombrar/mover/clonar y funciona incluso sin git):

```bash
mool workspace backfill                  # llena workspace.db (lee events.db en sólo lectura)
mool workspace list                      # workspaces con conteos de sesiones / archivos / toques fs
mool workspace session <session_id>      # qué workspaces tocó una sesión
mool workspace sessions <workspace_key>  # qué sesiones tocaron un workspace
```

**Observar carpetas directamente** — marcá una raíz y MoolMesh vigila sus cambios de archivos, así una carpeta de recolección de materiales, una herramienta que no es CLI (por ejemplo una app de diseño cuya salida sólo se observa por ruta) o un trabajo que no es software se encienden **sin ninguna actividad de agente ni de git**. Estrictamente **opt-in**: sin raíces marcadas, no se observa nada.

```bash
mool workspace root add ~/Projects/campaign --max-depth 4 --exclude drafts
mool workspace root list                 # raíces marcadas
mool workspace root remove ~/Projects/campaign
mool workspace touches <workspace_key>   # toques de filesystem atribuidos a un workspace
```

El watcher es puro stdlib: un escaneo recursivo acotado con un cursor de mtime (sin un watch por archivo), exclusiones por defecto razonables (internos de VCS, carpetas de dependencias, salidas de build, carpetas de sync/caché) y un límite `max_depth`. Todos los toques de ruta van a un **`workspace.db` separado** — el hot path de `events.db` y el stream SSE nunca se tocan. Poné `hide_project_names = true` bajo `[workspace]` en `~/.moolmesh/config.toml` para enmascarar los nombres de carpetas en la superficie visible.

**Ver el portafolio** — todos los toques de ruta se agregan en un **portafolio de toda la máquina** que es *agnóstico a la señal*: un workspace se enciende ya sea que la actividad venga de una sesión, del filesystem o de git. Encima, un **`delivery_candidate`** local marca trabajo probablemente entregado — presentado como *candidato con confianza, nunca como un hecho* (quietud más una segunda señal registrada: el cierre de una sesión, un commit de git o un artefacto nuevo en la raíz; sin entidades en la nube, sin LLM).

```bash
mool workspace rollup                    # (re)construye el rollup del portafolio + corre el detector
mool workspace portfolio                 # workspaces calientes, por señal
mool workspace delivery                  # candidatos de entrega (con confianza + la 2.ª señal)
```

El dashboard suma una vista **`/portfolio`** que se lee al cargar sobre `workspace.db` (el stream SSE no se toca). Los agentes pueden leer todo esto por MCP con las herramientas de workspace de sólo lectura (`list_workspaces`, `get_session_workspaces`, `get_workspace_sessions`, `get_workspace_touches`, `get_portfolio`, `get_workspace_activity`, `get_delivery_candidates`).

**Marca de equipo + orden por config (v1.26)** — todas las claves son opcionales y vacías por defecto:

- `[user] emails = ["you@example.com"]` — tus emails de commit (sin distinguir mayúsculas). Un estado caliente de proyecto sostenido *sólo* por commits de otras personas lleva `team_only` y la UI muestra el chip `· equipo`; las sesiones locales y los toques de filesystem siempre cuentan como tuyos. Nunca se expone nada por persona, y sin emails configurados no se marca nada.
- `[workspace] project_aliases = { "<ruta o clave vieja>" = "<ruta o clave canónica>" }` — pliega una carpeta movida/renombrada (o una ruta interna de contenedor como `/app`) en el proyecto real: su historia, estado y outcome se mueven con ella, y no queda fila de origen. Un destino inexistente sólo avisa.
- `[workspace] reference_containers = ["<ruta>"]` — la carpeta y sus hijas van a la sección "Contenedores / referencia" (nunca proyectos), y su actividad ya no hace activo al padre. Funciona aunque la carpeta ya no exista.
- `[workspace] temporary_containers = ["<ruta>"]` — las hijas directas se muestran en su propia sección "Temporal", visible y colapsada, conservando su estado e historia.

**Un cliente con varios dueños, registro compartido, repos confidenciales (#95)** — todas las claves son opcionales y vacías por defecto:

- `[workspace] client_owner_aliases = { "acme-labs" = "acme" }` — asigna un dueño entero (una organización o una cuenta personal) a un cliente. Todo repo de ese dueño, incluso uno creado mañana, cae en el cliente sin override por proyecto. La escalera de atribución, gana la primera coincidencia: **`client_overrides` (por proyecto) > `client_owner_aliases` (por dueño) > `personal_orgs` / `client_orgs` > el resto** (externos / referencia, carpeta compartida). El alias también aplica a una carpeta padre sin git (`_acme-labs`) y suma para el cliente en la mayoría de repos hijos. Un alias gana sobre `personal_orgs`, así una cuenta que es "tuya" en una máquina puede agruparse bajo un cliente.
- `[workspace] client_registry = "~/shared/clients.toml"` — registro de clientes opcional y **de sólo lectura** que varias máquinas comparten para agrupar igual (una ruta relativa es relativa a `~/.moolmesh`). Se fusiona debajo de los valores locales: la config local gana en una clave en conflicto, las listas se unen y nunca se copia a `config.toml`. Si falta o es inválido se avisa una vez en el log y se sigue con la config local. Formato (todas las claves opcionales; `personal_orgs` es por máquina a propósito y no se lee de ahí):

  ```toml
  version = 1
  client_orgs = ["acme", "globex"]
  exclude_owners = ["initech"]
  exclude_repos = ["globex/secret-plans"]

  [client_owner_aliases]
  "acme-labs" = "acme"

  [client_overrides]
  "git_remote:github.com/globex/widget-services" = "acme"
  ```

- `[workspace] exclude_owners = ["initech"]`, `exclude_repos = ["globex/secret-plans"]` — repos confidenciales que **nunca se leen**: ni `git fetch`/`git log`, ni llamadas a la API de GitHub (issues, PRs, milestones, projects, chequeo de identidad); `repo add` (ruta, `--board`, `--remote`, `--owner`) los rechaza (`--owner` sobre un dueño excluido se rechaza antes de listar nada), `repo sync`/`backfill-pr-stats` los saltean, y desaparecen del portfolio, de las herramientas MCP (`get_portfolio`, `get_portfolio_grouped`, `get_portfolio_production`, `get_board_issues`, `get_snapshot`) y de `/api/repos`. La comparación ignora mayúsculas; `exclude_repos` acepta `owner/repo`, `github.com/owner/repo` o una URL de GitHub. Un repo registrado **antes** de excluirlo conserva sus datos guardados — el daemon sólo lo saltea (se loguea una vez por repo). Para borrarlos: `mool repo remove <ruta | owner/repo>` (entrada de config y sus filas de `github.db`). Registrar un clon local por ruta igual lee una vez su URL de `origin` para saber el dueño, y ahí se detiene.

**Desglose por persona, opt-in (#83)** — el outcome del portfolio no distingue personas por defecto. Un coordinador de equipo puede encender un desglose por login de GitHub, leído sólo de lo que `github.db` ya tiene cacheado (ninguna llamada nueva a GitHub):

```toml
[team]
per_contributor = true                                  # default false
contributor_aliases = { "alice-acme-old" = "alice-acme" } # opcional; varios logins → un alias se suman
bot_logins = ["ci-runner-acme"]                         # opcional; se suma a dependabot, *[bot], app/*
```

Encendido, `get_portfolio_production` (MCP) y `/api/workspace/portfolio/production` traen `per_contributor: true` y, por proyecto, `contributors` (`{login o alias: PRs mergeadas, issues cerradas / abiertas, totales y en la ventana, y líneas +/- de PR cuando sus stats están cacheadas}`) y `contributors_bots`; los digests diario y semanal suman un bloque "Por persona"; la tarjeta de producción del dashboard muestra "vista por persona activa" y el desglose en el tooltip de cada proyecto. Una PR mergeada se acredita a su autor, una issue a su primer assignee que no sea bot (si no, a su autor); cada ítem a una sola persona, así que la suma por persona es el total menos los bots. Los bots — y los ítems sin login — cuentan en los totales, nunca como persona. Con la opción apagada (o ausente) toda salida es byte por byte la de antes. Los digests ya cacheados conservan su texto hasta regenerarse.

**Historia completa una vez:** `mool repo sync --all-registered [--all | --days N]` sincroniza todos los repos registrados en una pasada acotada y reanudable (el error de un repo nunca frena al resto) e imprime un resumen. Con `--all` ingesta la historia git completa **y** la historia completa de issues/PRs (el poll incremental tiene un tope de 1000 ítems por repo), y marca cada repo `history_complete`; hasta entonces el portafolio rotula los totales de outcome `desde <primer commit ingestado>` en lugar de "all-time". Los repos registrados que nunca se encendieron localmente se pliegan en un subgrupo colapsado "registrados sin actividad" por cliente, y un remote de GitHub que nunca se registró dice "no medido" (distinto de "— sin repo" y de un "0 entregado" medido).

---

## Servidor MCP (API inter-agentes)

MoolMesh expone un servidor MCP de solo lectura vía stdio, permitiendo que cualquier agente compatible con MCP consulte los datos de sesión.

El servidor MCP usa [PEP 723](https://peps.python.org/pep-0723/) (inline script metadata) para sus dependencias (el paquete `mcp`). Esto mantiene MoolMesh con cero dependencias externas mientras permite que el servidor MCP funcione de forma independiente.

### Configuración rápida

```bash
mool mcp setup                  # Claude Code (global, scope usuario)
mool mcp setup claude-desktop   # Claude Desktop (macOS/Linux/Windows)
mool mcp setup cursor           # Cursor IDE
mool mcp setup codex            # Codex (OpenAI CLI)
mool mcp setup qwen             # Qwen CLI
mool mcp setup opencode         # OpenCode
mool mcp setup antigravity      # Antigravity (~/.gemini/config/mcp_config.json)
mool mcp setup json             # Imprimir JSON para cualquier cliente MCP
```

El comando auto-detecta el método de instalación (pipx / uv tool / pip / venv), encuentra las rutas correctas de Python y del servidor, y verifica la dependencia `mcp`. Si `mcp` no está instalado, muestra el comando exacto, o usa `--install-deps` para instalarlo automáticamente:

```bash
mool mcp setup --install-deps
```

`--install-deps` instala `mcp>=1.2.0,<2` (la cota importa: `mcp` 2.x es incompatible) de la forma que requiere tu método de instalación:

| Instalado con | `--install-deps` ejecuta |
|---------------|--------------------------|
| `pipx install moolmesh` | `pipx inject moolmesh "mcp>=1.2.0,<2"` |
| `uv tool install moolmesh` | `uv pip install --python <python del tool> "mcp>=1.2.0,<2"` |
| `pip install moolmesh` (venv o sistema) | `<python> -m pip install "mcp>=1.2.0,<2"` |

Los entornos de `uv tool` no traen pip, así que `python -m pip` no funciona en ellos. Si `uv` no está en tu `PATH`, reinstala incluyendo la dependencia: `uv tool install --with "mcp>=1.2.0,<2" moolmesh --force`.

Usa `--dry-run` para previsualizar sin cambiar nada: con Claude Code imprime los comandos `claude mcp remove` / `claude mcp add` que ejecutaría (nunca los ejecuta), y con los otros clientes la configuración que escribiría.

### Configuración manual

Si preferís configurar manualmente, apuntá el cliente al intérprete Python del
entorno donde está instalado MoolMesh (y `mcp`), y ejecutá el servidor como módulo
(requiere `mcp>=1.2.0,<2` en ese entorno, p. ej. `pipx inject moolmesh "mcp>=1.2.0,<2"`
o `uv pip install --python /ruta/a/python "mcp>=1.2.0,<2"`):

```json
{
  "mcpServers": {
    "moolmesh": {
      "command": "/ruta/a/pipx/venvs/moolmesh/bin/python",
      "args": ["-m", "hub.mcp_server"]
    }
  }
}
```

En Windows el intérprete es `...\Scripts\python.exe`. No uses
`uv run /ruta/a/hub/mcp_server.py`: ejecuta el script en un entorno aislado sin el
paquete `hub` y falla con `No module named 'hub'`.

### Herramientas disponibles

Todas las herramientas son de sólo lectura.

| Herramienta | Descripción |
|-------------|-------------|
| `get_server_info` | Versión en ejecución vs instalada de este servidor MCP, más un bloque `freshness` — llamala si los resultados parecen inconsistentes: un servidor desactualizado hay que reiniciarlo |
| **Eventos y sesiones** | |
| `get_recent_events` | Últimos N eventos de todos los proveedores (`offset` para paginar) |
| `get_active_sessions` | Sesiones activas en las últimas N horas |
| `get_sessions` | Sesiones con metadata (título, modelo, branch, cwd), filtrables por proveedor y branch |
| `get_session_detail` | Metadata, estadísticas y `linked_sessions` de una sesión |
| `get_session_events` | Eventos de una sesión, paginados (hasta 500 por página), con `text_mode` none / snippet / full; las sesiones de Pi muestran sólo la rama activa |
| `get_token_usage` | Consumo de tokens por proveedor |
| `get_tool_stats` | Herramientas más usadas por los agentes de IA |
| `search_events` | Búsqueda de texto completo en resúmenes de eventos (más nuevos primero) |
| `search_session_content` | Búsqueda de texto completo en el texto completo guardado de los eventos |
| `get_project_activity` | Resumen completo del proyecto con estadísticas |
| **Correlación** | |
| `get_branch_sessions` | Sesiones correlacionadas con un branch de git |
| `get_session_chain` | Sesiones vinculadas a una sesión (predecesoras y sucesoras) |
| **Workspace y portafolio** | |
| `list_workspaces` | Workspaces conocidos con conteos de sesiones, archivos y toques de filesystem |
| `get_session_workspaces` | Workspaces que tocó una sesión |
| `get_workspace_sessions` | Sesiones que tocaron un workspace |
| `get_workspace_touches` | Toques de filesystem atribuidos a un workspace |
| `get_workspace_activity` | Actividad por día de un workspace (sesión / filesystem / git) |
| `get_portfolio` | Rollup del portafolio de toda la máquina (con `sessions_worked` / `days_worked`) |
| `get_portfolio_grouped` | El portafolio agrupado por cliente → proyecto → materiales, con `freshness` |
| `get_portfolio_production` | Tira de producción por proyecto: días trabajados, outcome (histórico y en la ventana), desglose por persona opcional, `freshness` |
| `get_delivery_candidates` | Trabajo probablemente entregado, como candidatos con confianza |
| **GitHub** | |
| `get_board_issues` | Issues de un repo por label, assignee y antigüedad, con conteos de abiertas/cerradas por día local |
| `get_snapshot` | El snapshot versionado de `mool snapshot export` ([`docs/schema/snapshot.v1.md`](https://github.com/fmicalizzi/moolmesh/blob/main/docs/schema/snapshot.v1.md)) |

Recursos: `hub://schema` (esquema de base de datos), `hub://projects` (lista de proyectos con estadísticas).

El servidor abre SQLite en modo solo lectura (`?mode=ro`). Se ejecuta como un proceso separado (~15-20 MB RAM), independiente del dashboard.

Un servidor más viejo que la versión instalada lo dice: `get_server_info` devuelve
`stale: true`, las herramientas que devuelven un objeto agregan la clave `stale_server`
y la primera detección queda registrada una vez en el log. Además cada servidor se
anota en `~/.moolmesh/run/mcp-<pid>.json` (se borra al salir) para que `mool doctor`
liste los que corren código viejo — es estado propio de MoolMesh, no datos de proveedores.

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
  dashboard              Iniciar el dashboard de monitoreo en vivo (primer plano)
  daemon start           Ejecutar el dashboard como servicio en background (--foreground: quedarse adelante)
  daemon stop            Detener el servicio en background (vuelve cuando el proceso terminó)
  daemon status          Mostrar PID, uptime, autoarranque, tamaño de log (exit 1 si un watcher está degradado)
  daemon restart         Reiniciar el servicio en background
  daemon install         Arrancar el daemon al iniciar sesión (launchd / systemd --user / Task Scheduler)
  daemon uninstall       Quitar el autoarranque creado por daemon install
  status [--json]        Atajo rápido para daemon status
  doctor                 Ejecutar diagnóstico del sistema (frescura, autoarranque, servidores MCP viejos)
  install                Instalar comando mool globalmente (~/.local/bin)
  discover [--json]      Listar todos los proyectos de agentes de IA descubiertos
  backfill               Importar historia de sesiones fuera de la ventana en vivo
  sessions               Listar sesiones con metadata (--hours, --provider, --branch, --json)
  export SESSION_ID      Exportar el transcript de una sesión (--format markdown|json, --output ARCHIVO)
  link ORIGEN DESTINO    Vincular dos sesiones relacionadas (--type continues|references|reviews)
  chain SESSION_ID       Mostrar las sesiones vinculadas a una sesión
  detect-links           Detectar vínculos temporales entre sesiones (--auto los guarda)
  report [auto]          Generar reportes de análisis en Markdown por lotes
  repo add [PATH]        Registrar un repositorio git (por defecto: directorio actual; --board, --remote, --owner)
  repo list              Listar repos registrados con conteo de commits
  repo remove [PATH]     Desregistrar un repositorio (por defecto: directorio actual; owner/repo si no hay clon)
  repo sync [PATH]       Re-ingestar historial de commits (--all, --all-registered)
  repo backfill-pr-stats Traer +/-, archivos e issues cerradas de PRs cacheadas
  snapshot export        Snapshot JSON versionado de un repo para un rango de días
  device init|show       Identidad de dispositivo opt-in para el export de esfuerzo
  effort export          Esfuerzo por dispositivo (repos listados, días locales) en JSON
  workspace ...          Eje Workspace: backfill, list, session, sessions, touches,
                         rollup, classify, portfolio, delivery, root add|list|remove
  mcp setup [TARGET]     Configurar servidor MCP (claude-code|claude-desktop|cursor|codex|qwen|opencode|antigravity|json)
  query events           Eventos recientes como JSON
  query sessions         Sesiones activas como JSON
  query tokens           Uso de tokens por proveedor como JSON
  query tools            Herramientas más usadas como JSON
  query search TEXTO     Buscar eventos por texto como JSON
  query project NOMBRE   Resumen de actividad de un proyecto como JSON
  query chain SESSION_ID Cadena de sesiones como JSON

Opciones globales:
  --version              Mostrar versión y salir

Opciones del dashboard / daemon:
  --port PORT            Puerto del servidor (por defecto: 5200)
  --host HOST            Host del servidor (por defecto: localhost)
  --project NAME         Filtrar por nombre de proyecto
  --providers LIST       Separados por coma: claude,codex,qwen,opencode,cursor,pi,antigravity
  --log-level LEVEL      sólo dashboard: DEBUG|INFO|WARNING|ERROR (por defecto: INFO)

Opciones de backfill:
  --provider P           claude|codex|qwen|pi|antigravity|all (por defecto: todos los basados en archivos)
  --since AAAA-MM-DD     Sólo archivos modificados desde esta fecha local
  --dry-run              Contar lo que se procesaría; no escribe nada
  --limit N              Como mucho N archivos con datos nuevos por corrida (volver a correr continúa)
  --reparse codex|opencode  Re-ingestar con el parser actual (simula; --yes aplica, después de un respaldo)

Opciones de reportes:
  --complete             Modo contenido completo: sin truncar
  --daily                Sólo reportes por día (modo auto)
  --output DIR           Directorio de salida
  --provider PROVIDER    Filtrar por proveedor
```

Cada subcomando tiene su propio `--help`.

**Transcripts de sesión:** `mool export SESSION_ID` escribe Markdown (por
defecto) o JSON (`--format json`) a stdout o a `--output ARCHIVO`, con todos los
eventos de la sesión. Para las sesiones de Pi, que son árboles (`/tree` vuelve
atrás y ramifica), muestra sólo la rama en la que está el usuario; las ramas
abandonadas quedan en la base y en todas las demás vistas. Las sesiones de los
otros proveedores se muestran en el orden guardado.

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

**Antes del primer arranque del daemon** todavía no existe `~/.moolmesh/events.db`. `mool query …` y `mool sessions --json` imprimen entonces un objeto JSON de error en stdout y salen con **1** (la misma salida `no_data` que `mool snapshot export` y `mool effort export`), sin crear la base:

```json
{"error": "no_data", "message": "...", "hint": "mool daemon start"}
```

Una base que existe pero no se puede leer responde igual con `"error": "db_unreadable"`. `mool sessions` sin `--json` muestra la sugerencia en stderr y sale con 1. (`mool status --json` mantiene la salida 0 con `"running": false`: "no corre" es un estado válido, mientras que una consulta sin datos no es una respuesta.) El SDK `mcp` es opcional: sin él `mool query` y `mool sessions` funcionan y no imprimen nada en stderr; sólo el arranque del servidor MCP avisa que falta.

### Endpoint de salud

Cuando el dashboard está corriendo, `GET /health` retorna:

```json
{"status": "healthy", "pid": 4242, "version": "1.36.1", "uptime_seconds": 3600, "events_count": 45231, "watchers": {"...": "..."}}
```

`status` es `degraded` cuando un watcher en ejecución está muerto o trabado.
`GET /api/freshness` devuelve el mismo bloque `freshness` que las herramientas MCP.

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
| `github.db` | `~/.moolmesh/github.db` | Repos, commits, issues, PRs (y sus stats), milestones, resúmenes |
| `workspace.db` | `~/.moolmesh/workspace.db` | Atribución a workspaces, toques de filesystem, rollup del portafolio, días trabajados |

Las bases se crean automáticamente y migran al iniciar (migraciones versionadas;
una migración que reescribe filas respalda antes en `~/.moolmesh/backups/`, se
conservan las 5 más nuevas de cada tipo). Otros archivos en `~/.moolmesh/`:
`config.toml` (configuración), `daemon.log` (rota a `daemon.log.1` al pasar los
10 MB) y `hub.log` (CLI y servidores MCP), `run/` (el registro de servidores MCP
que lee `mool doctor`), `reports/` (`mool report auto`) y, sólo después de
`mool device init`, `device.json` (permisos 600).

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

MoolMesh empezó con sesiones de agentes de programación, pero la visión es **observar el trabajo, no sólo las sesiones** — atribuir cada toque de archivo al proyecto dueño, y llegar a ver una carpeta directamente, antes o sin un agente (VISION §6).

| Estado | Versión | Alcance |
|--------|---------|---------|
| **Entregado** | v1.6 | Vinculación entre sesiones, metadata de sesiones, export de texto completo, búsqueda full-text, correlación por branch de git |
| **Entregado** | v1.7 | **Proveedor Cursor (5.º)** — Claude, Codex, Qwen, OpenCode, Cursor; `mool mcp setup <cliente>` universal |
| **Entregado** | v1.8 | Daemon en Windows, cliente de GitHub endurecido (reintentos/backoff, paginación), paginación y orden en MCP |
| **Entregado** | v1.9 | Higiene de la base de observación: ciclo de vida honesto de sesiones, clasificación de `tool_result`, timestamps honestos (#16/#17/#18) |
| **Entregado** | v1.10 | **Eje Workspace — Fase A:** resolver `ruta → workspace`, atribución M:N sobre un `workspace.db` separado (#20) |
| **Entregado** | v1.11 | **Eje Workspace — Fase B:** watcher de filesystem — observa carpetas marcadas directamente, sin agente ni git; raíces opt-in + cursor de mtime en `workspace.db` (#21) |
| **Entregado** | v1.12 | **Eje Workspace — Fase C:** rollup del portafolio agnóstico a la señal (sesión + filesystem + git) + `delivery_candidate` (candidato con confianza); vista `/portfolio` que se lee al cargar (#22) |
| **Entregado** | v1.13 | **Inteligencia de portafolio — Etapa 1:** pliega las carpetas de harness de agentes sobre su proyecto real (cwd de sesión + decodificación validada) + agrupación jerárquica (subcarpetas/materiales anidados, dotfolders de config despriorizadas); `workspace_classification` aditiva, vista `/portfolio` agrupada (#24) |
| **Entregado** | v1.14 | **Inteligencia de portafolio — Etapa 2:** producción en el tiempo — tira de contribución por proyecto (sesiones/día por `created_at` honesto de ingesta), coloreada por agente, ordenada por recencia, selector 4d/semana/mes; esfuerzo = sesiones + días activos (no duración); conteo de entregables imagen/video; SVG inline (cero dependencias); arreglo del caret de proyectos hoja (#24) |
| **Entregado** | v1.15 | **Inteligencia de proyectos — Unidad 1: capa de outcome:** PRs mergeadas / issues cerradas / issues abiertas por proyecto canónico en la vista de producción (entrega autoritativa desde `github.db`, sólo lectura, sin distinguir personas) + flag opt-in explícito `[workspace] filesystem_monitoring` e indicador en el dashboard (sólo controla el monitoreo de carpetas, no el portafolio) (#27) |
| **Entregado** | v1.16 | **Inteligencia de proyectos — Unidad 2: estado derivado del proyecto:** por proyecto canónico un único chip de estado — 🟢 activo · 🟡 enfriándose · 🔵 entregado · 🟠 estancado · ⚪ pausado — que fusiona la actividad local (sesión/fs/git sobre relojes honestos, `_parse_ts`) con la capa de outcome de GitHub; `outcome_measurable` distingue sin git (no medible) de repo con 0 PRs (medible); presentado con su base auditable, nunca como un flag suelto; absorbe la vista de proyectos fríos (#25) (#28) |
| **Entregado** | v1.17 | **Inteligencia de proyectos — Unidad 3: jerarquía cliente/org:** un tercer nivel cliente → proyecto → materiales sobre el portafolio; cliente = org del git remote (sin distinguir mayúsculas/guiones bajos), escalera override → org de git → carpeta padre; orgs dueñas conocidas = nodos cliente (`client_orgs`, auto-sembradas), la org propia del dueño suelta (`personal_orgs`), orgs desconocidas en un cajón externos/referencia, `SHARED-WORKSPACE` como nodo compartido; esfuerzo + outcome + estado agregados al cliente (sin distinguir personas); empieza invisible hasta configurar una identidad de dueño; **cierra la épica #26** (absorbe la atribución de clientes de #24 Etapa 3) (#29) |
| **Entregado** | v1.18 | **Rediseño UX del portafolio — Parte A (layout):** la vista `/portfolio` recupera el espacio horizontal desperdiciado — la tira de producción llena su columna (reemplaza el SVG inline anclado a la izquierda), filas densas de una línea (~2–3× más proyectos por pantalla), nombres limpios (`github.com/` fuera, 1 línea + elipsis + tooltip), outcome como columna de primera clase con tres estados (PR mergeada / 0 real / sin repo no medible), encabezados de columna, estado derivado como punto inicial para escanear; sólo presentación (resolver/estado/outcome #27/#28/#29, SSE y enmascarado sin tocar), cero dependencias nuevas (#35) |
| **Entregado** | v1.19 | **Rediseño UX del portafolio — Parte B (gráficos + KPIs):** un espacio de resumen ejecutivo sobre la lista — una fila de KPIs (proyectos activos, PR mergeados all-time, entregados, estancados, clientes), una barra de distribución de estados (paleta de estado + ícono + etiqueta, nunca sólo color) más un histograma de recencia (`state.age_days`, relojes reales, tramos hoy/1–3d/4–7d/8–30d/>30d) en una tarjeta, y barras top por actividad / top por entrega (un solo tono + etiquetas directas); honestidad de tres estados en el gráfico de entrega (0 real ≠ no medible), paleta validada para evitar el trío categórico que falla (ΔE 14.2); sólo presentación (SVG inline sin dependencias; resolver/estado/outcome #27/#28/#29, SSE y enmascarado sin tocar) (#37) |
| **Entregado** | v1.20 | **Portafolio en vivo — atribución programada:** el daemon atribuye sesiones nuevas a workspaces de forma incremental (cursor `events.id` en `workspace.db`, que avanza sólo después de un commit exitoso) y refresca rollup/candidatos/clasificación ante atribuciones nuevas o cada hora, así `/portfolio` ya no queda viejo hasta un `mool workspace backfill` manual; toggle `[workspace] auto_attribution` (encendido por defecto, independiente de `filesystem_monitoring`); frescura (`last_attribution_at`) expuesta en la API de monitoreo; I/O de disco fuera del lock del store, clasificación escrita atómicamente; `events.db` sigue en sólo lectura (#39) |
| **Entregado** | v1.21 | **Codex en el portafolio + fechado por hora del evento:** el parser de Codex lee el formato actual `custom_tool_call` (`exec` JS / `apply_patch`, extracción sólo por regex, resuelta contra `workdir`, sin truncar); un fallback por cwd atribuye a su directorio de trabajo las sesiones de cualquier proveedor que no tocaron archivos (`via='cwd'`, reemplazado cuando aparece una arista de archivo); la actividad de sesión se fecha por el evento más temprano (`event_ts`, formatos mixtos → día UTC) en vez de la hora de atribución, con una re-derivación automática única de la historia; conteos de sesiones reconciliados desde las aristas (fs/git sin tocar) (#40, #42) |
| **Entregado** | v1.22 | **Frescura del portafolio + aislamiento de tests:** `/portfolio` muestra cuándo se actualizó por última vez ("actualizado hace X") y avisa cuando está viejo, fallando, apagado o esperando un reinicio del daemon (punto + texto, sin nombres/rutas, re-fetch liviano cada 60 s); la suite ya no puede abrir ni migrar el `~/.moolmesh` real (override del home antes de importar + guarda; tests con datos reales opt-in y de sólo lectura); el watcher de carpetas se detiene limpio al apagar (#43, #44) |
| **Entregado** | v1.23 | **Ingesta de historia completa:** un `mool backfill` real y reanudable importa todas las sesiones de Claude / Codex / Qwen fuera de la ventana en vivo de 12 h del daemon (idempotente, por lotes, `--dry-run`/`--since`/`--provider`); el daemon se pone al día después de estar caído sin inundar el SSE; `--reparse codex --yes` re-ingesta sesiones viejas de Codex con el parser actual (respaldo primero, transacción por sesión); los archivos placeholder sólo-nube se saltean sin disparar descargas; un flag `events.historical` mantiene la historia importada fuera del feed "recientes" (#45) |
| **Entregado** | v1.24 | **Portafolio honesto (auditoría #36):** cada proyecto fechado por sus propias aristas de atribución (reloj de actividad por arista) en vez de la última actividad de cualquier sesión que lo tocó; los totales incluyen subcarpetas/materiales anidados como el estado; los comandos de shell ya no se vuelven carpetas; filas de OpenCode del día de instalación fechadas por hora del evento; carpetas temporales/de sistema y app bundles pasan a "sin clasificar"; re-derivación automática única (#56, #57, #58, #61) |
| **Entregado** | v1.25 | **Proveedor Pi + plantilla de proveedores:** MoolMesh observa el agente Pi (parser JSONL consciente del árbol, atribución de archivos, tokens y costo, daemon/backfill/recuperación/CLI/MCP/dashboard); `docs/PROVIDERS.md` + un test de contrato de proveedor abaratan los proveedores nuevos; los comandos de shell nunca se guardan como rutas de archivo; las rutas internas de contenedor (`/app`…) son "sin clasificar"; "Actualizar" en el README con `--no-cache-dir` (#66, #64, #63) |
| **Entregado** | v1.26 | **Equipo y orden:** marca "· equipo" cuando sólo commits de otras personas mantienen caliente un proyecto (`[user] emails`, nada por persona expuesto); repos registrados pero inactivos plegados y "no medido"; orden por config local con `project_aliases` (incl. rutas de contenedor), `reference_containers`, `temporary_containers`; escalón de cliente por org mayoritaria; `mool repo sync --all-registered --all` con rótulos "desde <fecha>" mientras la historia es parcial (#36, #60, #64, #59) |
| **Entregado** | v1.27 | **Proveedor Antigravity CLI (7.º)**; OpenCode ya no pierde las respuestas en streaming (`mool backfill --reparse opencode`); **`mool daemon install`** (launchd / systemd `--user` / Programador de tareas) y un flag de datos viejos medido desde los datos, con rotación de `daemon.log` (1.27.2) (#74, #75, #78, #76) |
| **Entregado** | v1.28 | **Un solo día local** (`[user] timezone`, con DST) para digests, el rollup de workspaces y el portafolio, más la entrega de una ventana de tiempo; timestamps de OpenCode en UTC; robustez de la observación: lecturas en streaming, tope de texto completo por evento, respaldos que se podan solos, datos viejos guardados como historia por cualquier camino, ids de entrada del proveedor (1.28.1) (#81, #79, #52, #54, #62, #67) |
| **Entregado** | v1.29 | **Stats por PR** (+/-, archivos cambiados, issues cerradas como `owner/repo#n`) y `mool repo backfill-pr-stats`; `daemon stop` espera a que el proceso termine, apagado en menos de un segundo (1.29.1) (#80, #96) |
| **Entregado** | v1.30 | **Repos board** (backlogs sólo de issues, MCP `get_board_issues`), **repos sólo por remoto** (`repo add --remote`, `--owner`) con identidad por id de GitHub, alias de dueños, un registro de clientes compartido y repos excluidos (#93, #94, #95) |
| **Entregado** | v1.31 | **Export versionado de snapshots** (`mool snapshot export`, MCP `get_snapshot`, [`docs/schema/snapshot.v1.md`](https://github.com/fmicalizzi/moolmesh/blob/main/docs/schema/snapshot.v1.md)); días de caída re-fechados; todos los módulos loguean bajo `hub.*` y `daemon start` espera un arranque lento (1.31.1–1.31.2) (#82, #101, #107, #108, #111) |
| **Entregado** | v1.32 | **Días trabajados:** la tira de producción cuenta una sesión en cada día local en que trabajó un proyecto (#114) |
| **Entregado** | v1.33 | Rama activa de sesiones en árbol en `mool export` y en los eventos de sesión MCP; `mool mcp setup antigravity`; las lecturas de `.git` nunca disparan descargas de la nube; workspaces huérfanos podados; arreglos de atribución de Codex (#67, #77, #52, #46, #47, #48, #49) |
| **Entregado** | v1.34 | Días trabajados junto a los toques en las vistas del rollup; rama activa de Qwen; los resultados de herramientas en paralelo vuelven a verse (1.34.1); una recuperación tras una caída cuenta en su propio día y `mool export` está completo (1.34.2); migraciones registradas en `hub.log` (1.34.3) (#122, #123, #127, #131, #132, #130, #136) |
| **Entregado** | v1.35 | **Desglose por persona, opt-in** (`[team]`) en el portafolio y los digests; también se lee **Antigravity de escritorio / IDE** (#83, #77) |
| **Entregado** | v1.36 | **Identidad de dispositivo y export de esfuerzo por dispositivo**, opt-in y denegado por defecto (`mool device`, `mool effort export`, [`docs/schema/effort.v1.md`](https://github.com/fmicalizzi/moolmesh/blob/main/docs/schema/effort.v1.md)); el snapshot siempre nombra su zona (1.36.1) (#140, #146) |
| **Siguiente** | — | **Motor mool-ops**, un paquete complementario en su propio repo que lee MoolMesh sólo por `mool snapshot export` y `mool effort export` (#84). La división de contenedores (#30) se cerró como "no por ahora". La visibilidad de equipo a escala de organización sigue diferida a `v2.x`; el core sólo ofrece el desglose opt-in de `[team]`. |
| **Futuro** | v2.x | Workspace Fase D (entre máquinas, opt-in); agentes autónomos; observabilidad a escala de organización |

Consultá [ROADMAP.md](https://github.com/fmicalizzi/moolmesh/blob/main/ROADMAP.md) para planes detallados, preguntas abiertas y principios de diseño.

---

## Limitaciones

- **Óptimo en macOS, compatible con Linux** — macOS usa `kqueue` para detección instantánea; Linux usa polling (~1s)
- **Sin autenticación** — el dashboard se vincula a localhost. Usa un proxy inverso para acceso remoto
- **Diseño mono-usuario** — no está pensado para despliegues multi-usuario o en servidor
- **Python 3.11+** — utiliza `tomllib` de la biblioteca estandar
- **Solo GitHub Projects v2** — Projects clasicos (v1) no estan soportados
- **Limitaciones de Cursor** — Cursor no guarda timestamps por mensaje localmente (MoolMesh los aproxima desde la metadata del composer) y su esquema en disco es reverse-engineered, por lo que una actualización de Cursor puede reducir temporalmente la ingesta hasta ajustar el parser
- **Limitaciones de Antigravity** — se leen el CLI (`agy`) y el directorio de la app de escritorio / IDE (`~/.gemini/antigravity`), y una conversación presente en los dos se ingiere una sola vez (desde el del CLI); no se leen modelo, tokens, costo ni branch de git (el transcript no los trae de forma confiable), y el formato en disco no es una API documentada y el CLI publica varias versiones por semana, por lo que una actualización puede reducir temporalmente la ingesta hasta ajustar el parser (detalles en `docs/PROVIDERS.md`)

---

## Desarrollo

```bash
# Ejecutar todos los tests
pytest tests/ -v

# Ejecutar con cobertura
pytest tests/ -v --cov=hub
```

2523 tests. Cero dependencias externas. Python stdlib + SQLite.

Consulta [CONTRIBUTING.md](https://github.com/fmicalizzi/moolmesh/blob/main/CONTRIBUTING.md) para las guías de contribución.

---

## Licencia

[MIT](https://github.com/fmicalizzi/moolmesh/blob/main/LICENSE) — Tu telemetria es tuya.
