# MoolMesh — Visión y Roadmap

> El norte estratégico de MoolMesh: dónde está el producto hoy y el camino hacia
> adelante. Leé primero [`PHILOSOPHY.md`](PHILOSOPHY.md) para el *por qué*; este
> documento es el *hacia dónde* y *en qué orden*. [`ROADMAP.md`](ROADMAP.md) es el
> log táctico de releases; este documento es la capa por encima.
>
> *(English version: [`VISION_ROADMAP.md`](VISION_ROADMAP.md).)*

---

## 1. Qué es MoolMesh realmente

MoolMesh es **un observador que evoluciona hacia coordinador.** La observación no
es el fin — es el *substrato*. Cada sesión capturada se vuelve memoria compartida
que un agente (o un humano) puede leer para continuar, supervisar o reconciliar
trabajo que ocurrió en otro lado.

Esto ya es así en la práctica, no es aspiración:

- Un agente llama `get_session_chain` o `search_session_content` para retomar lo
  que hizo una sesión previa y continuar desde ahí.
- Un orquestador inspecciona `get_session_detail` para ver qué gastó y qué tocó un
  agente en ejecución antes de decidir el próximo paso.

Eso **es** coordinación — asíncrona, mediada por memoria. El `PHILOSOPHY.md` la
nombra directo: la **superficie A2A (Agent-to-Agent)** de la Interaction Matrix,
*"un orquestador monitorea agentes en ejecución a través de la interfaz MCP".* El
producto simplemente no estaba organizado alrededor de esa verdad. Este documento
lo hace.

---

## 2. La escalera de madurez

MoolMesh madura sobre un solo eje. Cada peldaño se apoya en el de abajo; ninguno
reemplaza la base de observación.

```
   OBSERVE      Capturar cada sesión como eventos unificados.
      │         El humano ve el dashboard; los agentes consultan vía MCP.
      ▼
   CORRELATE    Conectar sesiones: chains, branches, contexto compartido.
      │         "¿Qué hizo la otra sesión? Continúo desde ahí."
      ▼         (coordinación asíncrona / mediada por memoria)
   COORDINATE   Supervisión activa: detección de conflictos, recursos
                compartidos, el mesh en vivo. "¿El agente A deshizo lo de B?"
```

| Peldaño | Estado hoy | Qué significa |
|---------|-----------|---------------|
| **Observe** | **Construido.** 5 providers, dashboard SSE en vivo, superficie MCP de consulta, analytics, Project Pulse, Code Timeline. | El punto ciego está cerrado: eventos unificados entre agentes, persistidos en SQLite, consultables por humano y máquina. |
| **Correlate** | **Parcial y vivo.** Metadata de sesión, storage/búsqueda full-text, cross-session linking (`link`, `detect-links`, `chain`, `get_session_chain`), correlación de branch. | La coordinación asíncrona ya funciona vía MCP. Las señales que la alimentan son todavía mayormente heurísticas (ver §4). |
| **Coordinate** | **Próximo horizonte.** | Convertir la correlación pasiva en supervisión activa: detectar conflictos entre agentes concurrentes, mostrar "quién tocó esto último", hacer el mesh legible en tiempo real. |

El objetivo estratégico no es "más dashboards". Es subir esta escalera manteniendo
la base de observación zero-friction y read-only.

> La base Observe está ganando un **segundo eje** además de la sesión de agente: el
> **Workspace** (carpetas/proyectos como unidad de primera clase, más allá de lo que
> toca un agente). Ver §6. No confundir con la vista "Project Pulse" del dashboard actual.

---

## 3. Dos workstreams alimentan la escalera

El avance viene de dos workstreams independientes. Ninguno es "el plan" por sí solo.

### Amplitud — más fuentes observadas
La Amplitud tiene **dos ramas**. La primera, *más agentes*: cada provider nuevo ensancha
el mesh; agregar uno es un adapter, no un rewrite (el quartet
`model → parser → adapter → watcher`, ~300–500 LOC, sin tocar el core), con retorno
decreciente — el 6.º provider importa menos que hacer coordinar bien a los primeros 5.
La segunda, *el eje Workspace* (§6): observar la carpeta/proyecto como fuente de primera
clase, más allá de la sesión. Cumple la agnosticidad radical más literalmente que el
pipeline de agentes —una carpeta *es* una fuente de eventos observables— y su piso de
filesystem **abarata toda futura amplitud**: un agente no parseado igual deja rastro de
salida.

### Profundidad — coordinación más rica
Correlación más precisa y, eventualmente, coordinación activa. Acá vive la
identidad del producto. La profundidad es donde está el valor diferencial, y hoy
está sub-invertida respecto de la amplitud.

> **Principio de secuencia:** cuando amplitud y profundidad compiten por el mismo
> slot, gana profundidad — salvo que un provider puntual desbloquee un usuario
> concreto. Ensanchar un mesh que todavía no coordina bien es invertir en
> superficie por encima de sustancia.

---

## 4. Corto plazo: endurecer la base Observe (fidelidad del ciclo de vida)

La escalera de coordinación es tan confiable como los eventos que la sostienen.
Tres brechas de correctness en la base Observe/Correlate son la prioridad más
alta, porque todo lo de arriba hereda sus errores. Corresponden a las issues
abiertas.

### 4.1 Ciclo de vida de sesión — inicio, activa, cierre (issue #16)
Hoy "activa" se infiere de dos maneras que no coinciden:
- `sessions.is_active` se pone en `1` al upsert y **nunca vuelve a `0`** — así toda
  sesión parece activa para siempre.
- `get_active_sessions` ignora ese flag y usa una **heurística de ventana temporal**
  (eventos en las últimas N horas).

MoolMesh no tiene una señal real de *fin de sesión*. Infiere actividad por recencia
de eventos, no por si el agente subyacente sigue corriendo.

**Qué tomamos del patrón de tmux-bridge (en evaluación, ver §7):** su disciplina
para *saber si el proceso de un panel está vivo* — detección de proceso/identidad y
un check de conectividad tipo `doctor`. Adaptado al modelo read-only y file-based de
MoolMesh, se traduce en: derivar el cierre de sesión de señales concretas (el
archivo de sesión ya no recibe appends + marcadores de fin propios de cada provider
+ un timeout de inactividad acotado) en vez de asumir actividad perpetua. Una sesión
debe tener un ciclo de vida honesto: `starting → active → idle → closed`.

### 4.2 Fidelidad de eventos — tool results vs mensajes de usuario (issue #17)
Distinguir un *resultado* de tool de un mensaje genuino de usuario con un event type
dedicado `tool_result`. La correlación y cualquier detección de conflictos futura
dependen de leer el stream de eventos con precisión; confundirlos corrompe cada
peldaño de arriba. Además, un `tool_result` limpio es **prerequisito del resolver
`path → workspace`** (§6): sin file paths fiables no hay atribución de proyecto honesta.

### 4.3 Honestidad de timestamps en sesiones reanudadas (issue #18)
Exponer timestamps de ingest / última-actividad de forma distinta a los timestamps
originales de los eventos. Las sesiones reanudadas cargan timestamps originales, lo
que distorsiona el "qué pasó cuándo" — la espina dorsal de la correlación y las
chains.

**Estas tres van primero.** Son baratas, son higiene, y hacen confiable toda la
escalera.

---

## 5. Pipeline de providers (el workstream de Amplitud)

Un único pipeline hacia adelante. El orden favorece providers que sean (a) de bajo
esfuerzo y (b) que desbloqueen casos de uso reales de coordinación, por encima de
los exóticos.

| Provider | Storage / discovery | Esfuerzo | Notas |
|----------|--------------------|----------|-------|
| **Aider** | `~/.aider/history/` (text + SQLite metadata) | Bajo | El formato mejor documentado; el próximo provider de menor fricción. |
| **Pi** | JSONL tree (`~/.pi/agent/sessions/`) | Bajo–Med | Las sesiones-árbol necesitan linearización leaf-to-root en el parser. |
| **Goose** | SQLite (`sessions.db`) + legacy JSONL | Med | Paths cross-platform; `ccusage` es referencia de mapeo. |
| **Copilot CLI** | Logs locales | Med | Formato por confirmar. |
| **Hermes** | SQLite WAL + FTS5 (`~/.hermes/`) | Med–Alto | Agente autónomo; hay que stitchear la parent-session chain. |
| **Odysseus** | SQLite en Docker (`./data/app.db`) | Alto + recon | Agente autónomo; **schema sin documentar — bloqueado en extracción externa de schema.** |
| **Paperclip** | PostgreSQL / PGlite | Alto, patrón nuevo | Control plane, no un agente único. Requeriría un harvester REST/SSE — **el primer provider network-based, que presiona el principio zero-dependency.** Diferir salvo demanda real. |

**Decisión del owner (abierta):** cuáles entran en la próxima ola y en qué orden. La
recomendación de arriba es *bajo esfuerzo primero, agentes autónomos una vez
confirmados sus schemas, Paperclip solo con demanda real*. Los agentes autónomos
(Hermes, Odysseus) además dependen de confirmación externa de schema y nunca deben
bloquear el resto del pipeline.

**Enabler antes de escalar amplitud:** un template de provider + guía de
contribución + auto-detección, para que un provider se agregue escribiendo solo su
quartet — nunca editando el core, el dashboard o el MCP server. Esto vuelve
verificable la "agnosticidad radical" (PHILOSOPHY §2) en vez de aspiracional, y se
paga solo en el próximo provider.

---

## 6. El eje Workspace — observar el trabajo, no solo las sesiones

Hasta acá la base Observe tiene *una* espina: la **sesión de agente**. Pero el proyecto
raíz del que salió MoolMesh (`ai-session-analyzer`) ya era *project-first*: adjudicaba
cada sesión a su carpeta/proyecto con un motor de tres reglas (`cwd → paths → tiempo`).
MoolMesh, al reorganizarse alrededor de la sesión, degradó el proyecto a una decoración.
El **eje Workspace recupera esa raíz** y le agrega la única pieza que nunca existió:
observar la carpeta *directamente*, sin depender de que un agente se auto-loguee.

**El átomo: el path-touch.** La unidad de observación es `(ruta, timestamp, fuente)`.
Tres fuentes lo emiten y **ninguna es privilegiada**: las sesiones (los file paths de
sus `tool_use`, ya persistidos en `events.file_path`), el filesystem (deltas de mtime) y
git (archivos de cada commit). Un **resolver `path → workspace`** atribuye cada toque a
su proyecto, con una escalera de identidad estable (`git-remote → raíz .git → hash de
path`) que sobrevive a renombrar, mover o clonar, y que existe aunque no haya git.

**Vocabulario (no romper lo shipeado).** El campo `project` actual —derivado del nombre
de directorio de la sesión y ya consumido por agentes vía MCP— **queda intacto**. La
atribución nueva (dueño-del-archivo) vive bajo el nombre **`workspace`**, en columnas y
store propios. Son dos nociones distintas a propósito: una sesión en `~/work` que edita
`~/work/repo-a/x.py` es `project="work"` y `workspace="repo-a"`.

**Por qué importa:** con el path-touch, una sesión que toca tres subcarpetas produce
actividad en tres workspaces (M:N, hoy imposible: un evento = un solo `project`); un
proyecto sin ninguna sesión igual se prende con toques crudos del filesystem; y el
**portfolio** es la agregación sobre el árbol de workspaces, agnóstica de qué señal lo
encendió. Es lo que ni MoolMesh, ni su raíz, ni el estado del arte hace hoy: ver la
carpeta *antes/sin* agente ni git — la recopilación de materiales, el trabajo de diseño
con archivos opacos, el agente que no es una CLI estándar.

**Dos niveles de observación.** Esto redefine la Amplitud (§3): un **piso universal**
(el filesystem, cero parser: "algo pasó acá, en este workspace, ahora") + un
**enriquecimiento por-provider** (los parsers de sesión: Q&A, tokens, costo). Un agente
jamás visto obtiene visibilidad de salida gratis; el quartet completo se invierte solo
cuando se quieren métricas ricas.

**Plan por fases** (arco multi-release, no un solo release):

| Fase | Qué | Costo / alcance honesto |
|------|-----|-------------------------|
| **A — Resolver** | `path → workspace` sobre los eventos que **ya** tenemos (`file_path`/`cwd` persistidos). Recupera el motor de la raíz, en vivo. **No toca `linker.py`.** | Bajo. Entrega atribución multi-proyecto correcta del trabajo de *agentes* — la *mitad de recuperación*, **no** el portfolio completo. |
| **B — Filesystem** | El watcher de carpetas (la pieza nueva). Raíces marcadas + scan acotado + excludes. **Store propio (`workspace.db`)** para no contender con el hot-path/SSE de `events.db`; el resolver lee `events.db` read-only. | Medio. Stdlib puro. Cierra la brecha "carpeta sin agente". Recién acá se completa el portfolio. |
| **C — Portfolio** | Proyección de rollup máquina-wide + `delivery_candidate` (correlate). | Medio. Es la profundidad diferencial. |
| **D — Cross-máquina** | Agregación multi-usuario opt-in (split tipo Wakapi: captura local intacta + servidor self-hosted opcional), con identidad `git-remote`. Superficie A2A distribuida. | Diferido. En el mapa, no comprometido. |

**Disciplina de honestidad** (hereda de §4.1): `delivery_candidate` se expone como
*candidato con confianza*, nunca como hecho — requiere una segunda señal co-ocurrente
(artefacto nuevo en la raíz / tag o commit / cierre de sesión). Quiescencia sola es
indistinguible de un descanso.

**Contención y privacidad = correctitud:** el descubrimiento son **raíces padre
marcadas** (opt-in en workstation; raíz `/` válida en un servidor de agente autónomo,
donde la máquina entera es el trabajo), con excludes por defecto (internos VCS, deps,
build, carpetas de sync) y `max_depth`. Es metadata de toda la máquina: raíces opt-in y
exclusiones son parte del diseño, no un extra.

**Axioma Dual, sostenido:** el humano ve el portfolio (supervisión a nivel *trabajo*);
el agente consulta por MCP qué workspaces están calientes y quién los toca (evitar
conflictos). Toda pieza cuyo valor sea solo humano-productividad —p. ej. una lente de
*tiempo/atención* estilo ActivityWatch— queda **fuera del núcleo**: rompe el Axioma Dual
y es la máxima superficie de privacidad.

---

## 7. Pregunta abierta — coordinación síncrona

Hay un segundo modo de coordinación entre agentes más allá del asíncrono/mediado por
memoria que MoolMesh ya hace: **mensajería síncrona en vivo** — agentes hablándose en
tiempo real mientras trabajan (el modelo que ejemplifica tmux-bridge-mcp, que
convierte paneles de tmux en un bus de mensajes entre agentes con sobres
estructurados: `from / to / correlationId`).

**Estado: en evaluación, no es dirección comprometida.** Se está probando en campo
por separado. Por ahora lo tratamos como:

1. **Fuente de patrones tácticos** que podemos adoptar en la base Observe/Correlate
   hoy — ante todo la detección de ciclo de vida de sesión de §4.1, y más adelante el
   sobre de mensaje estructurado como señal *determinística* para cross-session
   linking (reemplazando las heurísticas temporales de hoy por "quién le habló a
   quién de verdad").
2. **Posible fuente observada a futuro** — MoolMesh podría ingestar el tráfico de un
   bus en vivo como eventos, haciendo *legible* la coordinación síncrona sin que
   MoolMesh mismo se vuelva write-capable.

Lo que **no** decidimos todavía: si MoolMesh alguna vez debería *manejar*
coordinación en vivo (volverse write-capable). Eso compensa contra la arquitectura
read-only y necesita que la evaluación de campo concluya primero.

---

## 8. Invariantes (de PHILOSOPHY §Architecture Principles)

Sostienen todo lo anterior:

1. **El estado es la única fuente de verdad** — SQLite, persistido, consultable.
2. **Agnosticidad radical** — cualquier provider que emita eventos observables;
   agregar uno es un adapter, no un rewrite.
3. **Zero cloud lock-in** — Python stdlib + SQLite, todo local.
4. **Zero friction** — descubrimiento automático de sesiones; setup en menos de un minuto.
5. **Base de observación read-only** — MoolMesh lee archivos de sesión, nunca los
   modifica. Cualquier capacidad de escritura/coordinación es un paso deliberado y
   decidido aparte, no una deriva (ver §7).
6. **Human-First y Agent-First en tensión** — nunca servir a uno cegando al otro.

---

## 9. Qué necesita decisión del owner

| # | Decisión | Recomendación |
|---|----------|---------------|
| A | Orden del trabajo de higiene (§4) vs el primer provider nuevo | Higiene primero (#16 → #17 → #18); des-arriesga cada peldaño. |
| B | Qué providers entran en la próxima ola de amplitud, y en qué orden (§5) | Bajo esfuerzo primero (Aider, Pi, Goose); autónomos tras confirmar schema; Paperclip solo con demanda. |
| C | Construir el enabler de providers (template + auto-detección) antes o después del próximo provider | Antes — se paga en el provider #2. |
| D | Coordinación síncrona (§7) — solo fuente de patrones, o peldaño declarado del roadmap | Solo fuente de patrones hasta que concluya la evaluación de campo. |
| E | El eje Workspace (§6): ¿rama de Amplitud en MoolMesh o proyecto aparte? | Rama de Amplitud dentro de MoolMesh (Fases A–C); reusa dashboard/MCP/SSE, con store propio (`workspace.db`). |
| F | Secuencia del eje Workspace vs el próximo provider de agente | Después de la higiene (§4); a la par o antes del 6.º provider (profundidad > amplitud). Arrancar por Fase A — barata, sobre datos ya persistidos. |
| G | Tablero cross-máquina + servidor self-hosted (Workspace Fase D) | En el mapa, diferido. Exigir solo que el esquema Workspace nazca agregable (identidad `git-remote`). |

---

*Esta es la capa estratégica. El detalle táctico de releases vive en `ROADMAP.md` y
`CHANGELOG.md`. Cuando divergen de este documento, este documento fija la intención y
ellos fijan el estado.*
