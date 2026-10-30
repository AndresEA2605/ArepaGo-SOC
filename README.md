# ArepaGo SOC

Aplicación de pedidos de arepas con panel de observabilidad (SOC, *Security Operations Center*: centro que vigila y responde a incidentes). Los clientes usan catálogo, carrito, pedidos, pago contra entrega, historial y calificaciones; el panel administrativo supervisa negocio, telemetría (métricas automáticas del sistema) e incidentes (degradaciones detectadas). Todo el estado vive en PostgreSQL (base de datos relacional) y los servicios son Python estándar (`http.server` + `psycopg`, el driver de PostgreSQL para Python), sin frameworks web.

- **Local:** `./scripts/start_local.sh` → `http://localhost:8081`
- **Stack:** 22 contenedores (procesos aislados que ejecutan una imagen: plantilla con el código y sus dependencias), 2 réplicas (instancias del mismo servicio) de cada servicio de negocio y del enrutador
- **Pruebas:** `python3 -m unittest discover -s tests -v` (60 pruebas)

**Secciones:** [Arquitectura](#arquitectura) · [Flujos](#flujos-de-extremo-a-extremo) · [Datos](#modelo-de-datos) · [Reglas](#reglas-de-negocio) · [Seguridad](#seguridad-y-sesiones) · [Observabilidad](#observabilidad-y-detección-de-incidentes) · [Local](#ejecución-local) · [API](#api)

## Arquitectura

El tráfico entra por `edge-alb`, un Nginx (*servidor web y proxy inverso*) que actúa como balanceador de entrada local. El mapa completo:

```text
Navegador
  │
  ▼
edge-alb (Nginx :8081)  ── cabeceras de seguridad, IP real, protocolo original
  ├── /            → frontend-service (Nginx, HTML/CSS/JS estático)
  └── /api, /health, /metrics
        │
        ▼
  transaction-router (2 réplicas, :8080)
        ├── /api/auth/*          → auth-service (:8004)          → PostgreSQL
        ├── /api/telemetry*      → telemetry-service (:8001)     → PostgreSQL
        ├── /api/incidents*      → incident-service (:8002)      → PostgreSQL
        ├── /api/views/*         → views-service (:8003)         → telemetry-service + incident-service
        ├── /api/reports*        → reports-service (:8005)       → PostgreSQL
        ├── /api/pedidos/*       → pedidos-alb (Nginx) → pedidos-service ×2 (:8000)
        ├── /api/pagos/*         → pagos-alb → pagos-service ×2
        ├── /api/calificaciones/*→ calificaciones-alb → calificaciones-service ×2
        └── /api/catalogo/*      → catalogo-alb → catalogo-service ×2
```

El sufijo `-alb` identifica los balanceadores locales (Nginx) que reparten cada familia de negocio entre sus dos réplicas. El mecanismo está en [Réplicas y balanceo](#réplicas-y-balanceo).

### Componentes

**Entrada**

- `edge-alb` — puerto 80 (host 8081). Única puerta: seguridad, IP real y proxy.
- `frontend-service` — cliente estático, sin build (sin paso de compilación).

**Enrutado**

- `transaction-router` (enrutador central) ×2 — sesión, CSRF (*Cross-Site Request Forgery*: peticiones falsificadas desde otro sitio), identidad interna, métricas y reparto.
- `pedidos-alb`, `pagos-alb`, `calificaciones-alb` y `catalogo-alb` ×1 — Nginx que reparten cada dominio entre sus dos réplicas (puerto 80).

**Negocio**

- `pedidos-service`, `pagos-service`, `calificaciones-service` y `catalogo-service` — 2 réplicas cada uno, puerto 8000.
- `auth-service` ×1 — registro, login, sesiones y rate limiting (*límite de peticiones por cliente*), puerto 8004.

**Observabilidad**

- `telemetry-service` (8001) — ingesta (recepción) y consulta de mediciones y logs (registros de eventos).
- `incident-service` (8002) — umbrales y ciclo de vida de incidentes.
- `views-service` (8003) — vistas agregadas (resúmenes calculados) de lectura, sin BD propia.
- `reports-service` (8005) — reportes de clientes e historial de respuestas.

**Datos**

- `postgres` (5432) — PostgreSQL 18.6, volumen `postgres-data`.

### Réplicas y balanceo

Dos réplicas significan el mismo código en dos contenedores con IP distinta. Sirven para repartir carga, tolerar la caída de una instancia y escalar un servicio sin tocar los demás.

Funciona porque los servicios no guardan estado en memoria: la sesión y los pedidos viven en PostgreSQL, así que cualquier réplica puede atender cualquier petición.

- **Con 2 réplicas:** `transaction-router`, `pedidos-service`, `pagos-service`, `calificaciones-service` y `catalogo-service` (el tráfico de usuario).
- **Con 1 réplica:** `auth-service` (rate limiting en memoria), `telemetry-service`, `incident-service` (evaluador periódico), `views-service` y `reports-service`.
- **Con 1 instancia:** `edge-alb`, `frontend-service`, los cuatro `*-alb` y `postgres`.

El conteo de 22 contenedores:

```text
1 edge-alb + 1 frontend + 2 router + 4 *-alb
+ 8 réplicas de negocio (4 servicios × 2)
+ 5 servicios de apoyo + 1 postgres = 22
```

**Cómo reparte un `*-alb`:** cada archivo `infra/nginx/*.conf` declara un `upstream` (grupo de servidores destino) con el nombre del servicio y `resolve`; Nginx pregunta al DNS (*Domain Name System*: traducción de nombres a IPs) interno de Docker (`127.0.0.11`), recibe las dos IPs y las rota en round-robin (una petición a cada réplica por turno). `max_fails=3` y `fail_timeout=5s` sacan de rotación una réplica que falla; `proxy_next_upstream` reintenta la petición en la otra si hay error, timeout o `502/503/504`.

El router no conoce las réplicas: solo llama a `http://pedidos-alb`, y el balanceador decide. Así se cambia el número de réplicas sin tocar el router.

**Límite local:** `edge-alb` y cada `*-alb` son una sola instancia. Si cae `edge-alb` no hay entrada; si cae un `*-alb`, ese dominio deja de responder aunque sus réplicas estén sanas. Es el punto único de fallo del entorno local.

## Flujos de extremo a extremo

Los tres recorridos comparten el mismo camino; cambian el destino y la lógica.

### Camino común

1. El navegador llama `http://localhost:8081/api/...`; `edge-alb` recibe la petición y conserva el protocolo y la IP originales en `X-Forwarded-Proto` y `X-Forwarded-For`.
2. `edge-alb` resuelve la IP real del cliente (rangos privados del balanceador), añade CSP (*Content Security Policy*: limita el origen de scripts y estilos) y `X-Frame-Options`, y reparte entre las dos réplicas del `transaction-router`.
3. El router valida sesión (cookie `ag_session`) y CSRF (exige `Origin` —sitio de origen— y protocolo del mismo sitio), y autoriza por rol y recurso.
4. El router firma la identidad (`X-ArepaGo-Identity`) con HMAC-SHA256 (*Hash-based Message Authentication Code* con SHA-256: firma con clave secreta) y la reenvía al servicio destino.
5. El servicio verifica firma y expiración (120 s) antes de confiar en el `user_id`/rol, ejecuta la lógica y responde JSON (*JavaScript Object Notation*: formato de texto estructurado).
6. El router registra la observación (servicio, ruta normalizada, estado, latencia) para el muestreo de telemetría.

La identidad interna **no** viaja como cabecera de confianza: un cliente no puede falsificarla sin el secreto. `INTERNAL_AUTH_SECRET_FILE` se monta en router, servicios de negocio, incidents y reports; auth/telemetry/views no lo necesitan.

### Login

1. `POST /api/auth/login` con `{email, password}` → `edge-alb` → router.
2. El router aplica la comprobación CSRF del camino común y reenvía a `auth-service` (1 réplica).
3. `auth-service` verifica la contraseña con `scrypt` (parámetros en Modelo de datos) y crea la sesión: token aleatorio, su SHA-256 y una expiración.
4. El router recibe el token y responde al navegador con `Set-Cookie: ag_session` (cabecera que instala la cookie; atributos en Seguridad y sesiones).
5. En las siguientes peticiones el router lee la cookie, pregunta a `auth-service` por la sesión y, si es válida, firma la identidad del usuario.

### Pedido

1. `POST /api/pedidos/orders` con `items`, `delivery_address` e `Idempotency-Key`.
2. `edge-alb` → router: valida sesión, CSRF y rol (cliente o admin).
3. El router firma la identidad y llama a `pedidos-alb`.
4. `pedidos-alb` reparte entre las dos réplicas de `pedidos-service`.
5. La réplica valida el pedido contra `catalogo-alb` y guarda pedido y líneas en `soc_orders` dentro de una transacción (todo o nada).
6. Crea el pago `pending` a través de `pagos-alb`; si falla, cancela el pedido.
7. Responde JSON con pedido y pago. Un reintento con la misma `Idempotency-Key` no duplica el pedido (ver Modelo de datos).

### Panel SOC

1. El panel pide `/api/views/overview`, `/api/telemetry` o `/api/incidents`.
2. `edge-alb` → router: estas rutas exigen rol `admin`.
3. El router consulta `views-service` (estado por servicio), `telemetry-service` (mediciones y logs) o `incident-service` (incidentes).
4. De forma independiente, el router publica sus mediciones y `telemetry-service` las guarda en `soc_telemetry.measurements`.
5. `incident-service` evalúa umbrales y abre, actualiza o cierra incidentes.
6. `views-service` compone las vistas con datos de `telemetry-service` e `incident-service`.

El detalle de muestreo, umbrales y p95 está en [Observabilidad](#observabilidad-y-detección-de-incidentes).

## Modelo de datos

Un esquema (*schema*: espacio de nombres que agrupa tablas) por módulo, con un pool de conexiones (*conjunto reutilizable de conexiones a la base*) por proceso:

- `soc_auth` → `users`, `sessions` — cuentas (email, `scrypt` —derivación de contraseñas deliberadamente costosa—, rol) y sesiones (hash —huella irreversible— del token, expiración).
- `soc_catalog` → `products` — nombre, descripción, precio, imagen y estado activo.
- `soc_orders` → `orders`, `order_items` — pedido (cliente, dirección, estado, total, ETA —tiempo estimado de entrega—, `idempotency_key` —clave que evita duplicados al reintentar—) y líneas con copia de nombre/precio.
- `soc_payments` → `payments` — pago por pedido: método, estado, monto, quién confirmó y cuándo.
- `soc_ratings` → `ratings` — calificación 1–5 y comentario por pedido entregado.
- `soc_telemetry` → `measurements`, `logs` — muestras de tráfico y logs estructurados.
- `soc_incidents` → `incidents` — incidentes con evidencia inicial, estado y motivo de cierre.
- `soc_reports` → `reports`, `report_events` — reportes de clientes y auditoría de cambios de estado.
- `soc_migrations` → `schema_migrations` — versión aplicada de cada módulo (DDL —sentencias que definen el esquema— transaccional).

Cada servicio aplica sus migraciones versionadas (cambios de esquema reproducibles) al arrancar (`services/migrations.py`): `soc_migrations.schema_migrations` registra la versión por módulo y un lock (*cerradura* de PostgreSQL) evita carreras entre réplicas. La versión 1 reproduce el esquema existente con `IF NOT EXISTS`, así que una base ya poblada se adopta sin recrear tablas.

### Manejo y retención de datos

- **Aislamiento por cliente:** pedidos, pagos, calificaciones y reportes filtran por el `user_id` de la sesión en el servidor; el admin ve el conjunto global.
- **Integridad:** el checkout revalida productos y precios y guarda una copia de nombre/precio en `order_items`, de modo que cambios futuros del catálogo no alteran pedidos históricos. `order_items` se borra en cascada con su pedido.
- **Idempotencia** (repetir la operación no duplica datos): `orders(customer_id, idempotency_key)` es único; reintentar el checkout no duplica pedidos.
- **Contraseñas:** `scrypt` (N=2^14, r=8, p=1, 64 bytes) con sal (valor aleatorio añadido antes de hashear). Nunca se guarda la contraseña en claro.
- **Sesiones:** se guarda solo el SHA-256 (*hash criptográfico*) del token, con expiración (`SESSION_HOURS`, 12 h por defecto). Las expiradas se purgan al iniciar sesión.
- **Logs:** mensajes truncados a 4.000 caracteres y trazas a 16.000; rutas normalizadas (`/orders/{id}`) para no almacenar identificadores ni cadenas de consulta. Los parámetros SQL (*Structured Query Language*: lenguaje de consultas) no se registran.
- **Retención:** logs, mediciones e incidentes resueltos se purgan cada hora según `LOG_RETENTION_DAYS` (30), `MEASUREMENT_RETENTION_DAYS` (30) e `INCIDENT_RETENTION_DAYS` (180). Los incidentes abiertos nunca se purgan; pedidos, pagos, calificaciones y reportes se conservan.
- **Respaldos (backups):** el volumen `postgres-data` conserva todo; para un respaldo lógico usa `pg_dump` (herramienta de respaldo de PostgreSQL).
- **Catálogo inicial:** vacío. Los productos `DEMO · …` quedan inactivos y no se cargan solos; el menú real se publica desde el panel admin. Un producto retirado sigue visible para el admin (marcado como retirado) y puede republicarse sin recrearlo.

## Seguridad y sesiones

- **Cookie:** `ag_session` con `HttpOnly` (JavaScript no puede leerla), `SameSite=Strict` (no se envía desde otro sitio) y `Max-Age` (vigencia) de la sesión. Las respuestas llevan `Cache-Control: no-store` (sin caché) y `nosniff` (no adivinar el tipo de contenido).
- **CSRF:** las rutas con cookie exigen `Origin` del mismo host y esquema; sin `Origin` y con cookie se rechaza (403). Los clientes API (*Application Programming Interface*: contrato de rutas HTTP) sin cookie (Bearer: token en `Authorization`) no se ven afectados.
- **Rate limiting:** login 5/min por IP+correo y 30/min por IP; registro 5/min por IP. Es en memoria y por proceso, así que con una sola réplica de auth es efectivo.
- **Cabeceras:** CSP, `X-Frame-Options: DENY` (impide incrustar la página), `Referrer-Policy` (qué información de referencia se envía), `Permissions-Policy` (qué APIs del navegador se permiten) y `X-Content-Type-Options` (evita adivinar el tipo de contenido).
- **Contenedores:** imagen non-root (sin privilegios) con UID/GID (identificadores de usuario/grupo en Linux) configurables, `cap_drop: ALL` (sin capacidades), `no-new-privileges` (no escalar privilegios) y `server_tokens off` (oculta la versión de Nginx).
- **Secretos:** `db_password`, `admin_password` e `internal_auth_secret` se montan como archivos (`*_FILE`) y están en `.gitignore`.

## Reglas de negocio

Reglas del dominio; el recorrido técnico está en [Flujos de extremo a extremo](#flujos-de-extremo-a-extremo).

1. **Catálogo:** lectura pública paginada (solo productos publicados); altas, cambios, publicación y retiro requieren admin. Retirar es una baja lógica: el producto se oculta al público pero el admin puede volver a publicarlo.
2. **Pedido:** autenticado, con `items`, `delivery_address` y `payment_method: cash_on_delivery`. Estados: `placed → confirmed → preparing → out_for_delivery → delivered`, con cancelación antes del despacho.
3. **Pago:** se crea `pending`; el admin lo marca `paid` solo si el pedido está `delivered`. No hay pasarela de tarjeta.
4. **Calificación:** solo el dueño de un pedido `delivered`, una vez, con 1–5 estrellas.
5. **Reportes:** el cliente crea `web_issue` o `store_complaint` (puede vincular su pedido); el admin responde y cambia estado, con historial en `report_events`.

## Observabilidad y detección de incidentes

### Recolección de métricas

El `transaction-router` mide cada petición que reenvía a un servicio de negocio: servicio, método, ruta normalizada, estado HTTP y duración. Un hilo muestreador publica cada **15 segundos** (`METRICS_SAMPLE_SECONDS`, mínimo 5):

- `request_count` y `error_count` (HTTP ≥ 500, fallos del servidor) desde la muestra anterior,
- latencia **p95** (*percentil 95*: el 95 % de las peticiones tardó eso o menos) de las observaciones de la ventana de 15 s,
- hasta 20 rutas con errores (método, ruta normalizada, conteos y estados),
- pool de la base (`active`/`max`) consultado por cada router a una réplica del servicio; el agregado conserva la peor ocupación.

Cada réplica del router publica su propia medición con `replica` (nombre del contenedor): en un intervalo hay dos filas por servicio y cada una cubre el tráfico que atendió esa réplica. `telemetry-service` agrega la **última muestra de cada réplica**: suma `request_count`/`error_count`, toma el peor p95 y la peor ocupación de pool, y fusiona los diagnósticos. Una réplica que deja de publicar sale del agregado tras `MEASUREMENT_FRESH_SECONDS` (120 s), para no inflar el tráfico con datos viejos.

Las mediciones se guardan en `soc_telemetry.measurements`. `incident-service` consulta el agregado cada `INCIDENT_EVALUATE_SECONDS` (15 s) y aplica los umbrales **una vez por servicio**, no por réplica. `POST /evaluate` sigue disponible para evaluación manual o pruebas.

### Umbrales

Un umbral es el valor a partir del cual se considera que hay degradación.

- **Latencia** — p95 ≥ 1.000 ms (`LATENCY_THRESHOLD_MS`).
- **Errores** — tasa ≥ 20 % con al menos 1 solicitud (`ERROR_RATE_THRESHOLD`).
- **Pool** — ocupación ≥ 75 % del máximo (`POOL_WARNING_RATIO`).

La severidad (gravedad) es `critical` si la latencia llega a 5× el umbral (5.000 ms), la tasa de error alcanza 50 % o el pool está lleno; en cualquier otro caso es `warning`.

### Latencia p95

La **latencia** es cuánto tarda una petición desde que llega hasta que responde. **p95 = el tiempo que tarda el 95 % de las peticiones o menos.** Dicho al revés: solo 1 de cada 20 peticiones fue igual o más lenta.

Si ordenas las peticiones de la ventana de menor a mayor, el p95 deja al 95 % por debajo. El promedio puede esconder una cola de peticiones lentas; el p95 la hace visible.

Ejemplo con 20 peticiones (ms):

```text
10  12  15  18  20  22  25  30  35  40
45  50  60  80 100 120 150 200 300 900
                                  ▲
                          p95 ≈ 300 ms
```

19 de 20 usuarios tuvieron ≤ 300 ms; uno tardó 900 ms. El promedio (~130 ms) escondería esa cola.

Por eso el umbral es **p95 ≥ 1.000 ms**: no reacciona a un pico aislado, sino a que una parte relevante del tráfico ya sea lenta. Se calcula sobre la ventana de 15 s y, entre réplicas, se toma el peor valor.

**p50** es la mediana y **p99** el 1 % más lento. Cuanto más alto el percentil, más sensible a la cola.

### Ciclo de vida del incidente

1. **Apertura:** una medición que supera cualquier umbral crea un incidente `open`. Un índice único parcial (regla de unicidad sobre un subconjunto de filas) garantiza **uno abierto por servicio**; la evaluación usa un lock por servicio para evitar carreras entre réplicas.
2. **Actualización:** cada nueva medición degradada actualiza severidad, causas, `last_seen` y la última medición, y **reinicia** el contador de recuperación.
3. **Recuperación con tráfico:** una medición sin brecha y con solicitudes suma 1 al contador. Al llegar a `INCIDENT_RECOVERY_SAMPLES` (3 por defecto, entre 2 y 10) el incidente pasa a `resolved` con motivo `recovered_with_traffic`.
4. **Ventana sin tráfico:** una muestra con 0 solicitudes no confirma ni niega la recuperación: **no suma ni reinicia** el contador. Así, el tráfico intermitente entre réplicas no impide cerrar un incidente ya recuperado.
5. **Cierre por inactividad:** si no hay nuevas brechas durante `INCIDENT_INACTIVITY_MINUTES` (30 por defecto; 0 lo desactiva), el incidente se cierra con motivo `inactivity_timeout`. Evita incidentes eternos en servicios que dejaron de recibir visitas.
6. **Evidencia:** `initial_reasons` e `initial_measurement` conservan las causas y la medición del momento de apertura, separadas de la última medición. El detalle incluye fragmentos de código del gateway (router de entrada) y del handler (manejador) implicado (`/incidents/{id}/code-context`), extraídos por AST (*Abstract Syntax Tree*: árbol de sintaxis del código) de la imagen actual del servicio de incidentes.

Una muestra degradada reinicia el conteo; por eso se requieren muestras **consecutivas** con tráfico real.

### Qué detecta y qué no

- **Detecta:** degradación del tráfico que atraviesa el gateway (latencia, 5xx —errores del servidor—, pool de la réplica más saturada) y la conserva como incidente auditable.
- **No detecta:** fallos internos sin tráfico, errores solo en logs, saturación de disco/CPU, caída de la base que no llegue a responder al gateway, ni problemas de red del balanceador.
- **No alerta:** no hay paging (avisos a una persona de guardia) ni notificaciones. `/health` y `/metrics` son para sondas (comprobaciones externas) y paneles externos.
- **Healthchecks** (*comprobaciones de salud*): son independientes de los incidentes; Compose (*Docker Compose*: definición de varios contenedores) reinicia contenedores `unhealthy`, pero eso no abre un incidente.

### Logs

Cada servicio escribe JSON a stdout y envía de forma asíncrona a `telemetry-service` los eventos de nivel `warning`, `error` y `critical` (cola de 1.000, sin bloquear la petición). `GET /api/telemetry/logs` (admin) filtra por servicio y rango, hasta 31 días y 200 filas. Docker (plataforma de contenedores) rota `10 MB × 3` por contenedor.

### Verificaciones periódicas

**Cada 15 segundos**

- Muestreo de métricas del router (`METRICS_SAMPLE_SECONDS`).
- Evaluación de umbrales (`INCIDENT_EVALUATE_SECONDS`).
- Healthcheck de contenedores: timeout 5 s, 5–6 reintentos y `start_period` (periodo de gracia inicial) de 20–60 s.
- Refresco del panel.

**Cada hora**

- Purga de logs y mediciones vencidos.
- Purga de incidentes resueltos.

**En cada login**

- Purga de sesiones expiradas.

**Retención y cierres**

- Logs y mediciones: 30 días (`LOG_RETENTION_DAYS`, `MEASUREMENT_RETENTION_DAYS`).
- Incidentes resueltos: 180 días (`INCIDENT_RETENTION_DAYS`); los abiertos nunca se purgan.
- Incidentes sin brechas: se cierran tras 30 min (`INCIDENT_INACTIVITY_MINUTES`).

### Timeouts y límites

- **Borde (edge-alb) → router:** connect 3 s, read 10 s.
- **Router → servicios:** 4 s (`UPSTREAM_TIMEOUT_SECONDS`).
- **SQL:** `statement_timeout` (tiempo máximo por sentencia) 15 s, `lock_timeout` (espera máxima de un lock) 5 s e `idle_in_transaction` (transacción inactiva máxima) 30 s.
- **Pool:** adquisición 10 s, vida máxima 1.800 s, inactividad 300 s.
- **Conexión a BD:** 5 s (`DB_CONNECT_TIMEOUT`).
- **Sesión:** 12 h (`SESSION_HOURS`, 1–168).
- **Identidad interna:** 120 s.

## Ejecución local

Requisitos: Docker con Compose. Crea los secretos locales (mínimo 12 caracteres) si no existen:

```sh
printf 'una-contraseña-local\n' > secrets/db_password.txt
printf 'otra-contraseña-local\n' > secrets/admin_password.txt
chmod 600 secrets/db_password.txt secrets/admin_password.txt
```

`scripts/start_local.sh` genera `secrets/internal_auth_secret.txt` si falta, exporta tu UID/GID (identificadores de usuario/grupo en Linux; permiten que el proceso non-root lea secretos `600`) y levanta el stack:

```sh
./scripts/start_local.sh     # http://localhost:8081
./scripts/stop_local.sh      # conserva postgres-data
```

No uses `docker compose down -v` si quieres conservar pedidos e historial. Admin inicial: `ADMIN_EMAIL` (por defecto `admin@arepago.local`) con la contraseña de `secrets/admin_password.txt`; se aplica solo al crear la cuenta.

Para levantar a mano:

```sh
APP_UID=$(id -u) APP_GID=$(id -g) docker compose --profile local up --build -d
```

## API

API: rutas HTTP disponibles (todas devuelven JSON salvo que se indique).

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
- `POST /api/incidents/simulator/run` — `{"mode":"local_benchmark","rows":100000,"mitigations":["index"]}`: `EXPLAIN ANALYZE` (plan de ejecución real de SQL) sobre tabla temporal; no toca datos de negocio.
- `POST /api/incidents/actions/orders-report` — reporte de pedidos por estado (30 días) con `EXPLAIN`.
- `POST /api/incidents/actions/analyze-orders` — `ANALYZE soc_orders.orders` (actualiza estadísticas de la tabla).
- `POST /api/incidents/actions/index-orders-created-at` — índice concurrente (no bloquea escrituras) por fecha si el diagnóstico lo recomienda.
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

## Pruebas y validación

```sh
python3 -m unittest discover -s tests -v      # 60 pruebas
python3 -m compileall -q services scripts tests
```

La validación local incluye `pip check`, compilación, pruebas, validación de Compose y build de la imagen. El flujo completo se validó contra contenedores locales: catálogo, pedido, entrega, pago, calificación, identidad interna y rate limiting.

Scripts auxiliares (**solo local**):

- `python3 scripts/test_incident_detection.py` — induce una caída real de Pedidos y verifica detección y recuperación; `--verify-only` solo consulta.
- `python3 scripts/smoke_test_customer_flow.py` — prueba de humo (recorrido básico de compra) con datos QA (*Quality Assurance*: pruebas de calidad).
- `python3 scripts/seed_demo_catalog.py` — catálogo demo inactivo para pruebas.

## Pool y capacidad

`DB_POOL_MIN_SIZE`/`DB_POOL_MAX_SIZE` configuran el pool de cada proceso (máximo 10). Compose ejecuta 12 procesos con pool (8 réplicas de negocio + `telemetry-service`, `incident-service`, `auth-service` y `reports-service`): 120 conexiones máximas frente a las 250 de PostgreSQL local.

Las consultas que superan `SLOW_QUERY_MS` (500 ms) generan un log JSON con módulo, operación, duración y huella SHA-256; no se registran valores ni parámetros SQL.
