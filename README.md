# Inventory API

API REST para gestionar inventario multi-almacén con usuarios, roles y un libro de movimientos
auditable. FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · Alembic · PostgreSQL.

- **Especificación** (requisitos, supuestos, contratos): [docs/SPEC.md](docs/SPEC.md)
- **Arquitectura y decisiones** (ADRs, concurrencia, modos de fallo): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **OpenAPI interactivo:** `http://localhost:8000/docs` (desactivado por defecto en producción)

## Qué ofrece

| Área | Funcionalidad |
|---|---|
| Autenticación | Login email/contraseña (Argon2id), access JWT 15 min, refresh rotatorio con detección de robo, logout, cambio de contraseña |
| Usuarios | Alta/edición/desactivación por admins; roles `viewer` < `operator` < `manager` < `admin`; nunca queda el sistema sin admin |
| Catálogo | Categorías, productos (SKU único), almacenes; búsqueda, filtros, paginación, bloqueo optimista, archivado |
| Stock | Entradas, salidas, ajustes, transferencias atómicas; nunca negativo; idempotencia con `Idempotency-Key` |
| Consultas | Saldos por producto/almacén, alertas de reposición, libro de movimientos con cursor |
| Auditoría | Logins, gestión de usuarios y cambios de catálogo; logs JSON con `request_id` |

## Desarrollo local

Requisitos: Python 3.12, PostgreSQL 15+ (probado con 17).

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/dev.txt    # Windows: .venv\Scripts\python
cp .env.example .env            # completar DATABASE_URL, TEST_DATABASE_URL y JWT_SECRET
```

Crear rol y bases de datos (como superusuario de PostgreSQL):

```sql
CREATE ROLE inventory LOGIN PASSWORD '<password>';
CREATE DATABASE inventory OWNER inventory;
CREATE DATABASE inventory_test OWNER inventory;   -- sólo para tests; se borra en cada ejecución
```

```bash
python -m alembic upgrade head
ADMIN_PASSWORD='<min 12 caracteres>' python -m app.cli create-admin --email admin@example.com --full-name "Admin"
python -m uvicorn app.main:app_factory --factory --reload --port 8000
```

> **Windows con Smart App Control:** si `import sqlalchemy` falla con *"An Application Control
> policy has blocked this file"*, reinstalar SQLAlchemy sin extensiones C (sólo afecta al venv local):
> `set DISABLE_SQLALCHEMY_CEXT=1 && python -m uv pip install --reinstall-package sqlalchemy --no-binary sqlalchemy sqlalchemy==2.1.4`

## Uso rápido

```bash
# 1. Login
curl -s -X POST localhost:8000/api/v1/auth/login -H 'content-type: application/json' \
     -d '{"email":"admin@example.com","password":"..."}'
# → {"access_token":"…","refresh_token":"…","token_type":"bearer","expires_in":900,…}

# 2. Crear almacén y producto (rol manager o admin)
curl -s -X POST localhost:8000/api/v1/warehouses -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"code":"MAD-1","name":"Madrid"}'
curl -s -X POST localhost:8000/api/v1/products -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"sku":"BOLT-M8","name":"Tornillo M8","reorder_level":100}'

# 3. Entrada de stock, segura ante reintentos (rol operator o superior)
curl -s -X POST localhost:8000/api/v1/stock-movements -H "Authorization: Bearer $TOKEN" \
     -H 'Idempotency-Key: PO-2026-0042-L1' -H 'content-type: application/json' \
     -d '{"product_id":"…","warehouse_id":"…","movement_type":"receipt","quantity":500,"reference":"PO-2026-0042"}'
```

- `quantity` es siempre positiva para `receipt`/`issue` (la API aplica el signo); en `adjustment`
  es un delta con signo (requiere `manager`).
- `PATCH` es parcial y exige la `version` leída; si otro la cambió → 409 `VERSION_CONFLICT`.
- Productos y almacenes con historial no se borran: se archivan con `PATCH {"is_active": false}`.
- Todos los errores tienen la forma
  `{"error": {"code", "message", "details", "request_id"}}`; códigos en [SPEC §7.2](docs/SPEC.md#72-errores).

## Testing

```bash
python -m ruff format --check . && python -m ruff check .   # estilo y lint
python -m mypy                                                # tipos (strict)
python -m pytest tests/unit                                   # unit, sin BD
python -m pytest --cov                                        # todo: requiere TEST_DATABASE_URL
```

Los tests de integración usan un **PostgreSQL real**: aplican las migraciones (downgrade + upgrade),
verifican que no hay deriva modelo↔migración, ejecutan concurrencia real (`asyncio.gather` con
conexiones independientes) y truncan las tablas tras cada test. Se niegan a correr si el nombre de
la base no contiene `test`.

Smoke test E2E contra un servidor en marcha (crea usuarios temporales y los desactiva al final):

```bash
SMOKE_BASE_URL=http://127.0.0.1:8000 SMOKE_ADMIN_EMAIL=admin@example.com \
SMOKE_ADMIN_PASSWORD='…' python scripts/smoke_test.py
```

## Docker

Usar un fichero de entorno **propio de Compose** (`.env.docker`, git-ignorado): el `.env` de
desarrollo local lleva `APP_ENV=development` y Compose lo leería por defecto.

```bash
# .env.docker: APP_ENV=production, POSTGRES_PASSWORD=<hex>, JWT_SECRET=<48+ caracteres>
docker compose --env-file .env.docker up --build -d
docker compose --env-file .env.docker run --rm -e ADMIN_PASSWORD='…' api \
    python -m app.cli create-admin --email admin@example.com --full-name Admin
curl localhost:8000/health/ready
```

- Imagen multi-stage, `python:3.12-slim`, usuario no-root (uid 10001), dependencias verificadas
  por hash, sin compiladores, `HEALTHCHECK` sobre `/health/live`.
- Compose: `db` → `migrate` (job único) → `api`; rootfs de sólo lectura, `cap_drop: ALL`,
  `no-new-privileges`, límites de CPU/memoria, puertos publicados sólo en `127.0.0.1`.

## Operación

| Tarea | Cómo |
|---|---|
| Migrar | `alembic upgrade head` **antes** de desplegar código nuevo (job separado, nunca al arrancar) |
| Política de migraciones | Compatibles hacia atrás (*expand/contract*): las réplicas antiguas siguen sirviendo con el esquema nuevo ([ADR-10](docs/ARCHITECTURE.md)) |
| Revertir migración | `alembic downgrade -1` (cada migración tiene `downgrade`) |
| Probes | Liveness: `GET /health/live`. Readiness: `GET /health/ready` (BD + revisión de esquema) |
| Limpieza periódica | `python -m app.cli purge-expired` (p. ej. cron cada hora): refresh tokens caducados, claves de idempotencia > 24 h, contadores de rate limit |
| Proxy inverso | Definir `FORWARDED_ALLOW_IPS` con la IP del proxy. **Si no**, todos los clientes compartirán la IP del proxy y el rate limit por IP se volverá global |
| Escalado | `WEB_CONCURRENCY` workers por contenedor o más réplicas; todo el estado compartido está en PostgreSQL. Conexiones ≈ réplicas × workers × (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) |
| Memoria | ≈ workers × (100 MB + `PASSWORD_HASH_CONCURRENCY` × `ARGON2_MEMORY_COST_KIB`) |
| Logs | JSON a stdout; cada línea lleva `request_id` (también en la cabecera `X-Request-ID`) y `user_id` cuando hay sesión. Contraseñas/tokens nunca se registran (y claves sensibles se redactan) |
| Secretos | Sólo por variables de entorno; producción falla al arrancar con valores débiles o por defecto |

Variables de entorno: todas documentadas en [.env.example](.env.example).

## Estructura

```
app/
  api/            routers, dependencias, mapeo de errores
  core/           config tipada, logging, seguridad, errores, paginación
  db/             engine/sesión, base declarativa, interpretación de errores de BD
  middleware/     request-id + access log, cabeceras, límite de cuerpo
  models/         SQLAlchemy (tablas y constraints)
  repositories/   acceso a datos
  schemas/        contratos Pydantic
  services/       reglas de negocio, autorización, auditoría, idempotencia, rate limit
  cli.py          create-admin, purge-expired
alembic/          migraciones
tests/unit        sin BD
tests/integration PostgreSQL real
scripts/          smoke test E2E
```


<img width="1451" height="871" alt="image" src="https://github.com/user-attachments/assets/dc281de4-897e-4954-a1b2-56a3abe29744" />
