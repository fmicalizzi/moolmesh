# Mensaje de contexto para el equipo de desarrollo de moolmesh

Hola, equipo:

Les comparto una propuesta para una capacidad nueva de moolmesh. Viene de un caso real de uso, y les dejo el contexto completo para que decidan cómo implementarla.

## 1. De dónde sale

Coordinamos el trabajo de un equipo de desarrollo de una empresa con varios sistemas en producción, unos 10 repos activos. Durante meses llevamos el seguimiento con scripts propios sobre `gh`:
- snapshots diarios o por periodo;
- reportes de avance por ronda;
- producción por persona;
- verificación de totales contra GitHub.

El método funciona, pero está disperso en scripts y no se puede compartir bien. Ahora se suma a la coordinación una segunda persona: el coordinador de TI de la empresa. Trabaja en **Linux** y también usa Claude Code. Necesitamos que los dos veamos lo mismo, con números confiables.

Al revisar moolmesh a fondo vimos que ya tiene casi toda la base:
- el portafolio cliente → proyecto → materiales, con estado derivado;
- Project Pulse por repo;
- digests diarios y semanales con estadísticas por autor y continuidad;
- el caché de GitHub con autor, asignados y labels;
- el MCP.

Además, el roadmap ya contempla la agregación entre máquinas y multiusuario (Fase D / v2.x). Por eso, en vez de seguir con un desarrollo propio aparte, preferimos que esto **crezca dentro del ecosistema moolmesh, como open source**.

## 2. Qué les pedimos

Que implementen las capacidades que describe el kit, con el nombre de trabajo **`mool-ops`**. **Las decisiones de diseño son de ustedes:**
- si va dentro de moolmesh, como módulo opcional o como paquete complementario;
- cómo se instala y se activa;
- qué CLI y almacenamiento usa;
- cuándo y cómo se publica.

No creamos ningún repo público para esto. El repo que importa es el que después se instale para activar estas funciones, y ese lo definen ustedes.

**Las issues no están abiertas.** Los 8 borradores están en `issues/`. Ustedes deciden cuáles abrir, fusionar o descartar, y cuándo. El 01 amplía la #76, que ya existe.

## 3. Qué necesitamos como resultado

1. **Ver, para una organización, qué se entregó, quién lo hizo y en qué periodo**, en todos sus repos.
2. **Números honestos y reproducibles:**
   - el día se cuenta en hora local;
   - los totales se encadenan con el corte anterior y se verifican contra la fuente;
   - los bots no reciben crédito;
   - los datos viejos se marcan como tales.
3. **Cada persona lo corre en su máquina** (macOS y Linux), y existe un lugar privado común con el registro, los snapshots y los reportes de la organización.
4. **El desglose por persona es opcional y viene apagado.** Con el flag apagado, moolmesh debe dar exactamente la misma salida que hoy.

## 4. Dos conceptos clave

**"Organización" no es lo mismo que "org de GitHub".** Una empresa real tiene repos en tres situaciones:
- en su org de GitHub (`core`);
- en cuentas personales que funcionan como parte de la organización (`affiliated`);
- fuera de la org **a propósito**, porque guardan información sensible (`confidential`).

Los repos confidenciales **no se leen nunca**. Se listan solo para excluirlos, y si alguien intenta seguirlos, el registro debe dar error. La agrupación por cliente debe salir de un registro compartido, no de la configuración de cada máquina: la misma cuenta puede ser "personal" en una máquina y "del cliente" en otra.

**Identidad estable.**
- Cada repo se identifica por su id numérico de GitHub, porque los repos se transfieren y se renombran.
- Cada issue se identifica como `owner/repo#n`, porque un PR puede cerrar issues de otro repo con el mismo número.

## 5. Lo que encontramos en moolmesh (evidencia)

- **El daemon se detuvo el 27 oct y nadie lo notó en siete días.** El portafolio y el caché de GitHub quedaron congelados, y las tools MCP siguieron respondiendo con datos viejos sin avisar. Para un coordinador ese es el riesgo principal: tomar como actuales números que no lo son. De ahí sale la issue 01.
- **Los digests cortan el día en UTC.** Con UTC-6, el trabajo de la tarde cae en el día siguiente.
- **El outcome del portafolio es acumulado de toda la historia.** No hay forma de responder "qué se entregó esta semana".
- **Los PRs no guardan líneas, archivos ni las issues que cierran.**
- **Cada repo necesita un clon local**, y un cliente repartido en varias cuentas exige un override por repo.

El mapa completo de lo que ya existe y lo que falta está en `docs/07-moolmesh-gap-map.md`.

## 6. Qué hay en el kit

`HANDOFF.md` es el punto de entrada. Además:
- **`docs/01` a `07`:** conceptos, arquitectura en tres capas, registro, esquema del snapshot, metodología, autenticación cuando una organización abarca varias cuentas u orgs, y mapa de brechas.
- **`schema/snapshot.v1.schema.json`:** esquema propuesto para un export versionado. Pueden adoptarlo, cambiarlo o reemplazarlo.
- **`reference/ops_snapshot.py`:** implementación de referencia que funciona, solo con la librería estándar. No es para copiarla tal cual: sirve para que el método no tenga ambigüedades.
- **`tests/`:** 8 pruebas sin red (`python3 -m unittest discover -s tests`). Cubren el día local, las referencias entre repos, los bots, el encadenado, el ajuste cuando no cuadra, no sobrescribir y la exclusión de confidenciales.
- **`examples/`:** una empresa ficticia (Acme), con fixtures, snapshots generados y un reporte de ronda armado a partir de ellos.
- **`issues/`:** los 8 borradores, en el orden sugerido. Del 01 al 04 se destraba el uso de moolmesh como fuente de datos; el 05 abre la capa de equipo; del 06 al 08 resuelven las organizaciones con varias cuentas.

## 7. Cómo vamos a validar

- **Paridad:** la implementación de ustedes debe dar los mismos totales que la referencia en los ejemplos. Además, tenemos cortes reales verificados contra GitHub para validar en nuestro entorno, que no van en el kit.
- **Confidencialidad:** un dueño o repo confidencial nunca se sincroniza.
- **Compatibilidad:** con el desglose por persona apagado, nada cambia.

## 8. Después

Cuando esté listo, lo instalamos nosotros: registro, repo privado de datos, corrida programada y puesta en marcha con el segundo usuario en Linux. Hasta entonces seguimos con nuestros scripts como fuente operativa, así que no hay urgencia que comprometa la calidad. Preferimos que salga bien, con los principios de moolmesh intactos: local-first, stdlib, base de solo lectura y datos honestos.

Si algo del kit no encaja con la filosofía o el roadmap de moolmesh, díganlo y lo ajustamos. El kit es una propuesta, no una especificación cerrada.

Gracias.
