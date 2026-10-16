# Especificación técnica — Inventory API

> Requisito original: *"Quiero una API para gestionar inventario con usuarios y PostgreSQL."*
>
> Este documento transforma ese requisito en una especificación verificable. Las decisiones
> de arquitectura con sus alternativas están en [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Alcance

API REST (OpenAPI 3.1) para que una organización gestione su catálogo de productos y el stock
de esos productos en uno o varios almacenes, con usuarios autenticados y permisos por rol.
No incluye frontend: los clientes son aplicaciones internas, integraciones o una UI futura.

## 2. Supuestos (ambigüedades resueltas)

El requisito es breve; estas son las decisiones tomadas con criterio propio. Cada una se puede
cambiar, pero algunas (marcadas ⚠) afectan al modelo de datos y conviene confirmarlas pronto.

| # | Ambigüedad | Decisión | Motivo |
|---|---|---|---|
| A1 ⚠ | ¿Una o varias empresas (multi-tenant)? | **Una sola organización** por despliegue. | Multi-tenancy añade `tenant_id` a todo y aislamiento por fila; no se pidió. Añadirlo después es una migración conocida. |
| A2 ⚠ | ¿Un almacén o varios? | **Varios almacenes**; el stock es por (producto, almacén). | Es lo habitual en inventario real y degradar a uno es trivial; lo contrario no. |
| A3 ⚠ | ¿Cantidades enteras o decimales? | **Enteras** (unidad base del producto, p. ej. gramos en vez de kg). | Evita errores de redondeo en un libro contable; el campo `unit` documenta la unidad. |
| A4 | ¿Cómo se modifica el stock? | **Solo mediante movimientos** (libro mayor inmutable). No existe `PUT stock`. | Auditabilidad: cada unidad tiene historia; el saldo se puede reconstruir. |
| A5 | ¿Stock negativo? | **Prohibido** (409 `INSUFFICIENT_STOCK`), garantizado también por `CHECK` en BD. | Integridad de datos. |
| A6 | ¿Registro público de usuarios? | **No.** Un admin crea usuarios; el primer admin se crea por CLI. | Sistema interno; un registro público sería superficie de ataque y de enumeración. |
| A7 | Roles | `viewer` < `operator` < `manager` < `admin` (jerárquicos). | Cubre lectura, operación de almacén, gestión de catálogo y gestión de usuarios. |
| A8 | ¿Precios, proveedores, pedidos de compra/venta? | **Fuera de alcance.** `reference` libre en movimientos (p. ej. nº de pedido). | No se pidió; el modelo admite añadirlos sin romper contratos. |
| A9 | ¿Borrado de entidades? | Borrado real solo si **no hay historial**; si lo hay, 409 y se debe **archivar** (`is_active=false`). Usuarios: nunca se borran, se desactivan. | Preserva la integridad referencial del libro mayor y de la auditoría. |
| A10 | Clientes de la API | Tokens en el cuerpo JSON (`Authorization: Bearer`), sin cookies. | Clientes máquina/SPA; sin cookies no hay CSRF. |

## 3. Requisitos funcionales

### 3.1 Autenticación y sesiones
- RF-1 Login con email + contraseña → access token (JWT, 15 min) + refresh token (opaco, 7 días).
- RF-2 Refresh con **rotación**: cada refresh token sirve una sola vez. Reutilizar uno ya rotado
  revoca toda la familia de sesión (detección de robo).
- RF-3 Logout revoca la sesión (familia de refresh tokens).
- RF-4 Cambio de la propia contraseña (exige la actual); invalida todos los tokens del usuario.
- RF-5 `GET /auth/me` devuelve el usuario autenticado.

### 3.2 Usuarios (solo `admin`)
- RF-6 Crear, listar (paginado, búsqueda, filtros por rol/estado), ver, modificar nombre/rol/estado.
- RF-7 Restablecer la contraseña de otro usuario (invalida sus sesiones).
- RF-8 Desactivar o degradar un usuario invalida sus tokens inmediatamente.
- RF-9 No se puede dejar el sistema sin ningún admin activo (409 `LAST_ADMIN`).

### 3.3 Catálogo
- RF-10 Categorías: CRUD; nombre único sin distinguir mayúsculas.
- RF-11 Productos: CRUD; `sku` único (normalizado a mayúsculas), nombre, descripción, categoría
  opcional, unidad, nivel de reposición (`reorder_level`), activo/archivado.
- RF-12 Listado de productos paginado, con búsqueda por SKU/nombre y filtros por categoría/estado.
- RF-13 Almacenes: CRUD; `code` único (mayúsculas), nombre, dirección, activo/archivado.

### 3.4 Stock
- RF-14 Movimientos: `receipt` (entrada), `issue` (salida), `adjustment` (ajuste ±, recuento físico).
- RF-15 Transferencias entre almacenes: atómicas, generan `transfer_out` + `transfer_in` enlazados.
- RF-16 Consultar saldos por producto/almacén, con filtro "bajo nivel de reposición".
- RF-17 Consultar el libro de movimientos (paginación por cursor, filtros por producto, almacén,
  tipo y rango de fechas).
- RF-18 Los movimientos aceptan cabecera `Idempotency-Key`: un reintento devuelve el mismo
  resultado sin duplicar stock.

### 3.5 Auditoría
- RF-19 Eventos de auditoría para login (éxito/fallo), gestión de usuarios y cambios de catálogo
  y almacenes. Consultables por admin. Los movimientos de stock son su propio registro.

## 4. Matriz de permisos

| Acción | viewer | operator | manager | admin |
|---|:-:|:-:|:-:|:-:|
| Leer catálogo, almacenes, saldos, movimientos | ✓ | ✓ | ✓ | ✓ |
| Registrar `receipt`, `issue`, transferencias | | ✓ | ✓ | ✓ |
| Registrar `adjustment` | | | ✓ | ✓ |
| Crear/editar/borrar categorías, productos, almacenes | | | ✓ | ✓ |
| Gestionar usuarios, leer auditoría | | | | ✓ |

La autorización se comprueba **en la capa de servicio** (no solo en el router), de modo que
cualquier punto de entrada futuro (CLI, workers) hereda los mismos controles.

## 5. Requisitos no funcionales

| Área | Requisito |
|---|---|
| Seguridad | Argon2id; JWT HS256 con `iss`/`aud`/`exp`; refresh tokens almacenados como SHA-256; rate limit en login/refresh; anti-enumeración (misma respuesta y tiempo similar para email inexistente); sin secretos en código/imagen; validación estricta de entrada; límite de tamaño de cuerpo (1 MiB); cabeceras de seguridad. |
| Integridad | Constraints en BD (FK, UNIQUE, CHECK); actualización de stock con `UPDATE` condicional atómico; bloqueo optimista (`version`) en entidades editables; transacciones por operación. |
| Concurrencia | N peticiones concurrentes sobre el mismo stock nunca producen saldo negativo ni pérdida de actualizaciones; transferencias bloquean filas en orden determinista (sin deadlocks). |
| Fiabilidad | Idempotencia en escrituras de stock; timeouts de conexión, pool y `statement_timeout`; BD caída → 503 `SERVICE_UNAVAILABLE`, no 500 con traza. |
| Observabilidad | Logs JSON estructurados con `request_id`; `X-Request-ID` propagado; log de acceso con latencia; `/health/live` y `/health/ready` (BD + revisión de esquema). |
| Rendimiento | Paginación obligatoria (máx. 100); sin N+1 (joins explícitos); índices según consultas; hashing de contraseñas fuera del event loop y con concurrencia limitada (protege la memoria: Argon2 usa 64 MiB por hash). |
| Operación | Configuración por variables de entorno tipadas; arranque falla si la configuración de producción es insegura; migraciones como paso separado del arranque; contenedor no-root con healthcheck. |

## 6. Modelo de datos

```
users ─┬─< refresh_tokens          categories ─< products ─┐
       ├─< stock_movements >───────────────────────────────┤
       ├─< audit_events                                    │
       └─< idempotency_keys        warehouses ─< stock_levels >─ products
                                   warehouses ─< stock_movements
rate_limit_counters (independiente)
```

| Tabla | Columnas clave | Restricciones / índices |
|---|---|---|
| `users` | id uuid, email, full_name, password_hash, role, is_active, token_version, version, last_login_at, timestamps | UNIQUE(email); CHECK email en minúsculas; CHECK role ∈ roles |
| `refresh_tokens` | id, user_id, family_id, token_hash, expires_at, revoked_at, replaced_by_id | UNIQUE(token_hash); idx(user_id), idx(family_id), idx(expires_at) |
| `categories` | id, name, description, version, timestamps | UNIQUE(lower(name)) |
| `products` | id, sku, name, description, category_id, unit, reorder_level, is_active, version, timestamps | UNIQUE(sku); CHECK formato sku; CHECK reorder_level ≥ 0; FK categoría ON DELETE RESTRICT; idx trigram en name/sku |
| `warehouses` | id, code, name, address, is_active, version, timestamps | UNIQUE(code); CHECK formato code |
| `stock_levels` | PK(product_id, warehouse_id), quantity bigint, updated_at | CHECK quantity ≥ 0; FKs RESTRICT; idx(warehouse_id) |
| `stock_movements` | id bigint identity, product_id, warehouse_id, movement_type, quantity (con signo), balance_after, reference, note, transfer_id, created_by, created_at | CHECK signo coherente con el tipo; CHECK balance_after ≥ 0; idx(product_id,id), idx(warehouse_id,id), idx(transfer_id), idx(created_at) |
| `idempotency_keys` | PK(user_id, key), endpoint, request_hash, response_status, response_body jsonb, created_at | idx(created_at) para purga |
| `audit_events` | id bigint identity, occurred_at, actor_id, action, entity_type, entity_id, request_id, ip, details jsonb | idx(occurred_at), idx(entity_type, entity_id), idx(actor_id) |
| `rate_limit_counters` | PK(key, window_start), count | idx(window_start) para purga |

**Invariante del libro mayor:** para cada (producto, almacén),
`stock_levels.quantity = Σ stock_movements.quantity`. Se verifica en los tests de concurrencia.

## 7. Contrato de la API

Prefijo `/api/v1` (salvo health). JSON en ambos sentidos. Documentación interactiva en
`/docs` (desactivable; desactivada por defecto en producción).

| Método | Ruta | Rol mín. | Notas |
|---|---|---|---|
| GET | `/health/live` | — | Proceso vivo (no toca BD). |
| GET | `/health/ready` | — | BD accesible y esquema en la revisión esperada; 503 si no. |
| POST | `/auth/login` | — | Rate limit por IP y por email. |
| POST | `/auth/refresh` | — | Rotación; reutilización ⇒ revoca familia. |
| POST | `/auth/logout` | — | 204 siempre (no revela si el token existía). |
| GET | `/auth/me` | viewer | |
| POST | `/auth/password` | viewer | Cambia la propia contraseña; 204. |
| GET/POST | `/users` | admin | Listado paginado / alta. |
| GET/PATCH | `/users/{id}` | admin | PATCH exige `version`. |
| PUT | `/users/{id}/password` | admin | Restablece contraseña; 204. |
| GET/POST | `/categories` | viewer / manager | |
| GET/PATCH/DELETE | `/categories/{id}` | viewer / manager | DELETE ⇒ 409 si tiene productos. |
| GET/POST | `/products` | viewer / manager | `q`, `category_id`, `is_active`. |
| GET/PATCH/DELETE | `/products/{id}` | viewer / manager | DELETE ⇒ 409 si tiene movimientos. |
| GET | `/products/{id}/stock` | viewer | Saldo por almacén + total. |
| GET/POST | `/warehouses` | viewer / manager | |
| GET/PATCH/DELETE | `/warehouses/{id}` | viewer / manager | DELETE ⇒ 409 si tiene movimientos. |
| GET | `/stock-levels` | viewer | `product_id`, `warehouse_id`, `below_reorder`. |
| GET | `/stock-movements` | viewer | Paginación por cursor. |
| GET | `/stock-movements/{id}` | viewer | |
| POST | `/stock-movements` | operator (adjustment: manager) | `Idempotency-Key` opcional. |
| POST | `/stock-transfers` | operator | `Idempotency-Key` opcional. |
| GET | `/audit-events` | admin | Paginación por cursor. |

### 7.1 Paginación
- Offset (catálogo, usuarios, saldos): `?limit=50&offset=0` → `{"items", "total", "limit", "offset"}`; `limit` ≤ 100.
- Cursor (movimientos, auditoría; tablas que crecen sin límite): `?limit=50&cursor=…` →
  `{"items", "next_cursor", "limit"}`. El cursor es opaco.

### 7.2 Errores
Formato único para todo error, incluidos 404 de rutas, 405, 422 de validación y 500:

```json
{"error": {"code": "INSUFFICIENT_STOCK", "message": "Insufficient stock", "details": {"available": 3, "requested": 5}, "request_id": "…"}}
```

| HTTP | Códigos |
|---|---|
| 400 | `BAD_REQUEST` (p. ej. cursor inválido) |
| 401 | `UNAUTHENTICATED`, `INVALID_CREDENTIALS`, `INVALID_REFRESH_TOKEN` |
| 403 | `FORBIDDEN` |
| 404 | `NOT_FOUND` (con `details.resource`) |
| 409 | `ALREADY_EXISTS`, `VERSION_CONFLICT`, `INSUFFICIENT_STOCK`, `RESOURCE_IN_USE`, `RESOURCE_INACTIVE`, `LAST_ADMIN`, `CONFLICT` |
| 413 | `PAYLOAD_TOO_LARGE` |
| 422 | `VALIDATION_ERROR`, `IDEMPOTENCY_KEY_REUSED` |
| 429 | `RATE_LIMITED` (+ `Retry-After`) |
| 500 | `INTERNAL_ERROR` (sin detalles internos) |
| 503 | `SERVICE_UNAVAILABLE` (BD inaccesible / timeout) |

## 8. Estrategia de testing

| Nivel | Qué cubre | Dónde |
|---|---|---|
| Unit | Hashing, JWT (expiración, firma, tipo, audiencia), validación de configuración de producción, normalización y validación de esquemas, escapado de `LIKE`, cursores, request-id, middleware de tamaño de cuerpo, matriz de permisos. | `tests/unit` |
| Integración (PostgreSQL real) | Migraciones (upgrade/downgrade y ausencia de deriva modelo↔migración), todos los endpoints, autorización por rol, flujos de sesión (rotación, reutilización, revocación), bloqueo optimista, idempotencia (incluida concurrente), **carreras de stock** (N salidas concurrentes), invariante del libro mayor, rate limiting, readiness con BD caída, que los errores no filtren internals. | `tests/integration` |
| E2E / smoke | Recorrido completo contra el servidor real por HTTP (login → catálogo → entradas → transferencia → salida → saldos → sesión). | `scripts/smoke_test.py` |

## 9. Estrategia Docker

- `Dockerfile` multi-stage: *builder* instala dependencias con hashes en un venv; *runtime*
  `python:3.12-slim` con usuario no-root (uid 10001), sin compiladores, `HEALTHCHECK` contra
  `/health/live`, `uvicorn` en forma *exec* (PID 1 recibe SIGTERM → apagado ordenado).
- `docker-compose.yml`: `db` (PostgreSQL 17 con healthcheck y volumen), `migrate` (job
  `alembic upgrade head`, una sola vez), `api` (depende de `migrate` completado). Rootfs de solo
  lectura, `cap_drop: ALL`, `no-new-privileges`, límites de CPU/memoria.
- Los secretos llegan por variables de entorno (`.env`, no versionado); nunca en la imagen.
