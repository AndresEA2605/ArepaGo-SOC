#!/usr/bin/env python3
"""Opt-in local demo menu; products are conspicuously marked as demo data."""
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.smoke_test_customer_flow import ADMIN_EMAIL, ROOT, Api


DEMO_PRODUCTS = [
    {"name": "DEMO · Arepa de queso", "price": "8000.00",
     "description": "PRODUCTO DE DEMOSTRACIÓN. Arepa rellena de queso; confirmar receta y precio antes de ofrecer."},
    {"name": "DEMO · Arepa con huevo", "price": "9000.00",
     "description": "PRODUCTO DE DEMOSTRACIÓN. Arepa con huevo; confirmar receta y precio antes de ofrecer."},
    {"name": "DEMO · Arepa reina pepiada", "price": "16000.00",
     "description": "PRODUCTO DE DEMOSTRACIÓN. Arepa con pollo y aguacate; confirmar receta y precio antes de ofrecer."},
    {"name": "DEMO · Arepa de carne mechada", "price": "18000.00",
     "description": "PRODUCTO DE DEMOSTRACIÓN. Arepa con carne mechada; confirmar receta y precio antes de ofrecer."},
]


def main():
    api = Api()
    password = (ROOT / "secrets/admin_password.txt").read_text().strip()
    api.call("/api/auth/login", "POST", {"email": ADMIN_EMAIL, "password": password})
    existing = api.call("/api/catalogo/products?limit=100").get("products", [])
    existing_names = {product["name"] for product in existing}
    created = []
    for product in DEMO_PRODUCTS:
        if product["name"] in existing_names:
            print(f"OMITIDO ya existe: {product['name']}")
            continue
        result = api.call("/api/catalogo/products", "POST", {**product, "image_url": None}, expected=(201,))
        created.append(result["product"])
    print("\nProductos DEMO activos:")
    print(json.dumps([{"name": p["name"], "price": p["price"], "id": p["id"]} for p in created],
                     ensure_ascii=False, indent=2))
    print("Son productos de ejemplo con precios ilustrativos. Revísalos/edítalos en Admin > Catálogo antes de vender.")


if __name__ == "__main__":
    main()
