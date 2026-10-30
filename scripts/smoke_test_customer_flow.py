#!/usr/bin/env python3
"""End-to-end local smoke test: catalog -> order -> delivery -> payment -> rating."""
import http.cookiejar
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


BASE_URL = "http://localhost:8081"
ADMIN_EMAIL = "admin@arepago.local"
CUSTOMER_EMAIL = "cliente.prueba@arepago.local"
ROOT = Path(__file__).resolve().parents[1]


class Api:
    def __init__(self):
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, method="GET", payload=None, extra_headers=None, expected=(200,)):
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = BASE_URL
        headers.update(extra_headers or {})
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(BASE_URL + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                status, raw = response.status, response.read()
        except HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"{method} {path}: no se pudo conectar a {BASE_URL}: {exc}") from exc
        try:
            result = json.loads(raw.decode()) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            result = {"response": raw.decode(errors="replace")}
        if status not in expected:
            raise RuntimeError(f"{method} {path}: HTTP {status}: {result}")
        print(f"OK {method} {path} -> {status}")
        return result


def login_or_register_customer(api, password):
    result = api.call("/api/auth/login", "POST",
                      {"email": CUSTOMER_EMAIL, "password": password}, expected=(200, 401))
    if "user" not in result:
        api.call("/api/auth/register", "POST",
                 {"email": CUSTOMER_EMAIL, "password": password}, expected=(201, 409))
        result = api.call("/api/auth/login", "POST",
                          {"email": CUSTOMER_EMAIL, "password": password})
    if result["user"]["role"] != "customer":
        raise RuntimeError(f"{CUSTOMER_EMAIL} no tiene rol customer")


def main():
    admin_password = (ROOT / "secrets/admin_password.txt").read_text().strip()
    customer_password = (ROOT / "secrets/test_customer_password.txt").read_text().strip()
    admin, customer = Api(), Api()
    product_id = order_id = None
    customer_logged_in = False
    stamp = time.strftime("%Y%m%d-%H%M%S")
    product_name = f"QA Arepa de prueba {stamp}"
    try:
        health = admin.call("/health")
        if health.get("status") != "ok":
            raise RuntimeError(f"servicios no saludables: {health}")
        admin.call("/api/auth/login", "POST", {"email": ADMIN_EMAIL, "password": admin_password})
        product = admin.call("/api/catalogo/products", "POST", {
            "name": product_name,
            "description": "Producto QA automatizado. No preparar ni despachar.",
            "price": "25000.00",
            "image_url": None,
        }, expected=(201,))["product"]
        product_id = product["id"]

        listed = admin.call("/api/catalogo/products?limit=100")
        if not any(item["id"] == product_id for item in listed.get("products", [])):
            raise RuntimeError("el producto creado no aparece en el catálogo activo")
        detailed = customer.call(f"/api/catalogo/products/{product_id}")["product"]
        if detailed["name"] != product_name:
            raise RuntimeError("el detalle de catálogo no coincide con el producto creado")

        login_or_register_customer(customer, customer_password)
        customer_logged_in = True
        order_result = customer.call("/api/pedidos/orders", "POST", {
            "items": [{"product_id": product_id, "quantity": 1}],
            "delivery_address": "PRUEBA QA ArepaGo, no despachar",
            "payment_method": "cash_on_delivery",
        }, extra_headers={"Idempotency-Key": f"qa-flow-{stamp}-{int(time.time())}"}, expected=(201,))
        order, payment = order_result["order"], order_result["payment"]
        order_id = order["id"]
        if order["items"][0]["name"] != product_name or str(order["total"]) != "25000.00":
            raise RuntimeError(f"pedido no conserva producto/precio esperado: {order}")
        if payment["status"] != "pending" or payment["order_id"] != order_id:
            raise RuntimeError(f"pago pendiente no vinculado al pedido: {payment}")

        for status in ("confirmed", "preparing", "out_for_delivery", "delivered"):
            admin.call(f"/api/pedidos/orders/{order_id}", "PATCH", {"status": status})
        order_check = customer.call(f"/api/pedidos/orders/{order_id}")["order"]
        if order_check["status"] != "delivered":
            raise RuntimeError(f"estado final inesperado: {order_check['status']}")

        payments = customer.call(f"/api/pagos/payments?order_id={order_id}")["payments"]
        payment = next((row for row in payments if row["order_id"] == order_id), None)
        if not payment or payment["status"] != "pending":
            raise RuntimeError(f"no se encuentra el pago pendiente del pedido: {payments}")
        paid = admin.call(f"/api/pagos/payments/{payment['id']}", "PATCH", {"status": "paid"})["payment"]
        if paid["status"] != "paid":
            raise RuntimeError(f"pago no quedó confirmado: {paid}")
        rating = customer.call("/api/calificaciones/ratings", "POST", {
            "order_id": order_id,
            "score": 5,
            "comment": "Calificación QA automatizada; compra de prueba.",
        }, expected=(201,))["rating"]
        history = customer.call("/api/pedidos/orders")["orders"]
        if not any(row["id"] == order_id and row["status"] == "delivered" for row in history):
            raise RuntimeError("pedido entregado no aparece en el historial del cliente")

        print("\nFLUJO QA COMPLETO")
        print(json.dumps({"product_id": product_id, "order_id": order_id,
                          "payment_id": paid["id"], "payment_status": paid["status"],
                          "rating_id": rating["id"], "rating_score": rating["score"],
                          "order_status": order_check["status"],
                          "retained_test_sale": True}, ensure_ascii=False, indent=2))
        print("La venta de prueba queda en el historial con dirección y comentario QA; el producto se ocultará del catálogo.")
    finally:
        if order_id and customer_logged_in:
            try:
                current = customer.call(f"/api/pedidos/orders/{order_id}")["order"]
                if current["status"] == "placed":
                    customer.call(f"/api/pedidos/orders/{order_id}/cancel", "PATCH", {})
                elif current["status"] in {"confirmed", "preparing"}:
                    admin.call(f"/api/pedidos/orders/{order_id}", "PATCH", {"status": "cancelled"})
            except Exception as exc:
                print(f"AVISO limpieza pedido de prueba: {exc}", file=sys.stderr)
        if product_id:
            try:
                admin.call(f"/api/catalogo/products/{product_id}", "DELETE", {}, expected=(200, 404))
            except Exception as exc:
                print(f"AVISO: desactiva manualmente el producto {product_id} ({product_name}): {exc}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FALLO DEL FLUJO QA: {error}", file=sys.stderr)
        sys.exit(1)
