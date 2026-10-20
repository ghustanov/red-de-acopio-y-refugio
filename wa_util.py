"""Utilidades de WhatsApp — Red de Acopio y Refugio.

Normalización de teléfonos venezolanos para el match de identidad (migración
Opción B): cuando alguien escribe por WhatsApp, comparamos su número contra los
teléfonos ya guardados (registro por Telegram) para ofrecerle VINCULARSE a su
ficha existente en vez de crear un duplicado en la web.

Solo stdlib (re) — no toca el venv del bot vivo. Importable sin efectos.
"""
import re

__all__ = ["canonico", "candidatos", "coincide"]

# Separadores que suelen dividir DOS números en un mismo campo de texto libre.
_SEP = re.compile(r"\s-\s|[,/;]|\s+y\s+|\s+o\s+")


def _digitos(s):
    return re.sub(r"\D", "", s or "")


def canonico(numero):
    """Número VE canónico: 10 dígitos que empiezan en 4 (móvil), o None.

    Acepta cualquier formato de UN solo número: 0424..., +58 424..., 58424...,
    con espacios o guiones. Descarta lo que no sea móvil venezolano.
    """
    d = _digitos(numero)
    if not d:
        return None
    if d.startswith("58") and len(d) == 12:       # prefijo país +58
        d = d[2:]
    elif d.startswith("0") and len(d) == 11:      # formato nacional 0424...
        d = d[1:]
    if len(d) == 10 and d[0] == "4":              # móvil VE (412/414/416/424/426/…)
        return d
    return None


def candidatos(texto):
    """Conjunto de números canónicos hallados en un campo (puede traer VARIOS).

    '04240000099 - 041228' -> {'4240000099'}
    '+58 422-1915191'       -> {'4221915191'}
    '+56900000000' (Chile)  -> set()
    """
    out = set()
    if not texto:
        return out
    partes = _SEP.split(texto) + [texto] + re.findall(r"\d{6,}", texto)
    for p in partes:
        c = canonico(p)
        if c:
            out.add(c)
    return out


def coincide(wa_numero, guardado):
    """True si el número de WhatsApp (formato Meta '584240000011') coincide con
    el teléfono guardado (formato libre de la DB)."""
    wa = canonico(wa_numero)
    return bool(wa) and wa in candidatos(guardado)


if __name__ == "__main__":
    import os
    import sqlite3

    # 1) Casos sintéticos que replican los formatos reales vistos en la DB.
    casos = [
        ("584240000011", "04240000011",           True),
        ("584220000022", "+58 422-0000022",        True),
        ("584240000033", "04240000033 - 041200",   True),   # dos números en un campo
        ("584240000044", "0424-0000044",           True),
        ("584140000055", "04140000055",            True),
        ("584240000011", "+56900000000",           False),  # Chile: NO debe matchear
        ("584240000011", "04129999999",            False),  # números distintos
    ]
    fails = 0
    for wa, guardado, esperado in casos:
        got = coincide(wa, guardado)
        if got != esperado:
            fails += 1
        print(f"[{'ok' if got == esperado else 'FALLA'}] wa={wa} vs '{guardado}' -> {got} (esperado {esperado})")
    print(f"\n{len(casos) - fails}/{len(casos)} casos correctos")

    # 2) Cobertura real contra los teléfonos ya guardados.
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "angeles.db")
    if os.path.exists(db):
        con = sqlite3.connect(db)
        print("\n--- Cobertura de teléfonos guardados (registro por Telegram) ---")
        for tabla, col in (("representantes", "telefono"), ("aliados", "contacto"), ("voluntarios", "contacto")):
            filas = con.execute(f"SELECT id,{col} FROM {tabla} WHERE TRIM(COALESCE({col},''))<>''").fetchall()
            con_match = sum(1 for _, v in filas if candidatos(v))
            print(f"{tabla}: {con_match}/{len(filas)} normalizables")
            for i, v in filas:
                print(f"   id={i}: '{v}' -> {sorted(candidatos(v)) or 'NINGUNO'}")
        con.close()

    raise SystemExit(1 if fails else 0)
