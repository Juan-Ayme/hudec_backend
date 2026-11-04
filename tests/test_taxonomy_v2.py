# -*- coding: utf-8 -*-
"""Tests de la taxonomía v2: migración por operaciones, reglas de
reclasificación, inferencia de atributos y driver Excel.

Todo es lógica pura (sin BD). Los nombres de producto son reales del
catálogo de Kawii Pluss, para que un cambio en las reglas que rompa un caso
conocido se note.
"""

import io
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.attribute_inference import (
    group_key,
    infer_product_attributes,
    infer_variant_attributes,
    name_issues,
    parse_contenido,
    parse_unidades_caja,
    parse_unidades_pack,
)
from app.services.driver_import import _canonical, _clean_code, parse_driver
from app.services.driver_template import build_driver_template, tipos_from_tree
from app.services.taxonomy_migration import (
    MigrationError,
    apply_to_tree,
    translate_path,
    validate_ops,
)
from app.services.taxonomy_migration_db import check_invariants
from app.services.taxonomy_rules import RULES, TYPE_REMAP, propose

BASE = Path(__file__).resolve().parent.parent / "Nueva_estructura"
V1 = json.loads((BASE / "kawii-pluss-v1.json").read_text(encoding="utf-8"))
V2 = json.loads((BASE / "kawii-pluss-v2.json").read_text(encoding="utf-8"))
OPS = json.loads((BASE / "migracion_v1_v2.json").read_text(encoding="utf-8"))["ops"]
V2_PATHS = {(d, c, s) for d, cs in V2.items() for c, ss in cs.items() for s in ss}


def _paths(tree):
    return {(d, c, s) for d, cs in tree.items() for c, ss in cs.items() for s in ss}


# ── Migración v1 → v2 ────────────────────────────────────────────────────────


def test_v2_json_es_el_resultado_de_aplicar_las_ops():
    tree, rep = apply_to_tree(V1, OPS)
    assert _paths(tree) == V2_PATHS
    assert all(r.status == "applied" for r in rep.results), [
        (r.index, r.op, r.path, r.status, r.detail) for r in rep.results if r.status != "applied"
    ]


def test_migracion_es_reejecutable():
    """Aplicar las ops sobre v2 no cambia nada (lo migrado ya no está en el origen)."""
    tree, rep = apply_to_tree(V2, OPS)
    assert _paths(tree) == V2_PATHS
    assert all(r.status in ("skipped", "applied") for r in rep.results)


def test_ninguna_subcategoria_v1_se_pierde():
    """Cada subcategoría v1 cae en una subcategoría que existe en v2."""
    for path in _paths(V1):
        assert translate_path(path, OPS) in V2_PATHS, path


@pytest.mark.parametrize("v1, v2", [
    (("Alimentos y Bebidas Asiáticas", "Snacks y Dulces Asiáticos", "Galletas Asiáticos"),
     ("Alimentos y Bebidas Asiáticas", "Snacks y Dulces Asiáticos", "Galletas Asiáticas")),
    (("Belleza", "Perfumería", "Perfumes de Hombre"), ("Belleza", "Perfumería", "Perfumes de Hombre")),
    (("Hogar y Decoración", "Cocina", "Tapers de Vidrio"),
     ("Cocina y Comedor", "Almacenaje", "Tapers de Vidrio")),
    (("Hogar y Decoración", "Cocina", "Ollas y Sartenes"), ("Cocina y Comedor", "Menaje", "Ollas")),
    (("Hogar y Decoración", "Cocina", "Cuchillos"), ("Cocina y Comedor", "Utensilios de Cocina", "Cuchillos")),
    (("Hogar y Decoración", "Organización", "Ganchos de Ropa"), ("Hogar y Decoración", "Organización", "Ganchos de Ropa")),
    (("Cuidado Personal y Salud", "Higiene Personal", "Toallitas Húmedas Licencia"),
     ("Cuidado Personal y Salud", "Higiene Personal", "Toallitas Húmedas con Licencia")),
    (("Tendencias Kawaii", "Peluches", "Peluches Kawaii"), ("Juguetes y Entretenimiento", "Peluches", "Peluches Kawaii")),
    (("Limpieza", "Cuidado del Auto", "Shampoo para Auto"),
     ("Ferretería, Jardín y Automotriz", "Automotriz", "Shampoo para Auto")),
    (("Moda y Accesorios", "Ropa Infantil", "Guantes, Chullos y Chalinas"),
     ("Moda y Accesorios", "Accesorios de Invierno", "Chullos y Gorros")),
])
def test_translate_path(v1, v2):
    assert translate_path(v1, OPS) == v2


def test_rename_a_nombre_existente_fusiona():
    tree = {"A": {"X": {"s1": []}, "Y": {"s2": []}}}
    out, rep = apply_to_tree(tree, [{"op": "rename", "path": ["A", "X"], "to": "Y"}])
    assert out == {"A": {"Y": {"s1": [], "s2": []}}}
    assert rep.results[0].merged


def test_drop_empty_no_borra_si_tiene_hijos():
    tree = {"A": {"X": {"s1": []}}}
    out, rep = apply_to_tree(tree, [{"op": "drop_empty", "path": ["A"]}])
    assert out == tree
    assert rep.results[0].status == "refused"


@pytest.mark.parametrize("bad", [
    [],
    [{"op": "borrar", "path": ["A"]}],
    [{"op": "move", "path": ["A", "B"], "to": ["X", "Y"]}],
    [{"op": "merge", "path": ["A", "B"], "into": ["X"]}],
    [{"op": "rename", "path": ["A"]}],
])
def test_validate_ops_rechaza_formas_invalidas(bad):
    with pytest.raises(MigrationError):
        validate_ops(bad)


def test_invariantes_detectan_productos_perdidos():
    before = {"productos": 10, "productos_clasificados": 9, "tipos": 5, "tipos_mapeados": 4, "metas": 3}
    assert check_invariants(before, dict(before), 0) == []
    after = dict(before, productos_clasificados=8)
    assert check_invariants(before, after, 0)
    after = dict(before, metas=2)
    assert check_invariants(before, after, 1) == []
    assert check_invariants(before, after, 0)


# ── Reglas de reclasificación ────────────────────────────────────────────────


def test_destinos_de_reglas_existen_en_v2():
    assert [r.code for r in RULES if r.target not in V2_PATHS] == []
    assert [k for k, v in TYPE_REMAP.items() if v not in V2_PATHS] == []


def _v2(path):
    return translate_path(path, OPS) if path else None


@pytest.mark.parametrize("name, v1_path, type_name, expected_sub", [
    ("CORREA PARA MASCOTA", ("Mascotas", "Alimentos y Arena", "Alimentos para Mascotas"), None,
     "Correas, Collares y Pecheras"),
    ("PORTAHUEVOS CARAMELO", ("Alimentos Nacionales", "Dulces y Chocolates", "Caramelos"), None,
     "Organizadores de Cocina"),
    ("AC Nutribela Argán 370ml (CJAX3)", ("Alimentos Nacionales", "Galletas y Panadería", "Galletas"), None,
     "Acondicionadores"),
    ("CALABAZA KUROMI Y HELLO KITY", ("Belleza", "Perfumería", "Perfumes Infantiles"), None, "Calabazas"),
    ("BYWIN SHAMPOO 2EN1 X 236ML GROOT", ("Belleza", "Perfumería", "Perfumes Infantiles"), None, "Shampoos"),
    ("HOJA BOND COLORES X 100", ("Librería y Oficina", "Escritura y Corrección", "Colores y Crayolas"), None,
     "Papel Bond y Especiales"),
    ("MOCHILA DAMAS", ("Librería y Oficina", "Escolar", "Mochilas Escolares"), None, "Mochilas de Moda"),
    ("SKY COOL BODY SPRAY 150ML", ("Belleza", "Perfumería", "Colonias y Splashes"), None, "Body Spray"),
    ("LEJIA ANTIBACTERIAL ZACALY CLEAN  GL. DE  4LT. (CAJA X 6 UNID)", None, "Sin Tipo", "Lejías"),
    ("PELUCHE LUCIFER", None, "Sin Tipo", "Peluches Kawaii"),
    ("CANDADO VINSA GRANDE PLATEADO 70MM", None, "nuevo", "Candados y Cadenas"),
    ("ESPONJA P/COCINA CON MALLAS 3 PCS", None, "Limpieza / Esponjas, Paños y Guantes", "Esponjas"),
    ("Galleta Field Soda 6x32Gr.", ("Alimentos Nacionales", "Galletas y Panadería", "Galletas"), None, "Galletas Saladas"),
    ("Wafer Costa Nik Fresa 6x27Gr (PQTX12)", ("Alimentos Nacionales", "Galletas y Panadería", "Galletas"), None, "Wafers"),
    ("TOMATODO NIÑOS 500 ML", ("Hogar y Decoración", "Cocina", "Tomatodos y Botellas"), None, "Tomatodos Infantiles"),
    ("TOMATODO SPORTS 900ML", ("Hogar y Decoración", "Cocina", "Tomatodos y Botellas"), None, "Tomatodos Deportivos"),
    ("AFILADOR DE CUCHILLO", ("Hogar y Decoración", "Cocina", "Cuchillos"), None, "Afiladores de Cuchillos"),
    ("BON BON BUM FRESA( 15 SB/24/19gr)", ("Alimentos Nacionales", "Dulces y Chocolates", "Caramelos"), None, "Chupetines"),
    ("INFLADOR GLOBOS", ("Fiestas y Cumpleaños", "Cumpleaños y Fiestas", "Globos e Infladores"), None, "Infladores"),
    ("SET MOTA + PLUMÓN X4", ("Librería y Oficina", "Escritura y Corrección", "Plumones y Marcadores"), None,
     "Pizarras, Tizas y Accesorios"),
    ("TAJADOR DINOSAURIO", ("Librería y Oficina", "Escritura y Corrección", "Borradores y Tajadores"), None, "Tajadores"),
    ("Pistola de silicona", ("Librería y Oficina", "Oficina", "Pegamentos y Siliconas"), None, "Pistolas de Silicona"),
    ("GOMA EN BARRA LAYCONSA X 40GR.", ("Librería y Oficina", "Oficina", "Pegamentos y Siliconas"), None, "Gomas y Colas"),
    ("MASKING 1 X 15 YD ECOFAN", ("Librería y Oficina", "Oficina", "Cintas Adhesivas y Contact"), None, "Masking"),
    ("25 SCOOL RANGER SET MALETA GDE EVA 3D KUROMI", ("Librería y Oficina", "Escolar", "Mochilas Escolares"), None,
     "Maletas Escolares con Ruedas"),
    ("Bebida con Nata de Coco Piña Baolisa 450 ML", ("Alimentos y Bebidas Asiáticas", "Bebidas Asiáticas",
     "Jugo y Nata de Coco"), None, "Bebidas con Nata de Coco"),
    ("TAZON", ("Hogar y Decoración", "Cocina", "Bowls, Fuentes y Bandejas"), None, "Bowls y Tazones"),
    ("PISTOLA HIDROGEL P38", ("Juguetes y Entretenimiento", "Juegos al Aire Libre", "Hidrogel y Pistolas de Agua"),
     None, "Pistolas de Hidrogel"),
    ("ME TOO LA BELLE EDT 50ML", ("Belleza", "Perfumería", "Perfumes de Hombre"), None, "Perfumes de Mujer"),
    ("MY MELODY EDT 30ML", ("Belleza", "Perfumería", "Perfumes de Mujer"), None, "Perfumes Infantiles"),
    ("PRESTIGE HASSAN EDP REGULAR 100ML", ("Belleza", "Perfumería", "Perfumes de Mujer"), None, "Perfumes de Hombre"),
    ("SARTEN 16 CM", ("Hogar y Decoración", "Cocina", "Ollas y Sartenes"), None, "Sartenes"),
    ("BLOCK ARCOIRIS MERCYFAN", ("Librería y Oficina", "Papelería", "Cuadernos y Blocks"), None, "Blocks y Sketchbooks"),
    ("PAPEL CREPE X10", ("Librería y Oficina", "Papelería", "Papeles y Cartulinas"), None, "Papel Lustre, Crepé y Seda"),
    ("CRAYOLAS ARTESCO DELGADA X12 (CAJONX288)", ("Librería y Oficina", "Escritura y Corrección", "Colores y Crayolas"),
     None, "Crayolas y Óleos"),
    ("CAJA TOUCH 36", ("Librería y Oficina", "Escritura y Corrección", "Colores y Crayolas"), None, "Plumones Escolares"),
    ("SET RESALTADOR PASTEL X6", ("Librería y Oficina", "Escritura y Corrección", "Plumones y Marcadores"),
     None, "Resaltadores"),
    ("BOLIG FAB 035-F TRILUX AZUL", ("Librería y Oficina", "Escritura y Corrección", "Lápices y Lapiceros"),
     None, "Lapiceros"),
])
def test_propuestas_conocidas(name, v1_path, type_name, expected_sub):
    prop = propose(name, type_name, _v2(v1_path))
    assert prop is not None and prop[0][2] == expected_sub, prop


@pytest.mark.parametrize("name, expected_sub", [
    ("JARRA D/ VIDRIO GRANDE DIAMANTE", "Jarras de Vidrio"),
    ("JARRA PLASTICA 2L", "Jarras de Plástico"),
    ("MINI FRASCO D/ VIDRIO MODELO CALABAZA CON TAPA", "Frascos y Tarros"),
    ("SOMBRILLA MEDIANO COLORES ENTEROS 55CM", "Paraguas"),
    ("SOMBRILLA DE PLAYA 1.8M", "Toldos y Sombrillas"),
])
def test_productos_nuevos_en_tipo_generico(name, expected_sub):
    prop = propose(name, "nuevo", None)
    assert prop is not None and prop[0][2] == expected_sub, prop


@pytest.mark.parametrize("name, v1_path", [
    ("Galleta Costa D Chips Chocolate 6x34Gr", ("Alimentos Nacionales", "Galletas y Panadería", "Galletas")),
    ("Galleta Field D Chocosoda 6x36Gr.", ("Alimentos Nacionales", "Galletas y Panadería", "Galletas")),
    ("JGT MOCHILA MASCOTA", ("Juguetes y Entretenimiento", "Juego de Roles", "Mascotas")),
    ("PELUCHE CAPIBARA", ("Tendencias Kawaii", "Peluches", "Peluches Kawaii")),
    ("GANCHO X12 ROPA NAYRA", ("Hogar y Decoración", "Organización", "Ganchos de Ropa")),
    ("SILICONA ARTESCO LIQUIDA 250 ml (CAJONX72)", ("Librería y Oficina", "Oficina", "Pegamentos y Siliconas")),
    ("FOCO LED 20W", ("Electrohogar", "Iluminación y Eléctricos", "Luces y Focos")),
    ("PISTOLA CHISQUETE", ("Juguetes y Entretenimiento", "Juegos al Aire Libre", "Hidrogel y Pistolas de Agua")),
    ("BYWIN COLONIA X 250ML FURBY LT.", ("Belleza", "Perfumería", "Perfumes Infantiles")),
    ("ME TOO KING MILLION EDT 50ML", ("Belleza", "Perfumería", "Perfumes de Hombre")),
    ("Tomatodo pequeño con cinturón", ("Hogar y Decoración", "Cocina", "Tomatodos y Botellas")),
])
def test_lo_bien_clasificado_no_se_mueve(name, v1_path):
    assert propose(name, "x", _v2(v1_path)) is None


# ── Inferencia de atributos ──────────────────────────────────────────────────


def _vals(name, **kw):
    return {k: (v.value_text if v.value_text is not None else v.value_num)
            for k, v in infer_product_attributes(name, **kw).items()}


def test_inferencia_bywin_colonia():
    out = _vals("BYWIN COLONIA X 250ML MARVEL COMICS LT.", department="Belleza",
                category="Perfumería", subcategory="Perfumes Infantiles")
    assert out["marca"] == "Bywin"
    assert out["licencia"] == "Marvel"
    assert out["publico"] == "Infantil"
    assert out["contenido"] == 250.0


def test_origen_desde_clasificacion_y_codigo_de_barras():
    asia = _vals("Bebida con Nata de Coco Lychee Baolisu 450 ML",
                 department="Alimentos y Bebidas Asiáticas", subcategory="Jugo y Nata de Coco")
    assert asia["origen"] == "Asiático"
    peru = _vals("Galleta Costa D Chips Chocolate 6x34Gr", department="Alimentos y Bebidas",
                 bar_code="7750885022316")
    assert peru["origen"] == "Nacional"
    # Fuera de alimentos no se infiere origen.
    assert "origen" not in _vals("TAPER VIDRIO", department="Hogar y Decoración", bar_code="6974766060438")


def test_material_desde_subcategoria_que_desaparece():
    assert _vals("TAPER DE VIDRIO CIRCULAR MEDIANO 1100 ML", department="Hogar y Decoración",
                 subcategory="Tapers de Vidrio")["material"] == "Vidrio"
    assert _vals("JARRA BONITA 2.5LT + 1 VASO", department="Hogar y Decoración",
                 subcategory="Jarras de Plástico")["material"] == "Plástico"


@pytest.mark.parametrize("name, contenido, pack, caja", [
    ("Galleta Costa D Vainilla 6x32Gr(CJAX20)", (32.0, "g"), 6.0, 20.0),
    ("COLORES ARTESCO X 12 LARGOS (CAJONX192)", None, 12.0, 192.0),
    ("DELINEADOR LIQUIDO SAMANTHA X 24 PCS (CJA X 36 SET)", None, 24.0, 36.0),
    ("A4 IQ ZAPATILLA X 30 PZS X 18.50", None, None, 30.0),
    ("ME TOO VARIADO HOMBRE EDT 24UNDX20ML", (20.0, "ml"), 24.0, None),
    ("COLET GEMA X1 MAYOR X3", None, 3.0, None),
    ("ALFOMBRA 80*150MT", None, None, None),
    ("FORRO VINFAN T/A4 X 5 mts", None, None, None),
    ("Limpiatodo Queen GALON", (3.785, "L"), None, None),
])
def test_numeros_del_nombre(name, contenido, pack, caja):
    c = parse_contenido(name)
    assert (c.value_num, c.unit) == contenido if contenido else c is None
    p = parse_unidades_pack(name)
    assert (p.value_num if p else None) == pack
    k = parse_unidades_caja(name)
    assert (k.value_num if k else None) == caja


def test_costo_y_prefijo_en_el_nombre():
    assert name_issues("A4 IQ ZAPATILLA X 30 PZS X 18.50") == {
        "costo_en_nombre": "18.50", "prefijo_logistico": "A4 IQ"}
    assert name_issues("N-B7/DOU/JTSZ-150 ARBOL DE NAVIDAD")["prefijo_logistico"] == "N-B7/DOU/"
    assert name_issues("JARRA BONITA 1LT") == {}


def test_atributos_de_variante():
    out = infer_variant_attributes("ZAPATILLA DAMA TALLA 35-39", None, department="Calzado")
    assert out["talla"].value_text == "35-39"
    out = infer_variant_attributes("Bebida Lychee Baolisu", "Sabor Mango", department="Alimentos y Bebidas")
    assert out["sabor_aroma"].value_text == "Mango"  # la variante manda sobre el nombre


def test_group_key_une_variaciones_de_escritura():
    assert group_key("JARRA BONITA 2.5L +1 VASO") == group_key("JARRA BONITA 2.5LT + 1 VASO")
    assert group_key("JARRA BONITA 4LT + 1 VASO") == group_key("JARRA BONITA 4L +1 VASO")


# ── Driver Excel ─────────────────────────────────────────────────────────────


def test_plantilla_se_lee_con_el_importador():
    content = build_driver_template(tipos_from_tree(V2))
    rows, cols = parse_driver(content)
    assert rows == []  # la fila de ayuda no tiene SKU
    assert {"sku", "proveedor", "marca", "tipo_bsale", "color", "unidades_caja"} <= set(cols)


def test_parse_driver_con_encabezado_desplazado_y_codigos_float():
    wb = Workbook()
    ws = wb.active
    ws.append(["DRIVER DE COMPRAS JULIO"])
    ws.append([])
    ws.append(["FECHA", "SKU", "NOMBRE PRODUCTO", "PROVEDOR", "CODIGO DE BARRA", "CANTIDAD", "CAJAS", "C/U", "MARCA"])
    ws.append(["2026-07-01", "AR16415101", "ACUARELAS ARTESCO X12", "Artesco SAC", 7750082136342.0, 144, 1, 3.5, "artesco"])
    ws.append([None, None, "fila vacía", None, None])
    buf = io.BytesIO()
    wb.save(buf)
    rows, cols = parse_driver(buf.getvalue())
    assert len(rows) == 1
    assert rows[0].row_number == 4
    assert _clean_code(rows[0].values["bar_code"]) == "7750082136342"
    assert rows[0].values["proveedor"] == "Artesco SAC"


def test_canonical_enum():
    assert _canonical("origen", "asiatico") == ("Asiático", None)
    val, warn = _canonical("origen", "chino")
    assert val is None and "no es un valor válido" in warn
    assert _canonical("marca", "  Artesco  ") == ("Artesco", None)
