# ArepaGo SOC · APP Con PostgreSQL y Dashboard 

Aplicación local de pedidos y observabilidad para ArepaGo. El dashboard SOC conserva la supervisión operativa; los clientes usan una interfaz de catálogo, carrito, pedidos, pagos contra entrega e historial. Las cuentas, sesiones y módulos de negocio persisten en esquemas separados de PostgreSQL.

## Topología

```text
Navegador
  ├→ ALB de entrada → cliente visual
  └→ ALB de entrada → enrutador de transacciones
                         ├→ ALB Pedidos → servicio ECS Pedidos
                         ├→ ALB Pagos → servicio ECS Pagos
                         ├→ ALB Calificaciones → servicio ECS Calificaciones
                         ├→ ALB Catálogo → servicio ECS Catálogo
                         ├→ Auth Service → PostgreSQL
                         ├→ Reports Service → PostgreSQL
                         ├→ Telemetry / Incidents → PostgreSQL
                         └→ Views Service → consultas agregadas
```

En Docker, los ALB se representan localmente con Nginx. La configuración usa resolución dinámica de destinos y los servicios de negocio y el enrutador arrancan con dos réplicas. En AWS, esos contenedores Nginx se reemplazarán por Application Load Balancers y ECS.

`Telemetry Service` crea el esquema `soc_telemetry`; `Incident Service`, `soc_incidents`; `Auth Service`, `soc_auth`; `Reports Service`, `soc_reports`; y los servicios de negocio usan `soc_catalog`, `soc_orders`, `soc_payments` y `soc_ratings`. Cada módulo conserva su pool y esquema lógico. `Views Service` compone respuestas de lectura a través de las APIs internas de Telemetry e Incidents.

## Conexión local

El Compose local usa PostgreSQL 18.6. Las contraseñas locales de base de datos y administrador se leen desde `secrets/db_password.txt` y `secrets/admin_password.txt`; ambos archivos están ignorados por Git. En un clon nuevo crea contraseñas propias de al menos 12 caracteres. No uses la plantilla de base de datos sin cambiarla.

Para iniciar la aplicación en segundo plano desde la carpeta del repositorio:

```sh
./scripts/start_local.sh
```

Abre `http://localhost:8081`. Para apagar los contenedores cuando termines:

```sh
./scripts/stop_local.sh
```

El apagado conserva el volumen `postgres-data`, incluidos los pedidos y el historial de incidentes. No uses `docker compose down -v` si quieres conservar esa información. Ambos scripts usan `sudo` si tu cuenta no tiene acceso directo al daemon de Docker.

```sh
APP_UID=$(id -u) APP_GID=$(id -g) docker compose --profile local up --build -d
```

`start_local.sh` exporta tu UID/GID automáticamente para que el proceso no-root pueda leer los secretos locales con permisos 600. Si ejecutas Compose a mano, pasa esas variables como en el ejemplo.

El cliente visual queda en `http://localhost:8081`; las APIs se mantienen en la misma dirección bajo `/api`. La base local conserva datos en el volumen `postgres-data`. Para reiniciarla conservando datos usa `docker compose --profile local down` y vuelve a levantarla.

El usuario administrador inicial usa el correo de `ADMIN_EMAIL` (por defecto `admin@arepago.local`) y la contraseña de `secrets/admin_password.txt`. Puedes consultar la contraseña local con `cat secrets/admin_password.txt`. El servicio la aplica solo cuando crea ese usuario; cambiar el archivo no rota una cuenta que ya existe. Las contraseñas se almacenan con scrypt y las sesiones se guardan como hashes con expiración de 12 horas. La cookie de sesión es `HttpOnly` y `SameSite=Strict`; en desarrollo local no usa `Secure` porque Compose sirve HTTP. En producción, el override RDS activa `APP_ENV=production` y `AUTH_COOKIE_SECURE=true`; el gateway se niega a arrancar si falta cualquiera de los dos. El edge conserva el `X-Forwarded-Proto` del ALB, aplica la IP real del cliente para el rate limiting y añade CSP, HSTS, `X-Frame-Options`, `Referrer-Policy` y `Permissions-Policy`.

El gateway no reenvía el ID ni el rol del usuario como headers de confianza: firma una identidad de corta duración con HMAC-SHA256 y cada servicio interno verifica la firma y la expiración antes de autorizar. El secreto compartido vive en `secrets/internal_auth_secret.txt`, se monta como `INTERNAL_AUTH_SECRET_FILE` y `scripts/start_local.sh` lo genera si falta. Sin ese secreto el gateway, los servicios de negocio, reportes e incidentes se niegan a arrancar.

El dashboard de administración requiere una sesión de administrador e integra el resumen reciente del negocio con la supervisión SOC. Las APIs de vistas, telemetría e incidentes están limitadas a ese rol. La consulta de catálogo es pública y sus cambios requieren rol administrador. Las rutas de pedidos, pagos y calificaciones requieren sesión y filtran por el ID de la cuenta autenticada en el servidor. El registro público crea siempre cuentas `customer`; no acepta un rol elegido por el cliente.

Un incidente requiere tres mediciones sanas consecutivas con tráfico para pasar a resuelto (`INCIDENT_RECOVERY_SAMPLES`, configurable entre 2 y 10). Una medición degradada reinicia el conteo; una ventana sin solicitudes no valida recuperación. La evidencia de apertura y las causas iniciales quedan guardadas por separado de las mediciones recientes y se conservan al actualizar o resolver el incidente.

El checkout revalida en el servidor los productos y precios del catálogo y toma una copia del nombre y precio en cada pedido. La dirección de entrega es obligatoria. El checkout acepta efectivo contra entrega y registra el pago como `pending`; no hay pasarela de tarjeta ni cobro simulado. No se cargan productos automáticamente: un administrador publica el menú real o puede ejecutar `python3 scripts/seed_demo_catalog.py` para crear arepas demo con precios ilustrativos, claramente marcadas para revisión. Solo se puede calificar un pedido cuyo estado sea `delivered`, y solo el administrador puede actualizar el ciclo de entrega en la fase administrativa.

## Preparar conexión a Amazon RDS PostgreSQL

No se ha conectado a una instancia real: faltan endpoint, base, usuario, contraseña y acceso de red. Cuando exista la instancia:

1. Copia `.env.example` a `.env`; configura `DB_HOST` con el endpoint RDS, `DB_NAME`, `DB_USER` y `DB_PORT=5432`.
2. Escribe la contraseña del usuario de aplicación en `secrets/db_password.txt`.
3. Define `ADMIN_EMAIL` en `.env` y escribe una contraseña propia de al menos 12 caracteres en `secrets/admin_password.txt`.
4. Descarga el bundle CA vigente de AWS en `secrets/global-bundle.pem`:

   ```sh
   curl -fsS https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -o secrets/global-bundle.pem
   ```

5. Configura el grupo de seguridad de RDS para aceptar PostgreSQL desde los servicios de aplicación, y otorga al usuario de aplicación permisos para crear/usar sus esquemas.
6. Inicia con el override TLS:

   ```sh
   APP_UID=$(id -u) APP_GID=$(id -g) docker compose -f docker-compose.yml -f docker-compose.rds.yml up --build
   ```

   En ECS los secretos se montan con permisos de lectura para el contenedor y se usa el UID 10001 por defecto. Si pruebas el override con archivos locales `600`, exporta tu UID/GID como en el comando anterior.

El override usa TLS `verify-full` con el endpoint y CA de RDS y marca la cookie de autenticación como `Secure`. Los servicios se niegan a iniciar en ese modo si no reciben el certificado raíz configurado. No guardes contraseñas en `.env`, el código ni Git. En AWS, sustituiremos los archivos locales por AWS Secrets Manager/Secrets Manager integration de ECS.

## Checklist de producción

Este repositorio queda endurecido para ejecutarse en un entorno controlado, pero el despliegue público depende de infraestructura que no vive en Compose. Antes de abrir tráfico real:

1. **TLS y exposición**: termina TLS en el ALB con certificado válido y redirige HTTP a HTTPS. El contenedor `edge-alb` no debe tener IP pública; solo el ALB debe alcanzarlo. Mantén el grupo de seguridad del ALB restringido a 80/443 y el de los servicios a la VPC.
2. **Secretos**: usa AWS Secrets Manager o el soporte de secretos de ECS para `db_password`, `admin_password` e `internal_auth_secret`. Rota la contraseña de RDS y el secreto HMAC con un procedimiento que reinicie gateway y servicios. No montes archivos locales en producción.
3. **RDS**: red privada, cifrado en reposo, backups automáticos con point-in-time recovery, Multi-AZ si el SLA lo exige, `max_connections` dimensionado como `réplicas × DB_POOL_MAX_SIZE + reserva`, y TLS `verify-full` con el bundle CA vigente.
4. **Capacidad**: el pool por proceso es 10 y el máximo agregado local es 120 conexiones. Recalcula al cambiar réplicas. `auth-service`, `telemetry-service`, `incident-service`, `views-service` y `reports-service` corren con una sola réplica; si los escalas, mueve el rate limiting de login a Redis/PostgreSQL y revisa la agregación de métricas.
5. **Observabilidad**: los logs de Docker rotan a 10 MB × 3 archivos; en AWS envíalos a CloudWatch o al colector central. Telemetry conserva logs 30 días (`LOG_RETENTION_DAYS`) y las mediciones no se purgan automáticamente. Configura alertas externas sobre `/health`, latencia, errores 5xx y pool.
6. **Datos**: el catálogo arranca vacío. Los cuatro productos `DEMO` quedaron desactivados y no se cargan automáticamente. Publica el menú real desde el panel admin. No ejecutes `scripts/seed_demo_catalog.py` ni los scripts de smoke/incidentes contra producción.
7. **Entrega**: el workflow `.github/workflows/ci.yml` compila, ejecuta los tests, valida Compose y construye la imagen. Añade un paso de escaneo de imagen (por ejemplo Trivy) y un pipeline de despliegue con migraciones controladas antes de usar ECS.
8. **Límites conocidos**: no hay migraciones versionadas (los servicios usan `CREATE TABLE IF NOT EXISTS`), no hay WAF ni protección DDoS propia del repositorio, y el rate limiting es por proceso. El rate limit de login es 5/minuto por IP+correo y 30/minuto por IP; con una sola réplica de auth es efectivo, pero no sustituye un WAF.

## API

- `GET /api/views/overview`: resumen de telemetría, estado e incidentes por servicio.
- `GET /api/views/services`: estado y última medición de Pedidos, Pagos, Calificaciones y Catálogo.
- `GET /api/views/incidents?status=open`: vista de incidentes (`open`, `resolved` o `all`).
- `POST /api/auth/login`: inicia sesión con `{ "email": "…", "password": "…" }` y establece una cookie HttpOnly.
- `POST /api/auth/register`: crea una cuenta cliente; exige contraseña de al menos 12 caracteres y no permite escoger rol.
- `GET /api/auth/me`: devuelve la cuenta asociada a la sesión actual.
- `POST /api/auth/logout`: revoca la sesión y elimina la cookie.
- `POST /api/telemetry`: guarda una medición y evalúa umbrales de latencia, fallos y pool.
- `GET /api/telemetry?service=pedidos&limit=100`
- `GET /api/telemetry/summary`
- `GET /api/incidents?status=open` (también `resolved` o `all`)
- `GET /api/incidents/{id}`
- `GET /api/incidents/{id}/code-context`: fragmentos del gateway y del handler correspondiente incluidos en la imagen actual de Incident.
- `GET /api/telemetry/logs?service=pedidos&from=...&to=...&limit=100`: logs estructurados de advertencia/error/crítico, protegidos por rol admin y retenidos 30 días por defecto.
- `GET /api/incidents/simulator/reference`: escenario fijo proporcionado del 27-09-2026, separado de telemetría real.
- `POST /api/incidents/simulator/run` admite `{"mode":"local_benchmark","rows":100000,"mitigations":["index"]}`: ejecuta `EXPLAIN ANALYZE` antes/después de un índice sobre tabla temporal con datos sintéticos (1.000–500.000 filas). No toca tablas de negocio ni persiste mediciones. El pool reportado es el del Incident Service; no equivale a saturar los pools de Pedidos/Pagos. La comparación de horario es conceptual y una réplica requiere datos de benchmark aprovisionados en ella.
- `POST /api/incidents/actions/analyze-orders`: acción administrativa real que ejecuta `ANALYZE soc_orders.orders`, registra en JSON el actor/acción y devuelve estadísticas de PostgreSQL. Solo actualiza estadísticas del planificador; no repara consultas que necesiten otro índice ni altera pedidos.
- `POST /api/incidents/actions/orders-report`: reporte administrativo en vivo de pedidos y valor por estado de los últimos 30 días, estadísticas e `EXPLAIN` de PostgreSQL sobre `soc_orders.orders`. El agregado se limita a 5 s y se omite si el plan estima más de 100.000 filas.
- `POST /api/incidents/actions/index-orders-created-at`: acción administrativa explícita que crea `orders_created_at_report` con `CREATE INDEX CONCURRENTLY`, verifica su presencia y devuelve el reporte actualizado. Solo se recomienda desde el diagnóstico cuando las estadísticas reales indican un filtro temporal selectivo y no existe ya un índice por fecha.
- Los servicios envían logs estructurados a Telemetry de forma asíncrona. stdout se conserva; el visor muestra logs de la base local. Los incidentes previos a habilitar esta captura conservan solo las mediciones y diagnósticos que ya tenían.
- `POST /api/reports`: el cliente crea un reporte `web_issue` o `store_complaint`; puede vincular un pedido propio.
- `GET /api/reports?status=new&category=web_issue`: cada cliente solo ve sus reportes; Admin puede filtrar y consultar la bandeja global.
- `GET /api/reports/{id}`: detalle aislado para el dueño o Admin.
- `PATCH /api/reports/{id}`: Admin cambia estado y responde; los cambios se guardan en el historial. Resolver o cerrar requiere respuesta al cliente.
- `GET /api/catalogo/products`: catálogo público paginado (`limit`, `offset`).
- `GET /api/catalogo/products/{id}`: detalle de producto activo.
- `POST /api/catalogo/products`: crea producto (administrador).
- `PATCH /api/catalogo/products/{id}` / `DELETE /api/catalogo/products/{id}`: actualiza o desactiva producto (administrador).
- `POST /api/pedidos/orders`: crea pedido autenticado con `items`, `delivery_address` y `payment_method: "cash_on_delivery"`; admite encabezado `Idempotency-Key`.
- `GET /api/pedidos/orders` y `GET /api/pedidos/orders/{id}`: historial y detalle propios del cliente; el administrador consulta todos.
- `PATCH /api/pedidos/orders/{id}/cancel`: cancela un pedido propio aún en estado `placed`.
- `PATCH /api/pedidos/orders/{id}`: administrador avanza un pedido por transiciones permitidas (`placed` → `confirmed` → `preparing` → `out_for_delivery` → `delivered`) o lo cancela antes del despacho.
- `GET /api/pagos/payments`: pagos propios; el administrador puede consultar los últimos 500.
- `PATCH /api/pagos/payments/{id}` con `{ "status": "paid" }`: administrador registra que recibió el efectivo, solo tras verificar que el pedido está entregado.
- `GET /api/calificaciones/ratings`: calificaciones propias; el administrador puede consultar todas.
- `POST /api/calificaciones/ratings`: califica un pedido propio entregado (1 a 5 estrellas).
- `GET /health`: revisa el enrutador, los servicios y su conexión a PostgreSQL.

El panel administrativo incluye catálogo, pedidos, pagos, calificaciones y una bandeja de reportes de clientes sobre problemas web o la tienda. Los reportes son registros propios, con aislamiento por cliente, respuesta administrativa y seguimiento de estado; no son agregados de ventas ni incidentes técnicos.

La ingesta de telemetría requiere una sesión administrativa y una solicitud del mismo origen. El ejemplo interactivo del dashboard inicia la sesión mediante la cookie HttpOnly; el navegador no puede leer el token de sesión.

## Pool y capacidad de RDS

`DB_POOL_MIN_SIZE` y `DB_POOL_MAX_SIZE` configuran el pool de cada proceso; el máximo predeterminado es 10. Compose ejecuta 12 procesos con pool (8 réplicas de negocio + Telemetry, Incidents, Auth y Reports), por lo que el máximo agregado es 120 conexiones frente a las 250 de PostgreSQL local, dejando margen para administración y para el solapamiento de contenedores durante un despliegue. En RDS se debe dimensionar el parámetro `max_connections` con la misma fórmula (réplicas × pool + reserva) y dejar margen adicional.

El gateway registra observaciones reales de solicitudes de negocio y cada 15 segundos persiste conteos, errores HTTP 5xx, latencia p95 y hasta 20 rutas/estados HTTP con errores en Telemetry; UUID y parámetros de consulta no se almacenan en esta atribución. La latencia y los conteos describen el tráfico que atraviesa el router. La métrica de pool se consulta a una réplica del servicio por muestreo y se identifica así en el campo `source`; no debe interpretarse como agregado de todas las réplicas. `/metrics` y `/health/detailed` requieren una sesión de administrador; `/health` sigue disponible para el health check básico. `/health/detailed` presenta conexiones activas y disponibles, espera y errores de adquisición por proceso. Las consultas SQL que exceden `SLOW_QUERY_MS` (500 ms por defecto) producen logs JSON con el módulo, operación, duración e identificador de huella; no se escriben valores ni parámetros SQL.

## Estado de validación

La suite de reglas y autorización se ejecuta con `python3 -m unittest discover -s tests -v` (49 pruebas) y el workflow de CI repite compilación, tests, validación de Compose y build de imagen. El flujo completo ya se validó contra los contenedores locales: catálogo, pedido, entrega, pago y calificación, además de la verificación de identidad interna y del rate limiting de login.

Para verificar detección real de fallos en el entorno local, ejecuta `python3 scripts/test_incident_detection.py`. La prueba se detiene si ya existe un incidente abierto de Pedidos, detiene los contenedores de Pedidos durante 1 segundo, envía solicitudes fallidas por el gateway, los inicia de nuevo y espera Telemetry/Incidents. Interrumpe temporalmente Pedidos (el arranque puede añadir unos segundos), conserva la medición real y el incidente en el historial, pero no modifica pedidos. Si Docker requiere privilegios, ejecuta `sudo -v` y añade `--sudo-docker`. No usar en producción. Para consultar recuperación sin volver a inducir una falla, ejecuta `python3 scripts/test_incident_detection.py --verify-only`.

Para probar el recorrido de compra real en la instancia local, con credenciales de QA y dejando
el pedido pagado/calificado en el historial, ejecuta `python3 scripts/smoke_test_customer_flow.py`.
El script crea y luego desactiva un producto QA; la dirección del pedido lo marca como no
despachable. No usar en producción.

Antes de usar las mitigaciones de reportes en producción, se debe validar el DDL candidato con
la consulta y el esquema reales, dimensionar una réplica de solo lectura y aprovisionar RDS con
red privada, backups, TLS y credenciales administradas.
