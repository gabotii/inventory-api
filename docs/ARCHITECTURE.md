# Arquitectura — Inventory API

Complementa a [SPEC.md](SPEC.md) (qué hace el sistema). Este documento explica **cómo** está
construido y **por qué**, con las alternativas descartadas.

## 1. Vista general

```
                ┌──────────────────────── proceso uvicorn (N workers) ────────────────────────┐
HTTP ──► Middleware ASGI ──► Router (api/routes) ──► Servicio (services) ──► Repositorio ──► PostgreSQL
         · request-id + log      · valida (Pydantic)    · autorización         · SQL explícito
         · cabeceras seguridad   · da forma a la resp.  · reglas de negocio    · bloqueos/upserts
         · límite de cuerpo                             · transacción/commit
         · 500 genérico
```

| Capa | Carpeta | Responsabilidad | No hace |
|---|---|---|---|
| Middleware | `app/middleware` | Request-ID, access log, cabeceras, límite de cuerpo, red de seguridad para 500 | Lógica de negocio |
| API | `app/api` | Contratos HTTP, dependencias, mapeo excepción→HTTP | Consultas SQL, reglas |
| Servicios | `app/services` | Autorización, reglas, límites de transacción, auditoría | Conocer HTTP |
| Repositorios | `app/repositories` | Consultas, paginación, bloqueos, `UPDATE` atómicos | Decidir permisos |
| Modelos | `app/models` | Esquema y constraints | Comportamiento |
| Núcleo | `app/core` | Configuración tipada, logging, seguridad, errores | — |

Los servicios no importan nada de FastAPI: la CLI (`app/cli.py`) y cualquier worker futuro
pueden usarlos con las mismas reglas.

## 2. Decisiones (ADR)

### ADR-1 · Libro mayor inmutable + saldo materializado
**Decisión:** el stock sólo cambia insertando un `stock_movement`; `stock_levels` guarda el saldo
y se actualiza **en la misma transacción**.
**Por qué:** auditoría completa (quién, cuándo, por qué) y lecturas O(1) del saldo.
**Alternativas:** sólo saldo (pierde historia); sólo libro mayor y `SUM()` al leer (lento a escala).
**Garantía:** `Σ movimientos = saldo` por (producto, almacén); verificado por tests de concurrencia.

### ADR-2 · `UPDATE` condicional atómico en vez de `SELECT … FOR UPDATE` + comprobación
```sql
UPDATE stock_levels SET quantity = quantity + :delta
WHERE product_id = :p AND warehouse_id = :w AND quantity + :delta >= 0
RETURNING quantity
```
Una sola sentencia comprueba y modifica; con `READ COMMITTED`, PostgreSQL re-evalúa el `WHERE`
tras esperar el bloqueo de fila, así que N salidas concurrentes nunca venden de más. Las entradas
usan `INSERT … ON CONFLICT DO UPDATE` (la fila puede no existir). El `CHECK (quantity >= 0)` es
una segunda barrera. Test: 25 salidas concurrentes de 1 unidad sobre 10 → exactamente 10 éxitos.

### ADR-3 · Orden determinista de bloqueos en transferencias
Una transferencia toca dos filas. Se actualizan **ordenadas por `warehouse_id`**, de modo que
transferencias en sentidos opuestos no se interbloquean. Verificado por mutación: sin el orden,
el test de 20 transferencias cruzadas produce deadlocks reales (→ 503).

### ADR-4 · Bloqueo optimista (`version`) en entidades editables
`PATCH` exige la `version` leída; SQLAlchemy emite `UPDATE … WHERE version = :leída`.
Evita *lost updates* sin mantener bloqueos entre peticiones. Conflicto → 409 `VERSION_CONFLICT`
con `current_version`. Los cambios de contabilidad (`last_login_at`) usan `UPDATE` Core para no
invalidar ediciones concurrentes de un admin.

### ADR-5 · Idempotencia en la misma transacción que la operación
`Idempotency-Key` → fila `(user_id, key)` insertada con `ON CONFLICT DO NOTHING` **dentro** de la
transacción del movimiento. Consecuencias:
- Duplicados concurrentes: el segundo `INSERT` espera al primero; luego lee la respuesta guardada.
- Si la operación falla, la clave se revierte con ella: **los errores no se cachean** y el reintento
  vuelve a ejecutar.
- Misma clave con otro cuerpo/endpoint → 422 `IDEMPOTENCY_KEY_REUSED`.
Retención: `IDEMPOTENCY_KEY_TTL_SECONDS` (24 h) vía `python -m app.cli purge-expired`.

### ADR-6 · Sin Redis
Rate limiting e idempotencia viven en PostgreSQL (contadores de ventana fija con upsert).
**Por qué:** los requisitos se cumplen con un componente menos que operar, asegurar y respaldar;
funciona con varias réplicas. **Coste:** una escritura extra por intento de login/refresh;
irrelevante a esta escala. **Revisar si:** el volumen de login justifica un almacén en memoria.
**Limitación conocida:** ventana fija (permite hasta 2× ráfaga en el borde de la ventana).

### ADR-7 · Autenticación: JWT corto + refresh opaco rotatorio
- Access token JWT HS256 de 15 min con `iss`, `aud`, `typ`, `tv` (token_version). El usuario se
  carga en cada petición: desactivar, cambiar rol o contraseña incrementa `token_version` y
  revoca **al instante** todos sus access tokens.
- Refresh token: 256 bits aleatorios, sólo se guarda su SHA-256, un solo uso (rotación), agrupado
  en "familias" (una por login). Reutilizar uno ya rotado revoca la familia (señal de robo,
  RFC 6819 §5.2.2.3), sin afectar a otras sesiones del usuario.
- **Tradeoff aceptado:** tras un logout, el access token sigue siendo válido hasta 15 min
  (revocar exige estado por token; se prefirió TTL corto).

### ADR-8 · Argon2id fuera del event loop y con concurrencia limitada
El hashing (≈50–100 ms, 64 MiB) corre en hilos con un `CapacityLimiter`
(`PASSWORD_HASH_CONCURRENCY`). Sin el límite, una avalancha de logins agotaría la memoria;
sin hilos, bloquearía todas las peticiones del worker. Presupuesto de memoria por worker:
≈ 100 MB + concurrencia × `ARGON2_MEMORY_COST_KIB`.

### ADR-9 · Anti-enumeración
Email desconocido, contraseña errónea y usuario desactivado devuelven **la misma** respuesta, y un
email desconocido verifica contra un hash señuelo para igualar tiempos. No hay registro público
(el primer admin se crea con la CLI).

### ADR-10 · Readiness consciente de migraciones (despliegue rolling)
`/health/ready` compara la revisión Alembic de la BD con la de la build:
- igual → listo; conocida pero anterior → **no listo** (faltan migraciones);
- desconocida (más nueva) → listo: otra release ya migró. Por eso **las migraciones deben ser
  compatibles hacia atrás** (patrón *expand/contract*): añadir columnas nullable, nunca renombrar o
  borrar en la misma release que el código que deja de usarlas.
`/health/live` no toca la BD: una caída de PostgreSQL no debe provocar reinicios en cascada.

### ADR-11 · Migraciones como paso separado
`alembic upgrade head` corre como job (`migrate` en Compose), nunca al arrancar la app: con
varias réplicas, migrar en el arranque produce carreras. `lock_timeout` de 30 s evita que una
migración quede colgada detrás de una transacción larga; cada migración es transaccional
(PostgreSQL tiene DDL transaccional), así que un fallo a mitad no deja el esquema a medias.

### ADR-12 · Paginación por offset en catálogos, por cursor en tablas sin límite
Catálogo/usuarios/saldos: offset + `total` (cómodo para UIs; tablas acotadas).
Movimientos/auditoría: cursor opaco sobre `id` (keyset), O(limit) a cualquier profundidad y
estable ante inserciones. `limit` ≤ 100 en todos.

### ADR-13 · Nivel de reposición a nivel de producto
`reorder_level` es del producto y se compara con su **total** entre almacenes (en
`/products/{id}/stock` y en `/stock-levels`, que expone `product_total_quantity`). Detectado por el
smoke test: la primera versión comparaba cada almacén por separado, lo que daba alertas falsas.
**Evolución natural:** reglas de reposición por (producto, almacén).

### ADR-14 · Errores
Un único sobre `{"error": {code, message, details, request_id}}` para todo (incluidos 404/405/422
del framework y 500). Los 422 nunca devuelven el valor rechazado (podría ser una contraseña).
BD inaccesible, timeouts, deadlocks y *lock timeouts* → 503 `SERVICE_UNAVAILABLE` con
`Retry-After`, no 500. Excepciones no previstas → 500 genérico; la traza sólo va al log.

## 3. Modelo de concurrencia (resumen)

| Situación | Mecanismo | Test |
|---|---|---|
| Salidas concurrentes del mismo stock | `UPDATE` condicional (ADR-2) | `test_concurrent_issues_never_oversell` |
| Entradas concurrentes en fila nueva | `INSERT … ON CONFLICT` | `test_concurrent_receipts_on_new_stock_row` |
| Transferencias cruzadas | Orden de bloqueo (ADR-3) | `test_opposite_transfers_do_not_deadlock…` |
| Ediciones concurrentes | `version` (ADR-4) | `test_concurrent_edits_with_same_version` |
| Reintentos / dobles clics | Idempotency-Key (ADR-5) | `test_concurrent_duplicates_execute_once` |
| Dos admins se degradan mutuamente | Bloqueo ordenado de filas admin | `test_two_admins_cannot_demote_each_other…` |

## 4. Modos de fallo

| Fallo | Comportamiento |
|---|---|
| PostgreSQL caído | `live` 200, `ready` 503, API 503 + `Retry-After`; al volver, `pool_pre_ping` reconecta sin reiniciar (verificado manualmente) |
| Consulta lenta / bloqueo largo | `statement_timeout` 15 s, `lock_timeout` 10 s → 503 |
| Transacción abandonada por un bug | `idle_in_transaction_session_timeout` 60 s la cierra en el servidor |
| Pool agotado | espera ≤ `DB_POOL_TIMEOUT_SECONDS` → 503 |
| Reintento de cliente | Idempotency-Key → misma respuesta, sin efecto doble |
| Migración fallida | se revierte entera; `ready` sigue indicando la revisión real |
| SIGTERM | uvicorn deja de aceptar conexiones, drena ≤ 20 s, cierra el pool |
| Avalancha de logins | rate limit por IP y email + concurrencia de Argon2 acotada |
