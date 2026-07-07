# -*- coding: utf-8 -*-
"""Detección de productos SIMILARES por nombre dentro de la misma subcategoría.

Responde la pregunta del gerente ante una sugerencia de compra: "¿ya tengo un
producto parecido con stock en esta tienda?". Ej.: antes de reponer
"BYWIN HISOPOS X 505 UND" (agotado), avisar que "HISOPO POTE PEQUEÑO" está
sano con 169 unidades. La advertencia NUNCA excluye al SKU de la lista de
compras: el matching por nombre es heurístico y ocultar podría causar
quiebres reales.

Es el port en Python de
`frontend_hudec/src/features/ventas-jerarquicas/utils/similarity.ts`
(el chip "N en saludable" de /ventas-jerarquicas), con dos mejoras que deben
mantenerse IDÉNTICAS en ambos lados:

  1. Stemming ligero singular/plural: "hisopos" → "hisopo" ("es" con len≥5,
     "s" con len≥4).
  2. Umbral adaptativo: match con ≥2 tokens compartidos, O con 1 token cuando
     cubre ≥50% del set más chico (productos de nombre corto como
     "BYWIN HISOPOS" → {bywin, hisopo}).

El mapeo clasificación → columna kanban es el port literal de
`getKanbanColumn` (frontend `features/ventas-jerarquicas/utils/index.ts`) y
NO `classify_action()` de app/kawii_matrix/service.py: los conteos deben
coincidir con lo que el usuario ve en /ventas-jerarquicas (p.ej. "POCO STOCK
CON DEMANDA" es kanban `comprar` pero classify_action "evaluar").

| estado    | etiqueta UI | clasificaciones                                   |
|-----------|-------------|---------------------------------------------------|
| vigilar   | Saludable   | PRODUCTO NUEVO, EMERGENTE, INVENTARIO SANO, ...   |
| lentos    | Lento       | LENTO PERO CONSTANTE, BAJA ROTACIÓN, EXCESO, ...  |
| liquidar  | Liquidar    | fallback (STOCK PARADO, PRODUCTO MUERTO, ...)     |
"""

from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# ── Tokenización (espejo de similarity.ts:4-33) ──────────────────────────

STOPWORDS = frozenset({
    "de", "la", "el", "los", "las", "para", "con", "sin", "por", "del", "al",
    "un", "una", "unos", "unas", "se", "su", "sus", "que",
    "und", "uds", "unidad", "unidades", "pack", "caja", "bolsa",
})

_PRESENTATION_RE = re.compile(
    r"^\d+(\.\d+)?(ml|cl|kg|gr|cm|mm|oz|pcs|pz|und|uds|hjs|hs|hjas|h)$",
    re.IGNORECASE,
)
_PURE_DIGITS_RE = re.compile(r"^\d+(\.\d+)?$")
_FIRST_NUM_RE = re.compile(r"-?\d+(\.\d+)?")


def _s(v) -> str:
    return "" if v is None else str(v)


def _num(v) -> float:
    """Primer número de un valor mixto (espejo del `n()` del frontend).

    Sirve para celdas de la matriz que traen texto, ej. cobertura "72 días".
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    m = _FIRST_NUM_RE.search(str(v))
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0
    return 0.0


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _stem(token: str) -> str:
    if token.endswith("es") and len(token) >= 5:
        return token[:-2]
    if token.endswith("s") and len(token) >= 4:
        return token[:-1]
    return token


def tokenize(name: str) -> set[str]:
    tokens: set[str] = set()
    for t in _normalize(name).split(" "):
        if len(t) < 3:
            continue
        if t in STOPWORDS:  # sobre el token crudo, antes del stem
            continue
        if _PRESENTATION_RE.match(t):
            continue
        if _PURE_DIGITS_RE.match(t):
            continue
        tokens.add(_stem(t))
    return tokens


def _is_similar(tokens_a: set[str], tokens_b: set[str]) -> bool:
    shared = 0
    for t in tokens_a:
        if t in tokens_b:
            shared += 1
            if shared >= 2:
                return True
    if shared == 0:
        return False
    return shared / min(len(tokens_a), len(tokens_b)) >= 0.5


# ── Clasificación → columna kanban (port literal de getKanbanColumn) ─────

_COMPRAR_KEYS = (
    "BESTSELLER ACTIVO", "BESTSELLER RÁPIDO AGOTADO", "OPORTUNIDAD PERDIDA",
    "QUIEBRE DE BESTSELLER", "AGOTADO CON DEMANDA", "ALTA ROTACIÓN POR LOTE",
    "ALTA ROTACIÓN", "ROTACIÓN ACTIVA AL BORDE", "POCO STOCK CON DEMANDA",
)
_ALERTAS_KEYS = (
    "PÉRDIDA DE STOCK", "VENTAS CON PÉRDIDA", "VENDIÓ Y SE PERDIÓ",
    "STOCK BAJO QUIETO", "RITMO PERDIDO", "EX-BESTSELLER ENFRIADO",
    "CASO ATÍPICO",
)
_VIGILAR_KEYS = (
    "PRODUCTO NUEVO", "EMERGENTE", "STOCK RECIÉN LLEGADO",
    "LOTE NUEVO VENDIENDO", "RECIÉN REABASTECIDO", "ROTACIÓN ACTIVA",
    "INVENTARIO SANO", "VENDIENDO MÁS", "RECIBIDO Y NO VENDIDO",
)
_LENTOS_KEYS = (
    "LENTO PERO CONSTANTE", "BAJA ROTACIÓN", "EXCESO", "STOCK EXCESIVO",
    "ROTACIÓN BAJANDO",
)


def kanban_column(clasif: str) -> str:
    c = _s(clasif).upper()
    if any(k in c for k in _COMPRAR_KEYS):
        return "comprar"
    if any(k in c for k in _ALERTAS_KEYS):
        return "alertas"
    if any(k in c for k in _VIGILAR_KEYS):
        return "vigilar"
    if any(k in c for k in _LENTOS_KEYS):
        return "lentos"
    return "liquidar"


# Estados con stock que sirven de "similar disponible" (SIMILAR_TARGET_COLS).
CANDIDATE_COLS = frozenset({"vigilar", "lentos", "liquidar"})

_ESTADO_ORDER = {"vigilar": 0, "lentos": 1, "liquidar": 2, "alertas": 3, "comprar": 4}


# ── Índice de similares ──────────────────────────────────────────────────

def build_similarity_index(
    rows: list[dict],
    *,
    label_col: str = "Clasificación",
    target_keys: set[tuple[str, str]] | None = None,
    max_group: int = 800,
) -> dict[tuple[str, str], dict]:
    """Índice (Sucursal, Código SKU) → info de similares con stock en tienda.

    `rows` debe ser el universo COMPLETO de la matriz (todas las
    clasificaciones): los candidatos sanos/lentos no sobreviven al filtro de
    quiebre, así que hay que buscar antes de filtrar.

    Se agrupa por (Sucursal, Subcategoría) porque la matriz es SKU×Sucursal y
    el aviso significa "similar en la MISMA tienda".

    `target_keys`: si se pasa, solo esas (sucursal, sku) reciben aviso (ej. el
    universo 🔴/🟠 de compras, que es un superset de la columna kanban
    "comprar"); si es None, aplica el default del frontend (kanban "comprar").

    info = {"vigilar": n, "lentos": n, "liquidar": n,
            "items": [{sku, producto, estado, stock, cobertura,
                       unds_vend_90d, sucursal}]}
    """
    groups: dict[tuple[str, str], list[tuple[dict, set[str], str]]] = {}
    for row in rows:
        subcat = _s(row.get("Subcategoría")) or _s(row.get("Categoría")) or "—"
        tokens = tokenize(_s(row.get("Producto")))
        if not tokens:
            continue
        col = kanban_column(_s(row.get(label_col)))
        groups.setdefault((_s(row.get("Sucursal")), subcat), []).append((row, tokens, col))

    result: dict[tuple[str, str], dict] = {}

    for key, entries in groups.items():
        if len(entries) < 2:
            continue
        if len(entries) > max_group:
            # Guard O(g²): un grupo así de grande huele a taxonomía rota.
            logger.warning(
                "similares: grupo %s con %d filas supera max_group=%d; se omite",
                key, len(entries), max_group,
            )
            continue

        for target_row, target_tokens, target_col in entries:
            sku = _s(target_row.get("Código SKU"))
            if not sku:
                continue
            suc = _s(target_row.get("Sucursal"))
            if target_keys is not None:
                if (suc, sku) not in target_keys:
                    continue
            elif target_col != "comprar":
                continue

            counts = {"vigilar": 0, "lentos": 0, "liquidar": 0}
            items: list[dict] = []

            for cand_row, cand_tokens, cand_col in entries:
                if cand_row is target_row:
                    continue
                if cand_col not in CANDIDATE_COLS:
                    continue
                # Un similar AGOTADO no sustituye nada: el aviso significa
                # "ya tienes stock parecido en tienda". (Un lento/liquidar
                # puede estar clasificado así con stock 0.)
                cand_stock = _num(cand_row.get("Stock Disp"))
                if cand_stock <= 0:
                    continue
                cand_sku = _s(cand_row.get("Código SKU"))
                if cand_sku == sku:
                    continue
                if not _is_similar(target_tokens, cand_tokens):
                    continue
                counts[cand_col] += 1
                items.append({
                    "sku": cand_sku,
                    "producto": _s(cand_row.get("Producto")),
                    "estado": cand_col,
                    "stock": cand_stock,
                    "cobertura": _s(cand_row.get("Cobertura")) or "—",
                    "unds_vend_90d": _num(cand_row.get("Unds Vend (90d)")),
                    "sucursal": _s(cand_row.get("Sucursal")) or None,
                })

            if not items:
                continue
            items.sort(key=lambda it: (_ESTADO_ORDER[it["estado"]], -it["stock"]))
            result[(suc, sku)] = {**counts, "items": items}

    return result


def merge_similars_by_sku(index: dict[tuple[str, str], dict]) -> dict[str, dict]:
    """Consolida el índice por SKU (para el Excel, que une sucursales)."""
    merged: dict[str, dict] = {}
    seen: dict[str, set[tuple[str, str | None]]] = {}

    for (_suc, sku), info in index.items():
        m = merged.setdefault(sku, {"vigilar": 0, "lentos": 0, "liquidar": 0, "items": []})
        s = seen.setdefault(sku, set())
        for it in info["items"]:
            k = (it["sku"], it.get("sucursal"))
            if k in s:
                continue
            s.add(k)
            m["items"].append(it)
            m[it["estado"]] += 1

    for m in merged.values():
        m["items"].sort(key=lambda it: (_ESTADO_ORDER[it["estado"]], -it["stock"]))
    return merged


def similar_text(info: dict) -> str:
    """Texto de celda para el Excel: mejor similar + cuántos más hay."""
    items = info.get("items") or []
    if not items:
        return ""
    it = items[0]
    txt = f"{it['producto']} ({it['sku']}) · {int(it['stock'])} und · cob. {it['cobertura']}"
    if len(items) > 1:
        txt += f" · +{len(items) - 1} más"
    return txt
