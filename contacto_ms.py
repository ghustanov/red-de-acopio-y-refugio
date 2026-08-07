"""Solicitudes de contacto a Manos Solidarias — Red de Acopio y Refugio.

La web ya NO muestra el número de los aliados (anti-acoso / anti-estafa).
Quien quiera contactar a una Mano Solidaria llena un mini-formulario y aquí:
  1. La solicitud queda en BITÁCORA (tabla solicitudes_ms — quién pidió, a quién,
     cuándo, qué respondió y cuándo se liberaron los números).
  2. Se le avisa al aliado por SU canal de origen — mismo cableado para todos:
     Telegram (telegram_id) o WhatsApp (wa_phone / su contacto) — con botones
     ✅ Aceptar / ❌ Ahora no.
  3. Solo si ACEPTA se liberan AMBOS números (aliado ↔ interesado).
  4. Recordatorio a las 12 h; sin respuesta en 24 h la solicitud EXPIRA y se
     avisa al interesado.

Compartido por backend.py (crea la solicitud), bot_apoyo.py y bot_wa.py
(procesan la respuesta del aliado, callback/reply 'msq|<id>|si|no') y
run_once.py (revisar(): recordatorio + expiración, corre cada ~5 min).
Solo stdlib + wa_api, igual que el resto del proyecto.
"""
import html
import json
import re
import secrets
import urllib.parse
import urllib.request

import db
import wa_api
from wa_util import canonico
from config import APOYO_BOT_TOKEN, GRUPO_MODERADORES, WA_TPL_LLAMADO

API_APOYO = f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}"


def init_tabla():
    con = db.conectar()
    con.execute(
        "CREATE TABLE IF NOT EXISTS solicitudes_ms ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "aliado_id INTEGER, aliado_nombre TEXT, "
        "nombre TEXT, contacto TEXT, mensaje TEXT, "          # interesado (privado)
        "canal TEXT, destino TEXT, token TEXT UNIQUE, "       # tg / wa / sin_canal
        "estado TEXT DEFAULT 'pendiente', "                   # pendiente/aceptada/rechazada/expirada
        "creado_en TEXT DEFAULT CURRENT_TIMESTAMP, "
        "recordado_en TEXT, respondido_en TEXT, notif_interesado TEXT)")
    con.commit()
    con.close()


def _tg(metodo, **params):
    req = urllib.request.Request(
        f"{API_APOYO}/{metodo}", data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001 — best-effort, igual que backend.py
        print("contacto_ms tg error:", e)
        return {}


def _wa_dest(numero):
    """Destino Meta ('58424…') desde un número en formato libre. Respeta un
    prefijo internacional explícito con '+' (p. ej. +52… de México)."""
    c = canonico(numero)
    if c:
        return "58" + c
    d = re.sub(r"\D", "", numero or "")
    if str(numero or "").strip().startswith("+") and 10 <= len(d) <= 15:
        return d
    return None


def solicitud(sid):
    con = db.conectar()
    row = con.execute("SELECT * FROM solicitudes_ms WHERE id = ?", (sid,)).fetchone()
    con.close()
    return dict(row) if row else None


def por_token(token):
    con = db.conectar()
    row = con.execute("SELECT * FROM solicitudes_ms WHERE token = ?", (token,)).fetchone()
    con.close()
    return dict(row) if row else None


def crear(aliado_id, nombre, contacto, mensaje):
    """Registra la solicitud en la bitácora y resuelve el canal del aliado.
    Devuelve dict con id/token/canal, o None si el aliado no existe/no está
    aprobado. NO notifica (eso lo hace notificar_aliado, para separar I/O)."""
    ali = db.aliado(aliado_id)
    if not ali or ali.get("estado") != "aprobado":
        return None
    if ali.get("telegram_id"):
        canal, destino = "tg", str(ali["telegram_id"])
    else:
        destino = _wa_dest(ali.get("wa_phone") or "") or _wa_dest(ali.get("contacto") or "")
        canal = "wa" if destino else "sin_canal"
    init_tabla()
    token = secrets.token_urlsafe(16)
    con = db.conectar()
    cur = con.execute(
        "INSERT INTO solicitudes_ms (aliado_id, aliado_nombre, nombre, contacto, "
        "mensaje, canal, destino, token) VALUES (?,?,?,?,?,?,?,?)",
        (aliado_id, ali.get("nombre"), nombre, contacto, mensaje, canal, destino, token))
    con.commit()
    sid = cur.lastrowid
    con.close()
    return {"id": sid, "token": token, "canal": canal, "aliado": ali}


def notificar_aliado(sid, recordatorio=False):
    """Aviso al aliado por su canal de origen, con botones msq|<id>|si / no."""
    sol = solicitud(sid)
    if not sol or sol["estado"] != "pendiente":
        return False
    quien = sol["nombre"] or "Una persona"
    motivo = (sol["mensaje"] or "").strip()
    pre_tg = "⏰ <b>Recordatorio</b> (la solicitud expira pronto)\n\n" if recordatorio else ""
    pre_wa = "⏰ *Recordatorio* (la solicitud expira pronto)\n\n" if recordatorio else ""

    if sol["canal"] == "tg":
        texto = (pre_tg + "🤝 <b>Solicitud de contacto</b> — Red de Acopio y Refugio\n\n"
                 f"👤 <b>{html.escape(quien)}</b> vio tu ayuda de Manos Solidarias en la web "
                 "y pide tu contacto."
                 + (f"\n✍️ «{html.escape(motivo)}»" if motivo else "")
                 + "\n\n¿Compartimos tu número con esta persona? Si aceptas, tú también "
                   "recibirás el suyo para coordinar.\n"
                   "⏳ Si no respondes en 24 horas, la solicitud expira sola.")
        kb = {"inline_keyboard": [[
            {"text": "✅ Sí, compartir", "callback_data": f"msq|{sol['id']}|si"},
            {"text": "❌ Ahora no", "callback_data": f"msq|{sol['id']}|no"}]]}
        r = _tg("sendMessage", chat_id=int(sol["destino"]), text=texto,
                parse_mode="HTML", reply_markup=kb)
        return bool(r.get("ok"))

    if sol["canal"] == "wa":
        botones = [(f"msq|{sol['id']}|si", "🤝 Puedo ayudar"),
                   (f"msq|{sol['id']}|no", "🛑 Ahora no puedo")]
        cuerpo = (pre_wa + "🤝 *Solicitud de contacto* — Red de Acopio y Refugio\n\n"
                  f"*{quien}* vio tu ayuda de Manos Solidarias en la web y pide tu contacto."
                  + (f"\n✍️ «{motivo}»" if motivo else "")
                  + "\n\nSi aceptas, compartimos tu número y tú recibes el suyo para coordinar."
                    "\n⏳ Si no respondes en 24 horas, la solicitud expira sola.")
        try:
            if WA_TPL_LLAMADO:
                # Plantilla aprobada: llega SIEMPRE, aunque la ventana de 24 h esté cerrada.
                r = wa_api.enviar_plantilla(
                    sol["destino"], WA_TPL_LLAMADO,
                    variables=("la web de la Red",
                               "tu contacto para coordinar: " + (motivo or "tu ayuda solidaria"),
                               quien),
                    payloads=[b[0] for b in botones])
                if r.get("messages"):
                    return True
                print("contacto_ms: plantilla rechazada, fallback a botones:", r)
            r = wa_api.enviar_botones(sol["destino"], cuerpo, botones)
            return bool(r.get("messages"))
        except Exception as e:  # noqa: BLE001
            print("contacto_ms wa error:", e)
            return False

    # sin_canal: no hay cómo avisarle automáticamente → que lo resuelvan los moderadores
    avisar_moderadores(
        f"⚠️ Solicitud de contacto #{sol['id']} para {sol['aliado_nombre'] or '?'} "
        "NO se pudo notificar (el aliado no tiene Telegram ni WhatsApp válido). "
        "Contáctalo a mano y responde desde su ficha.")
    return False


def _avisar_interesado(sol, texto):
    """WhatsApp al interesado (best-effort: si nunca ha escrito al bot, Meta puede
    rechazar por ventana de 24 h — le queda la página de estado con su token)."""
    dest = _wa_dest(sol["contacto"])
    if not dest:
        return "sin_numero"
    try:
        r = wa_api.enviar_texto(dest, texto)
        return "wa_ok" if r.get("messages") else "wa_fallo"
    except Exception as e:  # noqa: BLE001
        print("contacto_ms aviso interesado error:", e)
        return "wa_fallo"


def avisar_moderadores(texto):
    if GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        _tg("sendMessage", chat_id=GRUPO_MODERADORES, text=texto)


def responder(sid, acepta):
    """Procesa la decisión del aliado. Actualiza bitácora, avisa al interesado y a
    los moderadores. Devuelve datos para que el bot arme SU mensaje al aliado
    (nombre/contacto/wa_link del interesado si aceptó), o None si ya no estaba
    pendiente (doble toque, expirada, etc.)."""
    sol = solicitud(sid)
    if not sol or sol["estado"] != "pendiente":
        return None
    ali = db.aliado(sol["aliado_id"]) or {}
    quien = sol["nombre"] or ""

    if acepta:
        n_ali = _wa_dest(ali.get("contacto") or ali.get("wa_phone") or "")
        t = (f"✅ ¡Buenas noticias{', ' + quien if quien else ''}! "
             f"{ali.get('nombre') or 'La persona de Manos Solidarias'} aceptó tu solicitud "
             "de contacto en Red de Acopio y Refugio 🏠\n\n")
        if n_ali:
            t += f"Escríbele por WhatsApp: https://wa.me/{n_ali}\n"
        t += f"📞 Su número: {ali.get('contacto') or ali.get('wa_phone') or 'ver web'}\n\n"
        t += "Coordina en lugares seguros y verifica identidad. 💙"
        notif = _avisar_interesado(sol, t)
        estado = "aceptada"
    else:
        t = (f"ℹ️ Hola{', ' + quien if quien else ''}. "
             f"{ali.get('nombre') or 'La persona de Manos Solidarias'} no puede atender tu "
             "solicitud por ahora 🙏. Puedes intentar con otra Mano Solidaria en "
             "la web de Red de Acopio y Refugio. 💙")
        notif = _avisar_interesado(sol, t)
        estado = "rechazada"

    con = db.conectar()
    con.execute("UPDATE solicitudes_ms SET estado=?, respondido_en=datetime('now'), "
                "notif_interesado=? WHERE id=?", (estado, notif, sid))
    con.commit()
    con.close()

    avisar_moderadores(
        f"🤝 Solicitud de contacto #{sid}: {sol['aliado_nombre'] or '?'} "
        f"{'ACEPTÓ ✅ (números cruzados)' if acepta else 'rechazó ❌'} "
        f"la petición de {quien or 'una persona de la web'}. "
        f"(aviso al interesado: {notif})")

    if not acepta:
        return {"acepto": False, "nombre": quien}
    wa_i = _wa_dest(sol["contacto"])
    msg_pre = (f"Hola{' ' + quien if quien else ''} 👋, soy "
               f"{ali.get('nombre') or 'de Manos Solidarias'} (Red de Acopio y Refugio). "
               "Acepté tu solicitud y sí puedo ayudarte. ¿Cómo coordinamos?")
    link = ("https://wa.me/" + wa_i + "?text=" + urllib.parse.quote(msg_pre)) if wa_i else None
    return {"acepto": True, "nombre": quien, "contacto": sol["contacto"], "wa_link": link}


# R1: relay para /api/atender (pedido de ayuda -> solicitante opt-in).
def init_tabla_atencion():
    con = db.conectar()
    con.execute(
        "CREATE TABLE IF NOT EXISTS solicitudes_atencion ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "llamado_id INTEGER, ayuda_nombre TEXT, ayuda_contacto TEXT, nota TEXT, "
        "sol_nombre TEXT, sol_contacto TEXT, canal TEXT, destino TEXT, token TEXT UNIQUE, "
        "estado TEXT DEFAULT 'pendiente', creado_en TEXT DEFAULT CURRENT_TIMESTAMP, "
        "recordado_en TEXT, respondido_en TEXT, notif_ayuda TEXT)")
    con.commit()
    con.close()


def crear_atencion(llamado_id, ayuda_nombre, ayuda_contacto, nota):
    con = db.conectar()
    sol = con.execute(
        "SELECT nombre, contacto FROM solicitudes_web WHERE llamado_id = ?",
        (llamado_id,)).fetchone()
    con.close()
    if not sol:
        return None
    destino = _wa_dest(sol["contacto"] or "")
    canal = "wa" if destino else "sin_canal"
    init_tabla_atencion()
    token = secrets.token_urlsafe(16)
    con = db.conectar()
    cur = con.execute(
        "INSERT INTO solicitudes_atencion "
        "(llamado_id, ayuda_nombre, ayuda_contacto, nota, sol_nombre, sol_contacto, canal, destino, token) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (llamado_id, ayuda_nombre, ayuda_contacto, nota, sol["nombre"], sol["contacto"], canal, destino, token))
    con.commit()
    sid = cur.lastrowid
    con.close()
    return {"id": sid, "token": token, "canal": canal}


def solicitud_atencion(sid):
    con = db.conectar()
    row = con.execute("SELECT * FROM solicitudes_atencion WHERE id = ?", (sid,)).fetchone()
    con.close()
    return dict(row) if row else None


def notificar_solicitante_atencion(sid, recordatorio=False):
    sol = solicitud_atencion(sid)
    if not sol or sol["estado"] != "pendiente":
        return False
    if sol["canal"] != "wa":
        avisar_moderadores(
            f"⚠️ Atención #{sid} para pedido #{sol['llamado_id']} no se pudo notificar: "
            "el solicitante no tiene WhatsApp válido. Resolver manualmente.")
        return False
    pre = "⏰ *Recordatorio* (la solicitud expira pronto)\n\n" if recordatorio else ""
    cuerpo = (pre + "🤝 *Alguien quiere ayudarte* — Red de Acopio y Refugio\n\n"
              f"*{sol['ayuda_nombre']}* dejó sus datos para atender tu pedido."
              + (f"\n✍️ «{sol['nota']}»" if sol.get("nota") else "")
              + "\n\n¿Compartimos tu número con esta persona? Si aceptas, también recibirás su contacto."
                "\n⏳ Si no respondes en 24 horas, la solicitud expira sola.")
    botones = [(f"atq|{sol['id']}|si", "🤝 Sí, compartir"),
               (f"atq|{sol['id']}|no", "🛑 Ahora no")]
    try:
        if WA_TPL_LLAMADO:
            r = wa_api.enviar_plantilla(
                sol["destino"], WA_TPL_LLAMADO,
                variables=("la web de la Red", "tu pedido de ayuda", sol["ayuda_nombre"]),
                payloads=[b[0] for b in botones])
            if r.get("messages"):
                return True
        r = wa_api.enviar_botones(sol["destino"], cuerpo, botones)
        return bool(r.get("messages"))
    except Exception as e:
        print("contacto_ms atencion wa error:", e)
        return False


def responder_atencion(sid, acepta):
    sol = solicitud_atencion(sid)
    if not sol or sol["estado"] != "pendiente":
        return None
    estado = "aceptada" if acepta else "rechazada"
    notif = "no_aplica"
    if acepta:
        dest_ayuda = _wa_dest(sol["ayuda_contacto"])
        texto = (f"✅ ¡Buenas noticias{', ' + sol['sol_nombre'] if sol.get('sol_nombre') else ''}! "
                 f"{sol['ayuda_nombre'] or 'Una persona'} aceptó atender tu pedido.\n\n"
                 f"📞 Su contacto: {sol['ayuda_contacto']}\n\n")
        if dest_ayuda:
            texto += f"WhatsApp: https://wa.me/{dest_ayuda}\n"
        texto += "Coordinen en lugares seguros y verifica identidad. 💙"
        notif = _avisar_interesado({"contacto": sol["sol_contacto"]}, texto)
    con = db.conectar()
    con.execute("UPDATE solicitudes_atencion SET estado=?, respondido_en=datetime('now'), "
                "notif_ayuda=? WHERE id=?", (estado, notif, sid))
    con.commit()
    con.close()
    avisar_moderadores(
        f"🤝 Atención #{sid} pedido #{sol['llamado_id']}: solicitante "
        f"{'ACEPTÓ ✅ (números cruzados)' if acepta else 'rechazó ❌'} "
        f"la ayuda de {sol['ayuda_nombre'] or 'web'}.")
    if not acepta:
        return {"acepto": False, "nombre": sol.get("sol_nombre")}
    wa_sol = _wa_dest(sol["sol_contacto"])
    msg_pre = (f"Hola 👋, soy {sol['ayuda_nombre'] or 'la persona que quiere ayudarte'} "
               "(Red de Acopio y Refugio). Aceptaste compartir tu contacto para coordinar.")
    link = ("https://wa.me/" + wa_sol + "?text=" + urllib.parse.quote(msg_pre)) if wa_sol else None
    return {"acepto": True, "nombre": sol.get("sol_nombre"), "contacto": sol["sol_contacto"], "wa_link": link}


def revisar():
    """Recordatorio a las 12 h y expiración a las 24 h. Lo llama run_once.py
    (timer de ~5 min). Devuelve cuántas solicitudes procesó."""
    init_tabla()
    con = db.conectar()
    rec = [dict(r) for r in con.execute(
        "SELECT id FROM solicitudes_ms WHERE estado='pendiente' AND recordado_en IS NULL "
        "AND julianday('now') - julianday(creado_en) >= 0.5 "
        "AND julianday('now') - julianday(creado_en) < 1.0").fetchall()]
    exp = [dict(r) for r in con.execute(
        "SELECT * FROM solicitudes_ms WHERE estado='pendiente' "
        "AND julianday('now') - julianday(creado_en) >= 1.0").fetchall()]
    con.close()

    n = 0
    for s in rec:
        if notificar_aliado(s["id"], recordatorio=True):
            con = db.conectar()
            con.execute("UPDATE solicitudes_ms SET recordado_en=datetime('now') WHERE id=?",
                        (s["id"],))
            con.commit()
            con.close()
            n += 1

    for s in exp:
        quien = s["nombre"] or ""
        t = (f"⌛ Hola{', ' + quien if quien else ''}. "
             f"{s['aliado_nombre'] or 'La persona de Manos Solidarias'} no respondió tu "
             "solicitud de contacto en 24 horas, así que la cerramos. Puedes intentar "
             "con otra Mano Solidaria en la web de Red de Acopio y Refugio. 💙")
        notif = _avisar_interesado(s, t)
        con = db.conectar()
        con.execute("UPDATE solicitudes_ms SET estado='expirada', "
                    "respondido_en=datetime('now'), notif_interesado=? WHERE id=?",
                    (notif, s["id"]))
        con.commit()
        con.close()
        avisar_moderadores(
            f"⌛ Solicitud de contacto #{s['id']} para {s['aliado_nombre'] or '?'} "
            f"expiró sin respuesta (24 h). Interesado: {quien or 'web'} (aviso: {notif}).")
        n += 1
    return n
