"""Domain APIs for catalog, orders, payments, and customer ratings."""
import json
import os
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit

from services.common import JsonHandler
from services.db import connection, init_pool, pool_stats
from services.internal_auth import IDENTITY_HEADER, load_secret, verify_identity
from services.migrations import apply_migrations
from services.order_states import ORDER_TRANSITIONS, can_confirm_cash_payment, can_manage_payment, can_transition_order

SERVICE_NAME = os.environ.get("SERVICE_NAME", "servicio").strip().lower()
PORT = int(os.environ.get("PORT", "8000"))
CATALOG_API_URL = os.environ.get("CATALOG_API_URL", "http://127.0.0.1:8104")
PAYMENTS_API_URL = os.environ.get("PAYMENTS_API_URL", "http://127.0.0.1:8102")
ORDERS_API_URL = os.environ.get("ORDERS_API_URL", "http://127.0.0.1:8101")
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "4"))
SCHEMAS = {"catalogo": "soc_catalog", "pedidos": "soc_orders",
           "pagos": "soc_payments", "calificaciones": "soc_ratings"}


def internal_json(url, method="GET", payload=None, headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, {"error": "respuesta interna inválida"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ConnectionError(str(exc)) from exc


def identity(handler):
    return verify_identity(handler.headers.get(IDENTITY_HEADER, ""))


def parse_uuid(value, label="id"):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{label} inválido") from exc


def parse_price(value):
    try:
        price = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("price debe ser un monto válido") from exc
    if not price.is_finite() or price <= 0 or price > Decimal("99999999.99"):
        raise ValueError("price debe ser mayor que cero y estar dentro del límite")
    return price


def _migrations(schema):
    if SERVICE_NAME == "catalogo":
        return [(1, "catalog schema", [
            f"CREATE SCHEMA IF NOT EXISTS {schema}",
            f"""CREATE TABLE IF NOT EXISTS {schema}.products (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name TEXT NOT NULL CHECK (length(name) BETWEEN 2 AND 120),
                description TEXT NOT NULL DEFAULT '',
                price NUMERIC(10,2) NOT NULL CHECK (price > 0),
                image_url TEXT,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""",
            f"CREATE INDEX IF NOT EXISTS products_active_name ON {schema}.products(active,name)",
        ])]
    if SERVICE_NAME == "pedidos":
        return [(1, "orders schema", [
            f"CREATE SCHEMA IF NOT EXISTS {schema}",
            f"""CREATE TABLE IF NOT EXISTS {schema}.orders (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                customer_id UUID NOT NULL,
                idempotency_key TEXT NOT NULL,
                delivery_address TEXT NOT NULL CHECK (length(delivery_address) BETWEEN 10 AND 400),
                status TEXT NOT NULL CHECK (status IN ('placed','confirmed','preparing','out_for_delivery','delivered','cancelled')),
                payment_method TEXT NOT NULL CHECK (payment_method IN ('cash_on_delivery')),
                total NUMERIC(12,2) NOT NULL CHECK (total > 0),
                eta TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""",
            f"""CREATE TABLE IF NOT EXISTS {schema}.order_items (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                order_id UUID NOT NULL REFERENCES {schema}.orders(id) ON DELETE CASCADE,
                product_id UUID NOT NULL,
                product_name TEXT NOT NULL,
                unit_price NUMERIC(10,2) NOT NULL CHECK (unit_price > 0),
                quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 50)
            )""",
            f"CREATE INDEX IF NOT EXISTS orders_customer_created ON {schema}.orders(customer_id,created_at DESC)",
            f"CREATE UNIQUE INDEX IF NOT EXISTS orders_customer_idempotency ON {schema}.orders(customer_id,idempotency_key)",
        ])]
    if SERVICE_NAME == "pagos":
        return [(1, "payments schema", [
            f"CREATE SCHEMA IF NOT EXISTS {schema}",
            f"""CREATE TABLE IF NOT EXISTS {schema}.payments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                order_id UUID NOT NULL UNIQUE,
                customer_id UUID NOT NULL,
                method TEXT NOT NULL CHECK (method IN ('cash_on_delivery')),
                status TEXT NOT NULL CHECK (status IN ('pending','paid','cancelled','failed')),
                amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""",
            f"CREATE INDEX IF NOT EXISTS payments_customer_created ON {schema}.payments(customer_id,created_at DESC)",
            f"ALTER TABLE {schema}.payments ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ",
            f"ALTER TABLE {schema}.payments ADD COLUMN IF NOT EXISTS confirmed_by UUID",
        ])]
    return [(1, "ratings schema", [
        f"CREATE SCHEMA IF NOT EXISTS {schema}",
        f"""CREATE TABLE IF NOT EXISTS {schema}.ratings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            order_id UUID NOT NULL UNIQUE,
            customer_id UUID NOT NULL,
            score SMALLINT NOT NULL CHECK (score BETWEEN 1 AND 5),
            comment TEXT NOT NULL DEFAULT '' CHECK (length(comment) <= 1000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        f"CREATE INDEX IF NOT EXISTS ratings_customer_created ON {schema}.ratings(customer_id,created_at DESC)",
    ])]


def initialize():
    if SERVICE_NAME not in SCHEMAS:
        raise RuntimeError(f"SERVICE_NAME no reconocido: {SERVICE_NAME}")
    schema = SCHEMAS[SERVICE_NAME]
    init_pool(f"arepago-{SERVICE_NAME}")
    with connection() as db:
        apply_migrations(db, SERVICE_NAME, _migrations(schema))


def product_dict(row):
    if row is None:
        return None
    return {"id": str(row["id"]), "name": row["name"], "description": row["description"],
            "price": str(row["price"]), "image_url": row["image_url"], "active": row["active"],
            "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()}


def order_dict(row, items):
    return {"id": str(row["id"]), "status": row["status"], "payment_method": row["payment_method"],
            "delivery_address": row["delivery_address"], "total": str(row["total"]), "eta": row["eta"].isoformat(),
            "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat(),
            "items": [{"product_id": str(item["product_id"]), "name": item["product_name"],
                       "unit_price": str(item["unit_price"]), "quantity": item["quantity"]} for item in items]}


def order_with_items(db, order_id, customer_id=None):
    schema = SCHEMAS["pedidos"]
    if customer_id:
        row = db.execute(f"SELECT * FROM {schema}.orders WHERE id=%s AND customer_id=%s", (order_id, customer_id)).fetchone()
    else:
        row = db.execute(f"SELECT * FROM {schema}.orders WHERE id=%s", (order_id,)).fetchone()
    if row is None:
        return None
    items = db.execute(f"SELECT * FROM {schema}.order_items WHERE order_id=%s ORDER BY id", (order_id,)).fetchall()
    return order_dict(row, items)


class Handler(JsonHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            try:
                with connection() as db:
                    db.execute("SELECT 1")
                self.send_json(200, {"service": SERVICE_NAME, "status": "ok", "database": "connected"})
            except Exception as exc:
                self.log_exception(exc, "health_check_failed")
                self.send_json(503, {"service": SERVICE_NAME, "status": "degraded", "database": "unavailable"})
            return
        if path == "/health/detailed":
            try:
                with connection() as db:
                    db.execute("SELECT 1")
                database_pool = pool_stats()
                self.send_json(200, {"service": SERVICE_NAME, "status": "ok", "database": "connected",
                                     "database_pool": database_pool})
            except Exception as exc:
                self.send_json(503, {"service": SERVICE_NAME, "status": "degraded", "database": "unavailable",
                                     "database_pool": pool_stats(), "diagnostic": type(exc).__name__})
            return
        try:
            if SERVICE_NAME == "catalogo":
                self.catalog_get(path, parsed.query)
            elif SERVICE_NAME == "pedidos":
                self.orders_get(path)
            elif SERVICE_NAME == "pagos":
                self.payments_get(path, parsed.query)
            elif SERVICE_NAME == "calificaciones":
                self.ratings_get(path)
            else:
                self.send_json(404, {"error": "ruta no encontrada"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.log_exception(exc)
            self.send_json(503, {"error": "servicio temporalmente no disponible"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if SERVICE_NAME == "catalogo":
                self.catalog_post(path)
            elif SERVICE_NAME == "pedidos":
                self.orders_post(path)
            elif SERVICE_NAME == "pagos":
                self.payments_post(path)
            elif SERVICE_NAME == "calificaciones":
                self.ratings_post(path)
            else:
                self.send_json(404, {"error": "ruta no encontrada"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.log_exception(exc)
            self.send_json(503, {"error": "servicio temporalmente no disponible"})

    def do_PATCH(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if SERVICE_NAME == "catalogo":
                self.catalog_update(path)
            elif SERVICE_NAME == "pedidos":
                if path.startswith("/orders/") and not path.endswith("/cancel"):
                    self.order_status_update(path)
                else:
                    self.order_cancel(path)
            elif SERVICE_NAME == "pagos":
                if path.startswith("/payments/") and not path.startswith("/internal/"):
                    self.payment_confirm(path)
                else:
                    self.payment_cancel(path)
            else:
                self.send_json(404, {"error": "ruta no encontrada"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.log_exception(exc)
            self.send_json(503, {"error": "servicio temporalmente no disponible"})

    def do_PUT(self):
        try:
            if SERVICE_NAME == "catalogo":
                self.catalog_update(urlparse(self.path).path.rstrip("/"))
            else:
                self.send_json(404, {"error": "ruta no encontrada"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.log_exception(exc)
            self.send_json(503, {"error": "servicio temporalmente no disponible"})

    def do_DELETE(self):
        try:
            if SERVICE_NAME == "catalogo":
                self.catalog_delete(urlparse(self.path).path.rstrip("/"))
            else:
                self.send_json(404, {"error": "ruta no encontrada"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.log_exception(exc)
            self.send_json(503, {"error": "servicio temporalmente no disponible"})

    def require_user(self, roles=("customer", "admin")):
        user = identity(self)
        if user is None:
            self.send_json(401, {"error": "identidad autenticada requerida"})
            return None
        if user["role"] not in roles:
            self.send_json(403, {"error": "rol no autorizado"})
            return None
        return user

    def body(self):
        return self.read_json()

    def identity_headers(self):
        token = self.headers.get(IDENTITY_HEADER, "")
        return {IDENTITY_HEADER: token} if token else {}

    def catalog_get(self, path, query):
        schema = SCHEMAS["catalogo"]
        if path == "/products":
            params = parse_qs(query)
            try:
                limit = min(100, max(1, int(params.get("limit", ["50"])[0])))
                offset = max(0, int(params.get("offset", ["0"])[0]))
            except ValueError as exc:
                raise ValueError("limit y offset deben ser enteros válidos") from exc
            # El catálogo público solo ve productos publicados. El admin puede
            # pedir los retirados para poder volver a publicarlos.
            include_inactive = params.get("include_inactive", ["false"])[0].lower() in {"1", "true", "yes"}
            if include_inactive and self.require_user(("admin",)) is None:
                return
            clause = "" if include_inactive else "WHERE active=TRUE"
            with connection() as db:
                rows = db.execute(f"SELECT * FROM {schema}.products {clause} ORDER BY name LIMIT %s OFFSET %s", (limit, offset)).fetchall()
                total = db.execute(f"SELECT COUNT(*) AS count FROM {schema}.products {clause}").fetchone()["count"]
            self.send_json(200, {"products": [product_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset})
            return
        parts = path.split("/")
        if len(parts) == 3 and parts[1] == "products":
            product_id = parse_uuid(parts[2], "product_id")
            with connection() as db:
                row = db.execute(f"SELECT * FROM {schema}.products WHERE id=%s AND active=TRUE", (product_id,)).fetchone()
            self.send_json(404, {"error": "producto no encontrado"}) if row is None else self.send_json(200, {"product": product_dict(row)})
            return
        self.send_json(404, {"error": "ruta no encontrada"})

    def catalog_post(self, path):
        if path != "/products":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        if self.require_user(("admin",)) is None:
            return
        data = self.body()
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        image_url = data.get("image_url")
        if not 2 <= len(name) <= 120 or len(description) > 2000:
            raise ValueError("name debe tener entre 2 y 120 caracteres; description admite hasta 2000")
        price = parse_price(data.get("price"))
        if image_url is not None and (not isinstance(image_url, str) or len(image_url) > 2048):
            raise ValueError("image_url inválida")
        if image_url is not None:
            parsed_image = urlsplit(image_url)
            if parsed_image.scheme not in {"https", "http"} or not parsed_image.netloc:
                raise ValueError("image_url debe ser una URL http o https")
        schema = SCHEMAS["catalogo"]
        with connection() as db:
            row = db.execute(f"""INSERT INTO {schema}.products(name,description,price,image_url)
                VALUES (%s,%s,%s,%s) RETURNING *""", (name, description, price, image_url)).fetchone()
        self.send_json(201, {"product": product_dict(row)})

    def catalog_update(self, path):
        parts = path.split("/")
        if len(parts) != 3 or parts[1] != "products":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        if self.require_user(("admin",)) is None:
            return
        product_id = parse_uuid(parts[2], "product_id")
        data = self.body()
        fields = {}
        if "name" in data:
            name = str(data["name"]).strip()
            if not 2 <= len(name) <= 120:
                raise ValueError("name debe tener entre 2 y 120 caracteres")
            fields["name"] = name
        if "description" in data:
            description = str(data["description"]).strip()
            if len(description) > 2000:
                raise ValueError("description admite hasta 2000 caracteres")
            fields["description"] = description
        if "price" in data:
            fields["price"] = parse_price(data["price"])
        if "image_url" in data:
            image_url = data["image_url"]
            if image_url is not None and (not isinstance(image_url, str) or len(image_url) > 2048):
                raise ValueError("image_url inválida")
            if image_url is not None:
                parsed_image = urlsplit(image_url)
                if parsed_image.scheme not in {"https", "http"} or not parsed_image.netloc:
                    raise ValueError("image_url debe ser una URL http o https")
            fields["image_url"] = image_url
        if "active" in data:
            active = data["active"]
            if not isinstance(active, bool):
                raise ValueError("active debe ser true o false")
            fields["active"] = active
        if not fields:
            raise ValueError("envía al menos un campo modificable")
        assignments = ",".join(f"{key}=%s" for key in fields)
        schema = SCHEMAS["catalogo"]
        with connection() as db:
            # Se permite editar productos retirados para poder republicarlos.
            row = db.execute(f"UPDATE {schema}.products SET {assignments},updated_at=now() WHERE id=%s RETURNING *",
                              (*fields.values(), product_id)).fetchone()
        self.send_json(404, {"error": "producto no encontrado"}) if row is None else self.send_json(200, {"product": product_dict(row)})

    def catalog_delete(self, path):
        parts = path.split("/")
        if len(parts) != 3 or parts[1] != "products":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        if self.require_user(("admin",)) is None:
            return
        product_id = parse_uuid(parts[2], "product_id")
        schema = SCHEMAS["catalogo"]
        with connection() as db:
            row = db.execute(f"UPDATE {schema}.products SET active=FALSE,updated_at=now() WHERE id=%s AND active=TRUE RETURNING id", (product_id,)).fetchone()
        self.send_json(404, {"error": "producto no encontrado"}) if row is None else self.send_json(200, {"ok": True})

    def catalog_product(self, product_id):
        code, payload = internal_json(CATALOG_API_URL + f"/products/{product_id}")
        if code == 404:
            raise ValueError("uno de los productos ya no está disponible")
        if code != 200:
            raise ConnectionError("Catálogo no disponible")
        return payload["product"]

    def orders_post(self, path):
        if path != "/orders":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        user = self.require_user(("customer",))
        if user is None:
            return
        data = self.body()
        if data.get("payment_method", "cash_on_delivery") != "cash_on_delivery":
            raise ValueError("El método disponible es efectivo contra entrega")
        address = str(data.get("delivery_address", "")).strip()
        if not 10 <= len(address) <= 400:
            raise ValueError("delivery_address debe tener entre 10 y 400 caracteres")
        idempotency_key = self.headers.get("Idempotency-Key", "").strip()
        if not 8 <= len(idempotency_key) <= 128:
            raise ValueError("Idempotency-Key es obligatorio y debe tener entre 8 y 128 caracteres")
        items = data.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            raise ValueError("el pedido debe incluir entre 1 y 30 productos")
        quantities = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("cada producto debe incluir product_id y quantity")
            product_id = str(parse_uuid(item.get("product_id"), "product_id"))
            quantity = item.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 50:
                raise ValueError("quantity debe ser un entero entre 1 y 50")
            quantities[product_id] = quantities.get(product_id, 0) + quantity
        if any(quantity > 50 for quantity in quantities.values()):
            raise ValueError("la cantidad por producto no puede superar 50")
        order_items = []
        total = Decimal("0.00")
        for product_id, quantity in quantities.items():
            product = self.catalog_product(product_id)
            price = parse_price(product["price"])
            order_items.append({"product_id": product_id, "name": product["name"], "unit_price": price, "quantity": quantity})
            total += price * quantity
        if total <= 0 or total > Decimal("99999999.99"):
            raise ValueError("total del pedido fuera de rango")
        schema = SCHEMAS["pedidos"]
        eta = datetime.now(timezone.utc) + timedelta(minutes=45)
        with connection() as db:
            order = db.execute(f"""INSERT INTO {schema}.orders(customer_id,idempotency_key,delivery_address,status,payment_method,total,eta)
                VALUES (%s,%s,%s,'placed','cash_on_delivery',%s,%s)
                ON CONFLICT(customer_id,idempotency_key) DO NOTHING RETURNING *""",
                               (user["id"], idempotency_key, address, total, eta)).fetchone()
            created = order is not None
            if order is None:
                order = db.execute(f"SELECT * FROM {schema}.orders WHERE customer_id=%s AND idempotency_key=%s",
                                   (user["id"], idempotency_key)).fetchone()
            else:
                for item in order_items:
                    db.execute(f"""INSERT INTO {schema}.order_items(order_id,product_id,product_name,unit_price,quantity)
                        VALUES (%s,%s,%s,%s,%s)""", (order["id"], item["product_id"], item["name"], item["unit_price"], item["quantity"]))
            public_order = order_with_items(db, order["id"], user["id"])
        if not created and public_order["status"] == "cancelled":
            self.send_json(409, {"error": "este intento de pedido ya fue cancelado; inicia un nuevo checkout"})
            return
        payment_payload = {"order_id": public_order["id"], "customer_id": user["id"], "amount": public_order["total"], "method": "cash_on_delivery"}
        try:
            status, payment_result = internal_json(PAYMENTS_API_URL + "/internal/payments", "POST",
                                                   payment_payload, self.identity_headers())
            if status >= 400:
                raise ConnectionError("No se pudo registrar el pago pendiente")
        except ConnectionError:
            with connection() as db:
                db.execute(f"UPDATE {schema}.orders SET status='cancelled',updated_at=now() WHERE id=%s", (order["id"],))
            try:
                internal_json(PAYMENTS_API_URL + f"/internal/payments/{order['id']}/cancel", "PATCH", {},
                              self.identity_headers())
            except ConnectionError:
                pass
            self.send_json(503, {"error": "No se pudo registrar el pago pendiente; el pedido fue cancelado"})
            return
        self.send_json(201 if created else 200, {"order": public_order, "payment": payment_result["payment"]})

    def orders_get(self, path):
        user = self.require_user()
        if user is None:
            return
        schema = SCHEMAS["pedidos"]
        parts = path.split("/")
        if path == "/internal/orders" or path.startswith("/internal/orders/"):
            if user["role"] != "admin":
                self.send_json(404, {"error": "ruta no encontrada"})
                return
            order_id = parse_uuid(parts[-1], "order_id")
            with connection() as db:
                order = order_with_items(db, order_id)
                if order is not None:
                    owner = db.execute(f"SELECT customer_id FROM {schema}.orders WHERE id=%s", (order_id,)).fetchone()
                    order["customer_id"] = str(owner["customer_id"])
            self.send_json(404, {"error": "pedido no encontrado"}) if order is None else self.send_json(200, {"order": order})
            return
        if path == "/orders":
            with connection() as db:
                if user["role"] == "admin":
                    rows = db.execute(f"SELECT * FROM {schema}.orders ORDER BY created_at DESC LIMIT 500").fetchall()
                else:
                    rows = db.execute(f"SELECT * FROM {schema}.orders WHERE customer_id=%s ORDER BY created_at DESC LIMIT 100", (user["id"],)).fetchall()
                orders = [order_with_items(db, row["id"], None if user["role"] == "admin" else user["id"]) for row in rows]
                if user["role"] == "admin":
                    for order in orders:
                        owner = db.execute(f"SELECT customer_id FROM {schema}.orders WHERE id=%s", (order["id"],)).fetchone()
                        order["customer_id"] = str(owner["customer_id"])
            self.send_json(200, {"orders": orders})
            return
        if len(parts) == 3 and parts[1] == "orders":
            order_id = parse_uuid(parts[2], "order_id")
            with connection() as db:
                order = order_with_items(db, order_id, None if user["role"] == "admin" else user["id"])
                if order is not None and user["role"] == "admin":
                    owner = db.execute(f"SELECT customer_id FROM {schema}.orders WHERE id=%s", (order_id,)).fetchone()
                    order["customer_id"] = str(owner["customer_id"])
            self.send_json(404, {"error": "pedido no encontrado"}) if order is None else self.send_json(200, {"order": order})
            return
        self.send_json(404, {"error": "ruta no encontrada"})

    def order_cancel(self, path):
        parts = path.split("/")
        if len(parts) != 4 or parts[1] != "orders" or parts[3] != "cancel":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        user = self.require_user(("customer",))
        if user is None:
            return
        order_id = parse_uuid(parts[2], "order_id")
        schema = SCHEMAS["pedidos"]
        with connection() as db:
            row = db.execute(f"""UPDATE {schema}.orders SET status='cancelled',updated_at=now()
                WHERE id=%s AND customer_id=%s AND status='placed' RETURNING *""", (order_id, user["id"])).fetchone()
        if row is None:
            with connection() as db:
                row = db.execute(f"SELECT id,status,updated_at FROM {schema}.orders WHERE id=%s AND customer_id=%s",
                                  (order_id, user["id"])).fetchone()
            if row is None or row["status"] != "cancelled":
                self.send_json(409, {"error": "el pedido ya no se puede cancelar"})
                return
        try:
            code, _ = internal_json(PAYMENTS_API_URL + f"/internal/payments/{order_id}/cancel", "PATCH", {},
                                    self.identity_headers())
            if code >= 400:
                raise ConnectionError("no se pudo actualizar el pago")
        except ConnectionError:
            self.send_json(503, {"error": "pedido cancelado; no se confirmó la actualización del pago pendiente"})
            return
        self.send_json(200, {"order": {"id": str(row["id"]), "status": row["status"], "updated_at": row["updated_at"].isoformat()}})

    def order_status_update(self, path):
        user = self.require_user(("admin",))
        if user is None:
            return
        parts = path.split("/")
        if len(parts) != 3 or parts[1] != "orders":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        order_id = parse_uuid(parts[2], "order_id")
        next_status = self.body().get("status")
        if not isinstance(next_status, str) or next_status not in {status for states in ORDER_TRANSITIONS.values() for status in states}:
            raise ValueError("estado de pedido no válido")
        schema = SCHEMAS["pedidos"]
        with connection() as db:
            row = db.execute(f"SELECT * FROM {schema}.orders WHERE id=%s FOR UPDATE", (order_id,)).fetchone()
            if row is None:
                self.send_json(404, {"error": "pedido no encontrado"})
                return
            if not can_transition_order(row["status"], next_status):
                self.send_json(409, {"error": f"transición no permitida: {row['status']} → {next_status}"})
                return
            eta = datetime.now(timezone.utc) + timedelta(minutes=20) if next_status == "out_for_delivery" else row["eta"]
            if next_status == "delivered":
                eta = datetime.now(timezone.utc)
            updated = db.execute(f"UPDATE {schema}.orders SET status=%s,eta=%s,updated_at=now() WHERE id=%s RETURNING *",
                                 (next_status, eta, order_id)).fetchone()
            result = order_with_items(db, order_id)
            customer_id = str(updated["customer_id"])
        if next_status == "cancelled":
            code, _ = internal_json(PAYMENTS_API_URL + f"/internal/payments/{order_id}/cancel", "PATCH", {},
                                    self.identity_headers())
            if code >= 500:
                self.send_json(503, {"error": "pedido cancelado; no se pudo confirmar la cancelación del pago"})
                return
        result["customer_id"] = customer_id
        self.send_json(200, {"order": result})

    def payments_post(self, path):
        if path != "/internal/payments":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        user = self.require_user(("customer", "admin"))
        if user is None:
            return
        data = self.body()
        order_id = parse_uuid(data.get("order_id"), "order_id")
        customer_id = parse_uuid(data.get("customer_id"), "customer_id")
        if not can_manage_payment(user["role"], user["id"], customer_id):
            self.send_json(403, {"error": "no puedes registrar pagos de otro cliente"})
            return
        amount = parse_price(data.get("amount"))
        method = data.get("method")
        if method != "cash_on_delivery":
            raise ValueError("método de pago no disponible")
        code, order_result = internal_json(ORDERS_API_URL + f"/orders/{order_id}",
                                           headers=self.identity_headers())
        if code != 200:
            self.send_json(503, {"error": "no se pudo verificar el pedido"})
            return
        order = order_result["order"]
        if user["role"] == "admin" and order.get("customer_id") != customer_id:
            self.send_json(403, {"error": "el pedido no pertenece al cliente"})
            return
        if parse_price(order["total"]) != amount:
            raise ValueError("el monto no coincide con el total del pedido")
        schema = SCHEMAS["pagos"]
        with connection() as db:
            row = db.execute(f"""INSERT INTO {schema}.payments(order_id,customer_id,method,status,amount)
                VALUES (%s,%s,%s,'pending',%s) ON CONFLICT(order_id) DO NOTHING RETURNING *""",
                             (order_id, customer_id, method, amount)).fetchone()
            if row is None:
                row = db.execute(f"SELECT * FROM {schema}.payments WHERE order_id=%s", (order_id,)).fetchone()
                if row["customer_id"] != customer_id or row["amount"] != amount or row["method"] != method:
                    raise ValueError("el pedido ya tiene un registro de pago distinto")
        self.send_json(201, {"payment": self.payment_dict(row)})

    def payment_cancel(self, path):
        parts = path.split("/")
        if len(parts) != 5 or parts[1:3] != ["internal", "payments"] or parts[4] != "cancel":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        user = self.require_user(("customer", "admin"))
        if user is None:
            return
        order_id = parse_uuid(parts[3], "order_id")
        schema = SCHEMAS["pagos"]
        with connection() as db:
            existing = db.execute(f"SELECT * FROM {schema}.payments WHERE order_id=%s", (order_id,)).fetchone()
            if existing is None or not can_manage_payment(user["role"], user["id"], existing["customer_id"]):
                self.send_json(404, {"error": "pago no encontrado"})
                return
            row = db.execute(f"""UPDATE {schema}.payments SET status='cancelled',updated_at=now()
                WHERE order_id=%s AND status='pending' RETURNING *""", (order_id,)).fetchone()
            if row is None:
                row = existing
        self.send_json(200, {"payment": self.payment_dict(row)})

    def payment_confirm(self, path):
        user = self.require_user(("admin",))
        if user is None:
            return
        parts = path.split("/")
        if len(parts) != 3 or parts[1] != "payments":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        payment_id = parse_uuid(parts[2], "payment_id")
        if self.body().get("status") != "paid":
            raise ValueError("la única actualización disponible es confirmar efectivo recibido con status=paid")
        schema = SCHEMAS["pagos"]
        with connection() as db:
            payment = db.execute(f"SELECT * FROM {schema}.payments WHERE id=%s", (payment_id,)).fetchone()
        if payment is None:
            self.send_json(404, {"error": "pago no encontrado"})
            return
        if payment["method"] != "cash_on_delivery":
            self.send_json(409, {"error": "método de pago no compatible con confirmación manual"})
            return
        code, result = internal_json(ORDERS_API_URL + f"/orders/{payment['order_id']}",
                                     headers=self.identity_headers())
        if code != 200:
            self.send_json(503, {"error": "no se pudo verificar el estado del pedido"})
            return
        if not can_confirm_cash_payment(payment["method"], payment["status"], result["order"]["status"]):
            self.send_json(409, {"error": "solo se puede confirmar el efectivo de un pedido entregado"})
            return
        with connection() as db:
            row = db.execute(f"""UPDATE {schema}.payments SET status='paid',paid_at=now(),confirmed_by=%s,updated_at=now()
                WHERE id=%s AND status='pending' RETURNING *""", (user["id"], payment_id)).fetchone()
            if row is None:
                row = db.execute(f"SELECT * FROM {schema}.payments WHERE id=%s", (payment_id,)).fetchone()
        if row["status"] != "paid":
            self.send_json(409, {"error": "solo se pueden confirmar pagos pendientes"})
            return
        self.send_json(200, {"payment": self.payment_dict(row)})

    @staticmethod
    def payment_dict(row):
        return {"id": str(row["id"]), "order_id": str(row["order_id"]), "method": row["method"],
                "status": row["status"], "amount": str(row["amount"]),
                "paid_at": row["paid_at"].isoformat() if row.get("paid_at") else None,
                "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()}

    def payments_get(self, path, query):
        user = self.require_user()
        if user is None:
            return
        schema = SCHEMAS["pagos"]
        parts = path.split("/")
        if path == "/payments":
            params = parse_qs(query)
            order_id = params.get("order_id", [None])[0]
            with connection() as db:
                if order_id:
                    parsed_id = parse_uuid(order_id, "order_id")
                    if user["role"] == "admin":
                        rows = db.execute(f"SELECT * FROM {schema}.payments WHERE order_id=%s ORDER BY created_at DESC", (parsed_id,)).fetchall()
                    else:
                        rows = db.execute(f"SELECT * FROM {schema}.payments WHERE order_id=%s AND customer_id=%s ORDER BY created_at DESC", (parsed_id, user["id"])).fetchall()
                elif user["role"] == "admin":
                    rows = db.execute(f"SELECT * FROM {schema}.payments ORDER BY created_at DESC LIMIT 500").fetchall()
                else:
                    rows = db.execute(f"SELECT * FROM {schema}.payments WHERE customer_id=%s ORDER BY created_at DESC LIMIT 100", (user["id"],)).fetchall()
            self.send_json(200, {"payments": [self.payment_dict(row) for row in rows]})
            return
        if len(parts) == 3 and parts[1] == "payments":
            payment_id = parse_uuid(parts[2], "payment_id")
            with connection() as db:
                if user["role"] == "admin":
                    row = db.execute(f"SELECT * FROM {schema}.payments WHERE id=%s", (payment_id,)).fetchone()
                else:
                    row = db.execute(f"SELECT * FROM {schema}.payments WHERE id=%s AND customer_id=%s", (payment_id, user["id"])).fetchone()
            self.send_json(404, {"error": "pago no encontrado"}) if row is None else self.send_json(200, {"payment": self.payment_dict(row)})
            return
        self.send_json(404, {"error": "ruta no encontrada"})

    def ratings_post(self, path):
        if path != "/ratings":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        user = self.require_user(("customer",))
        if user is None:
            return
        data = self.body()
        order_id = parse_uuid(data.get("order_id"), "order_id")
        score = data.get("score")
        comment = str(data.get("comment", "")).strip()
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise ValueError("score debe ser un entero entre 1 y 5")
        if len(comment) > 1000:
            raise ValueError("comment admite hasta 1000 caracteres")
        code, result = internal_json(ORDERS_API_URL + f"/orders/{order_id}", headers=self.identity_headers())
        if code != 200:
            self.send_json(400, {"error": "no se pudo verificar el pedido"})
            return
        order = result["order"]
        # /orders/{id} already filters by customer_id for non-admin identities.
        if order["status"] != "delivered":
            self.send_json(403, {"error": "solo puedes calificar tus pedidos entregados"})
            return
        schema = SCHEMAS["calificaciones"]
        with connection() as db:
            row = db.execute(f"""INSERT INTO {schema}.ratings(order_id,customer_id,score,comment)
                VALUES (%s,%s,%s,%s) ON CONFLICT(order_id) DO NOTHING RETURNING *""",
                             (order_id, user["id"], score, comment)).fetchone()
        if row is None:
            self.send_json(409, {"error": "este pedido ya fue calificado"})
            return
        self.send_json(201, {"rating": self.rating_dict(row)})

    @staticmethod
    def rating_dict(row):
        return {"id": str(row["id"]), "order_id": str(row["order_id"]), "score": row["score"],
                "comment": row["comment"], "created_at": row["created_at"].isoformat()}

    def ratings_get(self, path):
        user = self.require_user()
        if user is None:
            return
        if path != "/ratings":
            self.send_json(404, {"error": "ruta no encontrada"})
            return
        schema = SCHEMAS["calificaciones"]
        with connection() as db:
            if user["role"] == "admin":
                rows = db.execute(f"SELECT * FROM {schema}.ratings ORDER BY created_at DESC LIMIT 500").fetchall()
            else:
                rows = db.execute(f"SELECT * FROM {schema}.ratings WHERE customer_id=%s ORDER BY created_at DESC LIMIT 100", (user["id"],)).fetchall()
        self.send_json(200, {"ratings": [self.rating_dict(row) for row in rows]})


def main():
    load_secret()
    initialize()
    print(f"{SERVICE_NAME} service listening on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
