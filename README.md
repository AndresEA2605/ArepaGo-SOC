# ArepaGo SOC

Aplicación de pedidos de arepas con panel de observabilidad (SOC). Los clientes usan catálogo, carrito, pedidos, pago contra entrega, historial y calificaciones; el panel administrativo supervisa negocio, telemetría e incidentes. Todo el estado vive en PostgreSQL y los servicios son Python estándar (`http.server` + `psycopg`), sin frameworks web.

- **Local:** `./scripts/start_local.sh` → `http://localhost:8081`
- **Stack:** 22 contenedores, 2 réplicas de cada servicio de negocio y del enrutador
- **Pruebas:** `python3 -m unittest discover -s tests -v` (60 pruebas)
- **Producción:** ver [Checklist de producción](#checklist-de-producción); el código está endurecido, pero el despliegue público requiere TLS, secretos gestionados, RDS con backups y WAF.
- **Siglas:** si no te suena ALB, ECS, RDS, WAF, p95 u otro término, consulta el [Glosario de términos](#glosario-de-términos).

## Arquitectura

```text
Navegador
  │
  ▼
edge-alb (Nginx :8081)  ── cabeceras de seguridad, IP real, proto del ALB
  ├── /            → frontend-service (Nginx, HTML/CSS/JS estático)
  └── /api, /health, /metrics
        │
        ▼
  transaction-router (2 réplicas, :8080)
        ├── /api/auth/*          → auth-service (:8004)          → PostgreSQL
        ├── /api/telemetry*      → telemetry-service (:8001)     → PostgreSQL
        ├── /api/incidents*      → incident-service (:8002)      → PostgreSQL
        ├── /api/views/*         → views-service (:8003)         → Telemetry + Incidents
        ├── /api/reports*        → reports-service (:8005)       → PostgreSQL
        ├── /api/pedidos/*       → pedidos-alb (Nginx) → pedidos-service ×2 (:8000)
        ├── /api/pagos/*         → pagos-alb → pagos-service ×2
        ├── /api/calificaciones/*→ calificaciones-alb → calificaciones-service ×2
        └── /api/catalogo/*      → catalogo-alb → catalogo-service ×2
```

En AWS, los contenedores Nginx que hacen de ALB se sustituyen por Application Load Balancers y los servicios por tareas ECS. En local cumplen la misma función con resolución DNS dinámica y reintentos entre réplicas (`proxy_next_upstream`, `max_fails`, `fail_timeout`).

### Componentes

| Componente | Réplicas | Puerto interno | Responsabilidad |
|---|---:|---:|---|
| `edge-alb` | 1 | 80 (host 8081) | Entrada única: seguridad, IP real, proxy |
| `frontend-service` | 1 | 80 | Cliente estático (sin build, sin CDN) |
| `transaction-router` | 2 | 8080 | Enrutado, sesión, CSRF, identidad interna, métricas |
| `*-alb` (4) | 1 c/u | 80 | Balanceo local entre réplicas de negocio |
| `pedidos/pagos/calificaciones/catalogo-service` | 2 c/u | 8000 | Lógica de negocio y persistencia |
| `auth-service` | 1 | 8004 | Registro, login, sesiones, rate limiting |
| `telemetry-service` | 1 | 8001 | Ingesta/consulta de mediciones y logs |
| `incident-service` | 1 | 8002 | Evaluación de umbrales y ciclo de incidentes |
| `views-service` | 1 | 8003 | Vistas de lectura agregadas (sin BD propia) |
| `reports-service` | 1 | 8005 | Reportes de clientes e historial de respuestas |
| `postgres` | 1 | 5432 | PostgreSQL 18.6, volumen `postgres-data` |

### Flujo de una petición

1. El navegador llama `https://host/api/...`; el ALB termina TLS y reenvía HTTP a `edge-alb` con `X-Forwarded-Proto` y `X-Forwarded-For`.
2. `edge-alb` resuelve la IP real del cliente (rangos privados del ALB), añade CSP/HSTS/`X-Frame-Options` y enruta al `transaction-router`.
3. El router valida sesión (cookie `ag_session`) y CSRF (`Origin` + proto), y autoriza por rol y recurso.
4. El router firma la identidad (`X-ArepaGo-Identity`) con HMAC-SHA256 y la reenvía al servicio destino.
5. El servicio verifica firma y expiración (120 s) antes de confiar en el `user_id`/rol, ejecuta la lógica y responde JSON.
6. El router registra la observación (servicio, ruta normalizada, estado, latencia) para el muestreo de telemetría.

La identidad interna **no** viaja como headers de confianza: un cliente no puede falsificarla sin el secreto. `INTERNAL_AUTH_SECRET_FILE` se monta en router, servicios de negocio, incidents y reports; auth/telemetry/views no lo necesitan.

## Modelo de datos

Un esquema por módulo, con un pool de conexiones por proceso:

| Esquema | Tablas | Contenido |
|---|---|---|
| `soc_auth` | `users`, `sessions` | Cuentas (email, `scrypt`, rol) y sesiones (hash del token, expiración) |
| `soc_catalog` | `products` | Nombre, descripción, precio, imagen, activo |
| `soc_orders` | `orders`, `order_items` | Pedido (cliente, dirección, estado, total, ETA, `idempotency_key`) y líneas con copia de nombre/precio |
| `soc_payments` | `payments` | Pago por pedido: método, estado, monto, quién confirmó y cuándo |
| `soc_ratings` | `ratings` | Calificación 1–5 y comentario por pedido entregado |
| `soc_telemetry` | `measurements`, `logs` | Muestras de tráfico y logs estructurados |
| `soc_incidents` | `incidents` | Incidentes con evidencia inicial, estado y motivo de cierre |
| `soc_reports` | `reports`, `report_events` | Reportes de clientes y auditoría de cambios de estado |
| `soc_migrations` | `schema_migrations` | Versión aplicada de cada módulo (DDL transaccional) |

Cada servicio aplica sus migraciones versionadas al arrancar (`services/migrations.py`): `soc_migrations.schema_migrations` registra la versión por módulo y un lock de PostgreSQL evita carreras entre réplicas. La versión 1 reproduce el esquema existente con `IF NOT EXISTS`, así que una base ya poblada se adopta sin recrear tablas.

### Manejo y retención de datos

- **Aislamiento por cliente:** pedidos, pagos, calificaciones y reportes filtran por el `user_id` de la sesión en el servidor; el admin ve el conjunto global.
- **Integridad:** el checkout revalida productos y precios y guarda una copia de nombre/precio en `order_items`, de modo que cambios futuros del catálogo no alteran pedidos históricos. `order_items` cae en cascada con su pedido.
- **Idempotencia:** `orders(customer_id, idempotency_key)` es único; reintentar el checkout no duplica pedidos.
- **Contraseñas:** `scrypt` (N=2^14, r=8, p=1, 64 bytes) con sal aleatoria. Nunca se guarda la contraseña en claro.
- **Sesiones:** se guarda solo el SHA-256 del token, con expiración (`SESSION_HOURS`, 12 h por defecto). Las expiradas se purgan al iniciar sesión.
- **Logs:** mensajes truncados a 4.000 caracteres y trazas a 16.000; rutas normalizadas (`/orders/{id}`) para no almacenar identificadores ni query strings. Los parámetros SQL no se registran.
- **Retención:** logs, mediciones e incidentes resueltos se purgan cada hora según `LOG_RETENTION_DAYS` (30), `MEASUREMENT_RETENTION_DAYS` (30) e `INCIDENT_RETENTION_DAYS` (180). Los incidentes abiertos nunca se purgan; pedidos, pagos, calificaciones y reportes se conservan.
- **Backups:** en local el volumen `postgres-data` conserva todo; para un respaldo lógico usa `pg_dump`. En RDS se usan backups automáticos con point-in-time recovery.
- **Catálogo inicial:** vacío. Los productos `DEMO · …` quedan inactivos y no se cargan solos; el menú real se publica desde el panel admin. Un producto retirado sigue visible para el admin (marcado como retirado) y puede republicarse sin recrearlo.

## Seguridad y sesiones

- **Cookie:** `ag_session` con `HttpOnly`, `SameSite=Strict`, `Max-Age` de la sesión y `Secure` cuando `AUTH_COOKIE_SECURE=true`. Las respuestas llevan `Cache-Control: no-store` y `nosniff`.
- **CSRF:** las rutas con cookie exigen `Origin` del mismo host y esquema; sin `Origin` y con cookie se rechaza (403). Los clientes API sin cookie (Bearer) no se ven afectados.
- **Rate limiting:** login 5/min por IP+correo y 30/min por IP; registro 5/min por IP. Es en memoria y por proceso: con una sola réplica de auth es efectivo; al escalar hay que moverlo a Redis/PostgreSQL.
- **Cabeceras:** CSP, HSTS (solo si el proto original es HTTPS), `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, `X-Content-Type-Options`.
- **Contenedores:** imagen non-root con UID/GID configurables, `cap_drop: ALL`, `no-new-privileges` y `server_tokens off`.
- **Producción:** `APP_ENV=production` obliga a `AUTH_COOKIE_SECURE=true` y a secreto interno; si falta algo, el gateway no arranca.
- **Secretos:** `db_password`, `admin_password` e `internal_auth_secret` se montan como archivos (`*_FILE`) y están en `.gitignore`. En AWS deben venir de Secrets Manager.

## Flujo de negocio

1. **Catálogo:** lectura pública paginada (solo productos publicados); altas, cambios, publicación y retiro requieren admin. Retirar es una baja lógica: el producto se oculta al público pero el admin puede volver a publicarlo.
2. **Pedido:** autenticado, con `items`, `delivery_address` y `payment_method: cash_on_delivery`. Estados: `placed → confirmed → preparing → out_for_delivery → delivered`, con cancelación antes del despacho.
3. **Pago:** se crea `pending`; el admin lo marca `paid` solo si el pedido está `delivered`. No hay pasarela de tarjeta.
4. **Calificación:** solo el dueño de un pedido `delivered`, una vez, con 1–5 estrellas.
5. **Reportes:** el cliente crea `web_issue` o `store_complaint` (puede vincular su pedido); el admin responde y cambia estado, con historial en `report_events`.

## Observabilidad y detección de incidentes

### Recolección de métricas

El `transaction-router` mide cada petición que reenvía a un servicio de negocio: servicio, método, ruta normalizada, estado HTTP y duración. Un hilo muestreador publica cada **15 segundos** (`METRICS_SAMPLE_SECONDS`, mínimo 5):

- `request_count` y `error_count` (HTTP ≥ 500) desde la muestra anterior,
- latencia **p95** de las observaciones de la ventana de 15 s,
- hasta 20 rutas con errores (método, ruta normalizada, conteos y estados),
- pool de la base (`active`/`max`) consultado por cada router a una réplica del servicio; el agregado conserva la peor ocupación.

Cada réplica del router publica su propia medición con `replica` (hostname): en un intervalo hay dos filas por servicio y cada una cubre el tráfico que atendió esa réplica. Telemetry agrega la **última muestra de cada réplica**: suma `request_count`/`error_count`, toma el peor p95 y la peor ocupación de pool, y fusiona los diagnósticos. Una réplica que deja de publicar sale del agregado tras `MEASUREMENT_FRESH_SECONDS` (120 s), para no inflar el tráfico con datos viejos.

Las mediciones se guardan en `soc_telemetry.measurements`. El servicio Incident consulta el agregado cada `INCIDENT_EVALUATE_SECONDS` (15 s) y aplica los umbrales **una vez por servicio**, no por réplica. `POST /evaluate` sigue disponible para evaluación manual o pruebas.

### Umbrales

| Señal | Condición | Variable |
|---|---|---|
| Latencia | p95 ≥ 1.000 ms | `LATENCY_THRESHOLD_MS` |
| Errores | tasa ≥ 20 % con al menos 1 solicitud | `ERROR_RATE_THRESHOLD` |
| Pool | ocupación ≥ 75 % del máximo | `POOL_WARNING_RATIO` |

**Severidad:** `critical` si la latencia ≥ 5× el umbral (5.000 ms), la tasa de error ≥ 50 % o el pool está lleno; en cualquier otro caso, `warning`.

### Ciclo de vida del incidente

1. **Apertura:** una medición que supera cualquier umbral crea un incidente `open`. Un índice único parcial garantiza **uno abierto por servicio**; la evaluación usa un lock por servicio para evitar carreras entre réplicas.
2. **Actualización:** cada nueva medición degradada actualiza severidad, causas, `last_seen` y la última medición, y **reinicia** el contador de recuperación.
3. **Recuperación con tráfico:** una medición sin brecha y con solicitudes suma 1 al contador. Al llegar a `INCIDENT_RECOVERY_SAMPLES` (3 por defecto, entre 2 y 10) el incidente pasa a `resolved` con motivo `recovered_with_traffic`.
4. **Ventana sin tráfico:** una muestra con 0 solicitudes no confirma ni niega la recuperación: **no suma ni reinicia** el contador. Así, el tráfico intermitente entre réplicas no impide cerrar un incidente ya recuperado.
5. **Cierre por inactividad:** si no hay nuevas brechas durante `INCIDENT_INACTIVITY_MINUTES` (30 por defecto; 0 lo desactiva), el incidente se cierra con motivo `inactivity_timeout`. Evita incidentes eternos en servicios que dejaron de recibir visitas.
6. **Evidencia:** `initial_reasons` e `initial_measurement` conservan las causas y la medición del momento de apertura, separadas de la última medición. El detalle incluye fragmentos de código del gateway y del handler implicado (`/incidents/{id}/code-context`), extraídos por AST de la imagen actual del servicio de incidentes.

Una muestra degradada reinicia el conteo; por eso se requieren muestras **consecutivas** con tráfico real.

### Qué detecta y qué no

- **Detecta:** degradación del tráfico que atraviesa el gateway (latencia, 5xx, pool de la réplica más saturada) y la conserva como incidente auditable.
- **No detecta:** fallos internos sin tráfico, errores solo en logs, saturación de disco/CPU, caída de la base que no llegue a responder al gateway, ni problemas de red del ALB.
- **No alerta:** no hay paging ni notificaciones. `/health` y `/metrics` son para sondas y dashboards externos.
- **Healthchecks:** son independientes de los incidentes; Compose reinicia contenedores `unhealthy`, pero eso no abre un incidente.

### Logs

Cada servicio escribe JSON a stdout y envía asíncrono a Telemetry los eventos de nivel `warning`, `error` y `critical` (cola de 1.000, sin bloquear la petición). `GET /api/telemetry/logs` (admin) filtra por servicio y rango, hasta 31 días y 200 filas. Docker rota `10 MB × 3` por contenedor.

### Verificaciones periódicas

| Verificación | Frecuencia |
|---|---|
| Muestreo de métricas del router | 15 s (`METRICS_SAMPLE_SECONDS`) |
| Evaluación de umbrales | 15 s (`INCIDENT_EVALUATE_SECONDS`) |
| Healthcheck de contenedores | 15 s (timeout 5 s; 5–6 reintentos; `start_period` 20–60 s) |
| Refresco del panel | 15 s |
| Purga de logs y mediciones vencidos | Cada hora |
| Purga de incidentes resueltos | Cada hora |
| Purga de sesiones expiradas | En cada login |
| Retención de logs y mediciones | 30 días (`LOG_RETENTION_DAYS`, `MEASUREMENT_RETENTION_DAYS`) |
| Retención de incidentes resueltos | 180 días (`INCIDENT_RETENTION_DAYS`) |
| Cierre por inactividad de incidentes | 30 min sin brechas (`INCIDENT_INACTIVITY_MINUTES`) |

### Timeouts y límites

| Nivel | Valor |
|---|---|
| Edge → router | connect 3 s, read 10 s |
| Router → servicios | 4 s (`UPSTREAM_TIMEOUT_SECONDS`) |
| SQL | `statement_timeout` 15 s, `lock_timeout` 5 s, `idle_in_transaction` 30 s |
| Pool | adquisición 10 s, vida máx. 1.800 s, inactividad 300 s |
| Conexión a BD | 5 s (`DB_CONNECT_TIMEOUT`) |
| Sesión | 12 h (`SESSION_HOURS`, 1–168) |
| Identidad interna | 120 s |

## Ejecución local

Requisitos: Docker con Compose. Crea los secretos locales (mínimo 12 caracteres) si no existen:

```sh
printf 'una-contraseña-local\n' > secrets/db_password.txt
printf 'otra-contraseña-local\n' > secrets/admin_password.txt
chmod 600 secrets/db_password.txt secrets/admin_password.txt
```

`scripts/start_local.sh` genera `secrets/internal_auth_secret.txt` si falta, exporta tu UID/GID (para que el proceso non-root lea secretos `600`) y levanta el stack:

```sh
./scripts/start_local.sh     # http://localhost:8081
./scripts/stop_local.sh      # conserva postgres-data
```

No uses `docker compose down -v` si quieres conservar pedidos e historial. Admin inicial: `ADMIN_EMAIL` (por defecto `admin@arepago.local`) con la contraseña de `secrets/admin_password.txt`; se aplica solo al crear la cuenta.

Para levantar a mano:

```sh
APP_UID=$(id -u) APP_GID=$(id -g) docker compose --profile local up --build -d
```

## Amazon RDS y producción

1. Copia `.env.example` a `.env` y configura `DB_HOST` (endpoint RDS), `DB_NAME`, `DB_USER`, `DB_PORT=5432`.
2. Escribe la contraseña de aplicación en `secrets/db_password.txt` y la del admin en `secrets/admin_password.txt`.
3. Descarga el bundle CA: `curl -fsS https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -o secrets/global-bundle.pem`.
4. Permite PostgreSQL desde los servicios en el security group de RDS y otorga permisos de esquema al usuario de aplicación.
5. Arranca con el override TLS:

   ```sh
   APP_UID=$(id -u) APP_GID=$(id -g) docker compose -f docker-compose.yml -f docker-compose.rds.yml up --build
   ```

El override usa `sslmode=verify-full` con la CA de RDS, activa `APP_ENV=production` y `AUTH_COOKIE_SECURE=true`. No guardes contraseñas en `.env`, en el código ni en Git.

### Checklist de producción

El repositorio está endurecido, pero el despliegue público depende de infraestructura que no vive en Compose:

1. **TLS y exposición:** certificado válido en el ALB, redirección HTTP→HTTPS, `edge-alb` sin IP pública y security groups restringidos.
2. **Secretos:** AWS Secrets Manager para `db_password`, `admin_password` e `internal_auth_secret`, con rotación que reinicie gateway y servicios.
3. **RDS:** red privada, cifrado, backups con PITR, Multi-AZ si el SLA lo exige, TLS `verify-full` y `max_connections ≥ réplicas × pool + reserva`.
4. **Capacidad:** 12 procesos con pool × 10 = 120 conexiones locales. Recalcula al escalar. `auth`, `telemetry`, `incidents`, `views` y `reports` corren con una réplica; si escalas auth, mueve el rate limiting a Redis/PostgreSQL.
5. **Observabilidad:** envía logs a CloudWatch o a un colector central y configura alertas externas sobre `/health`, 5xx, latencia y pool. Logs, mediciones e incidentes resueltos se purgan según la retención configurada.
6. **Datos:** publica el menú real; no ejecutes `seed_demo_catalog.py` ni los scripts de smoke/incidentes contra producción.
7. **Entrega:** antes de desplegar, ejecuta las pruebas, la compilación, la validación de Compose y el build de la imagen. Añade escaneo (Trivy) y un pipeline de despliegue con migraciones controladas.
8. **Límites conocidos:** sin WAF/DDoS propio, rate limiting por proceso y sin alertas integradas. El esquema se aplica con migraciones versionadas al arrancar; aun así, coordina los despliegues para no solapar versiones incompatibles.

## API

Autenticación y sesión:

- `POST /api/auth/register` — crea una cuenta `customer` (contraseña ≥ 12, sin elegir rol).
- `POST /api/auth/login` — `{email, password}`; responde con cookie `HttpOnly`.
- `GET /api/auth/me` — cuenta de la sesión.
- `POST /api/auth/logout` — revoca la sesión y borra la cookie.

Observabilidad (admin salvo `/health`):

- `GET /api/views/overview` — estado, última medición e incidentes por servicio.
- `GET /api/views/services` — estado y última medición de cada servicio.
- `GET /api/views/incidents?status=open|resolved|all`
- `POST /api/telemetry` — guarda una medición y evalúa umbrales.
- `GET /api/telemetry?service=pedidos&limit=100`, `GET /api/telemetry/summary`
- `GET /api/telemetry/logs?service=…&from=…&to=…&limit=…` — logs estructurados.
- `GET /api/incidents?status=…`, `GET /api/incidents/{id}`
- `GET /api/incidents/{id}/code-context` — fragmentos de código de la imagen actual.
- `GET /api/incidents/simulator/reference` — escenario fijo del 27-09-2026.
- `POST /api/incidents/simulator/run` — `{"mode":"local_benchmark","rows":100000,"mitigations":["index"]}`: `EXPLAIN ANALYZE` sobre tabla temporal; no toca datos de negocio.
- `POST /api/incidents/actions/orders-report` — reporte de pedidos por estado (30 días) con `EXPLAIN`.
- `POST /api/incidents/actions/analyze-orders` — `ANALYZE soc_orders.orders` (solo estadísticas).
- `POST /api/incidents/actions/index-orders-created-at` — índice concurrente por fecha si el diagnóstico lo recomienda.
- `GET /metrics` — métricas del proceso del router (requiere admin).
- `GET /health` — salud del router y sus dependencias.
- `GET /health/detailed` — salud, pools y dependencias (requiere admin).

Negocio:

- `GET /api/catalogo/products?limit=&offset=`, `GET /api/catalogo/products/{id}` (público).
- `GET /api/catalogo/products?include_inactive=true` — incluye retirados (solo admin).
- `POST|PATCH|DELETE /api/catalogo/products…` (admin).
- `POST /api/pedidos/orders` — `items`, `delivery_address`, `payment_method: "cash_on_delivery"`, `Idempotency-Key`.
- `GET /api/pedidos/orders`, `GET /api/pedidos/orders/{id}`, `PATCH /api/pedidos/orders/{id}/cancel`.
- `PATCH /api/pedidos/orders/{id}` — admin avanza el estado o cancela.
- `GET /api/pagos/payments`, `PATCH /api/pagos/payments/{id}` — admin confirma `paid` tras la entrega.
- `GET /api/calificaciones/ratings`, `POST /api/calificaciones/ratings`.
- `POST /api/reports`, `GET /api/reports?status=&category=`, `GET /api/reports/{id}`, `PATCH /api/reports/{id}`.

La ingesta de telemetría exige sesión admin y mismo origen. Los reportes de clientes son registros propios con aislamiento por cuenta; no son agregados de ventas ni incidentes técnicos.

## Glosario de términos

Esta sección explica en lenguaje llano las siglas y conceptos que aparecen en el README. No hace falta memorizarlos: es una referencia para leer el resto del documento.

### Infraestructura y despliegue

| Término | Nombre completo | Qué significa |
|---|---|---|
| **ALB** | Application Load Balancer | Balanceador de carga de AWS para HTTP/HTTPS (capa 7). Recibe cada petición y la reparte entre varias réplicas. En local lo imita un Nginx; en AWS es un servicio gestionado. |
| **Nginx** | *Engine-X* (nombre propio) | Servidor web y proxy inverso. Aquí sirve el frontend estático y hace de ALB interno entre réplicas. |
| **ECS** | Elastic Container Service | Orquestador de contenedores de AWS. Ejecuta la imagen Docker como tareas/servicios, las reinicia si fallan y las registra en un ALB. |
| **RDS** | Relational Database Service | PostgreSQL gestionado por AWS: AWS se encarga de backups, cifrado, réplicas y actualizaciones. |
| **WAF** | Web Application Firewall | Cortafuegos de aplicaciones web. Filtra ataques HTTP (SQLi, XSS, bots) antes de que lleguen al ALB. |
| **DDoS** | Distributed Denial of Service | Ataque de denegación de servicio distribuido: muchas máquinas saturan un servicio. Se mitiga en el borde (AWS Shield/WAF), no en el código. |
| **TLS** | Transport Layer Security | Protocolo que cifra HTTP. El ALB termina TLS y reenvía HTTP dentro de la red privada. |
| **HTTPS** | HTTP over TLS | HTTP cifrado con TLS. |
| **PITR** | Point-In-Time Recovery | Recuperar la base de datos a un instante concreto usando backups + logs. |
| **Multi-AZ** | Multi-Availability Zone | Desplegar en varias zonas de disponibilidad para sobrevivir a la caída de un centro de datos. |
| **SLA** | Service Level Agreement | Acuerdo de nivel de servicio: disponibilidad/rendimiento prometido al cliente. |
| **SLO** | Service Level Objective | Objetivo interno medible (por ejemplo, 99,9 % de peticiones por debajo de 1 s). |
| **CI** | Continuous Integration | Integración continua: compilar y probar automáticamente cada cambio. En este repo se ejecuta en local; el workflow de GitHub se retiró. |
| **CD** | Continuous Delivery/Deployment | Entrega/despliegue continuo: automatizar la publicación a producción. |
| **Docker** | — | Plataforma de contenedores: empaqueta la app y sus dependencias en una imagen. |
| **Docker Compose** | — | Herramienta para definir y levantar varios contenedores con un archivo YAML. |
| **Contenedor** | — | Proceso aislado que ejecuta una imagen y comparte el kernel del host. |
| **Imagen** | — | Plantilla inmutable con el código y las dependencias de un contenedor. |
| **UID/GID** | User ID / Group ID | Identificador numérico de usuario/grupo en Linux. Se usa para que el contenedor corra sin privilegios. |
| **Secrets Manager** | AWS Secrets Manager | Servicio de AWS para guardar y rotar contraseñas y claves fuera del código. |
| **CloudWatch** | Amazon CloudWatch | Servicio de AWS para recoger logs, métricas y alarmas. |
| **Trivy** | — | Escáner de vulnerabilidades de imágenes de contenedor y dependencias. |

### Red, proxies y balanceo

| Término | Nombre completo | Qué significa |
|---|---|---|
| **Proxy inverso** | Reverse proxy | Servidor que recibe peticiones de clientes y las reenvía a servicios internos; centraliza TLS, cabeceras y límites. |
| **Upstream** | — | Servidor destino al que un proxy reenvía la petición (aquí, una réplica de un servicio). |
| **Balanceo de carga** | Load balancing | Repartir peticiones entre varias réplicas para escalar y tolerar fallos. |
| **Réplica** | Replica | Otra instancia del mismo proceso que atiende el mismo servicio. No es una copia de la base de datos. |
| **Failover** | — | Conmutación automática a otra réplica cuando una falla. |
| **DNS** | Domain Name System | Traduce nombres (`pedidos-service`) a direcciones IP. |
| **X-Forwarded-For** | — | Cabecera HTTP que añade el proxy con la IP original del cliente. |
| **Healthcheck** | — | Comprobación periódica de que un servicio responde. Docker/ALB la usan para reiniciar o sacar de rotación una réplica. |
| **Borde (edge)** | — | Primera capa que toca la petición (ALB/Nginx). |
| **API** | Application Programming Interface | Contrato de funciones/rutas que un programa ofrece a otro. Aquí son rutas HTTP que devuelven JSON. |
| **JSON** | JavaScript Object Notation | Formato de texto para intercambiar datos estructurados. |

### Seguridad

| Término | Nombre completo | Qué significa |
|---|---|---|
| **SOC** | Security Operations Center | Centro de operaciones de seguridad: personas y procesos que vigilan y responden a incidentes. Aquí da nombre al panel de observabilidad. |
| **HMAC** | Hash-based Message Authentication Code | Firma con clave secreta. El router firma `{user_id, role, exp}` y los servicios verifican que no fue alterado. |
| **SHA-256** | Secure Hash Algorithm, 256 bits | Función hash criptográfica. Se usa para guardar el hash del token de sesión, no el token en claro. |
| **TTL** | Time To Live | Tiempo de vida. La identidad interna caduca a los 120 s. |
| **scrypt** | — | Algoritmo de derivación de contraseñas deliberadamente costoso; dificulta la fuerza bruta. |
| **Salt (sal)** | — | Valor aleatorio añadido a la contraseña antes de hashearla; dos contraseñas iguales producen hashes distintos. |
| **CSRF** | Cross-Site Request Forgery | Ataque que hace que el navegador del usuario envíe una petición no deseada. Se mitiga exigiendo `Origin` del mismo sitio. |
| **CORS** | Cross-Origin Resource Sharing | Reglas del navegador para permitir peticiones entre orígenes distintos. Aquí no se abre: mismo origen. |
| **CSP** | Content Security Policy | Cabecera que limita de qué fuentes se pueden cargar scripts, estilos e imágenes. |
| **HSTS** | HTTP Strict Transport Security | Cabecera que obliga al navegador a usar HTTPS durante un tiempo. |
| **Cookie HttpOnly** | — | Cookie que JavaScript no puede leer; reduce el robo de sesión por XSS. |
| **SameSite=Strict** | — | El navegador no envía la cookie en peticiones iniciadas desde otro sitio; ayuda contra CSRF. |
| **Secure (cookie)** | — | La cookie solo se envía por HTTPS. |
| **XSS** | Cross-Site Scripting | Inyección de JavaScript malicioso en una página. |
| **SQLi** | SQL Injection | Inyección de SQL malicioso en consultas. |
| **Rate limiting** | Limitación de tasa | Limitar cuántas peticiones puede hacer un cliente en una ventana de tiempo. |
| **Ventana deslizante** | Sliding window | Técnica de rate limiting que cuenta las peticiones de los últimos N segundos, no bloques fijos. |
| **Idempotencia** | Idempotency | Propiedad por la que repetir la misma operación produce el mismo resultado y no duplica datos. El checkout usa `Idempotency-Key`. |

### Datos y base de datos

| Término | Nombre completo | Qué significa |
|---|---|---|
| **PostgreSQL** | — | Base de datos relacional de código abierto. |
| **psycopg** | — | Driver de Python para PostgreSQL. |
| **Pool de conexiones** | Connection pool | Conjunto reutilizable de conexiones a la base de datos; evita abrir una conexión por petición. |
| **Esquema** | Schema | Espacio de nombres que agrupa tablas (`soc_auth`, `soc_orders`...). |
| **DDL** | Data Definition Language | Sentencias que definen estructura (`CREATE TABLE`, `ALTER TABLE`). |
| **Migración** | Migration | Cambio de esquema versionado y reproducible. |
| **JSONB** | JSON Binary | Tipo de PostgreSQL que guarda JSON binario, indexable y consultable. |
| **UUID** | Universally Unique Identifier | Identificador de 128 bits; aquí se usa como clave primaria. |
| **Transacción** | Transaction | Conjunto de operaciones que se confirman todas o se revierten todas. |
| **Lock asesorado** | Advisory lock | Cerradura de PostgreSQL que la aplicación usa para coordinar procesos (por ejemplo, migraciones o evaluación de incidentes). |
| **SQL** | Structured Query Language | Lenguaje para consultar y modificar bases de datos relacionales. |

### Observabilidad

| Término | Nombre completo | Qué significa |
|---|---|---|
| **Telemetría** | Telemetry | Datos automáticos sobre el comportamiento del sistema: tráfico, latencia, errores, pool. |
| **Métrica** | Metric | Valor numérico medido en el tiempo (peticiones, errores, latencia). |
| **Log** | — | Registro textual estructurado de un evento. |
| **Incidente** | Incident | Degradación detectada que se registra, se sigue y se cierra con evidencia. |
| **Severidad** | Severity | Gravedad del incidente: `warning` o `critical`. |
| **p95** | Percentil 95 | Ver la explicación detallada siguiente. |
| **Tasa de error** | Error rate | `errores / peticiones`. Aquí cuenta HTTP ≥ 500. |
| **Pool saturado** | Pool saturation | Porcentaje de conexiones ocupadas sobre el máximo del pool. |
| **Umbral** | Threshold | Valor a partir del cual se considera que hay degradación. |
| **Alertas / paging** | Alerting / paging | Avisar a una persona cuando algo falla. Este proyecto no las incluye; se conectan sondas externas. |
| **On-call** | — | Persona de guardia que responde a alertas. |

### ¿Qué significa “latencia p95”?

La **latencia** es cuánto tarda una petición desde que llega hasta que responde. Si miras solo el promedio, una minoría de peticiones muy lentas puede quedar oculta. El **percentil 95 (p95)** responde a esta pregunta:

> Si ordenas todas las peticiones de la ventana de menor a mayor, el p95 es el tiempo por debajo del cual queda el 95 % de ellas. El 5 % restante tardó igual o más.

Ejemplo con 20 peticiones ordenadas: 10, 12, 15, 18, 20, 22, 25, 30, 35, 40, 45, 50, 60, 80, 100, 120, 150, 200, 300 y 900 ms. El p95 está entre la posición 19 y la 20 (300–900 ms); en la práctica se toma un valor cercano a 300 ms. Eso significa que 19 de 20 usuarios tuvieron ≤ 300 ms y uno tuvo 900 ms. El promedio, en cambio, sería ~130 ms y escondería esa cola lenta.

Por eso el umbral de este proyecto es **p95 ≥ 1.000 ms**: no reacciona a un único pico aislado, sino a que una parte relevante del tráfico ya sea lenta. El p95 se calcula sobre las observaciones reales de la ventana de muestreo (15 s) y se agrega entre réplicas tomando el peor valor.

Los percentiles cercanos se leen igual: **p50** es la mediana (la mitad de las peticiones son más rápidas), **p99** es el 1 % más lento. Cuanto más alto el percentil, más sensible es a la cola lenta.

## Pruebas y validación

```sh
python3 -m unittest discover -s tests -v      # 60 pruebas
python3 -m compileall -q services scripts tests
```

La validación local incluye `pip check`, compilación, pruebas, validación de Compose (base + RDS) y build de la imagen. El flujo completo se validó contra contenedores locales: catálogo, pedido, entrega, pago, calificación, identidad interna y rate limiting.

Scripts auxiliares (**solo local**, nunca contra producción):

- `python3 scripts/test_incident_detection.py` — induce una caída real de Pedidos y verifica detección y recuperación; `--verify-only` solo consulta.
- `python3 scripts/smoke_test_customer_flow.py` — recorrido de compra con datos QA.
- `python3 scripts/seed_demo_catalog.py` — catálogo demo inactivo para pruebas.

## Pool y capacidad

`DB_POOL_MIN_SIZE`/`DB_POOL_MAX_SIZE` configuran el pool de cada proceso (máximo 10). Compose ejecuta 12 procesos con pool (8 réplicas de negocio + Telemetry, Incidents, Auth y Reports): 120 conexiones máximas frente a las 250 de PostgreSQL local. En RDS dimensiona `max_connections` con la misma fórmula, dejando reserva para administración y despliegues solapados.

Las consultas que superan `SLOW_QUERY_MS` (500 ms) generan un log JSON con módulo, operación, duración y huella SHA-256; no se registran valores ni parámetros SQL.
