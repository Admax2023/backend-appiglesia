"""Command line utilities.

    python -m app.cli create-admin --username admin --name "Administrador"   (password prompted, or
                                                                              ADMIN_PASSWORD env var)
    python -m app.cli reset-password --username admin   (password prompted, or ADMIN_PASSWORD env var)
    python -m app.cli seed-demo            # sample products (idempotent)
    python -m app.cli purge-idempotency [--hours 48]   # default: IDEMPOTENCY_RETENTION_HOURS
    python -m app.cli qr [--url URL] [--output menu-qr.png]   # printable QR of the public menu
    python -m app.cli dump-openapi ../docs/openapi.json
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.errors import AppError
from app.models import Product

DEMO_PRODUCTS = [
    ("Empanada de carne", "Empanada frita de carne desmechada", "2500", 1),
    ("Empanada de pollo", "Empanada frita de pollo", "2500", 2),
    ("Papa rellena", "Papa rellena de carne y arroz", "4000", 3),
    ("Arepa de huevo", "Arepa frita rellena de huevo", "4500", 4),
    ("Buñuelo", "Buñuelo tradicional", "1500", 5),
    ("Carimañola", "Carimañola de yuca rellena de queso", "3000", 6),
    ("Gaseosa 400 ml", "Bebida fría", "3000", 7),
    ("Jugo natural", "Jugo del día en agua", "3500", 8),
]


def cmd_create_admin(args: argparse.Namespace) -> int:
    from app.services.auth import create_user

    password = os.environ.get("ADMIN_PASSWORD") or args.password
    if not password:
        password = getpass.getpass("Contraseña (mín. 10 caracteres): ")
        if password != getpass.getpass("Repite la contraseña: "):
            print("Las contraseñas no coinciden", file=sys.stderr)
            return 1
    with SessionLocal() as db:
        try:
            user = create_user(db, args.username, args.name, password)
            db.commit()
        except AppError as e:
            print(f"Error: {e.detail}", file=sys.stderr)
            return 1
    print(f"Administrador creado: {user.username} ({user.id})")
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    from app.services.auth import reset_password

    password = os.environ.get("ADMIN_PASSWORD") or args.password
    if not password:
        password = getpass.getpass("Nueva contraseña (mín. 10 caracteres): ")
        if password != getpass.getpass("Repite la contraseña: "):
            print("Las contraseñas no coinciden", file=sys.stderr)
            return 1
    with SessionLocal() as db:
        try:
            user = reset_password(db, args.username, password)
            db.commit()
        except AppError as e:
            print(f"Error: {e.detail}", file=sys.stderr)
            return 1
    print(f"Contraseña restablecida para {user.username}. Se cerraron sus sesiones abiertas.")
    return 0


def cmd_seed_demo(_: argparse.Namespace) -> int:
    created = 0
    with SessionLocal() as db:
        for name, desc, price, order in DEMO_PRODUCTS:
            if db.scalar(select(Product).where(Product.name == name)) is None:
                db.add(Product(name=name, description=desc, price=Decimal(price), sort_order=order))
                created += 1
        db.commit()
    print(f"Productos de demostración creados: {created}")
    return 0


def cmd_purge_idempotency(args: argparse.Namespace) -> int:
    from app.services.idempotency import purge_older_than

    from app.config import get_settings

    hours = args.hours if args.hours is not None else get_settings().idempotency_retention_hours
    with SessionLocal() as db:
        n = purge_older_than(db, hours)
    print(f"Claves de idempotencia eliminadas: {n}")
    return 0


def cmd_qr(args: argparse.Namespace) -> int:
    from app import qr
    from app.config import get_settings

    url = args.url or get_settings().public_web_url
    try:
        out = qr.write(url, Path(args.output), error=args.error, border=args.border, box_size=args.box_size)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"QR de {url} escrito en {out}")
    return 0


def cmd_dump_openapi(args: argparse.Namespace) -> int:
    from app.main import app

    Path(args.output).write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"OpenAPI escrito en {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-admin", help="Crear un usuario administrador")
    p.add_argument("--username", required=True)
    p.add_argument("--name", default="Administrador")
    p.add_argument("--password", help="Evítalo en producción (queda en el historial); usa el prompt o ADMIN_PASSWORD")
    p.set_defaults(func=cmd_create_admin)
    p = sub.add_parser("reset-password", help="Restablecer la contraseña de un administrador")
    p.add_argument("--username", required=True)
    p.add_argument("--password", help="Evítalo en producción (queda en el historial); usa el prompt o ADMIN_PASSWORD")
    p.set_defaults(func=cmd_reset_password)
    p = sub.add_parser("seed-demo", help="Crear productos de ejemplo")
    p.set_defaults(func=cmd_seed_demo)
    p = sub.add_parser("purge-idempotency", help="Borrar claves de idempotencia antiguas")
    p.add_argument("--hours", type=int, default=None, help="Antigüedad mínima (def. IDEMPOTENCY_RETENTION_HOURS)")
    p.set_defaults(func=cmd_purge_idempotency)
    p = sub.add_parser("qr", help="Generar el QR imprimible del menú público (PNG o SVG)")
    p.add_argument("--url", help="URL pública del menú (por defecto PUBLIC_WEB_URL)")
    p.add_argument("--output", default="menu-qr.png", help="Archivo .png o .svg (def. menu-qr.png)")
    p.add_argument("--error", choices=["L", "M", "Q", "H"], default="M", help="Corrección de errores (def. M)")
    p.add_argument("--border", type=int, default=4, help="Margen en módulos (mínimo recomendado 4)")
    p.add_argument("--box-size", type=int, default=20, help="Píxeles por módulo en PNG (def. 20)")
    p.set_defaults(func=cmd_qr)
    p = sub.add_parser("dump-openapi", help="Escribir el esquema OpenAPI a un archivo")
    p.add_argument("output")
    p.set_defaults(func=cmd_dump_openapi)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
