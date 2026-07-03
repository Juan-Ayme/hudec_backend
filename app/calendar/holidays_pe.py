"""Feriados nacionales del Perú.

Tabla estática mantenible a mano (Perú tiene ~15 feriados/año, no migran a DB
hasta que el dueño quiera editarlos vía UI). Sirve para detectar desbalance de
feriados entre el período actual y los períodos de comparación en el
diagnóstico — un día feriado vs. un día hábil sesga la comparación.

Fuentes oficiales: D.S. 002-2025-PCM y leyes complementarias. Mantener
actualizado cada diciembre con los del año siguiente.
"""

from datetime import date

# {fecha: (nombre, alcance)}
# alcance: "nacional" | "regional:LIMA" | "movible"
HOLIDAYS_PE: dict[date, tuple[str, str]] = {
    # 2024
    date(2024, 1, 1):   ("Año Nuevo",                          "nacional"),
    date(2024, 3, 28):  ("Jueves Santo",                       "movible"),
    date(2024, 3, 29):  ("Viernes Santo",                      "movible"),
    date(2024, 5, 1):   ("Día del Trabajo",                    "nacional"),
    date(2024, 6, 7):   ("Batalla de Arica",                   "nacional"),
    date(2024, 6, 29):  ("San Pedro y San Pablo",              "nacional"),
    date(2024, 7, 23):  ("Día de la Fuerza Aérea",             "nacional"),
    date(2024, 7, 28):  ("Fiestas Patrias",                    "nacional"),
    date(2024, 7, 29):  ("Fiestas Patrias",                    "nacional"),
    date(2024, 8, 6):   ("Batalla de Junín",                   "nacional"),
    date(2024, 8, 30):  ("Santa Rosa de Lima",                 "nacional"),
    date(2024, 10, 8):  ("Combate de Angamos",                 "nacional"),
    date(2024, 11, 1):  ("Todos los Santos",                   "nacional"),
    date(2024, 12, 8):  ("Inmaculada Concepción",              "nacional"),
    date(2024, 12, 9):  ("Batalla de Ayacucho",                "nacional"),
    date(2024, 12, 25): ("Navidad",                            "nacional"),

    # 2025
    date(2025, 1, 1):   ("Año Nuevo",                          "nacional"),
    date(2025, 4, 17):  ("Jueves Santo",                       "movible"),
    date(2025, 4, 18):  ("Viernes Santo",                      "movible"),
    date(2025, 5, 1):   ("Día del Trabajo",                    "nacional"),
    date(2025, 6, 7):   ("Batalla de Arica",                   "nacional"),
    date(2025, 6, 29):  ("San Pedro y San Pablo",              "nacional"),
    date(2025, 7, 23):  ("Día de la Fuerza Aérea",             "nacional"),
    date(2025, 7, 28):  ("Fiestas Patrias",                    "nacional"),
    date(2025, 7, 29):  ("Fiestas Patrias",                    "nacional"),
    date(2025, 8, 6):   ("Batalla de Junín",                   "nacional"),
    date(2025, 8, 30):  ("Santa Rosa de Lima",                 "nacional"),
    date(2025, 10, 8):  ("Combate de Angamos",                 "nacional"),
    date(2025, 11, 1):  ("Todos los Santos",                   "nacional"),
    date(2025, 12, 8):  ("Inmaculada Concepción",              "nacional"),
    date(2025, 12, 9):  ("Batalla de Ayacucho",                "nacional"),
    date(2025, 12, 25): ("Navidad",                            "nacional"),

    # 2026
    date(2026, 1, 1):   ("Año Nuevo",                          "nacional"),
    date(2026, 4, 2):   ("Jueves Santo",                       "movible"),
    date(2026, 4, 3):   ("Viernes Santo",                      "movible"),
    date(2026, 5, 1):   ("Día del Trabajo",                    "nacional"),
    date(2026, 6, 7):   ("Batalla de Arica",                   "nacional"),
    date(2026, 6, 29):  ("San Pedro y San Pablo",              "nacional"),
    date(2026, 7, 23):  ("Día de la Fuerza Aérea",             "nacional"),
    date(2026, 7, 28):  ("Fiestas Patrias",                    "nacional"),
    date(2026, 7, 29):  ("Fiestas Patrias",                    "nacional"),
    date(2026, 8, 6):   ("Batalla de Junín",                   "nacional"),
    date(2026, 8, 30):  ("Santa Rosa de Lima",                 "nacional"),
    date(2026, 10, 8):  ("Combate de Angamos",                 "nacional"),
    date(2026, 11, 1):  ("Todos los Santos",                   "nacional"),
    date(2026, 12, 8):  ("Inmaculada Concepción",              "nacional"),
    date(2026, 12, 9):  ("Batalla de Ayacucho",                "nacional"),
    date(2026, 12, 25): ("Navidad",                            "nacional"),

    # 2027
    date(2027, 1, 1):   ("Año Nuevo",                          "nacional"),
    date(2027, 3, 25):  ("Jueves Santo",                       "movible"),
    date(2027, 3, 26):  ("Viernes Santo",                      "movible"),
    date(2027, 5, 1):   ("Día del Trabajo",                    "nacional"),
    date(2027, 6, 29):  ("San Pedro y San Pablo",              "nacional"),
    date(2027, 7, 28):  ("Fiestas Patrias",                    "nacional"),
    date(2027, 7, 29):  ("Fiestas Patrias",                    "nacional"),
    date(2027, 8, 30):  ("Santa Rosa de Lima",                 "nacional"),
    date(2027, 10, 8):  ("Combate de Angamos",                 "nacional"),
    date(2027, 11, 1):  ("Todos los Santos",                   "nacional"),
    date(2027, 12, 8):  ("Inmaculada Concepción",              "nacional"),
    date(2027, 12, 25): ("Navidad",                            "nacional"),
}


def is_holiday(d: date) -> bool:
    return d in HOLIDAYS_PE


def holiday_name(d: date) -> str | None:
    info = HOLIDAYS_PE.get(d)
    return info[0] if info else None


def holidays_in_range(dfrom: date, dto: date) -> list[dict]:
    """Lista feriados en [dfrom, dto) — extremo derecho exclusivo, igual al resto del módulo."""
    out: list[dict] = []
    for d, (name, scope) in HOLIDAYS_PE.items():
        if dfrom <= d < dto:
            out.append({"fecha": d.isoformat(), "nombre": name, "alcance": scope})
    out.sort(key=lambda x: x["fecha"])
    return out
