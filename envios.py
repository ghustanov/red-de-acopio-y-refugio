"""Envíos con verificación — Red de Acopio y Refugio (Fase B).

El ciclo de cumplimiento encima de `solicitudes` (la capa de pedido que ya existe
en Telegram con /solicitar) y del `stock` numérico compartido:

  DESPACHAR  el acopio arma un envío (ítems + cantidades de su stock)
             → se RESERVA el stock (disponible baja al instante en la web)
             → token único → QR / manifiesto → se avisa al refugio
  RECIBIR    el responsable escanea el QR (o confirma por el bot)
             → confirma por ítem cuánto llegó (+ motivo si faltó)
             → el stock se DESCUENTA definitivo (lo enviado salió del acopio)
             → si llegó incompleto, queda el rastro por ítem con motivo

Regla de descuento (honesta con la física): al cerrar se descuenta lo ENVIADO,
no lo recibido — lo que salió del acopio ya no está en su depósito; la
diferencia enviado−recibido queda registrada con motivo (auditoría), no
"reaparece" en el stock.

Motivos de faltante (lista corta, decidida con Gustavo):
  menos     📉 llegó menos cantidad
  danado    📦 llegó dañado / vencido
  equivocado❌ ítem equivocado
  no_llego  🚫 no llegó
  otro      ✍️ otro (texto libre en `nota`)
"""
import secrets
import sqlite3

import db

MOTIVOS = {"menos": "📉 Llegó menos cantidad", "danado": "📦 Llegó dañado/vencido",
           "equivocado": "❌ Ítem equivocado", "no_llego": "🚫 No llegó", "otro": "✍️ Otro"}


def _con():
    con = db.conectar()
    con.row_factory = sqlite3.Row
    return con


def crear_tablas():
    con = db.conectar()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS envios (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      token TEXT UNIQUE,
      de_punto_id INTEGER,          -- acopio que despacha
      de_rep INTEGER,
      a_rep INTEGER,                -- representante del refugio/centro que recibe
      a_punto_id INTEGER,
      estado TEXT DEFAULT 'despachado',  -- despachado / recibido / incompleto / cancelado
      nota TEXT,
      creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
      cerrado_en TEXT
    );
    CREATE TABLE IF NOT EXISTS envio_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      envio_id INTEGER,
      stock_id INTEGER,
      item TEXT,
      unidad TEXT,
      cant_enviada REAL,
      cant_recibida REAL,
      motivo TEXT,                  -- clave de MOTIVOS si faltó algo
      nota TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_envio_items ON envio_items(envio_id);
    """)
    con.commit()
    con.close()


def disponible(stock_row):
    """Cantidad realmente disponible de un renglón (cantidad − reservado)."""
    if stock_row["cantidad"] is None:
        return None
    return stock_row["cantidad"] - (stock_row["reservado"] or 0)


def despachar(de_punto_id, de_rep, a_rep, a_punto_id, items, nota=None):
    """Crea un envío y RESERVA el stock. `items` = [(stock_id, cantidad), ...].
    Valida contra lo disponible. Devuelve dict del envío (con token) o lanza
    ValueError con el detalle del renglón que no alcanza."""
    if not items:
        raise ValueError("el envío no tiene ítems")
    con = _con()
    con.isolation_level = None
    con.execute("BEGIN IMMEDIATE")
    try:
        filas = []
        for stock_id, cant in items:
            r = con.execute("SELECT * FROM stock WHERE id = ? AND punto_id = ? "
                            "AND estado = 'disponible'", (stock_id, de_punto_id)).fetchone()
            if not r:
                raise ValueError(f"renglón {stock_id} no existe en tu stock")
            if cant is None or cant <= 0:
                raise ValueError(f"cantidad inválida para {r['item']}")
            disp = disponible(r)
            if disp is not None and cant > disp:
                raise ValueError(f"solo hay {disp:g} {r['unidad'] or ''} de {r['item']}".strip())
            filas.append((r, float(cant)))
        token = secrets.token_urlsafe(12)
        cur = con.execute(
            "INSERT INTO envios (token, de_punto_id, de_rep, a_rep, a_punto_id, nota) "
            "VALUES (?,?,?,?,?,?)", (token, de_punto_id, de_rep, a_rep, a_punto_id, nota))
        eid = cur.lastrowid
        for r, cant in filas:
            con.execute("UPDATE stock SET reservado = IFNULL(reservado,0) + ? WHERE id = ?",
                        (cant, r["id"]))
            con.execute("INSERT INTO envio_items (envio_id, stock_id, item, unidad, cant_enviada) "
                        "VALUES (?,?,?,?,?)", (eid, r["id"], r["item"], r["unidad"], cant))
        con.execute("COMMIT")
        return {"id": eid, "token": token}
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def por_token(token):
    """Envío + sus ítems (para la página del QR y para el bot). None si no existe."""
    con = _con()
    e = con.execute("SELECT * FROM envios WHERE token = ?", (token,)).fetchone()
    if not e:
        con.close()
        return None
    items = [dict(r) for r in con.execute(
        "SELECT * FROM envio_items WHERE envio_id = ? ORDER BY id", (e["id"],)).fetchall()]
    de = con.execute("SELECT punto FROM representantes WHERE id = ?", (e["de_punto_id"],)).fetchone()
    a = con.execute("SELECT punto FROM representantes WHERE id = ?",
                    (e["a_punto_id"] or e["a_rep"],)).fetchone()
    con.close()
    d = dict(e)
    d["items"] = items
    d["de_punto"] = de["punto"] if de else None
    d["a_punto"] = a["punto"] if a else None
    return d


def recibir(token, recepciones, nota=None):
    """Cierra el ciclo. `recepciones` = {envio_item_id: (cant_recibida, motivo|None)}.
    Descuenta lo ENVIADO del stock (salió del acopio) y libera la reserva.
    Estado final: 'recibido' si todo completo, 'incompleto' si algo faltó.
    Devuelve el envío actualizado o None si el token no existe / ya cerrado."""
    con = _con()
    con.isolation_level = None
    con.execute("BEGIN IMMEDIATE")
    try:
        e = con.execute("SELECT * FROM envios WHERE token = ? AND estado = 'despachado'",
                        (token,)).fetchone()
        if not e:
            con.execute("ROLLBACK")
            return None
        items = con.execute("SELECT * FROM envio_items WHERE envio_id = ?", (e["id"],)).fetchall()
        incompleto = False
        for it in items:
            rec, motivo = recepciones.get(it["id"], (it["cant_enviada"], None))
            rec = float(rec if rec is not None else 0)
            if rec < (it["cant_enviada"] or 0):
                incompleto = True
            con.execute("UPDATE envio_items SET cant_recibida = ?, motivo = ? WHERE id = ?",
                        (rec, motivo, it["id"]))
            # Descuento definitivo: lo enviado sale del stock y se libera la reserva.
            con.execute(
                "UPDATE stock SET cantidad = MAX(0, IFNULL(cantidad,0) - ?), "
                "reservado = MAX(0, IFNULL(reservado,0) - ?), actualizado_en = datetime('now') "
                "WHERE id = ? AND cantidad IS NOT NULL", (it["cant_enviada"], it["cant_enviada"], it["stock_id"]))
            con.execute(  # renglón sin cantidad numérica: solo libera reserva
                "UPDATE stock SET reservado = MAX(0, IFNULL(reservado,0) - ?) "
                "WHERE id = ? AND cantidad IS NULL", (it["cant_enviada"], it["stock_id"]))
        estado = "incompleto" if incompleto else "recibido"
        con.execute("UPDATE envios SET estado = ?, nota = COALESCE(?, nota), "
                    "cerrado_en = datetime('now') WHERE id = ?", (estado, nota, e["id"]))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return por_token(token)


BASE_URL = "https://redacopioyrefugio.com"


def url_entrega(token):
    return f"{BASE_URL}/api/entrega/{token}"


def url_qr(token):
    return f"{BASE_URL}/api/entrega/{token}.png"


def qr_png(token):
    """PNG del QR que apunta a la página de la entrega. OJO: en RGB de 8 bits —
    qrcode genera PNG de 1 bit y WhatsApp lo rechaza (error 131053)."""
    import io
    import qrcode
    img = qrcode.make(url_entrega(token)).get_image().convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def parse_lineas(punto_id, texto):
    """Parsea el mensaje del acopio armando un envío: una línea por insumo,
    'pañales 40' o '40 pañales'. Matchea contra su stock disponible.
    Devuelve (items=[(stock_id, cantidad)], errores=[texto])."""
    import re as _re

    import stock as _stock
    inv = [r for r in _stock.de_punto(punto_id)]
    items, errores, usados = [], [], set()
    for linea in _re.split(r"[\n,;]+", texto or ""):
        linea = linea.strip()
        if not linea:
            continue
        m = _re.search(r"(\d+(?:[.,]\d+)?)", linea)
        if not m:
            errores.append(f"«{linea}»: no veo la cantidad")
            continue
        cant = float(m.group(1).replace(",", "."))
        nombre = _stock._norm((linea[:m.start()] + " " + linea[m.end():]).strip())
        if not nombre:
            errores.append(f"«{linea}»: no veo el insumo")
            continue
        hit = None
        for r in inv:
            if r["id"] in usados:
                continue
            if r["item_norm"] == nombre or nombre in r["item_norm"] or r["item_norm"] in nombre:
                hit = r
                break
        if not hit:
            errores.append(f"«{linea}»: no está en tu inventario")
            continue
        disp = disponible(hit)
        if disp is not None and cant > disp:
            errores.append(f"{hit['item']}: solo hay {disp:g} {hit['unidad'] or ''}".strip())
            continue
        usados.add(hit["id"])
        items.append((hit["id"], cant))
    return items, errores


def notificar_rep(rep_id, texto):
    """Avisa a un representante por SU canal: Telegram si tiene telegram_id
    (siempre entregable), si no WhatsApp (wa_phone). Best-effort."""
    import json as _json
    import urllib.request as _rq
    rep = db.representante(rep_id)
    if not rep:
        return False
    try:
        if rep.get("telegram_id"):
            from config import APOYO_BOT_TOKEN
            data = _json.dumps({"chat_id": rep["telegram_id"], "text": texto}).encode()
            req = _rq.Request(f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage",
                              data=data, headers={"Content-Type": "application/json"})
            with _rq.urlopen(req, timeout=20) as r:
                return _json.load(r).get("ok", False)
        if rep.get("wa_phone"):
            import wa_api
            return bool(wa_api.enviar_texto("58" + rep["wa_phone"], texto))
    except Exception as e:  # noqa: BLE001
        print("notificar_rep error:", e)
    return False


def manifiesto_texto(envio):
    """Resumen legible del envío (para chats y notificaciones)."""
    L = [f"📦 Envío de {envio.get('de_punto') or 'acopio'} → {envio.get('a_punto') or 'destino'}"]
    for it in envio["items"]:
        c = f"{it['cant_enviada']:g} {it['unidad'] or ''}".strip()
        L.append(f"• {it['item']} — {c}")
    return "\n".join(L)


def cancelar(envio_id):
    """Cancela un envío despachado y LIBERA las reservas (no descuenta nada)."""
    con = _con()
    con.isolation_level = None
    con.execute("BEGIN IMMEDIATE")
    try:
        e = con.execute("SELECT id FROM envios WHERE id = ? AND estado = 'despachado'",
                        (envio_id,)).fetchone()
        if not e:
            con.execute("ROLLBACK")
            return False
        for it in con.execute("SELECT * FROM envio_items WHERE envio_id = ?", (envio_id,)).fetchall():
            con.execute("UPDATE stock SET reservado = MAX(0, IFNULL(reservado,0) - ?) WHERE id = ?",
                        (it["cant_enviada"], it["stock_id"]))
        con.execute("UPDATE envios SET estado = 'cancelado', cerrado_en = datetime('now') "
                    "WHERE id = ?", (envio_id,))
        con.execute("COMMIT")
        return True
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
