"""
Cifra un token de BSale y lo guarda en la tabla `companies`.

Interactivo: pregunta company_id, name, slug y token. El token NO se
imprime (usa getpass — no queda en el terminal ni en el history).
Si el company_id existe, actualiza el token. Si no existe, crea la fila.

Uso:
    cd backend_hudec
    ./.venv/Scripts/python.exe tools/set_company_token.py

Requiere en .env:
    TOKEN_ENCRYPTION_KEY  — clave maestra para pgp_sym_encrypt
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


def prompt_int(msg: str) -> int:
    while True:
        v = input(msg).strip()
        if v.isdigit() and int(v) > 0:
            return int(v)
        print("  → Ingresá un entero positivo.")


def prompt_nonempty(msg: str) -> str:
    while True:
        v = input(msg).strip()
        if v:
            return v
        print("  → No puede estar vacío.")


def main() -> int:
    # Cargar .env desde la raíz del backend
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")

    master_key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not master_key:
        print("ERROR: TOKEN_ENCRYPTION_KEY no está en .env", file=sys.stderr)
        return 2

    dsn = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }

    print("=" * 60)
    print(" Cargar token BSale de una empresa (cifrado en DB)")
    print("=" * 60)

    company_id = prompt_int("Company ID (ej. 1 para Hudec, 2 para EmpresaB): ")

    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, slug FROM companies WHERE id = %s",
                (company_id,),
            )
            row = cur.fetchone()

            if row:
                cid, name, slug = row
                print(f"\nEmpresa existente: id={cid}, name={name!r}, slug={slug!r}")
                print("Se actualizará SU token.")
            else:
                print(f"\nNo existe empresa con id={company_id}. Se creará.")
                name = prompt_nonempty("Nombre de la empresa: ")
                slug = prompt_nonempty("Slug (kebab-case, único): ")

            # Token: usa getpass para NO mostrarlo al tipearlo
            token = getpass.getpass("Pega el token de BSale (no se mostrará): ").strip()
            if not token:
                print("Token vacío — abortando.", file=sys.stderr)
                return 1

            token_confirm = getpass.getpass("Confirmá el token: ").strip()
            if token != token_confirm:
                print("Los tokens no coinciden — abortando.", file=sys.stderr)
                return 1

            if row:
                cur.execute(
                    """
                    UPDATE companies
                    SET bsale_token = pgp_sym_encrypt(%s, %s)
                    WHERE id = %s
                    """,
                    (token, master_key, company_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO companies (id, name, slug, bsale_token)
                    VALUES (%s, %s, %s, pgp_sym_encrypt(%s, %s))
                    """,
                    (company_id, name, slug, token, master_key),
                )
                # Ajustar sequence para que próximo INSERT no reuse este id
                cur.execute(
                    "SELECT setval('companies_id_seq', "
                    "(SELECT MAX(id) FROM companies), true)"
                )

            # Verificar round-trip descifrado
            cur.execute(
                "SELECT pgp_sym_decrypt(bsale_token, %s)::text FROM companies WHERE id = %s",
                (master_key, company_id),
            )
            decrypted = cur.fetchone()[0]
            if decrypted != token:
                conn.rollback()
                print("ERROR: verificación de round-trip falló. Rollback.", file=sys.stderr)
                return 3

        conn.commit()

    # Mostrar preview seguro (primeros 4 + últimos 4 chars)
    preview = f"{token[:4]}…{token[-4:]}" if len(token) >= 8 else "…"
    print()
    print(f"✅ Token guardado y cifrado para empresa {company_id} ({name}).")
    print(f"   Preview: {preview}  (longitud {len(token)})")
    print(f"   Descifrado round-trip OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
