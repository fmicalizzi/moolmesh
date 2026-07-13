# Handoff XX — Network Resilience Audit

## Contexto

Usuario de campo (Adrián, Linux/Python 3.12, uv, repos con muchos issues/PRs) reporta `IncompleteRead` loop infinito en el log del dashboard. El error se repite cada 15 segundos sin parar. El dashboard sigue funcionando pero spamea warnings y nunca logra sincronizar los datos de GitHub de sus repos.

## El bug reportado

```
http.client.IncompleteRead: IncompleteRead(402544 bytes read, 371995 more expected)
```

**Causa raíz:** `github_client.py:58` — `resp.read()` lanza `http.client.IncompleteRead` cuando la conexión TCP se corta antes de recibir el body completo. Nuestro except en línea 63 solo atrapa `URLError`, `OSError`, `TimeoutError`. `IncompleteRead` hereda de `http.client.HTTPException`, no de `OSError` — se escapa y sube como excepción no manejada.

**Por qué afecta a Adrián y no a nosotros:** sus repos (`ddtyi/yipublic`, `ddtyi/YAAHub`) devuelven respuestas de ~1MB (muchos issues). En conexiones menos estables, la descarga se corta.

## Inventario de problemas encontrados

### P1 — `_request()` except incompleto (github_client.py:63)

No atrapa:
- `http.client.IncompleteRead` — respuesta truncada (el bug de Adrián)
- `http.client.RemoteDisconnected` — servidor cierra conexión
- `http.client.BadStatusLine` — respuesta HTTP corrupta
- Cualquier otro `http.client.HTTPException`

**Fix:** agregar `http.client.HTTPException` al except (cubre todas las subclases).

### P2 — `json.loads()` sin protección (github_client.py:99, 121)

`rest_get()` y `graphql()` hacen `json.loads(body)` sin try/except. Si el body llegó truncado (IncompleteRead parcial, o gzip corruption), se lanza `JSONDecodeError` no atrapado.

**Fix:** envolver en try/except, retornar `(0, None, None)` / `None`.

### P3 — Sin retry en errores transitorios (github_client.py:35-74)

`_request()` no reintenta. Un error de red transitorio (IncompleteRead, timeout, reset) falla inmediatamente. El harvester reintenta en el próximo ciclo (15s/60s), pero entre tanto spamea el log.

**Fix:** retry simple (1-2 intentos) con backoff corto (1s-2s) dentro de `_request()` para errores transitorios. No reintentar errores HTTP 4xx/5xx.

### P4 — Log spam sin backoff (github_harvester.py:93-105)

`_poll_all_repos()` atrapa excepciones con `_log.warning(..., exc_info=True)` — imprime traceback completo. Si un repo falla consistentemente (mala conexión), genera un traceback completo cada 15 segundos. No hay:
- Cooldown por repo (suprimir después de N fallos consecutivos)
- Log level degradation (WARNING → DEBUG después de 3 fallos iguales)
- Conteo de errores para reportar "repo X: 15 failures in last 5min" en vez de 15 tracebacks

**Fix:** contador de errores consecutivos por repo. Traceback completo en el primer fallo, resumen de 1 línea en los siguientes, traceback completo de nuevo si cambia el tipo de error.

### P5 — `openai_compat_client.py` — mismo patrón incompleto (líneas 49-57, 76-80)

El `chat()` tiene un except amplio que sí atrapa `JSONDecodeError` pero NO atrapa `http.client.HTTPException` / `IncompleteRead`. El `is_available()` tampoco.

Irónicamente `chat()` es más robusto que `github_client._request()` porque atrapa más tipos, pero le falta `HTTPException`.

### P6 — Respuestas grandes sin paginación (github_client.py:137)

`list_issues()` pide `per_page=100` pero no pagina. Repos con >100 issues solo ven los primeros 100. Además, repos con ~100 issues generan respuestas de ~1MB, que es exactamente el rango donde `IncompleteRead` aparece.

**Mitigación parcial:** reducir `per_page` a 30 reduciría el tamaño de respuesta a ~300KB, menos propenso a truncarse. Full fix sería implementar paginación via `Link` header.

### P7 — `resp.read()` dentro del try del `urlopen` (github_client.py:54-58)

```python
try:
    resp = urllib.request.urlopen(req, timeout=timeout)
    status = resp.status
    resp_headers = dict(resp.headers)
    resp_body = resp.read()     # ← este es el que falla
except urllib.error.HTTPError as e:
    ...
```

El `resp.read()` puede fallar DESPUÉS de que `urlopen()` tuvo éxito. En ese caso tenemos `resp` y `status` válidos pero el body incompleto. Podríamos al menos devolver `(status, headers, b"")` en vez de explotar.

## Archivos a modificar

| Archivo | Cambios |
|---------|---------|
| `hub/integrations/github_client.py` | P1: ampliar except, P2: proteger json.loads, P3: retry, P7: separar read del try |
| `hub/harvesters/github_harvester.py` | P4: log backoff por repo |
| `hub/integrations/openai_compat_client.py` | P5: agregar HTTPException al except |

## Tests a crear/modificar

- `test_github_client.py` — mock `IncompleteRead`, `RemoteDisconnected`, `BadStatusLine`, body truncado que falla `json.loads`, retry behavior
- `test_github_harvester.py` — verificar que errores consecutivos no generan traceback repetido
- `test_openai_compat_client.py` — mock `IncompleteRead` en chat() y is_available()

## Prioridad

- **P1 + P5** — fix mínimo, ataja el bug de Adrián y el mismo patrón en el LLM client
- **P2 + P7** — defensivos, previenen crash cascada
- **P4** — UX del log, reduce ruido
- **P3** — retry, mejora resiliencia real
- **P6** — paginación, scope más grande, evaluar si vale como MINOR feature

## Constraint

- Zero external dependencies — todo con stdlib
- Python 3.11+ (match/case ok)
- No romper la interfaz pública de `GitHubClient` ni `GitHubHarvester`

## Qué decirle a Adrián ahora

> Es un bug nuestro, no de tu instalación. El cliente HTTP no maneja bien las descargas grandes que se interrumpen — tu repo tiene muchos issues y la respuesta es pesada (~1MB). El dashboard sigue funcionando, solo la sincronización de GitHub falla y se reintenta cada 15 segundos. Ya tenemos el fix identificado, sale en la próxima versión. Mientras tanto, el error es cosmético — las sesiones de AI se siguen monitoreando normalmente, solo Project Pulse no se actualiza.
