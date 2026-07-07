# -*- coding: utf-8 -*-
"""Tests del matcher de productos similares (analytics/similares.py).

El caso de referencia es el que motivó la feature: "BYWIN HISOPOS X 505 UND"
(agotado, OPORTUNIDAD PERDIDA) debe advertir que "HISOPO POTE PEQUEÑO" está
sano con stock en la misma tienda.
"""

from analytics.similares import (
    _stem,
    build_similarity_index,
    kanban_column,
    merge_similars_by_sku,
    similar_text,
    tokenize,
)


def _row(sku, producto, clasif, *, suc="TIENDA 1", subcat="Hisopos y Algodón",
         stock=0, cobertura="—", unds=0):
    return {
        "Código SKU": sku,
        "Producto": producto,
        "Sucursal": suc,
        "Subcategoría": subcat,
        "Categoría": "Higiene Personal",
        "Clasificación": clasif,
        "Stock Disp": stock,
        "Cobertura": cobertura,
        "Unds Vend (90d)": unds,
    }


CLASIF_OPORTUNIDAD = "💎 OPORTUNIDAD PERDIDA — REPONER YA: lote RÁPIDO y ya van +60d sin reabastecer"
CLASIF_SANO = "🟢 INVENTARIO SANO — RITMO NORMAL"
CLASIF_LOTE_NUEVO = "🔵 LOTE NUEVO VENDIENDO BIEN"
CLASIF_LENTO = "🐢 LENTO PERO CONSTANTE"


BYWIN = _row("60380", "BYWIN HISOPOS X 505 UND", CLASIF_OPORTUNIDAD)
POTE_PEQ = _row("190006", "HISOPO POTE PEQUEÑO", CLASIF_SANO, stock=169,
                cobertura="72 días", unds=71)
POTE_GDE = _row("190008", "HISOPO POTE GRANDE", "🔥 ALTA ROTACIÓN — PRIORIDAD DE COMPRA",
                stock=1, cobertura="1 días", unds=74)


class TestTokenize:
    def test_bywin_queda_en_dos_tokens(self):
        # "x" <3 chars, "505" dígitos puros, "und" stopword, "hisopos"→stem.
        assert tokenize("BYWIN HISOPOS X 505 UND") == {"bywin", "hisopo"}

    def test_stopwords_presentaciones_y_acentos(self):
        assert tokenize("CREMA DE MANOS 500ML PEQUEÑA") == {"crema", "mano", "pequena"}

    def test_stem_singular_plural(self):
        assert _stem("hisopos") == "hisopo"
        assert _stem("papeles") == "papel"
        assert _stem("mes") == "mes"      # len<5 no pierde "es"
        assert _stem("gas") == "gas"      # len<4 no pierde "s"


class TestKanbanColumn:
    def test_mapeo_basico(self):
        assert kanban_column(CLASIF_OPORTUNIDAD) == "comprar"
        assert kanban_column(CLASIF_SANO) == "vigilar"
        assert kanban_column(CLASIF_LOTE_NUEVO) == "vigilar"
        assert kanban_column(CLASIF_LENTO) == "lentos"
        assert kanban_column("algo sin clasificar") == "liquidar"

    def test_orden_de_chequeos(self):
        # "ROTACIÓN ACTIVA AL BORDE" es comprar (se chequea antes que vigilar).
        assert kanban_column("⚡ ROTACIÓN ACTIVA AL BORDE DE QUIEBRE") == "comprar"


class TestBuildSimilarityIndex:
    def test_bywin_encuentra_al_pote_sano(self):
        index = build_similarity_index([BYWIN, POTE_PEQ, POTE_GDE])
        info = index[("TIENDA 1", "60380")]
        assert info["vigilar"] == 1
        assert [it["sku"] for it in info["items"]] == ["190006"]
        assert info["items"][0]["stock"] == 169
        assert info["items"][0]["estado"] == "vigilar"

    def test_pote_grande_tambien_recibe_aviso(self):
        index = build_similarity_index([BYWIN, POTE_PEQ, POTE_GDE])
        info = index[("TIENDA 1", "190008")]
        assert info["vigilar"] == 1
        assert info["items"][0]["sku"] == "190006"

    def test_no_matchea_en_otra_subcategoria(self):
        otro = _row("999", "HISOPO POTE MEDIANO", CLASIF_SANO,
                    subcat="Otra Subcategoría", stock=50)
        index = build_similarity_index([BYWIN, otro])
        assert ("TIENDA 1", "60380") not in index

    def test_no_matchea_en_otra_sucursal(self):
        lejos = _row("190006", "HISOPO POTE PEQUEÑO", CLASIF_SANO,
                     suc="TIENDA 2", stock=169)
        index = build_similarity_index([BYWIN, lejos])
        assert ("TIENDA 1", "60380") not in index

    def test_no_matchea_producto_no_relacionado(self):
        ajeno = _row("777", "CREMA DENTAL COLGATE", CLASIF_SANO, stock=30)
        index = build_similarity_index([BYWIN, ajeno])
        assert ("TIENDA 1", "60380") not in index

    def test_similar_agotado_no_cuenta(self):
        # Un lento/liquidar puede estar clasificado así con stock 0: un
        # similar AGOTADO no sustituye nada y no debe generar aviso.
        agotado = _row("888", "HISOPOS TIJERA PARA CEJAS", CLASIF_LENTO,
                       stock=0, cobertura="Agotado")
        index = build_similarity_index([BYWIN, agotado])
        assert ("TIENDA 1", "60380") not in index

    def test_producto_sano_no_es_target_por_default(self):
        # Sin target_keys solo la columna kanban "comprar" recibe aviso.
        index = build_similarity_index([BYWIN, POTE_PEQ])
        assert ("TIENDA 1", "190006") not in index

    def test_target_keys_restringe(self):
        index = build_similarity_index(
            [BYWIN, POTE_PEQ, POTE_GDE],
            target_keys={("TIENDA 1", "190008")},
        )
        assert ("TIENDA 1", "60380") not in index
        assert ("TIENDA 1", "190008") in index

    def test_items_ordenados_por_estado_y_stock(self):
        lento = _row("555", "HISOPOS BOLSA GRANDE", CLASIF_LENTO, stock=500)
        index = build_similarity_index([BYWIN, POTE_PEQ, lento])
        estados = [it["estado"] for it in index[("TIENDA 1", "60380")]["items"]]
        assert estados == ["vigilar", "lentos"]  # vigilar primero aunque tenga menos stock


class TestMergeYTexto:
    def test_merge_dedup_por_sku_y_sucursal(self):
        index = {
            ("TIENDA 1", "60380"): {
                "vigilar": 1, "lentos": 0, "liquidar": 0,
                "items": [{"sku": "190006", "producto": "HISOPO POTE PEQUEÑO",
                           "estado": "vigilar", "stock": 169.0,
                           "cobertura": "72 días", "unds_vend_90d": 71.0,
                           "sucursal": "TIENDA 1"}],
            },
            ("TIENDA 2", "60380"): {
                "vigilar": 1, "lentos": 0, "liquidar": 0,
                "items": [{"sku": "190006", "producto": "HISOPO POTE PEQUEÑO",
                           "estado": "vigilar", "stock": 40.0,
                           "cobertura": "30 días", "unds_vend_90d": 20.0,
                           "sucursal": "TIENDA 2"}],
            },
        }
        merged = merge_similars_by_sku(index)
        assert set(merged) == {"60380"}
        assert merged["60380"]["vigilar"] == 2  # mismo sku pero distinta sucursal
        # Y si la entrada está duplicada exacta, se dedupea:
        index[("TIENDA 3", "60380")] = index[("TIENDA 1", "60380")]
        merged = merge_similars_by_sku(index)
        assert merged["60380"]["vigilar"] == 2

    def test_similar_text(self):
        info = {"vigilar": 2, "lentos": 0, "liquidar": 0, "items": [
            {"sku": "190006", "producto": "HISOPO POTE PEQUEÑO", "estado": "vigilar",
             "stock": 169.0, "cobertura": "72 días", "unds_vend_90d": 71.0,
             "sucursal": "TIENDA 1"},
            {"sku": "190008", "producto": "HISOPO POTE GRANDE", "estado": "vigilar",
             "stock": 1.0, "cobertura": "1 días", "unds_vend_90d": 74.0,
             "sucursal": "TIENDA 1"},
        ]}
        assert similar_text(info) == (
            "HISOPO POTE PEQUEÑO (190006) · 169 und · cob. 72 días · +1 más"
        )
        assert similar_text({"items": []}) == ""
