# ArepaGo SOC · primera parte

Prototipo navegable de un centro de observabilidad para ArepaGo, construido a partir del informe y de los anexos B y C entregados en `Descargas/SOC`.

## Objetivo del producto

Reunir en una sola vista la salud de los servicios de Pedidos, Pagos, Calificaciones y Catálogo, y correlacionar fallos y latencia con procesos internos (en este caso, el reporte automático de ventas). La prioridad del producto es medir y explicar la telemetría que permita detectar una afectación antes de que escale y facilitar su seguimiento ante el ente de control.

## Qué incluye esta primera entrega

- Resumen del incidente del 27 de septiembre de 2026.
- Indicadores de latencia p95, operaciones fallidas, conexiones ocupadas y duración.
- Estado por servicio y comparación entre Pedidos/Pagos y Calificaciones/Catálogo.
- Línea de tiempo de eventos y ejecución del reporte automático.
- Vista adaptable a móvil y acción de imprimir/guardar como PDF.

## Datos y límites

Las cifras visibles son las consignadas en el informe: p95 máximo de 9,8 s para Pedidos y 8,3 s para Pagos; fallos de 64% y 59%; pool de hasta 20/20. Calificaciones y Catálogo se mantuvieron aproximadamente en 0,05–0,06 s y sin fallos. La serie de la gráfica es una reconstrucción ilustrativa para los diez momentos mencionados en el Anexo C, no una exportación de registros originales. El incidente está descrito para el 27 de septiembre de 2026; duración reportada de afectación: 8:03–8:22.

## Abrir

Abre `index.html` en un navegador. Es un prototipo estático: en la siguiente parte se puede conectar a una fuente real de métricas, definir el esquema de eventos y configurar umbrales/alertas.

## Telemetría que debe recibir la app

Cada punto de medición debería incluir `timestamp`, `service`, `latency_p95_ms`, `request_count`, `error_count`, `db_pool_active`, `db_pool_max` y `source`/`environment`. Los eventos operativos deberían incluir inicio y fin, proceso relacionado, servicio afectado y severidad. No se debe enviar información personal de clientes para medir estas señales.
