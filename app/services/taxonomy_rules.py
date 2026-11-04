"""Reglas de reclasificación de productos hacia la taxonomía v2.

Producen PROPUESTAS (CSV para revisar), nunca cambios directos. Dos fuentes:

  1. TYPE_REMAP: tipos de producto de BSale cuyo nombre no existe en el árbol
     (typos, "nuevo", tipos inventados) → subcategoría v2. Sirve para mapear el
     tipo completo con POST /bsale/product-types/bulk-map.
  2. RULES: reglas por palabra clave sobre el nombre del producto, cada una con
     un alcance (solo se aplica si el producto está HOY en cierta parte del
     árbol) para no mover lo que ya está bien.

Las rutas son de la taxonomía v2 (Nueva_estructura/kawii-pluss-v2.json) y se
validan contra ella en los tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.attribute_inference import norm

Path = tuple[str, ...]

# ── Atajos de rutas v2 ──────────────────────────────────────────────────────
AYB, AYB_AS = "Alimentos Nacionales", "Alimentos y Bebidas Asiáticas"
CYC, HYD = "Cocina y Comedor", "Hogar y Decoración"
BEL, CPS, MODA = "Belleza", "Cuidado Personal y Salud", "Moda y Accesorios"
JUG, LIB, LIM, MASC = "Juguetes y Entretenimiento", "Librería y Oficina", "Limpieza", "Mascotas"
FJA, ELEC, TEMP, TEC = "Ferretería, Jardín y Automotriz", "Electrohogar", "Temporada y Celebraciones", "Tecnología"
CALZ, BEBE, FIES = "Calzado", "Bebé e Infantil", "Fiestas y Regalos"

M_CORREAS: Path = (MASC, "Accesorios y Juguetes", "Correas, Collares y Pecheras")
M_CAMAS: Path = (MASC, "Accesorios y Juguetes", "Camas, Jaulas y Transportadores")
M_JUGUETES: Path = (MASC, "Accesorios y Juguetes", "Juguetes para Mascotas")
M_HIGIENE: Path = (MASC, "Higiene y Salud de Mascotas", "Peines, Cepillos y Cortadoras")
AUTO_ACC: Path = (FJA, "Automotriz", "Accesorios de Auto")
AUTO_AMB: Path = (FJA, "Automotriz", "Ambientadores de Auto")
AUTO_SHAMPOO: Path = (FJA, "Automotriz", "Shampoo para Auto")
AUTO_SIL: Path = (FJA, "Automotriz", "Siliconas y Protectores")


def _lim(cat: str, sub: str) -> Path:
    return (LIM, cat, sub)


# ── 1. Tipos de BSale fuera del árbol → subcategoría v2 ─────────────────────
TYPE_REMAP: dict[str, Path] = {
    "ocina / Descartables de Cocina": (CYC, "Descartables", "Descartables de Cocina"),
    "Perfumería / Perfumes de Muje": (BEL, "Perfumería", "Perfumes de Mujer"),
    "Maquillaje de Rostro / Iluminadore": (BEL, "Maquillaje de Rostro", "Iluminadores"),
    "Tecnología / Audio / Audífonos": (TEC, "Audio", "Audífonos"),
    "Limpieza / Esponjas, Paños y Guantes": _lim("Accesorios de Limpieza", "Esponjas"),
    "Halloween / Calabazas": (TEMP, "Halloween", "Calabazas"),
    "Perfumería / Shampoo Infantiles": (CPS, "Cuidado del Cabello", "Shampoos"),
    "Comedor / Artículos de Bar": (CYC, "Mesa", "Artículos de Bar"),
    "Cocina / Moledor de especias": (CYC, "Utensilios de Cocina", "Moledores y Prensas"),
    "Cocina / Prensador": (CYC, "Utensilios de Cocina", "Moledores y Prensas"),
    "Cocina / Picador de Verduras": (CYC, "Utensilios de Cocina", "Utensilios Varios"),
    "Cocina / Manoplas": (CYC, "Utensilios de Cocina", "Mandiles y Manoplas"),
    "Cocina / Purificadores y Filtros de Agua": (CYC, "Utensilios de Cocina", "Utensilios Varios"),
    "Cocina / Palitos y Brocheta": (CYC, "Descartables", "Palitos y Brochetas"),
    "Accesorios de Vestir / Cinturones y Correas": (MODA, "Otros Accesorios", "Cinturones y Correas"),
    "Otros Accesorios / Correas y Cinturones": (MODA, "Otros Accesorios", "Cinturones y Correas"),
    "Jardín / Accesorios": (FJA, "Jardín", "Riego"),
    "Electrodomésticos del Hogar / Electrodomésticos Menores": (ELEC, "Electrodomésticos del Hogar", "Balanzas"),
    "Electrodomésticos del Hogar / Accesorios y Soportes": (ELEC, "Electrodomésticos del Hogar", "Accesorios de Electrodomésticos"),
    "Zapatillas / Zapatillas de Niños": (CALZ, "Zapatillas", "Zapatillas Urbanas"),
    "Accesorios de Mascota / Jaula y transportador": M_CAMAS,
    "Accesorios de Vehiculo / Forros y fundas": AUTO_ACC,
    "Figuras y Personajes / Robots y Transformables": (JUG, "Figuras y Personajes", "Robots y Transformables"),
}
_TYPE_REMAP_NORM = {norm(k): v for k, v in TYPE_REMAP.items()}

# Tipos que no dicen nada: el producto se clasifica solo por su nombre.
GENERIC_TYPES = {norm("Sin Tipo"), norm("nuevo"), ""}


def remap_type(type_name: str | None) -> Path | None:
    return _TYPE_REMAP_NORM.get(norm(type_name))


# ── 2. Reglas por nombre ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Rule:
    code: str
    pattern: str                         # regex sobre el nombre normalizado (MAYÚSCULAS, sin tildes)
    target: Path
    motivo: str
    scope: tuple[Path, ...] = ()         # solo si la ruta actual empieza con alguno ('' = cualquiera)
    exclude: str | None = None           # regex que anula la regla
    only_unmapped: bool = False          # solo para productos sin clasificar / Sin Tipo / nuevo

    def matches(self, name_n: str, current: Path | None, unmapped: bool) -> bool:
        if self.only_unmapped and not unmapped:
            return False
        if self.scope and not (current and any(current[: len(s)] == s for s in self.scope)):
            return False
        if self.exclude and re.search(self.exclude, name_n):
            return False
        return re.search(self.pattern, name_n) is not None


_MASC = r"(MASCOTA|CACHORRO)"
_PERFUMES_ADULTO: tuple[Path, ...] = ((BEL, "Perfumería", "Perfumes de Hombre"), (BEL, "Perfumería", "Perfumes de Mujer"))
_PERFUMES: tuple[Path, ...] = tuple((BEL, "Perfumería", s) for s in
                                    ("Perfumes de Hombre", "Perfumes de Mujer", "Perfumes Infantiles"))
RULES: list[Rule] = [
    # ── Mascotas: accesorios que estaban en "Alimentos para Mascotas" ──
    Rule("masc-correa", rf"(CORREA|COLLAR|PECHERA|ARNES)", M_CORREAS, "Correa/collar de mascota",
         scope=((MASC,),), exclude=r"^JGT"),
    Rule("masc-correa-2", rf"(CORREA|COLLAR|PECHERA|ARNES).*{_MASC}|{_MASC}.*(CORREA|COLLAR|PECHERA|ARNES)",
         M_CORREAS, "Correa/collar de mascota", exclude=r"^JGT"),
    Rule("masc-cama", rf"(CAMA|JAULA|TRANSPORTADOR|TRASPORTADOR|CANGURO).*{_MASC}|{_MASC}.*(CAMA|JAULA|TRANSPORTADOR)|^CAMA PET",
         M_CAMAS, "Cama/jaula/transportador de mascota", exclude=r"^JGT"),
    Rule("masc-higiene", rf"(CEPILLO|PEINE|GUANTE QUITA PELO|SACA PELUSA|CORTAR|MAQUINA|MANGUERA)",
         M_HIGIENE, "Higiene de mascota", scope=((MASC,),), exclude=r"^JGT"),
    Rule("masc-higiene-2", rf"(CEPILLO|PEINE|SACA PELUSA|MAQUINA|MANGUERA).*{_MASC}", M_HIGIENE, "Higiene de mascota",
         exclude=r"^JGT"),
    Rule("masc-juguete", rf"(MORDEDOR|JUGUETE|JGT).*{_MASC}|{_MASC}.*(MORDEDOR|JUGUETE)|SANDALIA MASCOTA",
         M_JUGUETES, "Juguete de mascota", exclude=r"JGT (MOCHILA|TRANSPORTADOR)"),

    # ── Productos que cayeron en otro rubro por una palabra ──
    Rule("portahuevos", r"PORTA ?HUEVO", (CYC, "Almacenaje", "Organizadores de Cocina"),
         "'Caramelo' es el color, no un dulce"),
    Rule("acondicionador", r"^(AC|CT) (NUTRIBELA|AMARAS)|ACONDICIONADOR", (CPS, "Cuidado del Cabello", "Acondicionadores"),
         "Acondicionador de cabello", scope=((AYB,), (AYB_AS,), (BEL,), (CPS,))),
    Rule("calabaza", r"CALABAZA", (TEMP, "Halloween", "Calabazas"), "Calabaza de Halloween",
         exclude=r"FRASCO|TAPER|VASO|TAZA"),  # "modelo calabaza" es la forma, no el rubro
    Rule("shampoo-perfumeria", r"SHAMPOO", (CPS, "Cuidado del Cabello", "Shampoos"), "Shampoo, no perfume",
         scope=((BEL, "Perfumería"),)),
    Rule("shampoo-auto", r"SHAMPOO (PARA CARRO|CON CERA|NEUTRO)", AUTO_SHAMPOO, "Shampoo para auto"),
    Rule("silicona-auto", r"SILICONA (PARA AUTOS|DE TABLERO)|ABRILLANTADOR.*LLANTA", AUTO_SIL, "Producto para auto"),
    Rule("aroma-auto", r"AROMATIZANTES? BRUXSS|(AMBIENTADOR|PERFUMADOR) PARA AUTO", AUTO_AMB, "Ambientador de auto"),
    Rule("accesorio-auto", r"FORRO DE TIMON|PARABRISAS", AUTO_ACC, "Accesorio de auto"),
    Rule("mascarilla-capilar", r"MASCARILLA (CAPILAR|DE CABELLO)", (CPS, "Cuidado del Cabello", "Mascarillas Capilares"),
         "Mascarilla de cabello, no facial"),
    Rule("body-lotion", r"BODY LOTION|CREMA CORP|LOCION CORPORAL|ESTUCHE CORPORAL",
         (BEL, "Cuidado Corporal", "Cremas Corporales"), "Producto corporal, no facial", scope=((BEL, "Cuidado Facial"),)),
    Rule("depilatoria", r"DEPILATORI", (BEL, "Cuidado Corporal", "Depilación"), "Depilación", scope=((BEL, "Cuidado Facial"),)),
    Rule("dermolimpiador", r"DERMOLIMPIADOR|LIMPIADOR FACIAL", (BEL, "Cuidado Facial", "Limpiadores Faciales"),
         "Limpiador, no sérum", scope=((BEL, "Cuidado Facial", "Sérums Faciales"),)),
    Rule("serum", r"SERUM", (BEL, "Cuidado Facial", "Sérums Faciales"), "Sérum",
         scope=((BEL, "Cuidado Facial", "Cremas Faciales"),)),
    Rule("liga-cabello", r"LIGA|GANCHO|COLET|CABELLO", (MODA, "Accesorios para el Cabello", "Colets y Ganchos"),
         "Accesorio de cabello", scope=((BEL, "Maquillaje de Ojos y Cejas"), (BEL, "Accesorios de Belleza", "Brochas de Maquillaje"))),
    Rule("gancho-cabello", r"CABELLO|CEREZA", (MODA, "Accesorios para el Cabello", "Colets y Ganchos"),
         "Gancho de cabello, no de ropa",
         scope=((HYD, "Organización", "Ganchos de Ropa"), (HYD, "Organización", "Colgadores de Ropa"))),
    Rule("botellas-viaje", r"BOTELLAS DE VIAJE", (MODA, "Organizadores y Viaje", "Accesorios de Viaje"), "Accesorio de viaje"),
    Rule("polvo-espejo", r"^POLVO", (BEL, "Maquillaje de Rostro", "Polvos y Rubores"), "Polvo compacto",
         scope=((BEL, "Accesorios de Belleza", "Espejos"),)),
    Rule("manicure", r"SECADOR (DE )?UNAS|CORTA ?UNAS|SET MANICURE|CUTICULA", (BEL, "Manicure", "Herramientas de Manicure"),
         "Herramienta de manicure"),

    # ── Perfumería: formato (el público queda en el atributo) ──
    Rule("body-spray", r"BODY SPRAY", (BEL, "Perfumería", "Body Spray"), "Body spray",
         scope=((BEL, "Perfumería"), (CPS, "Higiene Personal", "Desodorantes"))),
    # Lo infantil se queda en Perfumes Infantiles (colonias y sets incluidos): así se busca en el POS.
    Rule("perfume-set", r"\bEST\b|\bSET\b|\+", (BEL, "Perfumería", "Sets de Regalo"), "Estuche / set de regalo",
         scope=(_PERFUMES_ADULTO + ((BEL, "Perfumería", "Colonias y Body Mist"),))),
    Rule("colonia", r"COLONIA|BODY MIST|SPLASH", (BEL, "Perfumería", "Colonias y Body Mist"), "Colonia / body mist",
         scope=_PERFUMES_ADULTO),
    Rule("perfume-infantil", r"MY MELODY|HELLO KITTY|KUROMI|CINNAMOROLL|DISNEY|BYWIN",
         (BEL, "Perfumería", "Perfumes Infantiles"), "Perfume infantil (licencia)",
         scope=_PERFUMES_ADULTO),
    Rule("perfume-mujer", r"CHERIE|LA BELLE|QUEEN MILLION|^CRISTAL EDT",
         (BEL, "Perfumería", "Perfumes de Mujer"), "Fragancia de mujer cargada en Hombre",
         scope=((BEL, "Perfumería", "Perfumes de Hombre"),)),
    Rule("perfume-hombre", r"PRESTIGE (AHMED|AZIZ|HASSAN|MALIK)\b",
         (BEL, "Perfumería", "Perfumes de Hombre"), "Revisar: nombre masculino cargado en Mujer",
         scope=((BEL, "Perfumería", "Perfumes de Mujer"),)),

    # ── Moda ──
    Rule("mochila-dama", r"MOCHILA.*\bDAMAS?\b|MOCHILA LOVE", (MODA, "Carteras y Bolsos", "Mochilas de Moda"),
         "Mochila de moda, no escolar"),
    Rule("invierno-gorro", r"CHULLO|GORRO", (MODA, "Accesorios de Invierno", "Chullos y Gorros"), "Chullo / gorro",
         scope=((MODA,), (BEBE,)), exclude=r"PAPA NOEL|CUMPLE|BRUJA"),
    Rule("invierno-bufanda", r"BUFANDA|CHALINA", (MODA, "Accesorios de Invierno", "Bufandas y Chalinas"), "Bufanda",
         scope=((MODA,),), exclude=r"GORRO"),
    Rule("invierno-guante", r"GUANTE", (MODA, "Accesorios de Invierno", "Guantes"), "Guantes", scope=((MODA,),)),
    Rule("invierno-manta", r"^MANTA", (MODA, "Accesorios de Invierno", "Mantas"), "Manta", scope=((MODA,),)),
    Rule("parca", r"^PARCA", (MODA, "Ropa de Mujer", "Casacas y Abrigos"), "Parca", scope=((MODA,),)),
    Rule("cinturon", r"^CINTURON|^CORREA ADULTO", (MODA, "Otros Accesorios", "Cinturones y Correas"), "Cinturón / correa"),
    Rule("smartwatch", r"SMARTWA|RELOJ SMART", (TEC, "Accesorios de Celular y Cómputo", "Relojes Inteligentes"), "Smartwatch"),
    Rule("reloj-ninos", r"SET RELOJ", (MODA, "Joyería y Bisutería", "Relojes"), "Reloj", scope=((BEBE,),)),

    # ── Librería ──
    Rule("hoja-bond", r"HOJA BOND|PAPEL BOND", (LIB, "Papelería", "Papel Bond y Especiales"), "Papel, no colores",
         scope=((LIB, "Escritura y Corrección"), (LIB, "Colores y Dibujo"))),
    Rule("chinche", r"CHINCHE", (LIB, "Oficina", "Clips y Sujetadores"), "Chinches, no colores",
         scope=((LIB, "Escritura y Corrección"), (LIB, "Colores y Dibujo"))),
    Rule("limpiatipo", r"LIMPIATIPO", (LIB, "Oficina", "Pizarras, Tizas y Accesorios"), "Limpiatipo",
         scope=((LIB, "Escritura y Corrección"), (LIB, "Colores y Dibujo"))),
    Rule("ceramica-frio", r"CERAMICA", (LIB, "Arte y Manualidades", "Plastilinas y Masas"), "Cerámica en frío / granulada",
         scope=((LIB, "Escritura y Corrección"), (LIB, "Colores y Dibujo"))),
    Rule("caja-lapices", r"LAPICES|^LAPIZ", (LIB, "Escritura y Corrección", "Lápices"), "Lápices",
         scope=((LIB, "Colores y Dibujo", "Colores"), (LIB, "Escritura y Corrección", "Borradores")),
         exclude=r"COLOR"),
    Rule("colores-en-borradores", r"^COLORES", (LIB, "Colores y Dibujo", "Colores"), "Colores",
         scope=((LIB, "Escritura y Corrección", "Borradores"),)),
    Rule("compas", r"^COMPAS", (LIB, "Escolar", "Estuches Geométricos"), "Compás"),
    Rule("cartuchera", r"CARTUFERA|CARTUCHERA", (LIB, "Escolar", "Cartucheras Escolares"), "Cartuchera",
         scope=((LIB, "Oficina"), (LIB, "Escolar", "Taperes Escolares"))),
    Rule("forro-en-reglas", r"^FORRO", (LIB, "Papelería", "Forros para Cuadernos"), "Forro",
         scope=((LIB, "Oficina", "Tijeras y Reglas"),)),
    Rule("papel-en-forros", r"FOTOGRAFICO", (LIB, "Papelería", "Papel Bond y Especiales"), "Papel fotográfico",
         scope=((LIB, "Papelería", "Forros para Cuadernos"),)),
    Rule("celofan", r"CELOFAN", (LIB, "Papelería", "Papel Lustre, Crepé y Seda"), "Papel celofán",
         scope=((LIB, "Papelería"),)),
    Rule("portapapeles", r"PORTAPAPELES", (LIB, "Oficina", "Archivadores y Folders"), "Funda portapapeles"),
    Rule("calculadora", r"CALCULADORA", (LIB, "Oficina", "Calculadoras"), "Calculadora", exclude=r"CARTUCHERA"),

    # ── Profundidad: subcategorías que se separan (solo dentro de su origen) ──
    Rule("galleta-salada", r"SODA|RITZ|AGUA LINE|CRAKENAS|CRACKER|SALADA|CLUB SOCIAL",
         (AYB, "Galletas y Panadería", "Galletas Saladas"), "Galleta salada",
         scope=((AYB, "Galletas y Panadería", "Galletas Dulces"),), exclude=r"CHOCOSODA"),
    Rule("wafer", r"WAFER|BARQUILLO", (AYB, "Galletas y Panadería", "Wafers"), "Wafer",
         scope=((AYB, "Galletas y Panadería"),)),
    Rule("conserva", r"CONSER", (AYB, "Snacks", "Conservas"), "Conserva", scope=((AYB,),)),
    Rule("tomatodo-infantil", r"NINO|NINA|KIDS|INFANTIL|ESCOLAR|PANDA|OSO|OJITOS|DINO|CAPIBARA|CAPYBARA|LENTES|CABEZA|TRULULU",
         (CYC, "Botellas y Termos", "Tomatodos Infantiles"), "Tomatodo infantil",
         scope=((CYC, "Botellas y Termos", "Tomatodos y Botellas"),), exclude=r"TERMIC|ACERO"),
    Rule("tomatodo-deportivo", r"SPORT|DEPORTIV|GYM|PLAY HARD|BIKE|SPEED|WINNING|\b2L\b|2\.6L|1500 ?ML|TIME",
         (CYC, "Botellas y Termos", "Tomatodos Deportivos"), "Tomatodo deportivo",
         scope=((CYC, "Botellas y Termos", "Tomatodos y Botellas"),)),
    Rule("afilador", r"AFILA", (CYC, "Utensilios de Cocina", "Afiladores de Cuchillos"), "Afilador",
         scope=((CYC, "Utensilios de Cocina", "Cuchillos"),)),
    Rule("cuchillo", r"CUCHILLO", (CYC, "Utensilios de Cocina", "Cuchillos"), "Cuchillo, no afilador",
         scope=((CYC, "Utensilios de Cocina", "Afiladores de Cuchillos"),), exclude=r"AFILA"),
    Rule("sarten", r"SARTEN", (CYC, "Menaje", "Sartenes"), "Sartén", scope=((CYC,),)),
    Rule("utensilio-reposteria", r"CUCHARAS? MEDIDORA|ESPATULA CUCHILLO TORTA|RODILLO|PORTA TORTA|SET PASTELERIA|PALA TORTA|MINI BATIDORA",
         (CYC, "Repostería", "Utensilios de Repostería"), "Utensilio de repostería", scope=((CYC,),)),
    Rule("block", r"BLOCK|SKETCH|SCKETCH|SKECTCH", (LIB, "Papelería", "Blocks y Sketchbooks"), "Block / sketchbook",
         scope=((LIB, "Papelería", "Cuadernos"),)),
    Rule("papelote", r"PAPELOTE", (LIB, "Papelería", "Papelotes"), "Papelote",
         scope=((LIB, "Papelería", "Papel Bond y Especiales"),)),
    Rule("cartulina", r"CARTUL|CORROSPUM", (LIB, "Papelería", "Cartulinas"), "Cartulina",
         scope=((LIB, "Papelería", "Papel Bond y Especiales"),)),
    Rule("papel-lustre", r"LUSTRE|CREPE|SEDA|CORRUGADO", (LIB, "Papelería", "Papel Lustre, Crepé y Seda"),
         "Papel lustre / crepé / seda", scope=((LIB, "Papelería", "Papel Bond y Especiales"),)),
    Rule("crayola", r"CRAYOLA|CRAYON|OLEO", (LIB, "Colores y Dibujo", "Crayolas y Óleos"),
         "Crayolas / óleos", scope=((LIB, "Colores y Dibujo", "Colores"),)),
    Rule("resaltador", r"RESALTADOR", (LIB, "Escritura y Corrección", "Resaltadores"), "Resaltador",
         scope=((LIB, "Colores y Dibujo", "Plumones Escolares"),)),
    Rule("plumon-pizarra", r"PIZARRA|ACRI|PERMANENTE|INDELEBLE|INDELEVLE",
         (LIB, "Escritura y Corrección", "Plumones de Pizarra"), "Plumón de pizarra / permanente",
         scope=((LIB, "Colores y Dibujo", "Plumones Escolares"),)),
    Rule("touch", r"TOUCH", (LIB, "Colores y Dibujo", "Plumones Escolares"), "Plumones Touch",
         scope=((LIB, "Colores y Dibujo", "Colores"),)),
    Rule("porta-lapicero", r"PORTA ?LAPICERO|PORTA LAPICES", (HYD, "Organización", "Organizadores de Escritorio y Oficina"),
         "Organizador de escritorio", scope=((LIB, "Escritura y Corrección"), (LIB, "Colores y Dibujo"))),
    Rule("lapicero", r"LAPICERO|LAPIZERO|BOLIG", (LIB, "Escritura y Corrección", "Lapiceros"), "Lapicero",
         scope=((LIB, "Escritura y Corrección", "Lápices"),)),

    # ── Profundidad (revisión 3) ──
    Rule("chupetin", r"BON BON BU[MN]|CHUPETE|CHUPETIN|LOLLIPOP", (AYB, "Dulces y Chocolates", "Chupetines"), "Chupetín",
         scope=((AYB, "Dulces y Chocolates", "Caramelos"),), exclude=r"GOMITA"),
    Rule("nata-coco", r"NATA DE COCO", (AYB_AS, "Bebidas Asiáticas", "Bebidas con Nata de Coco"), "Bebida con nata de coco",
         scope=((AYB_AS, "Bebidas Asiáticas", "Jugos y Bebidas Frutadas"),)),
    Rule("inflador", r"INFLADOR", (FIES, "Cumpleaños y Fiestas", "Infladores"), "Inflador",
         scope=((FIES, "Cumpleaños y Fiestas", "Globos"),)),
    Rule("cotillon", r"GORRITO|MATRACA|SPRAY DE NIEVE|LLUVIA|SERPENTINA|ESPANTASUEGRA",
         (FIES, "Cumpleaños y Fiestas", "Gorritos y Cotillón"), "Cotillón",
         scope=((FIES, "Cumpleaños y Fiestas"),)),
    Rule("bowl", r"BOWL|\bBOL\b|BOL/|TAZON|CUENCO", (CYC, "Vajilla", "Bowls y Tazones"), "Bowl / tazón",
         scope=((CYC, "Vajilla", "Fuentes y Bandejas"),)),
    Rule("maleta-escolar", r"MALETA", (LIB, "Escolar", "Maletas Escolares con Ruedas"), "Maleta con ruedas",
         scope=((LIB, "Escolar", "Mochilas Escolares"),)),
    Rule("panos-guantes", r"PANO|GUANTE|SECADOR|TRAPO", _lim("Accesorios de Limpieza", "Paños y Guantes"), "Paño / guante",
         scope=((LIM, "Accesorios de Limpieza", "Esponjas"),), exclude=r"ESPONJA"),
    Rule("luces-lamparas", r"LAMPARA|REFLECTOR|GUIRNALDA|LUCES|FLUORESCENTE|DISCO",
         (ELEC, "Iluminación y Eléctricos", "Luces y Lámparas"), "Lámpara / luces",
         scope=((ELEC, "Iluminación y Eléctricos", "Focos"),), exclude=r"^FOCO"),
    Rule("tajador", r"^(SET DE )?TAJADOR", (LIB, "Escritura y Corrección", "Tajadores"), "Tajador",
         scope=((LIB, "Escritura y Corrección", "Borradores"),)),
    Rule("pistola-silicona", r"PISTOLA", (LIB, "Pegamentos", "Pistolas de Silicona"), "Pistola de silicona",
         scope=((LIB, "Pegamentos", "Siliconas"),)),
    Rule("goma-cola", r"GOMA|\bCOLA\b|PEGAMENTO", (LIB, "Pegamentos", "Gomas y Colas"), "Goma / cola",
         scope=((LIB, "Pegamentos", "Siliconas"),), exclude=r"SILICONA"),
    Rule("masking", r"MASKING", (LIB, "Cintas Adhesivas", "Masking"), "Masking tape",
         scope=((LIB, "Cintas Adhesivas", "Embalaje y Scotch"),)),
    Rule("contact", r"CONTACT", (LIB, "Cintas Adhesivas", "Contact"), "Contact",
         scope=((LIB, "Cintas Adhesivas", "Embalaje y Scotch"),)),
    Rule("bolig-en-colores", r"^BOLIG", (LIB, "Escritura y Corrección", "Lapiceros"), "Lapicero",
         scope=((LIB, "Colores y Dibujo", "Colores"),)),
    Rule("chequeo", r"^CHEQUEO", (LIB, "Escritura y Corrección", "Lápices"), "Lápiz chequeo",
         scope=((LIB, "Colores y Dibujo", "Colores"),)),
    Rule("panos-arte", r"PANOS", (LIB, "Arte y Manualidades", "Sets de Arte"), "Paños para manualidades",
         scope=((LIB, "Colores y Dibujo", "Colores"),)),
    Rule("mota", r"^MOTA|SET MOTA", (LIB, "Oficina", "Pizarras, Tizas y Accesorios"), "Mota de pizarra",
         scope=((LIB, "Colores y Dibujo", "Plumones Escolares"),)),
    Rule("desinf-en-multiuso", r"DESINF", _lim("Desinfectantes y Lejías", "Desinfectantes y Pinos"), "Desinfectante",
         scope=((LIM, "Limpiadores", "Limpiadores Multiuso"),)),
    Rule("gancho-en-colgadores", r"GANCHO|GANCHITO", (HYD, "Organización", "Ganchos de Ropa"), "Gancho, no colgador",
         scope=((HYD, "Organización", "Colgadores de Ropa"),), exclude=r"CABELLO"),

    # ── Hogar / cocina ──
    Rule("vaso-termico", r"VASO TERMICO", (CYC, "Botellas y Termos", "Vasos Térmicos"), "Vaso térmico"),
    Rule("moledor", r"MOLEDOR|MORTERO|PRENSA(DOR)? ", (CYC, "Utensilios de Cocina", "Moledores y Prensas"), "Moledor / prensa",
         scope=((CYC,),)),
    Rule("batidor-manual", r"^BATIDOR( GLOBO| MANUAL| DE HUEVO|$)|ESPUMADERA DE CAFE",
         (CYC, "Repostería", "Utensilios de Repostería"), "Batidor manual, no eléctrico", exclude=r"ELECTRIC"),
    Rule("manopla", r"MANOPLA", (CYC, "Utensilios de Cocina", "Mandiles y Manoplas"), "Manopla"),
    Rule("hielera", r"HIELERA", (CYC, "Mesa", "Artículos de Bar"), "Hielera"),
    Rule("bombonera", r"BOMBONERA", (CYC, "Almacenaje", "Frascos y Tarros"), "Bombonera"),
    Rule("mesa-accesorio", r"MANTIQUILLERO|SERVILLETERO", (CYC, "Mesa", "Accesorios de Mesa"), "Accesorio de mesa"),
    Rule("condimentero", r"CONDIMENTERO", (CYC, "Almacenaje", "Condimenteros y Especieros"), "Condimentero",
         scope=((FJA,),)),
    Rule("caja-regalo", r"CAJA (DE )?REGALO", (FIES, "Empaque y Regalo", "Cajas de Regalo"), "Caja de regalo",
         scope=((FIES,),)),
    Rule("armario", r"^ARMARIO|ROPERO", (HYD, "Muebles", "Roperos y Closets"), "Ropero",
         scope=((HYD, "Dormitorio"),)),
    Rule("cobertor-sofa", r"COBERTOR DE SOFA", (HYD, "Decoración", "Fundas Decorativas"), "Funda de sofá"),
    Rule("estanteria-bano", r"ESTANTERIA", (HYD, "Muebles", "Estantes"), "Estante", scope=((HYD, "Baño"),)),
    Rule("escobilla-bano", r"ESCOBILLA (PARA )?BANO", (HYD, "Baño", "Escobilla para Baño"), "Escobilla de baño"),
    Rule("mata-insectos", r"MATA INSECTOS", _lim("Control de Plagas", "Trampas y Cebos"), "Mata insectos",
         exclude=r"PATITO"),

    # ── Bebé ──
    Rule("piso-goma", r"^PISO (ARMABLE|GRANDE DE NINOS|NINOS|MEDIANO|BLANCO PEGABLE)",
         (BEBE, "Accesorios del Bebé", "Pisos y Alfombras de Goma"), "Piso de goma infantil"),
    Rule("cepillo-dientes-bebe", r"CEPILLO (DE DIENTE|NINOS)", (CPS, "Cuidado Bucal", "Cepillos Dentales"), "Cepillo dental",
         scope=((BEBE,),)),
    Rule("crema-infantil", r"^CREMA INFANTIL", (BEL, "Cuidado Corporal", "Cremas Corporales"), "Crema corporal",
         scope=((BEBE,),)),
    Rule("sandalia-ninos", r"(SANDALIA|ZANDALIA).*NINOS?\b", (CALZ, "Sandalias y Pantuflas", "Sandalias de Niños"),
         "Sandalia de niño", exclude=r"ADULTO"),
    Rule("limpieza-zapatilla", r"LIMPIA ?ZAPATILLA|KIT LIMPIEZA ZAPATILLA", (CALZ, "Accesorios de Calzado", "Limpieza de Calzado"),
         "Limpieza de calzado"),

    # ── Juguetes ──
    Rule("hidrogel", r"HIDROGEL", (JUG, "Armas de Juguete", "Pistolas de Hidrogel"), "Pistola de hidrogel",
         scope=((JUG, "Juegos al Aire Libre"),)),
    Rule("pelota", r"PELOTA", (JUG, "Juegos al Aire Libre", "Pelotas y Juegos de Exterior"), "Pelota",
         scope=((JUG, "Deportes y Fitness", "Deportes y Movilidad"),)),
    Rule("peluche-banca", r"^BANCA", (HYD, "Muebles", "Bancos y Puffs"), "Banca", scope=((JUG, "Peluches"),)),
    Rule("peluche-pantufla", r"PANTUFLA", (CALZ, "Sandalias y Pantuflas", "Pantuflas"), "Pantufla", scope=((JUG, "Peluches"),)),
    Rule("peluche-ventilador", r"VENTILADOR", (ELEC, "Electrodomésticos del Hogar", "Ventiladores"), "Ventilador",
         scope=((JUG, "Peluches"),)),
    Rule("peluche-brazalete", r"BRAZALETE|PULSERA", (MODA, "Joyería y Bisutería", "Pulseras"), "Pulsera",
         scope=((JUG, "Peluches"),)),
    Rule("peluche-llavero", r"LLAVERO", (MODA, "Llaveros", "Llaveros Kawaii"), "Llavero", scope=((JUG, "Peluches"),)),
    Rule("llavero-parlante", r"BUFFER", (TEC, "Audio", "Parlantes"), "Parlante", scope=((MODA, "Llaveros"),)),
    Rule("mouse", r"^MOUSE", (TEC, "Accesorios de Celular y Cómputo", "Teclados y Mouse"), "Mouse",
         scope=((TEC, "Accesorios de Celular y Cómputo", "Pad Mouse"),)),

    # ── Limpieza ──
    Rule("kreso", r"KRESO|^PINO (NATURAL|DE \d|\d)", _lim("Desinfectantes y Lejías", "Desinfectantes y Pinos"), "Desinfectante",
         scope=((LIM,),)),
    Rule("limpiatodo-en-ropa", r"LIMPIATODO|LIMPIALODO", _lim("Limpiadores", "Limpiadores Multiuso"), "Limpiatodo",
         scope=((LIM, "Cuidado de la Ropa"),)),
    Rule("casco-chelero", r"CASCO CHELERO", (FIES, "Cumpleaños y Fiestas", "Decoración de Cumpleaños"), "Artículo de fiesta"),
    Rule("escalera", r"^ESCALERA", (FJA, "Herramientas", "Escaleras"), "Escalera"),
    Rule("balanza", r"BALANZA", (ELEC, "Electrodomésticos del Hogar", "Balanzas"), "Balanza"),
    Rule("base-lavadora", r"BASE LAVADORA", (ELEC, "Electrodomésticos del Hogar", "Accesorios de Electrodomésticos"),
         "Accesorio de electrodoméstico"),

    # ── Productos sin tipo ("Sin Tipo", "nuevo", sin mapear): por palabra clave ──
    Rule("st-lejia", r"LEJIA", _lim("Desinfectantes y Lejías", "Lejías"), "Lejía", only_unmapped=True),
    Rule("st-detergente-liq", r"DETERGENTE.*LIQUID", _lim("Cuidado de la Ropa", "Detergentes Líquidos"), "Detergente líquido",
         only_unmapped=True),
    Rule("st-detergente", r"DETERGENTE", _lim("Cuidado de la Ropa", "Detergentes en Polvo"), "Detergente", only_unmapped=True),
    Rule("st-suavizante", r"SUAVI[SZ]ANTE", _lim("Cuidado de la Ropa", "Suavizantes"), "Suavizante", only_unmapped=True),
    Rule("st-lavavajilla-pasta", r"LAVAVAJILLA.*PASTA", _lim("Lavavajillas", "Lavavajillas en Pasta"), "Lavavajilla en pasta",
         only_unmapped=True),
    Rule("st-lavavajilla", r"LAVAVAJILLA", _lim("Lavavajillas", "Lavavajillas Líquido"), "Lavavajilla líquido", only_unmapped=True),
    Rule("st-limpiavidrio", r"LIMPIA ?VIDRIO", _lim("Limpiadores", "Limpiavidrios"), "Limpiavidrios", only_unmapped=True),
    Rule("st-limpiatodo", r"LIMPIA ?TODO|LIMPIA BANO", _lim("Limpiadores", "Limpiadores Multiuso"), "Limpiador multiuso",
         only_unmapped=True),
    Rule("st-sacagrasa", r"SACA ?GRASA", _lim("Limpiadores", "Quitagrasas"), "Quitagrasa", only_unmapped=True),
    Rule("st-quitasarro", r"QUITASARRO|ACIDO", _lim("Limpiadores", "Ácidos y Quitasarros"), "Quitasarro", only_unmapped=True),
    Rule("st-jabon", r"JABON LIQUIDO", (CPS, "Higiene Personal", "Jabones Líquidos para Manos"), "Jabón líquido",
         only_unmapped=True),
    Rule("st-desinfectante", r"DESINFECTANTE|\bPINO\b", _lim("Desinfectantes y Lejías", "Desinfectantes y Pinos"),
         "Desinfectante", only_unmapped=True),
    Rule("st-cera", r"\bCERA\b", _lim("Limpiadores", "Abrillantadores"), "Cera", only_unmapped=True),
    Rule("st-peluche", r"PELUCHE", (JUG, "Peluches", "Peluches Kawaii"), "Peluche", only_unmapped=True),
    Rule("st-mordedor", r"MORDEDOR", M_JUGUETES, "Mordedor de mascota", only_unmapped=True),
    Rule("st-alcancia", r"ALCANCIA", (HYD, "Decoración", "Alcancías"), "Alcancía", only_unmapped=True),
    Rule("st-billetera", r"BILLETERA", (MODA, "Carteras y Bolsos", "Billeteras"), "Billetera", only_unmapped=True),
    Rule("st-perchero", r"PERCHERO", (HYD, "Organización", "Percheros"), "Perchero", only_unmapped=True),
    Rule("st-candado", r"CANDADO", (FJA, "Cerrajería y Seguridad", "Candados y Cadenas"), "Candado", only_unmapped=True),
    Rule("st-clips-cable", r"CLIPS P/ ?CABLES", (ELEC, "Iluminación y Eléctricos", "Extensiones e Interruptores"),
         "Organizador de cables", only_unmapped=True),
    Rule("st-cortina", r"CORTINA", (HYD, "Decoración", "Cortinas"), "Cortina", only_unmapped=True),
    Rule("st-jarra-vidrio", r"JARRA.*VIDRIO|VIDRIO.*JARRA", (CYC, "Vasos, Copas y Jarras", "Jarras de Vidrio"),
         "Jarra de vidrio", only_unmapped=True),
    Rule("st-jarra", r"JARRA", (CYC, "Vasos, Copas y Jarras", "Jarras de Plástico"), "Jarra", only_unmapped=True),
    Rule("st-frasco", r"FRASCO", (CYC, "Almacenaje", "Frascos y Tarros"), "Frasco", only_unmapped=True),
    Rule("st-ropero", r"ROPERO", (HYD, "Muebles", "Roperos y Closets"), "Ropero", only_unmapped=True),
    Rule("st-sandalia", r"SANDALIA", (CALZ, "Sandalias y Pantuflas", "Sandalias de Adulto"), "Sandalia", only_unmapped=True),
    # "sombrilla" de 55-60 cm es un paraguas; las de jardín/playa se nombran con PLAYA/JARDIN/TOLDO
    Rule("st-sombrilla-jardin", r"SOMBRILLA.*(PLAYA|JARDIN|TOLDO|PATIO)|(PLAYA|JARDIN).*SOMBRILLA",
         (FJA, "Jardín", "Toldos y Sombrillas"), "Sombrilla de jardín/playa", only_unmapped=True),
    Rule("st-sombrilla", r"SOMBRILLA", (MODA, "Otros Accesorios", "Paraguas"), "Sombrilla de mano (paraguas)",
         only_unmapped=True),
    Rule("st-corona", r"CORONA", (TEMP, "Navidad", "Guirnaldas y Coronas"), "Corona navideña", only_unmapped=True),
]


def propose(name: str, type_name: str | None, current: Path | None) -> tuple[Path, str, str] | None:
    """Propuesta de subcategoría v2 para un producto: (ruta, motivo, regla).
    `current` es su ruta v2 actual (ya traducida) o None si no está clasificado.
    Devuelve None si no hay nada que cambiar."""
    n = norm(name)
    unmapped = current is None or norm(type_name) in GENERIC_TYPES
    effective = current
    if current is None:
        remap = remap_type(type_name)
        if remap:
            effective = remap
            unmapped = False
    for rule in RULES:
        if rule.matches(n, effective, unmapped) and rule.target != effective:
            return rule.target, rule.motivo, rule.code
    if effective != current and effective is not None:
        return effective, f"El tipo de BSale '{type_name}' no existe en la taxonomía", "type-remap"
    return None
