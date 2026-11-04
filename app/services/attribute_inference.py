"""Inferencia de atributos a partir del nombre del producto, su clasificación
actual y su código de barras.

Funciones puras (sin BD) para poder testearlas con los nombres reales del
catálogo. Todo lo que devuelven se guarda con `source='inferred'`, que nunca
pisa un valor cargado por driver o a mano (ver attribute_catalog.can_overwrite).

Señales que se usan, de más a menos confiable:
  1. El nombre: "BYWIN COLONIA X 250ML MARVEL" → marca Bywin, licencia Marvel,
     contenido 250 ml.
  2. La clasificación ACTUAL (antes de migrar a la taxonomía v2): "Perfumes de
     Hombre" → público Caballero; "Alimentos y Bebidas Asiáticas" → origen
     Asiático. Por eso la inferencia se corre ANTES de migrar la taxonomía: la
     v2 junta esos tipos y la información pasa a vivir en el atributo.
  3. El prefijo GS1 del código de barras (solo alimentos): 775 = Perú.

También detecta problemas del nombre que conviene corregir en BSale (costo,
ubicación o proveedor metidos en el nombre), vía `name_issues`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Inferred:
    value_text: str | None
    value_num: float | None = None
    unit: str | None = None
    confidence: float = 0.8
    reason: str = ""


def norm(s: str | None) -> str:
    """Mayúsculas, sin tildes ni ñ, espacios colapsados. 'Niña  Bonita' → 'NINA BONITA'."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s.upper()).strip()


@lru_cache(maxsize=None)
def _word_re(alternatives: tuple[str, ...]) -> re.Pattern:
    alts = sorted({re.escape(norm(a)) for a in alternatives}, key=len, reverse=True)
    return re.compile(r"(?<![A-Z0-9])(" + "|".join(alts) + r")(?![A-Z0-9])")


def _first_match(table: list[tuple[str, list[str]]], text: str) -> tuple[str, str] | None:
    """Devuelve (valor canónico, alias encontrado) de la primera coincidencia,
    priorizando el alias más largo (MY MELODY antes que MELODY)."""
    best: tuple[int, str, str] | None = None
    for canonical, aliases in table:
        m = _word_re(tuple(aliases)).search(text)
        if m:
            cand = (len(m.group(1)), canonical, m.group(1))
            if best is None or cand[0] > best[0]:
                best = cand
    return (best[1], best[2]) if best else None


# ---------------------------------------------------------------------------
# Diccionarios (valor canónico, alias). Ampliables sin tocar la lógica.
# ---------------------------------------------------------------------------

MARCAS: list[tuple[str, list[str]]] = [
    # Librería / oficina
    ("Artesco", ["ARTESCO"]), ("Layconsa", ["LAYCONSA"]), ("Faber-Castell", ["FABER CASTELL", "FABER", "FAB"]),
    ("Vinifan", ["VINIFAN", "VINFAN", "VINIFORRO"]), ("Mercyfan", ["MERCYFAN", "MERCYFAND", "MERCY AND", "MERCIFAN", "MERCY"]),
    ("Nayra", ["NAYRA"]), ("Yimi", ["YIMI"]), ("Surco", ["SURCO"]), ("Justus", ["JUSTUS", "JUSTOS"]),
    ("Stanford", ["STANFORD"]), ("Apolo", ["APOLO"]), ("Alpha", ["ALPHA"]), ("Pegafan", ["PEGAFAN"]),
    ("Ecofan", ["ECOFAN"]), ("Shurtape", ["SHURTAPE"]), ("Twins", ["TWINS"]), ("Oxford", ["OXFORD"]),
    ("Chamequito", ["CHAMEQUITO"]), ("Scool", ["SCOOL"]), ("Kayros", ["KAYROS"]), ("Bikingo", ["BIKINGO"]),
    ("Crayola", ["CRAYOLA"]), ("Millenium", ["MILLENIUM"]), ("Onix", ["ONIX"]),
    # Belleza / cuidado personal
    ("Samantha", ["SAMANTHA", "SAMATHA", "SMT"]), ("Ever Beauty", ["EVER BEAUTY", "EVER BEA4TY"]),
    ("P&W", ["P&W", "PYW"]), ("Jarusa", ["JARUSA"]), ("Revel", ["REVEL"]), ("Bioaqua", ["BIOAQUA", "BOIAQUA"]),
    ("Flower Secret", ["FLOWER SECRET"]), ("Love Jojo", ["LOVE JOJO"]), ("Sadoer", ["SADOER"]),
    ("Dr. La Rosa", ["DR. LA ROSA", "DR LA ROSA", "DRA. LA ROSA", "DR-LA ROSA", "DR.LA ROSA"]),
    ("Niña Bonita", ["NINA BONITA"]), ("Aclarat", ["ACLARAT", "ACLARA-T", "ACLAR-T"]), ("Dermasol", ["DERMASOL"]),
    ("Dermoacne", ["DERMOACNE"]), ("La Cooper", ["LA COOPER"]), ("Daya", ["DAYA"]), ("Madison", ["MADISON"]),
    ("Me Too", ["ME TOO"]), ("Sky", ["SKY"]), ("Prestige", ["PRESTIGE"]), ("Califa Garden", ["CALIFA GARDEN"]),
    ("Nox", ["NOX"]), ("Barcelona", ["BARCELONA"]), ("Bywin", ["BYWIN", "BTWIN"]), ("Gotokids", ["GOTOKIDS"]),
    ("Nutribela", ["NUTRIBELA"]), ("Amaras", ["AMARAS"]), ("Tío Nacho", ["TIO NACHO"]), ("Ballerina", ["BALLERINA"]),
    ("Q'Fresh", ["Q'FRESH", "QFRESH"]), ("Lea", ["LEA"]), ("Barber Style", ["BARBER STYLE"]), ("Super Fix", ["SUPER FIX"]),
    ("Mansly", ["MANSLY", "MASLY"]), ("Mini Tango", ["MINI TANGO"]), ("Tieesiee", ["TIEESIEE"]), ("Italia", ["ITALIA"]),
    ("Handayan", ["HANDAYAN"]), ("Letelier", ["LETELIER"]), ("Petti", ["PETTI"]), ("One Care", ["ONE CARE"]),
    ("Listerine", ["LISTERINE"]), ("Kotex", ["KOTEX"]), ("Pond's", ["PONDS"]), ("Dexe", ["DEXE"]), ("Bomn", ["BOMN"]),
    ("Aqua", ["AQUA"]), ("Mathfarm", ["MATHFARM"]), ("Portugal", ["PORTUGAL"]), ("Valet", ["VALET"]),
    ("Beauty Mirella", ["BEAUTY MIRELLA"]), ("Sweet Queen", ["SWEET QUEEN"]), ("Manfei", ["MANFEI"]),
    # Limpieza / hogar
    ("Eficaz", ["EFICAZ"]), ("Chemiclin", ["CHEMICLIN", "CHEMI"]), ("Queen", ["QUEEN"]), ("Sapolio", ["SAPOLIO"]),
    ("Patito", ["PATITO"]), ("Trome", ["TROME"]), ("Aladino", ["ALADINO"]), ("Topaz", ["TOPAZ"]), ("Drive", ["DRIVE"]),
    ("Bay-Clin", ["BAY-CLIN", "BAY-CIN", "BAY CLIN"]), ("Poett", ["POETT"]), ("Clorox", ["CLOROX"]),
    ("Zacaly Clean", ["ZACALY CLEAN"]), ("Xtra Clean", ["XTRA CLEAN", "XTRA CLEA"]), ("Bruxss Car", ["BRUXSS CAR"]),
    ("Popeye", ["POPEYE"]), ("San Isidro", ["SAN ISIDRO"]), ("Neko", ["NEKO"]), ("Rosas y Limón", ["ROSAS Y LIMON"]),
    ("Aval", ["AVAL"]), ("Paracas", ["PARACAS"]), ("Suave", ["SUAVE"]), ("Huayu", ["HUAYU"]), ("Sunny", ["SUNNY"]),
    ("Kawii", ["KAWII"]), ("Bonita", ["BONITA"]), ("Utilísima", ["UTILISIMA"]), ("Mistura", ["MISTURA"]),
    ("Vinsa", ["VINSA"]), ("Force Cat", ["FORCE CAT"]), ("Sweet Can", ["SWEET CAN"]), ("Sweet Cat", ["SWEET CAT"]),
    ("Inti", ["INTI"]), ("Raf", ["RAF"]), ("Ufei", ["UFEI"]), ("Stanley", ["STANLEY"]), ("O'Kitchen", ["O'KITCHEN"]),
    # Alimentos
    ("Costa", ["COSTA"]), ("Field", ["FIELD"]), ("GN", ["GN"]), ("Ambrosoli", ["AMBROSOLI", "AMBROSSOLI"]),
    ("Colombina", ["COLOMBINA", "COLOMBINETAS"]), ("Trululu", ["TRULULU"]), ("Bon Bon Bum", ["BON BON BUM", "BON BON BUN"]),
    ("Grissly", ["GRISSLY"]), ("Millows", ["MILLOWS"]), ("Goloperú", ["GOLOPERU"]), ("Confiperú", ["CONFIPERU"]),
    ("Mentos", ["MENTOS"]), ("Big Ben", ["BIG BEN"]), ("Rikytok", ["RIKYTOK"]), ("Tiyapuy", ["TIYAPUY"]),
    ("Baolisu", ["BAOLISU", "BAOLISA"]), ("Tung Shaing", ["TUNG SHAING", "TUNG SHAIN"]), ("Bostar", ["BOSTAR"]),
    ("Copon", ["COPON"]), ("Muscafe", ["MUSCAFE"]), ("Hengyu", ["HENGYU", "HENGY"]), ("Biaoweizu", ["BIAOWEIZU", "BIAOWEI"]),
    ("Kacci", ["KACCI"]), ("Sable", ["SABLE"]), ("Joju", ["JOJU"]), ("Rio", ["RIO"]), ("Daliyuan", ["DALIYUAN"]),
    ("Hsufuchi", ["HSUFUCHI", "HSUFUC"]), ("Ronaya", ["RONAYA"]), ("Nanguo", ["NANGUO"]), ("Reyenito", ["REYENITO"]),
    ("Vizzio", ["VIZZIO"]), ("Best Food", ["BEST FOOD"]), ("Don Geyser", ["DON GEYSER"]),
]

LICENCIAS: list[tuple[str, list[str]]] = [
    ("Hello Kitty", ["HELLO KITTY", "HELLO KITY", "HELLO KITT"]), ("Kuromi", ["KUROMI"]),
    ("Cinnamoroll", ["CINNAMOROLL", "CINAMOROLL", "CINNAMOI", "CINNAMOR"]), ("My Melody", ["MY MELODY"]),
    ("Pochacco", ["POCHACCO", "POCHACCC"]), ("Pompompurin", ["POMPOMPURIN", "POMPOMPL"]),
    ("Labubu", ["LABUBU"]), ("Capibara", ["CAPIBARA", "CAPYBARA", "CABIRA"]), ("Stitch", ["STITCH", "STICH"]),
    ("Mickey / Minnie", ["MICKEY", "MICKY", "MINNIE", "MINIE"]), ("Disney Princesas", ["PRINCESAS", "PRINCESA DISNEY"]),
    ("Frozen", ["FROZEN"]), ("Toy Story", ["TOY STORY"]), ("Monsters Inc", ["MONSTER, INC.", "MONSTER INC", "MONSTER"]),
    ("Tinkerbell", ["TINKERBELL"]), ("Disney", ["DISNEY", "DC BAMBI", "BAMBI", "PINOCCHIO", "DALMATIANS", "MOANA", "VILLAINS", "PATO DONALD"]),
    ("Marvel", ["MARVEL", "SPIDERMAN", "VENOM", "DEADPOOL", "XMEN", "X MEN", "GROOT", "AVENGERS", "AVERGENS"]),
    ("DC Comics", ["DC COMICS", "BATMAN", "SUPERMAN", "WONDER WOMAN", "JOKER"]),
    ("Paw Patrol", ["PAW PATROL", "PAW PATRO"]), ("Sonic", ["SONIC"]), ("Dragon Ball", ["DRAGON BALL"]),
    ("Minecraft", ["MINECRAFT"]), ("Free Fire", ["FREE FIRE"]), ("Demon Slayer", ["DEMON SLAYER"]),
    ("Jurassic", ["JURASSIC"]), ("Pocoyo", ["POCOYO"]), ("Granja de Zenón", ["GRANJA DE ZENON", "GRANJA DE ZEN", "GRANJA DE ZENC"]),
    ("Bartolito", ["BARTOLITO"]), ("Cry Babies", ["CRY BABIES"]), ("LOL", ["LOL"]), ("Peppa Pig", ["PEPPA PIG", "PEPPA", "PEPA"]),
    ("Masha y el Oso", ["MASHA"]), ("Dora", ["DORA LA EXPLORADORA"]), ("Power Rangers", ["POWER RANGERS"]),
    ("Tortugas Ninja", ["TORTUGAS NINJA"]), ("Furby", ["FURBY"]), ("My Little Pony", ["MY LITTLE PONY"]),
    ("Play-Doh", ["PLAY DOH"]), ("Mr. Potato", ["MR. POTATO", "MR POTATO"]), ("Rugrats", ["RUGRATS"]),
    ("Los Increíbles", ["LOS INCREIBLES"]), ("Garfield", ["GARFIELD"]), ("Super Kitties", ["SUPER KITTIES"]),
    ("Universitario", ["UNIVERSITARIO"]), ("Alianza Lima", ["ALIANZA LIMA", "ALIANZA"]), ("Sporting Cristal", ["SPORTING CRISTAL"]),
    ("Frida Kahlo", ["FRIDA KAHLO"]), ("Looloo", ["LOOLOO"]), ("Italian Brainrot", ["BRAIROT", "BRAINROT", "TRALALELOS", "TRALALELO"]),
    ("K-Pop", ["KPOP", "K POP"]), ("Grinch", ["GRINCH"]),
]

PUBLICO_NOMBRE: list[tuple[str, list[str]]] = [
    ("Dama", ["DAMA", "DAMAS", "MUJER", "MUJERES"]),
    ("Caballero", ["CABALLERO", "CABALLEROS", "HOMBRE", "HOMBRES", "VARON"]),
    ("Niña", ["NINA", "NINAS"]),
    ("Niño", ["NINO"]),
    ("Infantil", ["NINOS", "NINO-NINA", "KIDS", "INFANTIL", "INFANTILES"]),
    ("Bebé", ["BEBE", "BEBES", "BABY"]),
    ("Adulto", ["ADULTO", "ADULTOS"]),
    ("Mascota", ["MASCOTA", "MASCOTAS"]),
]

# Clasificación actual (v1) → público. Clave = subcategoría o categoría normalizada.
PUBLICO_TAXONOMIA: dict[str, str] = {
    "PERFUMES DE HOMBRE": "Caballero", "PERFUMES DE MUJER": "Dama", "PERFUMES INFANTILES": "Infantil",
    "ROPA DE MUJER": "Dama", "PIJAMAS DE MUJER": "Dama", "ROPA DE NINAS": "Niña", "ROPA INFANTIL": "Infantil",
    "MEDIAS INFANTILES": "Infantil", "GUANTES DE ADULTOS": "Adulto", "SANDALIAS DE NINOS": "Infantil",
    "SANDALIAS DE ADULTO": "Adulto", "ZAPATILLAS DE NINOS": "Infantil", "SHAMPOO INFANTILES": "Infantil",
}
PUBLICO_DEPARTAMENTO: dict[str, str] = {"MASCOTAS": "Mascota", "BEBES E INFANTIL": "Bebé"}

ESTILOS: list[tuple[str, list[str]]] = [
    ("Kawaii", ["KAWAII", "KAWAI", "KAWII"]),
    ("Premium", ["PREMIUM", "PREMIUN"]),
]

TEMPORADA_NOMBRE: list[tuple[str, list[str]]] = [
    ("Navidad", ["NAVIDAD", "NAVIDENO", "NAVIDENA", "NAVIDENAS", "NAVIDENOS", "PAPA NOEL", "GRINCH", "PATI LARGA", "PATI LARGAS", "PATILARGA"]),
    ("Halloween", ["HALLOWEEN", "HAWOLLEN", "CALABAZA", "BRUJA", "CHUCKY"]),
    ("Día de la Madre", ["DIA DE LA MADRE", "MAMA"]),
    ("Día del Padre", ["DIA DEL PADRE", "PAPA", "FELIZ DIA PAPA"]),
    ("Escolar", ["SCOOL", "ESCOLAR", "LONCHERA", "CARTUCHERA"]),
    ("Invierno", ["CHULLO", "BUFANDA", "OREJERA", "POLAR", "CARNERO", "PARCA", "FRAZADA", "CALENTADOR", "INVIERNO"]),
    ("Verano", ["PISCINA", "VERANO", "SOMBRILLA", "TOLDO", "CHISQUETE", "INFLABLE", "PROTECTOR SOLAR", "SPF"]),
]
TEMPORADA_TAXONOMIA: dict[str, str] = {
    "NAVIDAD": "Navidad", "HALLOWEEN": "Halloween", "REGALOS DIA DE LA MADRE": "Día de la Madre",
    "REGALOS DIA DEL PADRE": "Día del Padre", "ESCOLAR": "Escolar", "ACCESORIOS DE ABRIGO": "Invierno",
    "ACCESORIOS DE INVIERNO": "Invierno", "PISCINAS Y JUEGOS DE PLAYA": "Verano",
}

MATERIAL_NOMBRE: list[tuple[str, list[str]]] = [
    ("Vidrio", ["VIDRIO", "CRISTAL"]), ("Cerámica", ["CERAMICA", "CERAMICO", "PORCELANA", "GRES"]),
    ("Loza", ["LOZA"]), ("Acero", ["ACERO", "INOXIDABLE"]), ("Aluminio", ["ALUMINIO"]),
    ("Metal", ["METAL", "METALICO", "METALICA"]), ("Madera", ["MADERA"]), ("Bambú", ["BAMBU"]),
    ("Plástico", ["PLASTICO", "PLASTICA", "PLASTIFICADO", "PLASTIFICADA", "PLAST", "PVC"]),
    ("Tela", ["TELA", "TEXTIL"]), ("Cuero", ["CUERO"]), ("Peluche", ["PELUCHE", "FELPA"]),
]
MATERIAL_TAXONOMIA: dict[str, str] = {
    "TAPERS DE VIDRIO": "Vidrio", "JARRAS DE VIDRIO": "Vidrio", "JARRAS DE PLASTICO": "Plástico",
    "TAZAS DE CERAMICA": "Cerámica",
}
# El material solo tiene sentido para artículos de hogar/cocina/organización.
DEPTOS_CON_MATERIAL = {"HOGAR Y DECORACION", "COCINA Y COMEDOR", "TEMPORADA Y CELEBRACIONES", "TEMPORADA", "MODA Y ACCESORIOS"}

COLORES: list[tuple[str, list[str]]] = [
    ("Rojo", ["ROJO", "ROJA", "ROJO VINO"]), ("Azul", ["AZUL"]), ("Verde", ["VERDE"]), ("Amarillo", ["AMARILLO"]),
    ("Negro", ["NEGRO", "NEGRA"]), ("Blanco", ["BLANCO", "BLANCA"]), ("Rosado", ["ROSADO", "ROSADA", "ROSA PASTEL"]),
    ("Morado", ["MORADO", "MORADA", "LILA"]), ("Celeste", ["CELESTE"]), ("Gris", ["GRIS"]), ("Dorado", ["DORADO", "DORADA", "GOLD"]),
    ("Plateado", ["PLATEADO", "PLATEADA", "SILVER"]), ("Marrón", ["MARRON"]), ("Beige", ["BEIGE", "HUESO"]),
    ("Transparente", ["TRANSPARENTE", "TRANSP", "TRAMSPARENTE"]), ("Fucsia", ["FUCSIA"]), ("Turquesa", ["TURQUESA"]),
    ("Pastel", ["PASTEL", "PASTELES"]), ("Caramelo", ["CARAMELO"]), ("Multicolor", ["MULTICOLOR", "ARCOIRIS", "ARCO IRIS"]),
]
SABORES: list[tuple[str, list[str]]] = [
    ("Fresa", ["FRESA", "FRESITA", "FRESAS"]), ("Chocolate", ["CHOCOLATE", "CHOCO", "CACAO"]), ("Vainilla", ["VAINILLA"]),
    ("Mango", ["MANGO"]), ("Uva", ["UVA", "UVA MORADA", "UVA VERDE"]), ("Durazno", ["DURAZNO", "DURAZ"]), ("Piña", ["PINA"]),
    ("Sandía", ["SANDIA"]), ("Lychee", ["LYCHEE", "LICHEE", "LITCHI"]), ("Maracuyá", ["MARACUYA", "MARACULLA"]),
    ("Coco", ["COCO"]), ("Limón", ["LIMON"]), ("Naranja", ["NARANJA", "NARAN"]), ("Manzana", ["MANZANA"]),
    ("Cereza", ["CEREZA", "CHERRY"]), ("Arándano", ["ARANDANO", "ARANDANOS", "BLUEBERRY"]), ("Menta", ["MENTA"]),
    ("Lavanda", ["LAVANDA"]), ("Floral", ["FLORAL"]), ("Bebé", ["BEBE"]), ("Pino", ["PINO"]), ("Cítrico", ["CITRICO", "CITRICA"]),
    ("Taro", ["TARO"]), ("Matcha", ["MATCHA"]), ("Café", ["CAFE"]), ("Lúcuma", ["LUCUMA"]), ("Kiwi", ["KIWI"]),
    ("Granada", ["GRANADA"]), ("Frutos Rojos", ["FRUTOS ROJOS"]), ("Canela", ["CANELA"]), ("Rosas", ["ROSAS"]),
    ("Picante", ["PICANTE"]), ("Natural", ["NATURAL", "ORIGINAL"]),
]
DEPTOS_CON_SABOR = {
    "ALIMENTOS NACIONALES", "ALIMENTOS Y BEBIDAS ASIATICAS", "ALIMENTOS Y BEBIDAS", "LIMPIEZA",
    "BELLEZA", "CUIDADO PERSONAL Y SALUD",
}
DEPTOS_ALIMENTOS = {"ALIMENTOS NACIONALES", "ALIMENTOS Y BEBIDAS ASIATICAS", "ALIMENTOS Y BEBIDAS"}

# Prefijos GS1 de países asiáticos (China 690-699, Corea 880, Taiwán 471,
# Hong Kong 489, Tailandia 885, Vietnam 893, Indonesia 899, Malasia 955, Japón 45/49).
_GS1_ASIA = ("69", "880", "471", "489", "885", "893", "899", "955", "45", "49")


# ---------------------------------------------------------------------------
# Regex numéricas
# ---------------------------------------------------------------------------

_UNIT_MAP = {
    "ML": "ml", "L": "L", "LT": "L", "LTS": "L", "LITRO": "L", "LITROS": "L",
    "G": "g", "GR": "g", "GRS": "g", "GRAMOS": "g", "KG": "kg", "KGS": "kg", "OZ": "oz", "ONZ": "oz", "ONZAS": "oz",
}
_RE_CONTENIDO = re.compile(
    r"(?<![A-WYZ0-9.])(\d+(?:[.,]\d+)?)\s*(ML|LTS|LT|LITROS|LITRO|L|GRAMOS|GRS|GR|G|KGS|KG|ONZAS|ONZ|OZ)(?![A-Z0-9])"
)
_RE_GALON = re.compile(r"(?<![A-Z])(GALON|GL\.)(?![A-Z])")

# Empaque del proveedor, por prioridad: "(CJAX24)", "(PQTX12)", "(X6)",
# "(CAJA X 12 UNID)", "(2C X60)", "(CAJONX144)" > "CJA X24", "BOX X24" >
# "X 48 PZS", "X 48 DOC".
_RE_CAJA_PAREN = re.compile(
    r"\(\s*(?:\d+\s*C\s*)?(?:CJA|CAJA|CAJON|CJ|PQT|PQ|PAQ|BLS|FCO|SET|C)?\s*X\s*(\d+)[^)]*\)"
)
_RE_CAJA_WORD = re.compile(r"(?:CJA|CAJA|CAJON|BOX|PQT)\s*X\s*(\d+)")
_RE_CAJA_PZS = re.compile(r"X\s*(\d+)\s*(?:PZS|PZAS|PCS|PIEZAS|DOC|DOCENAS?)(?![A-Z])")
# Pack de venta: "6x33Gr", "2x10", "X12", "X 100 UND", "X3 MAYOR", "24UNDX20ML".
_RE_PACK_MULT = re.compile(
    r"(?<![A-Z0-9.])(\d{1,3})\s*X\s*\d+(?:[.,]\d+)?(?!\s*(?:CM|MT|MTS|MM|M)(?![A-Z]))"
)
_RE_PACK_X = re.compile(
    r"(?<![A-Z])X\s?(\d{1,4})(?![\d.,])(?!\s*(?:ML|LT|L|LTS|GR|G|GRS|KG|CM|MM|MT|MTS|M|OZ|W|V|HOJ|#)\b)"
)
_RE_PACK_UND = re.compile(r"(\d{1,4})\s*(?:UND|UNDS|UNID|UNIDADES|PZ|PZA|PZAS)(?![A-WYZ])")

# Costo pegado al nombre: "X 30 PZS X 18.50", "#30 PZS X 9#", "X 4 PZS X 115.00".
_RE_COSTO = re.compile(r"(?:PZS?|PZAS|SET)\s*X\s*(\d+(?:[.,]\d{1,2}))")
# Prefijo logístico al inicio: "A4 IQ ", "B7 2 IQ ", "N-B7/DOU/", "HC-C10/D/", "A7 A-1 B-1 A4 IQ ".
_RE_PREFIJO = re.compile(
    r"^((?:(?:[A-C]\d{1,2}|[A-C]-\d|\d{1,2}N?|N)\s+)+(?:IQ|IZ)\b"
    r"|(?:N-|HC-)[A-Z]\d{1,2}/[A-Z]{1,3}/)"
)
_RE_TALLA = re.compile(r"TALLA\s*([0-9]{2}(?:\s*-\s*[0-9]{2})?|XXL|XL|XS|S|M|L)(?![A-Z0-9])")


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def parse_contenido(name: str) -> Inferred | None:
    n = norm(name)
    m = _RE_CONTENIDO.search(n)
    if m:
        unit = _UNIT_MAP[m.group(2)]
        return Inferred(None, _to_float(m.group(1)), unit, 0.9, f"'{m.group(0)}' en el nombre")
    if _RE_GALON.search(n):
        return Inferred(None, 3.785, "L", 0.6, "'galón' en el nombre")
    return None


def _caja_match(n: str) -> re.Match | None:
    for rx in (_RE_CAJA_PAREN, _RE_CAJA_WORD, _RE_CAJA_PZS):
        for m in rx.finditer(n):
            if int(m.group(1)) > 1:
                return m
    return None


def parse_unidades_caja(name: str) -> Inferred | None:
    m = _caja_match(norm(name))
    if not m:
        return None
    return Inferred(None, float(m.group(1)), "und", 0.8, f"'{m.group(0).strip()}' en el nombre")


def parse_unidades_pack(name: str) -> Inferred | None:
    """Unidades que se venden juntas. Se quita antes el empaque del proveedor
    para no confundir '(CJAX24)' con un pack de 24."""
    n = norm(name)
    m = _caja_match(n)
    if m:
        n = n[:m.start()] + " " + n[m.end():]
    n = _RE_COSTO.sub(" ", n)
    for rx, conf in ((_RE_PACK_MULT, 0.8), (_RE_PACK_UND, 0.8), (_RE_PACK_X, 0.7)):
        for m in rx.finditer(n):
            if int(m.group(1)) > 1:
                return Inferred(None, float(m.group(1)), "und", conf, f"'{m.group(0).strip()}' en el nombre")
    return None


def name_issues(name: str) -> dict[str, str]:
    """Problemas del nombre que conviene corregir en BSale."""
    n = norm(name)
    issues: dict[str, str] = {}
    m = _RE_COSTO.search(n)
    if m:
        issues["costo_en_nombre"] = m.group(1)
    m = _RE_PREFIJO.search(n)
    if m:
        issues["prefijo_logistico"] = m.group(0).strip()
    return issues


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------

def infer_product_attributes(
    name: str,
    *,
    department: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    bar_code: str | None = None,
) -> dict[str, Inferred]:
    """Atributos de nivel producto inferibles. Solo devuelve lo que encuentra."""
    n = norm(name)
    dep, cat, sub = norm(department), norm(category), norm(subcategory)
    out: dict[str, Inferred] = {}

    hit = _first_match(MARCAS, n)
    if hit:
        out["marca"] = Inferred(hit[0], confidence=0.85, reason=f"'{hit[1]}' en el nombre")

    hit = _first_match(LICENCIAS, n)
    if hit:
        out["licencia"] = Inferred(hit[0], confidence=0.85, reason=f"'{hit[1]}' en el nombre")

    # Público: nombre > subcategoría/categoría actual > departamento.
    hit = _first_match(PUBLICO_NOMBRE, n)
    if hit:
        out["publico"] = Inferred(hit[0], confidence=0.8, reason=f"'{hit[1]}' en el nombre")
    elif sub in PUBLICO_TAXONOMIA or cat in PUBLICO_TAXONOMIA:
        val = PUBLICO_TAXONOMIA.get(sub) or PUBLICO_TAXONOMIA[cat]
        out["publico"] = Inferred(val, confidence=0.5, reason=f"clasificación actual '{subcategory or category}'")
    elif dep in PUBLICO_DEPARTAMENTO:
        out["publico"] = Inferred(PUBLICO_DEPARTAMENTO[dep], confidence=0.6, reason=f"departamento '{department}'")

    # Origen: solo alimentos. Departamento actual > subcategoría "…Asiáticos" > GS1.
    if dep in DEPTOS_ALIMENTOS:
        if dep == "ALIMENTOS Y BEBIDAS ASIATICAS" or sub.endswith("ASIATICOS") or sub.endswith("ASIATICAS"):
            out["origen"] = Inferred("Asiático", confidence=0.8, reason=f"clasificación actual '{department}'")
        elif dep == "ALIMENTOS NACIONALES":
            out["origen"] = Inferred("Nacional", confidence=0.7, reason=f"clasificación actual '{department}'")
        code = re.sub(r"\D", "", bar_code or "")
        if len(code) in (8, 12, 13) and "origen" not in out:
            if code.startswith("775"):
                out["origen"] = Inferred("Nacional", confidence=0.7, reason="código de barras peruano (775)")
            elif code.startswith(_GS1_ASIA):
                out["origen"] = Inferred("Asiático", confidence=0.7, reason=f"código de barras asiático ({code[:3]})")
            elif code[:2] in ("76", "77", "78", "84", "30", "40", "50", "75"):
                out["origen"] = Inferred("Importado", confidence=0.6, reason=f"código de barras importado ({code[:3]})")

    hit = _first_match(ESTILOS, n)
    if hit:
        out["estilo"] = Inferred(hit[0], confidence=0.8, reason=f"'{hit[1]}' en el nombre")
    elif dep == "TENDENCIAS KAWAII":
        out["estilo"] = Inferred("Kawaii", confidence=0.7, reason="departamento 'Tendencias Kawaii'")

    hit = _first_match(TEMPORADA_NOMBRE, n)
    tax_temp = TEMPORADA_TAXONOMIA.get(sub) or TEMPORADA_TAXONOMIA.get(cat)
    if tax_temp:
        out["temporada"] = Inferred(tax_temp, confidence=0.8, reason=f"clasificación actual '{subcategory or category}'")
    elif hit:
        out["temporada"] = Inferred(hit[0], confidence=0.6, reason=f"'{hit[1]}' en el nombre")

    if sub in MATERIAL_TAXONOMIA:
        out["material"] = Inferred(MATERIAL_TAXONOMIA[sub], confidence=0.8, reason=f"clasificación actual '{subcategory}'")
    elif dep in DEPTOS_CON_MATERIAL:
        hit = _first_match(MATERIAL_NOMBRE, n)
        if hit:
            out["material"] = Inferred(hit[0], confidence=0.7, reason=f"'{hit[1]}' en el nombre")

    for code, fn in (("contenido", parse_contenido), ("unidades_caja", parse_unidades_caja),
                     ("unidades_pack", parse_unidades_pack)):
        val = fn(name)
        if val:
            out[code] = val

    return out


def infer_variant_attributes(
    product_name: str,
    variant_description: str | None = None,
    *,
    department: str | None = None,
) -> dict[str, Inferred]:
    """Color / talla / sabor-aroma. La descripción de la variante manda sobre
    el nombre del producto (es más específica)."""
    dep = norm(department)
    out: dict[str, Inferred] = {}
    for source_text, conf, where in ((variant_description, 0.85, "variante"), (product_name, 0.6, "nombre")):
        t = norm(source_text)
        if not t:
            continue
        if "talla" not in out:
            m = _RE_TALLA.search(t)
            if m:
                out["talla"] = Inferred(m.group(1).replace(" ", ""), confidence=conf, reason=f"'{m.group(0)}' en {where}")
        if dep in DEPTOS_CON_SABOR:
            if "sabor_aroma" not in out:
                hit = _first_match(SABORES, t)
                if hit:
                    out["sabor_aroma"] = Inferred(hit[0], confidence=conf, reason=f"'{hit[1]}' en {where}")
        elif "color" not in out:
            hit = _first_match(COLORES, t)
            if hit:
                out["color"] = Inferred(hit[0], confidence=conf, reason=f"'{hit[1]}' en {where}")
    return out


# Prefijos de SKU con los que el mismo artículo se cargó varias veces
# (ej. "JARRA BONITA 1LT" como PL-09, TED-8, KAW-9, JARK-001).
DUPLICATE_SKU_PREFIXES = ("PL-", "TED-", "KAW-", "JARK-", "PL-B")


def group_key(name: str) -> str:
    """Clave para agrupar el mismo artículo cargado con varios SKU."""
    n = norm(name)
    n = re.sub(r"\(.*?\)", " ", n)
    # "2.5LT" / "2.5 L" / "2.5LTS" → "2.5L"; "+1 VASO" / "+ 1 VASO" → "1 VASO".
    n = re.sub(r"(\d)\s*(LTS|LT|LITROS?|L)(?![A-Z])", r"\1L", n)
    n = re.sub(r"(\d)\s*(GRS|GR|G)(?![A-Z])", r"\1G", n)
    n = re.sub(r"[^A-Z0-9. ]", " ", n)
    return re.sub(r"\s+", "-", n.strip()).lower()
