"""Inventario numérico vivo por punto — Red de Acopio y Refugio.

Tabla `stock`: el inventario REAL y descontable de cada centro, con cantidad
numérica + unidad. COMPARTIDO por WhatsApp y Telegram: ambos bots llaman a
`aplicar_captura(cid)` al confirmar una captura, así el inventario numérico es
uno solo sin importar el canal. Aislado (solo usa db.conectar()).

Estado de un renglón: 'disponible' (cuenta como inventario; cantidad IS NULL =
falta cantidad por completar). El campo `batch` = id de la captura que lo trajo
(sirve para idempotencia: no aplicar dos veces la misma captura).
"""
import re
import sqlite3
import unicodedata

import db

_NUM = re.compile(r"(\d+(?:[.,]\d+)?)")


def _norm(s):
    """Normaliza el nombre de un ítem para agrupar/sumar ('Pañales' == 'panales')."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


def parse_cantidad(txt):
    """'30 cápsulas' -> (30.0, 'cápsulas'); '2 cajas' -> (2.0, 'cajas');
    'algunos'/'' -> (None, texto o None)."""
    if not txt:
        return None, None
    m = _NUM.search(txt)
    if not m:
        return None, (txt.strip() or None)
    num = float(m.group(1).replace(",", "."))
    unidad = (txt[:m.start()] + txt[m.end():]).strip(" .-") or None
    return num, unidad


def aplicar_captura(cid, reemplazar=False):
    """Vuelca los ítems de una captura confirmada al `stock` numérico del punto.
    reemplazar=True borra el inventario previo del punto; False suma (agrega
    cantidades si el ítem+unidad ya existe). Idempotente (si ya se aplicó esta
    captura, no hace nada). Devuelve el punto_id afectado, o None."""
    con = db.conectar()
    try:
        # Idempotencia: ¿ya se aplicó esta captura?
        if con.execute("SELECT 1 FROM stock WHERE batch = ? LIMIT 1", (cid,)).fetchone():
            r = con.execute("SELECT punto_id FROM stock WHERE batch = ? LIMIT 1", (cid,)).fetchone()
            return r[0] if r else None
        cap = con.execute("SELECT representante_id, clase FROM capturas WHERE id = ?", (cid,)).fetchone()
        if not cap:
            return None
        if cap[1] == "demanda":   # las demandas (/necesito) van a necesidades, NO al stock
            return None
        rep = con.execute("SELECT id, punto_id FROM representantes WHERE id = ?", (cap[0],)).fetchone()
        if not rep:
            return None
        rep_id, punto_id = rep[0], (rep[1] or rep[0])
        items = con.execute(
            "SELECT categoria, item, cantidad, detalle FROM inventario WHERE captura_id = ?", (cid,)).fetchall()
        if not items:
            return punto_id
        if reemplazar:
            con.execute("DELETE FROM stock WHERE punto_id = ? AND estado = 'disponible'", (punto_id,))
        for categoria, item, cantidad, detalle in items:
            num, uni = parse_cantidad(cantidad)
            inorm = _norm(item or "")
            existe = None
            if not reemplazar:
                existe = con.execute(
                    "SELECT id, cantidad FROM stock WHERE punto_id = ? AND estado = 'disponible' "
                    "AND item_norm = ? AND IFNULL(unidad,'') = IFNULL(?,'')",
                    (punto_id, inorm, uni)).fetchone()
            if existe and num is not None and existe[1] is not None:
                con.execute("UPDATE stock SET cantidad = cantidad + ?, actualizado_en = datetime('now') "
                            "WHERE id = ?", (num, existe[0]))
            else:
                con.execute(
                    "INSERT INTO stock (punto_id, rep_id, categoria, item, item_norm, cantidad, unidad, "
                    "detalle, estado, batch) VALUES (?,?,?,?,?,?,?,?,'disponible',?)",
                    (punto_id, rep_id, categoria, item, inorm, num, uni, detalle, cid))
        con.commit()
        return punto_id
    finally:
        con.close()


def de_punto(punto_id):
    """Inventario disponible de un punto (lista de dicts)."""
    con = db.conectar()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM stock WHERE punto_id = ? AND estado = 'disponible' ORDER BY categoria, item",
        (punto_id,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def faltan_cantidad(punto_id):
    """Ítems disponibles a los que les falta la cantidad numérica (para completar)."""
    con = db.conectar()
    rows = con.execute(
        "SELECT item FROM stock WHERE punto_id = ? AND estado = 'disponible' AND cantidad IS NULL",
        (punto_id,)).fetchall()
    con.close()
    return [r[0] for r in rows]


def parece_snapshot(cid):
    """Auditoría anti-duplicado (lección Ecocamping): ¿la captura se parece a lo YA
    publicado? La gente suele mandar la foto de TODO su inventario; si el solape con
    el stock actual es alto, 'Sumar' duplicaría. Devuelve (coinciden, total_captura)
    — se considera sospechoso si coinciden >= 50% y >= 3 ítems."""
    con = db.conectar()
    try:
        cap = con.execute("SELECT representante_id FROM capturas WHERE id = ?", (cid,)).fetchone()
        if not cap:
            return (0, 0)
        rep = con.execute("SELECT id, punto_id FROM representantes WHERE id = ?", (cap[0],)).fetchone()
        if not rep:
            return (0, 0)
        punto_id = rep[1] or rep[0]
        nuevos = {_norm(r[0]) for r in con.execute(
            "SELECT item FROM inventario WHERE captura_id = ?", (cid,)).fetchall() if r[0]}
        if not nuevos:
            return (0, 0)
        actuales = {r[0] for r in con.execute(
            "SELECT item_norm FROM stock WHERE punto_id = ? AND estado = 'disponible'",
            (punto_id,)).fetchall()}
        return (len(nuevos & actuales), len(nuevos))
    finally:
        con.close()


def completar_cantidad(punto_id, texto):
    """El centro responde 'pañales 200' (o '200 pañales') para completar un ítem sin
    cantidad. Matchea contra los ítems con cantidad NULL del punto. Devuelve
    (item, cantidad) si completó, o None."""
    m = _NUM.search(texto or "")
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    nombre = _norm((texto[:m.start()] + " " + texto[m.end():]).strip())
    if not nombre:
        return None
    con = db.conectar()
    try:
        rows = con.execute(
            "SELECT id, item, item_norm FROM stock WHERE punto_id = ? AND estado = 'disponible' "
            "AND cantidad IS NULL", (punto_id,)).fetchall()
        for sid, item, inorm in rows:
            if inorm == nombre or nombre in inorm or inorm in nombre:
                con.execute("UPDATE stock SET cantidad = ?, actualizado_en = datetime('now') "
                            "WHERE id = ?", (num, sid))
                con.commit()
                return (item, num)
        return None
    finally:
        con.close()
