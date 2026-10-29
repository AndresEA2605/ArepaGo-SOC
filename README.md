# ArepaGo SOC · microservicios con PostgreSQL

Aplicación de observabilidad para ArepaGo con cliente visual y backend de microservicios. El cliente muestra resumen, servicios e incidentes, y consume los endpoints agregados de Views Service. La ingesta y el ciclo de incidentes persisten en PostgreSQL; cada servicio conserva su esquema lógico propio dentro de la base.

## Topología

```text
Navegador
  ├→ ALB de entrada → cliente visual
  └→ ALB de entrada → enrutador de transacciones
                         ├→ ALB Pedidos → servicio ECS Pedidos
                         ├→ ALB Pagos → servicio ECS Pagos
                         ├→ ALB Calificaciones → servicio ECS Calificaciones
                         ├→ ALB Catálogo → servicio ECS Catálogo
                         ├→ Telemetry / Incidents → PostgreSQL
                         └→ Views Service → consultas agregadas
```

En Docker, los ALB se representan localmente con Nginx. La configuración usa resolución dinámica de destinos y los servicios de negocio y el enrutador arrancan con dos réplicas. En AWS, esos contenedores Nginx se reemplazarán por Application Load Balancers y ECS.

`Telemetry Service` crea el esquema `soc_telemetry`; `Incident Service`, `soc_incidents`. Usan pools PostgreSQL independientes, configurables por servicio, en una instancia PostgreSQL común. `Views Service` compone respuestas de lectura a través de las APIs internas de Telemetry e Incidents; no consulta sus tablas directamente. Los servicios de negocio siguen como scaffolds con respuesta `501`; las operaciones reales vendrán después.

## Conexión local

El Compose local usa PostgreSQL 18.6. La contraseña local se lee desde `secrets/db_password.txt`, archivo ignorado por Git. En un clon nuevo créalo copiando `secrets/db_password.txt.example` y cambia el valor; usa solo credenciales no productivas en local.

```sh
docker compose --profile local up --build
```

El cliente visual queda en `http://localhost:8081`; las APIs se mantienen en la misma dirección bajo `/api`. La base local conserva datos en el volumen `postgres-data`. Para reiniciarla conservando datos usa `docker compose --profile local down` y vuelve a levantarla.

## Preparar conexión a Amazon RDS PostgreSQL

No se ha conectado a una instancia real: faltan endpoint, base, usuario, contraseña y acceso de red. Cuando exista la instancia:

1. Copia `.env.example` a `.env`; configura `DB_HOST` con el endpoint RDS, `DB_NAME`, `DB_USER` y `DB_PORT=5432`.
2. Escribe la contraseña del usuario de aplicación en `secrets/db_password.txt`.
3. Descarga el bundle CA vigente de AWS en `secrets/global-bundle.pem`:

   ```sh
   curl -fsS https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -o secrets/global-bundle.pem
   ```

4. Configura el grupo de seguridad de RDS para aceptar PostgreSQL desde los servicios de aplicación, y otorga al usuario de aplicación permisos para crear/usar sus esquemas.
5. Inicia con el override TLS:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.rds.yml up --build
   ```

El override usa TLS `verify-full` con el endpoint y CA de RDS. Los servicios se niegan a iniciar en ese modo si no reciben el certificado raíz configurado. No guardes la contraseña en `.env`, el código ni Git. En AWS, sustituiremos el archivo local por AWS Secrets Manager/Secrets Manager integration de ECS.

## API

- `GET /api/views/overview`: resumen de telemetría, estado e incidentes por servicio.
- `GET /api/views/services`: estado y última medición de Pedidos, Pagos, Calificaciones y Catálogo.
- `GET /api/views/incidents?status=open`: vista de incidentes (`open`, `resolved` o `all`).
- `POST /api/telemetry`: guarda una medición y evalúa umbrales de latencia, fallos y pool.
- `GET /api/telemetry?service=pedidos&limit=100`
- `GET /api/telemetry/summary`
- `GET /api/incidents?status=open` (también `resolved` o `all`)
- `GET /api/incidents/{id}`
- `GET /health`: revisa el enrutador, los servicios y su conexión a PostgreSQL.
- `/api/pedidos`, `/api/pagos`, `/api/calificaciones`, `/api/catalogo`: pasan por sus balanceadores, pero la lógica de negocio aún no está implementada.

Ejemplo de medición:

```sh
curl -X POST http://localhost:8081/api/telemetry \
  -H 'Content-Type: application/json' \
  -d '{"service":"pedidos","latency_p95_ms":9800,"request_count":600,"error_count":384,"db_pool_active":20,"db_pool_max":20,"source":"arepa-go-api"}'
```

## Pool y capacidad de RDS

`DB_POOL_MIN_SIZE` y `DB_POOL_MAX_SIZE` configuran el pool de cada proceso; el máximo inicial es 5. La capacidad total consumible es el máximo por servicio multiplicado por el número de réplicas. Antes de escalar ECS, hay que fijar un presupuesto conjunto de conexiones que deje margen para administración y otros clientes de RDS.

## Próximas etapas

1. Implementar contratos y reglas de negocio de Pedidos, Pagos, Calificaciones y Catálogo.
2. Instrumentar ALB/ECS/RDS con OpenTelemetry y correlación de trazas.
3. Aprovisionar RDS con red privada, backups, TLS y credenciales administradas.
4. Afinar umbrales, alertas y reportes auditables con mediciones reales.
