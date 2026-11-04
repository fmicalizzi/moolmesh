# mool-ops-kit (español)

**Kit de diseño para un tracker de operaciones que complementa a MoolMesh.**
Estado: propuesta con implementación de referencia que funciona. Todo es genérico; los ejemplos usan una empresa ficticia ("Acme Corp").

## Para qué

MoolMesh observa a los agentes de IA y los repos que tocan, en la máquina de cada persona. Quien coordina un equipo de desarrollo necesita una vista vecina: **qué se entregó, quién lo hizo y en qué periodo, en todos los repos de una organización**. Esa vista debe ser reproducible, auditable y compartible con personas que no son dueñas de la máquina.

## Ideas clave

- **Organización lógica ≠ org de GitHub.** Una empresa puede tener repos en su org de GitHub, repos en cuentas personales que funcionan como parte de ella, y repos que se mantienen **fuera de la org a propósito** por confidencialidad. El registro lo modela con dueños (`owners`) y relación (`core`, `affiliated`, `confidential`).
- **Tres capas:** MoolMesh local de cada persona · motor OSS (este kit) · repo privado de datos por organización.
- **Snapshots encadenados y verificados**, por día local, sin sobrescribir, con los bots fuera de los créditos.
- **Repos confidenciales:** se listan solo para excluirlos; el tracker nunca los lee.

## Contenido

Ver la tabla del [README](README.md): conceptos, arquitectura, registro, esquema, metodología, autenticación multi-dueño, mapa de brechas con MoolMesh, borradores de issues, implementación de referencia, ejemplos y pruebas.

## Probar sin red

```bash
python3 -m unittest discover -s tests -v
python3 reference/ops_snapshot.py --registry examples/registry.example.toml \
        --data /tmp/acme-data --cut 2026-11-03 --start 2026-11-02 \
        --fixtures examples/fixtures
```
