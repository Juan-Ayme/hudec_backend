"""
Carga la configuración operativa de una empresa en `app_config[company_id, 'company']`.

Guarda los IDs de BSale que necesita el sistema para funcionar correctamente:
  - offices_tienda: sucursales que venden al público
  - office_almacen: almacén central (opcional)
  - tipos_venta: IDs de tipos de documento de venta
  - tipos_devolucion: IDs de notas de crédito / devoluciones
  - tipos_traslado: IDs de traslados internos entre sucursales
  - bsale_warehouse_user_ids: IDs de usuarios almaceneros
  - target_categories: (opcional) categorías destacadas
  - brand_name / classification_label: marca white-label

Interactivo. Los valores actuales del .env se ofrecen como defaults (útil
para Hudec: solo aprietas Enter y quedan cargados). Para una empresa nueva
(ej. Coya), tipeás los suyos.

Uso:
    cd backend_hudec
    ./.venv/Scripts/python.exe tools/set_company_config.py

Requiere en .env:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

NOTA: los IDs son los que aparecen en la interfaz de BSale de esa empresa.
Los sacas de:
  - Sucursales: /offices.json en BSale
  - Tipos de documento: /document_types.json
  - Usuarios: /users.json

Si el sync inicial ya corrió, podés usarlos como referencia con:
    curl -H "X-Company-Id: 2" http://localhost:8000/config/company/recommendations
(pero primero necesitas sync — círculo. Este script lo rompe.)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


# ============================================================================
# Prompts helpers
# ============================================================================


def prompt_int(msg: str) -> int:
    while True:
        v = input(msg).strip()
        if v.isdigit() and int(v) > 0:
            return int(v)
        print("  → Ingresá un entero positivo.")


def prompt_int_list(msg: str, default: list[int] | None = None) -> list[int]:
    """Pide lista de enteros separados por coma. Enter → default."""
    default_str = ",".join(str(x) for x in default) if default else ""
    hint = f" [Enter = {default_str}]" if default_str else ""
    while True:
        v = input(f"{msg}{hint}: ").strip()
        if not v and default is not None:
            return list(default)
        try:
            ids = [int(x.strip()) for x in v.split(",") if x.strip()]
            if all(x > 0 for x in ids):
                return ids
        except ValueError:
            pass
        print("  → Formato: 1,3,5 (enteros positivos separados por coma).")


def prompt_int_optional(msg: str, default: int | None = None) -> int | None:
    """Un entero o vacío. Enter → default (o None si no hay)."""
    hint = f" [Enter = {default}]" if default is not None else " [Enter = vacío]"
    while True:
        v = input(f"{msg}{hint}: ").strip()
        if not v:
            return default
        if v.lower() in ("none", "null", "vacio", "vacío"):
            return None
        if v.isdigit() and int(v) > 0:
            return int(v)
        print("  → Un entero positivo o Enter para dejar vacío.")


def prompt_str(msg: str, default: str = "") -> str:
    """String simple. Enter → default."""
    hint = f" [Enter = '{default}']" if default else ""
    v = input(f"{msg}{hint}: ").strip()
    return v or default


# ============================================================================
# Env defaults (los del .env global — típicamente son los de Hudec)
# ============================================================================


def _env_int_list(env_key: str, fallback: str = "") -> list[int]:
    raw = os.environ.get(env_key, fallback)
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return []


def _env_int(env_key: str) -> int | None:
    raw = os.environ.get(env_key)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def env_defaults() -> dict:
    """Extrae los valores del .env (los que Hudec usa hoy)."""
    return {
        "brand_name": os.environ.get("BRAND_NAME", ""),
        "classification_label": os.environ.get("CLASSIFICATION_LABEL", "Clasificación"),
        "offices_tienda": _env_int_list("OFFICES_TIENDA"),
        "office_almacen": _env_int("OFFICE_ALMACEN"),
        "tipos_venta": _env_int_list("TIPOS_VENTA"),
        "tipos_devolucion": _env_int_list("TIPOS_DEVOLUCION"),
        "tipos_traslado": _env_int_list("TIPOS_TRASLADO"),
        "bsale_warehouse_user_ids": _env_int_list("BSALE_WAREHOUSE_USER_IDS"),
        "target_categories": _env_int_list("TARGET_CATEGORIES"),
    }


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")

    dsn = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }

    print("=" * 68)
    print(" Cargar configuración operativa de BSale de una empresa")
    print("=" * 68)

    company_id = prompt_int("Company ID (ej. 1 para Hudec, 2 para Coya): ")

    # ========================================================================
    # PASO 1 — Leer empresa y config existente (conexión corta)
    # ========================================================================
    # Neon cierra conexiones ociosas rápido (~5 min). Si dejáramos la conexión
    # abierta durante los prompts, el INSERT final falla con SSL closed.
    # Solución: leer todo lo que necesitamos, cerrar la conexión, hacer los
    # prompts, y reconectar solo para el INSERT.
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, slug FROM companies WHERE id = %s",
                (company_id,),
            )
            row = cur.fetchone()
            if not row:
                print(
                    f"\n❌ No existe empresa con id={company_id}. "
                    f"Primero cargala con tools/set_company_token.py."
                )
                return 1
            cid, name, slug = row

            # Activar contexto de tenant para leer app_config
            # (por el USING de RLS, no estrictamente necesario para SELECT
            # porque la policy deja pasar cuando current_company_id IS NULL,
            # pero por consistencia lo seteamos igual).
            cur.execute(
                "SELECT set_config('app.current_company', %s, true)",
                (str(cid),),
            )
            cur.execute(
                "SELECT value FROM app_config WHERE company_id = %s AND key = 'company'",
                (cid,),
            )
            existing_row = cur.fetchone()

    # Conexión cerrada. A partir de acá NO tocamos DB hasta el final.
    print(f"\n✅ Empresa: id={cid}, name={name!r}, slug={slug!r}")

    if existing_row and existing_row[0]:
        try:
            existing = json.loads(existing_row[0])
            print(
                f"\n⚠️  Esta empresa YA tiene config guardada. "
                f"Los valores previos se ofrecen como defaults."
            )
        except (ValueError, TypeError):
            existing = {}
    else:
        existing = {}

    # ========================================================================
    # PASO 2 — Prompts al usuario (sin conexión activa a DB)
    # ========================================================================
    env_def = env_defaults()
    use_env = input(
        "\n¿Ofrecer los valores del .env como defaults? "
        "(útil solo para Hudec — la empresa cuya config vive en .env) [s/N]: "
    ).strip().lower() in ("s", "si", "sí", "y", "yes")

    def _default(key: str, fallback):
        if key in existing:
            return existing[key]
        if use_env:
            return env_def.get(key, fallback)
        return fallback

    print("\n--- MARCA (white-label) ---")
    brand_name = prompt_str(
        "Nombre de marca (brand_name)",
        default=_default("brand_name", ""),
    )
    classification_label = prompt_str(
        "Etiqueta de clasificación",
        default=_default("classification_label", "Clasificación"),
    )

    print("\n--- SUCURSALES (BSale office IDs) ---")
    offices_tienda = prompt_int_list(
        "IDs de sucursales que venden (coma-separados)",
        default=_default("offices_tienda", []) or None,
    )
    office_almacen = prompt_int_optional(
        "ID del almacén central (opcional)",
        default=_default("office_almacen", None),
    )

    print("\n--- TIPOS DE DOCUMENTO (BSale document_type IDs) ---")
    tipos_venta = prompt_int_list(
        "IDs de tipos de venta",
        default=_default("tipos_venta", []) or None,
    )
    tipos_devolucion = prompt_int_list(
        "IDs de tipos de devolución",
        default=_default("tipos_devolucion", []) or None,
    )
    tipos_traslado = prompt_int_list(
        "IDs de tipos de traslado interno",
        default=_default("tipos_traslado", []) or None,
    )

    print("\n--- USUARIOS ALMACENEROS ---")
    warehouse_user_ids = prompt_int_list(
        "IDs de usuarios BSale que hacen recepciones",
        default=_default("bsale_warehouse_user_ids", []) or None,
    )

    print("\n--- CATEGORÍAS OBJETIVO (opcional) ---")
    target_categories = prompt_int_list(
        "IDs de categorías destacadas (Enter = vacío)",
        default=_default("target_categories", []) or None,
    )

    config = {
        "brand_name": brand_name,
        "classification_label": classification_label,
        "offices_tienda": offices_tienda,
        "office_almacen": office_almacen,
        "tipos_venta": tipos_venta,
        "tipos_devolucion": tipos_devolucion,
        "tipos_traslado": tipos_traslado,
        "bsale_warehouse_user_ids": warehouse_user_ids,
        "target_categories": target_categories,
    }

    print("\n" + "=" * 68)
    print(" RESUMEN — se va a guardar:")
    print("=" * 68)
    print(json.dumps(config, indent=2, ensure_ascii=False))
    confirm = input("\n¿Confirmar guardado? [s/N]: ").strip().lower()
    if confirm not in ("s", "si", "sí", "y", "yes"):
        print("Abortado — nada se modificó.")
        return 0

    # ========================================================================
    # PASO 3 — Reconectar y guardar (conexión corta)
    # ========================================================================
    with psycopg2.connect(**dsn) as conn:
        with conn.cursor() as cur:
            # Activar contexto de tenant para el WITH CHECK de RLS.
            cur.execute(
                "SELECT set_config('app.current_company', %s, true)",
                (str(cid),),
            )

            if existing:
                cur.execute(
                    """
                    INSERT INTO app_config_history
                        (company_id, config_key, value, source, is_manual, label)
                    VALUES (%s, 'company', %s, 'set_company_config.py', TRUE, %s)
                    """,
                    (cid, json.dumps(existing), f"Antes de reemplazo por script"),
                )

            cur.execute(
                """
                INSERT INTO app_config (company_id, key, value, updated_at)
                VALUES (%s, 'company', %s, NOW())
                ON CONFLICT (company_id, key)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """,
                (cid, json.dumps(config, ensure_ascii=False)),
            )
        conn.commit()

    print(f"\n✅ Config guardada para empresa {cid} ({name}).")
    print("   El backend la va a leer en la siguiente request (no requiere reinicio).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
