"""Bot de WhatsApp — Red de Acopio y Refugio.

Canal PARALELO al bot de Telegram (bot_apoyo.py, que NO se toca). Reutiliza:
  - db.py       → crear/leer registros (misma angeles.db, misma web)
  - wa_util.py  → normalizar teléfonos para el match de identidad (Opción B)
  - wa_api.py   → hablar con la Cloud API de Meta

Alcance: CAPTACIÓN + REGISTRO de los 3 roles + vinculación por teléfono (Opción B)
+ INGESTIÓN de inventario (foto/PDF/Excel/Word/voz) reusando captura.py
+ LLAMADOS de apoyo del Voluntariado (paridad con /llamado de Telegram; la
triangulación se reusa de bot_apoyo con import perezoso solo en ese hilo). El
procesado pesado (Claude/Whisper) corre en un HILO para responder 200 al webhook
al instante (si no, Meta reintenta y duplica).

Moderación: al completar un registro se avisa al MISMO grupo de moderadores de
Telegram con los MISMOS botones (apr|/apra|/aprv|), así los coordinadores
aprueban desde Telegram como siempre (los handlers viven en bot_apoyo.py).
"""
import html
import json
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, request

import captura
import db
import efimero
import envios
import stock
from config import (APOYO_BOT_TOKEN, GRUPO_MODERADORES, AUTO_APROBAR,
                    AUTO_PUBLICAR_PEDIDOS, CATS_PEDIR_MODERADAS,
                    BASE, WEB_DIR, WHATSAPP_VERIFY_TOKEN)
from wa_util import canonico, coincide
import wa_api

app = Flask(__name__)

# Estado de la conversación: phone -> {"flujo", "paso", "datos"}. El dict es solo
# el scratch DEL MENSAJE en curso: _despachar lo hidrata desde la DB (efimero) al
# entrar y lo persiste al salir → N workers de gunicorn comparten la conversación
# y un restart no la pierde. (Antes vivía solo en memoria y obligaba a -w 1.)
estados = {}

# --- Catálogos (duplicados a propósito para NO importar bot_apoyo.py) ---
TIPOS = [
    ("acopio", "🏬 Centro de acopio"),
    ("refugio", "🏠 Refugio"),
    ("hospital", "🏥 Hospital"),
    ("cdi", "➕ CDI"),
    ("maternidad", "🤰 Maternidad"),
    ("clinica", "🩺 Clínica"),
    ("fundacion", "🏛 Fundación"),
    ("otro", "📍 Otro punto"),
]
TIPOS_LABEL = {k: v for k, v in TIPOS}

ALIADO_CATS = [
    ("comida", "🍲 Comida"),
    ("herramientas", "🔦 Herramientas/linternas"),
    ("transporte", "🚐 Transporte"),
    ("agua", "💧 Agua"),
    ("carga", "🔌 Carga de teléfonos"),
    ("salud", "🩹 Salud / 1ros auxilios"),
    ("hospedaje", "🛏️ Hospedaje temporal"),
    ("remoto", "💻 Remoto / En línea"),
    ("otro", "📦 Otra ayuda"),
]
ALIADO_LABEL = {k: v for k, v in ALIADO_CATS}

# Ficha del centro: campos (mismos que Telegram/FICHA_COLS; el contacto es el
# propio número de WhatsApp → no se pregunta). modo: "sino" / "sino_na" /
# "no_posee" / "no_aplica" (botón + permite escribir) / None (solo texto).
# solo / salvo: la pregunta aplica SOLO a esos tipos / a todos SALVO esos.
# (Campos nuevos SIEMPRE al final: `fi` persiste en efimero y un índice corrido
# re-preguntaría mal a quien esté a mitad de registro.)
FICHA = [
    {"campo": "tipo_inmueble",    "modo": None,        "preg": "🏚️ ¿Qué *tipo de inmueble* es? (escuela, iglesia, galpón, clínica, casa…)", "salvo": ("fundacion",)},
    {"campo": "aforo",            "modo": "no_aplica", "preg": "👥 ¿Cuál es el *aforo*? (cuántas personas caben)", "salvo": ("fundacion",)},
    {"campo": "banos",            "modo": "no_posee",  "preg": "🚻 ¿Cuántos *baños* tiene?", "salvo": ("fundacion",)},
    {"campo": "duchas",           "modo": "no_posee",  "preg": "🚿 ¿Cuántas *duchas/regaderas* tiene?", "salvo": ("fundacion",)},
    {"campo": "cocina",           "modo": "sino",      "preg": "🍳 ¿Posee *cocina*?", "salvo": ("fundacion",)},
    {"campo": "agua_potable",     "modo": "sino",      "preg": "🚰 ¿Tiene *agua potable*?", "salvo": ("fundacion",)},
    {"campo": "electricidad",     "modo": "sino",      "preg": "⚡ ¿Tiene *electricidad*?", "salvo": ("fundacion",)},
    {"campo": "planta",           "modo": "sino",      "preg": "🔋 ¿Tiene *planta o generador*?", "salvo": ("fundacion",)},
    {"campo": "camas",            "modo": "no_aplica", "preg": "🛏️ ¿Cuántas *camas disponibles* hay?", "salvo": ("fundacion",)},
    {"campo": "acceso_vehiculos", "modo": "sino",      "preg": "🚛 ¿Tiene *acceso para vehículos*? (ambulancias, camiones)"},
    {"campo": "accesibilidad",    "modo": "sino",      "preg": "♿ ¿Es *accesible para personas con discapacidad*? (rampas)", "salvo": ("fundacion",)},
    {"campo": "almacenamiento",   "modo": "no_aplica", "preg": "📦 ¿Qué *capacidad de almacenamiento* tiene?"},
    {"campo": "refrigeracion",    "modo": "sino_na",   "preg": "❄️ ¿Tiene *refrigeración / cadena de frío*?"},
    {"campo": "ubicacion",        "modo": None,        "preg": "📍 ¿Cuál es la *dirección exacta*? Lo más precisa posible (estado, ciudad, sector, calle) + un punto de referencia. Mientras más precisa, más fácil que lleguen a ti.", "core": True},
    {"campo": "horario",          "modo": None,        "preg": "🕐 ¿Cuál es el *horario de atención*? (ej. 24 horas, 8am-6pm)", "core": True},
    {"campo": "responsable",      "modo": None,        "preg": "🧑‍💼 ¿Quién es la *persona responsable* del punto?"},
    {"campo": "rif",              "modo": "no_posee",  "preg": "🪪 ¿Cuál es el *RIF* de la fundación? (ej. J-12345678-9)\n\nSi aún no están constituidos legalmente, toca *No posee* — igual pueden participar.", "solo": ("fundacion",), "core": True},
    {"campo": "cobertura",        "modo": None,        "preg": "🗺️ ¿En qué *zonas puede entregar o servir* la fundación? (ej. Caracas y Miranda; todo el país)", "solo": ("fundacion",), "core": True},
    {"campo": "areas",            "modo": None,        "preg": "🎯 ¿Qué *áreas atiende* la fundación? Nómbralas separadas por comas (ej. animales, salud, niños, alimentación, rescate).\n\n👉 Con esto te avisamos los *pedidos de ayuda y llamados* de tu área, aunque no tengas inventario cargado.", "solo": ("fundacion",), "core": True},
]
_FICHA_VAL = {"si": "Sí", "no": "No", "np": "No posee", "na": "No aplica"}


def _ficha_aplica(q, tipo):
    """¿Esta pregunta de ficha corresponde al tipo de punto? (solo/salvo)."""
    if q.get("solo") and tipo not in q["solo"]:
        return False
    if q.get("salvo") and tipo in q["salvo"]:
        return False
    return True


def _ficha_en_alta(q, d):
    """En el ALTA solo va la ficha esencial (core: dirección y horario; fundaciones
    suman RIF/cobertura/áreas — su filtro). El resto (aforo, baños, camas…) se
    completa después desde el menú «Completar ficha» (ficha_full)."""
    return bool(q.get("core")) or bool(d.get("ficha_full"))


# ────────────────────────── Telegram (solo para avisar al moderador) ──────────
def _tg_moderadores(prefijo, cabecera, cuerpo, forzar_manual=False):
    """Avisa al grupo de moderadores con los MISMOS botones que Telegram.
    prefijo: 'apr|<rid>' | 'apra|<aid>' | 'aprv|<vid>'. `forzar_manual`: el
    registro quedó pendiente aunque AUTO_APROBAR esté activo (fundaciones)."""
    if not (APOYO_BOT_TOKEN and GRUPO_MODERADORES):
        return
    if AUTO_APROBAR and not forzar_manual:
        kb = {"inline_keyboard": [[{"text": "❌ Quitar de la web", "callback_data": f"{prefijo}|no"}]]}
        cabecera += " · ✅ aprobado automáticamente"
    else:
        kb = {"inline_keyboard": [[{"text": "✅ Aprobar", "callback_data": f"{prefijo}|ok"},
                                   {"text": "❌ Rechazar", "callback_data": f"{prefijo}|no"}]]}
    payload = json.dumps({
        "chat_id": GRUPO_MODERADORES, "parse_mode": "HTML",
        "text": f"{cabecera}\n{cuerpo}\n\n📱 <i>vía WhatsApp</i>",
        "reply_markup": kb,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:  # noqa: BLE001
        print("tg moderadores error:", e)


# ────────────────────────────── Identidad (Opción B) ─────────────────────────
def _tg_botones(chat_id, texto, botones):
    """Mensaje de Telegram con botones inline (los callbacks llegan a bot_apoyo,
    que maneja los prefijos colok|/colrm| compartidos con este bot), best-effort."""
    if not (APOYO_BOT_TOKEN and chat_id):
        return
    kb = {"inline_keyboard": [[{"text": t, "callback_data": cd} for cd, t in fila]
                              for fila in botones]}
    payload = json.dumps({"chat_id": chat_id, "parse_mode": "HTML",
                          "text": texto, "reply_markup": kb}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:  # noqa: BLE001
        print("tg botones error:", e)


def _tg_texto(chat_id, texto):
    """Mensaje simple de Telegram (aviso informativo, sin botones), best-effort."""
    if not (APOYO_BOT_TOKEN and chat_id):
        return
    payload = json.dumps({"chat_id": chat_id, "text": texto}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:  # noqa: BLE001
        print("tg texto error:", e)


def _tg_foto_moderadores(foto_bytes, caption, kb):
    """sendPhoto (multipart) al grupo de moderadores para pedidos CON imagen —
    mismo patrón que backend._tg_foto (pedidos de la web). La imagen viaja
    adjunta, no por URL (vive en la carpeta privada pedidos_img/ hasta publicar)."""
    if not (APOYO_BOT_TOKEN and GRUPO_MODERADORES):
        return
    boundary = "----angpedwa" + str(int(time.time() * 1000))
    pre = []

    def campo(nombre, valor):
        pre.append(("--" + boundary + "\r\n"
                    'Content-Disposition: form-data; name="' + nombre + '"\r\n\r\n'
                    + str(valor) + "\r\n").encode())

    campo("chat_id", GRUPO_MODERADORES)
    campo("caption", caption[:1000])
    campo("parse_mode", "HTML")
    campo("reply_markup", json.dumps(kb))
    pre.append(("--" + boundary + "\r\n"
                'Content-Disposition: form-data; name="photo"; filename="pedido.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n").encode())
    body = b"".join(pre) + foto_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendPhoto",
        data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:  # noqa: BLE001
        print("tg foto moderadores error:", e)


_AVISO_SEGURO = ("\n\n⚠️ La Red solo conecta; no verifica ni supervisa la ayuda. "
                 "Coordina con precaución y en lugares seguros.")
_AVISO_SALUD = ("\n🧠 Apoyo voluntario entre personas: no sustituye atención médica ni "
                "psicológica profesional. En crisis, 911.")


def _coletilla(items):
    """Disclaimer para los mensajes de match por WhatsApp."""
    t = (items or "").lower()
    extra = _AVISO_SALUD if any(k in t for k in ("salud", "medicin", "psic",
                                                 "emocional", "mental")) else ""
    return _AVISO_SEGURO + extra


_TABLAS = (("representantes", "centro", "telefono"),
           ("aliados", "aliado", "contacto"),
           ("voluntarios", "voluntario", "contacto"))


RECHAZOS_BLOQUEO = 3   # registros rechazados que bloquean NUEVOS registros del número


def _rechazos_de(phone):
    """Cuántos registros RECHAZADOS acumula este número entre todas las tablas de roles.
    El rechazo libera el número (resolver_identidad lo ignora), pero reincidir tantas
    veces huele a spam: se bloquean nuevos registros (pedir ayuda sigue abierto)."""
    wa = canonico(phone)
    if not wa:
        return 0
    n = 0
    con = db.conectar()
    try:
        for tabla, _rol, col in _TABLAS:
            n += con.execute(
                f"SELECT COUNT(*) FROM {tabla} WHERE estado = 'rechazado' "
                f"AND (wa_phone = ? OR {col} = ?)", (wa, wa)).fetchone()[0]
    finally:
        con.close()
    return n


def resolver_identidad(phone):
    """Quién es este número de WhatsApp:
      {"tipo":"vinculado", ...}  ya enganchado por wa_phone
      {"tipo":"match", ...}      coincide un registro de Telegram sin vincular
      {"tipo":"nuevo"}           desconocido
    """
    wa = canonico(phone)
    con = db.conectar()
    con.row_factory = sqlite3.Row
    try:
        if wa:
            for tabla, rol, _ in _TABLAS:
                r = con.execute(
                    # Un registro RECHAZADO no ancla el número: la persona puede volver
                    # a registrarse desde cero (el freno anti-spam vive en _iniciar).
                    f"SELECT * FROM {tabla} WHERE wa_phone = ? AND estado != 'rechazado' "
                    "ORDER BY id DESC LIMIT 1",
                    (wa,)).fetchone()
                if r:
                    return {"tipo": "vinculado", "rol": rol, "tabla": tabla, "registro": dict(r)}
        for tabla, rol, col in _TABLAS:
            filas = con.execute(
                f"SELECT * FROM {tabla} WHERE estado != 'rechazado' "
                f"AND (wa_phone IS NULL OR wa_phone = '')").fetchall()
            for r in filas:
                if coincide(phone, r[col]):
                    return {"tipo": "match", "rol": rol, "tabla": tabla, "registro": dict(r)}
        return {"tipo": "nuevo"}
    finally:
        con.close()


def _set_wa_phone(tabla, rid, phone):
    con = db.conectar()
    con.execute(f"UPDATE {tabla} SET wa_phone = ? WHERE id = ?", (canonico(phone), rid))
    con.commit()
    con.close()


# ─────────────────────────────── Flujos de registro ──────────────────────────
# Pedir ayuda: mismas categorías y labels que el formulario web (backend.py CATS_PEDIR),
# para que el llamado salga igual en la web. (id de lista, título ≤24, descripción)
CATS_PEDIR = [
    ("comida", "🍲 Comida", None),
    ("agua", "💧 Agua", None),
    ("salud", "🩹 Salud / medicinas", None),
    ("refugio", "🛏️ Refugio", "Un lugar donde dormir"),
    ("rescate", "🆘 Rescate", "Persona atrapada o en peligro"),
    ("transporte", "🚐 Transporte", None),
    ("desaparecido", "🔍 Buscar a alguien", "Publicar una persona desaparecida"),
    ("otro", "📦 Otra necesidad", None),
]
from config import CATS_PEDIR as CATS_PEDIR_LABEL  # labels canónicos (config.py), mismos que web y Telegram
# La lista de arriba es solo presentación (títulos cortos ≤24 para WhatsApp); las claves
# deben calzar 1:1 con las canónicas — si alguien agrega una categoría en un solo lado,
# el servicio no arranca (mejor fallar al deploy que mandar mensajes desparejos).
assert {k for k, _, _ in CATS_PEDIR} == set(CATS_PEDIR_LABEL), \
    "CATS_PEDIR de bot_wa.py desincronizado con config.CATS_PEDIR"
PEDIR_GAP = 600  # freno anti-spam del flujo 'pedir' (seg); vive en efimero.rl_cooldowns
_esc = html.escape  # para el aviso HTML al grupo de moderadores


def _menu_roles(to):
    wa_api.enviar_lista(
        to,
        "🙌 ¡Hola! Soy el bot de *Red de Acopio y Refugio*.\n\n¿Cómo quieres participar?",
        "Elegir opción",
        [("rol|pedir", "🆘 Necesito ayuda", "Pide a la red de tu zona"),
         ("rol|centro", "🏠 Represento centro", "Acopio, hospital o refugio"),
         ("rol|aliado", "🤝 Manos Solidarias", "Ofrezco ayuda gratis"),
         ("rol|voluntario", "📣 Voluntariado", "Levanto llamados en terreno")])


def _iniciar(to, flujo, forzar_publico=False):
    if flujo in ("centro", "aliado", "voluntario") and _rechazos_de(to) >= RECHAZOS_BLOQUEO:
        # Umbral anti-spam: el rechazo libera el número para volver a empezar, pero
        # reincidir tantas veces bloquea NUEVOS registros. Pedir ayuda no se bloquea
        # nunca (es humanitario). Aviso a moderadores máx. 1 vez al día por número.
        wa_api.enviar_texto(to, "🚫 No podemos procesar un nuevo registro para este número.\n\n"
                                "Si crees que es un error, escríbenos a "
                                "*contacto@redacopioyrefugio.com* y lo revisamos. "
                                "Si necesitas ayuda, escribe *ayuda* y te atendemos igual. 💙")
        if efimero.cooldown_ok("wa_bloq_aviso", to, 86400):
            _tg_texto(GRUPO_MODERADORES,
                      f"🚫 Freno anti-spam (WhatsApp): el número {to} intentó registrarse otra vez "
                      f"con {RECHAZOS_BLOQUEO}+ registros rechazados acumulados. El bot ya no le "
                      "acepta nuevos registros (pedir ayuda le sigue funcionando). Para "
                      "desbloquearlo, un moderador debe eliminar sus registros rechazados.")
        return
    # Si quien pide REPRESENTA un punto (fundación, refugio, acopio…), su pedido
    # "para mi punto" va por la vía rica de /necesito, no por el formulario público.
    if flujo == "pedir" and not forzar_publico:
        ident = resolver_identidad(to)
        if ident["tipo"] == "vinculado" and ident["rol"] == "centro" \
                and ident["registro"].get("estado") == "aprobado":
            reg = ident["registro"]
            tipo_lbl = TIPOS_LABEL.get(reg.get("tipo_punto"), "punto")
            estados.pop(to, None)
            wa_api.enviar_botones(
                to, f"Tú representas *{reg.get('punto') or 'un punto'}*. "
                    "¿Para quién es este pedido?\n\n"
                    "• *Para tu punto* → declaras sus insumos con seguimiento y la red "
                    "con inventario recibe el aviso.\n"
                    "• *Para otra persona* → sale como pedido de ayuda al público.",
                [("pedq|punto", "📦 Para mi punto"), ("pedq|otro", "🧍 Otra persona")])
            return
    estados[to] = {"flujo": flujo, "paso": "", "datos": {}}
    if flujo == "centro":
        estados[to]["paso"] = "nombre"
        wa_api.enviar_texto(to, "🏠 *Represento un centro*.\n\n¿Cuál es tu *nombre y apellido*?")
    elif flujo == "aliado":
        estados[to]["paso"] = "categoria"
        wa_api.enviar_lista(to, "🤝 *Manos Solidarias* — ¡gracias por ayudar! 🙏\n\n¿Qué tipo de ayuda ofreces?",
                            "Elegir ayuda", [(f"cat|{k}", v) for k, v in ALIADO_CATS])
    elif flujo == "voluntario":
        estados[to]["paso"] = "nombre"
        wa_api.enviar_texto(to, "📣 *Voluntariado* — ¡gracias! 🙏\n\n¿Cuál es tu *nombre y apellido*?")
    elif flujo == "pedir":
        if time.time() - efimero.cooldown_ts("pedir_wa", to) < PEDIR_GAP:
            estados.pop(to, None)
            wa_api.enviar_texto(to, "⏳ Ya recibimos tu pedido hace poco y está en camino a la red. "
                                    "Si necesitas agregar algo, espera unos minutos e inténtalo de nuevo 🙏")
            return
        estados[to]["paso"] = "categoria"
        wa_api.enviar_lista(
            to, "🆘 *Pedir ayuda*\n\nVamos a avisarle a la red (acopios, Manos Solidarias y "
                "voluntarios de tu zona). Tu pedido sale *anonimizado* en la web; tu número solo "
                "se comparte en privado con quien puede ayudarte.\n\n"
                "Puedes elegir de la lista, o en cualquier momento mandar una *foto* 📷 "
                "(persona, lugar, lo que haga falta) y la leo sola. ¿Qué necesitas?",
            "Elegir", [(f"pcat|{k}", t, desc) for k, t, desc in CATS_PEDIR])


def _ficha_preguntar(to, st):
    """Envía la pregunta actual de la ficha, saltando las que no aplican al tipo
    de punto (solo/salvo) y, en el alta, las que no son esenciales (core); al
    terminar: registro nuevo → _finalizar_centro; «Completar ficha» → actualiza."""
    d = st["datos"]
    i = d["fi"]
    while i < len(FICHA) and not (_ficha_aplica(FICHA[i], d.get("tipo"))
                                  and _ficha_en_alta(FICHA[i], d)):
        i += 1
    d["fi"] = i
    if i >= len(FICHA):
        if d.get("editar_rid"):
            db.actualizar_ficha_representante(d["editar_rid"], d.get("ficha") or {})
            estados.pop(to, None)
            wa_api.enviar_texto(to, "✅ ¡Listo! La ficha de tu punto quedó *actualizada*. Gracias 🙌")
        else:
            _finalizar_centro(to, st)
        return
    q = FICHA[i]
    modo, preg = q["modo"], q["preg"]
    if modo == "sino":
        wa_api.enviar_botones(to, preg, [("fi|si", "✅ Sí"), ("fi|no", "❌ No")])
    elif modo == "sino_na":
        wa_api.enviar_botones(to, preg, [("fi|si", "✅ Sí"), ("fi|no", "❌ No"), ("fi|na", "➖ No aplica")])
    elif modo == "no_posee":
        wa_api.enviar_botones(to, preg + "\n\n(o escribe la cantidad)", [("fi|np", "No posee")])
    elif modo == "no_aplica":
        wa_api.enviar_botones(to, preg + "\n\n(o escribe la respuesta)", [("fi|na", "No aplica")])
    else:
        wa_api.enviar_texto(to, preg)


def _ficha_responder(to, st, valor):
    """Guarda la respuesta del campo actual y pasa al siguiente."""
    d = st["datos"]
    i = d["fi"]
    if i >= len(FICHA):
        return
    d["ficha"][FICHA[i]["campo"]] = valor
    d["fi"] = i + 1
    _ficha_preguntar(to, st)


def _menu_centro(to, reg):
    """Menú interactivo del rol centro: guía con botones a quien no domina los
    comandos de texto. Solo lo ve un responsable aprobado (la rama que lo llama
    ya validó identidad y estado)."""
    wa_api.enviar_lista(
        to, "👋 ¡Hola de nuevo! ¿Qué deseas hacer?\n\n"
            "Toca *Ver opciones* y elige. También puedes mandarme directo una "
            "*foto*, *nota de voz* o *archivo* con tu inventario y yo lo leo solo. 🙌",
        "Ver opciones",
        [("menu|inv", "📦 Cargar inventario", "Foto, voz, texto, PDF o Excel de lo que tienes"),
         ("menu|ver", "📋 Ver mi inventario", "Lo que tienes cargado en la red"),
         ("menu|nec", "🙏 Requerimientos", "Donaciones o insumos que te hacen falta"),
         ("menu|desp", "🚚 Despachar insumos", "Enviar insumos a otro punto de la red"),
         ("menu|pedir", "🆘 Pedido de ayuda", "Para tu punto o para otra persona"),
         ("menu|ficha", "📝 Completar ficha", "Aforo, baños, camas… datos del punto"),
         ("menu|salir", "🚪 Salir de la red", "Retirar tu punto de la plataforma")])


def _avisar_dueno_colab(rid, via):
    """Failsafe anti-sabotaje (pedido de Gustavo): avisa al DUEÑO del punto que se
    sumó un colaborador, por su propio canal (Telegram o WhatsApp), con botones
    para confirmarlo o removerlo si no lo reconoce."""
    rep = db.representante(rid)
    if not rep:
        return
    dueno = db.dueno_de_punto(rep.get("punto_id"))
    if not dueno or dueno.get("id") == rid:
        return
    tel = f" · 📞 {rep['telefono']}" if rep.get("telefono") else ""
    if dueno.get("telegram_id"):
        _tg_botones(dueno["telegram_id"],
                    f"🤝 <b>Se sumó un colaborador a tu punto</b> {html.escape(rep.get('punto') or '')}:\n"
                    f"👤 <b>{html.escape(rep['nombre'])}</b> (vía {via}{tel})\n"
                    "Ya puede cargar inventario y necesidades del punto.\n\n"
                    "¿Lo reconoces? Si no sabes quién es, toca «🚫 No lo conozco» y lo removemos.",
                    [[(f"colok|{rid}", "✅ Lo conozco"), (f"colrm|{rid}", "🚫 No lo conozco")]])
    elif dueno.get("wa_phone") or dueno.get("telefono"):
        wa_api.enviar_botones(
            dueno.get("wa_phone") or dueno.get("telefono"),
            f"🤝 *Se sumó un colaborador a tu punto* {rep.get('punto') or ''}:\n"
            f"👤 *{rep['nombre']}* (vía {via}{tel})\n"
            "Ya puede cargar inventario y necesidades del punto.\n\n"
            "¿Lo reconoces? Si no sabes quién es, toca *No lo conozco* y lo removemos.",
            [(f"colok|{rid}", "✅ Lo conozco"), (f"colrm|{rid}", "🚫 No lo conozco")])


def _registrar_colaborador_wa(to, st):
    """Suma un colaborador por WhatsApp a un punto ya registrado (paridad con el
    flujo de Telegram): hereda nombre exacto, tipo y punto_id del dueño, sin ficha
    (el punto ya tiene la suya), y pasa por la aprobación de moderadores."""
    d = st["datos"]
    dueno = {"punto": d.get("dueno_punto"), "tipo_punto": d.get("dueno_tipo"),
             "punto_id": d.get("dueno_punto_id")}
    rid = db.crear_colaborador(None, d["nombre"], dueno)
    con = db.conectar()
    con.execute("UPDATE representantes SET telefono = ? WHERE id = ?", (to, rid))
    con.commit()
    con.close()
    _set_wa_phone("representantes", rid, to)
    if AUTO_APROBAR:
        db.set_estado_representante(rid, "aprobado")
    _tg_moderadores(f"apr|{rid}", "🤝 <b>Nuevo colaborador</b>",
                    f"{d['nombre']} (WhatsApp · {to}) se suma a <b>{dueno['punto']}</b> "
                    f"({TIPOS_LABEL.get(dueno.get('tipo_punto'), 'punto')})")
    _avisar_dueno_colab(rid, "WhatsApp")
    estados.pop(to, None)
    if AUTO_APROBAR:
        wa_api.enviar_texto(to, f"✅ ¡Listo! Quedaste como *colaborador de {dueno['punto']}* y ya "
                                "estás activo. Mándame listas, fotos o audios con el inventario y "
                                "los requerimientos del punto. 🙌")
    else:
        wa_api.enviar_texto(to, f"🙌 ¡Gracias! Quedaste como *colaborador de {dueno['punto']}*, "
                                "*pendiente de aprobación*. Te avisamos apenas un coordinador "
                                "lo confirme.")


def _finalizar_centro(to, st):
    d = st["datos"]
    rid = db.crear_representante(None, d["nombre"], d["tipo"], d["punto"], to, ficha=d.get("ficha"))
    _set_wa_phone("representantes", rid, to)
    # Las fundaciones SIEMPRE pasan por aprobación manual (verificación de RIF
    # caso por caso), aunque AUTO_APROBAR esté activo para el resto de puntos.
    es_fund = d["tipo"] == "fundacion"
    auto_ok = AUTO_APROBAR and not es_fund
    if auto_ok:
        db.set_estado_representante(rid, "aprobado")
    ficha = d.get("ficha") or {}
    cuerpo = f"{d['nombre']} — {TIPOS_LABEL.get(d['tipo'], d['tipo'])}: <b>{d['punto']}</b>"
    if es_fund:
        rif = (ficha.get("rif") or "").strip()
        cuerpo += ("\n⚠️ <b>Declaró no poseer RIF</b> (colectivo informal) — aprobar caso por caso."
                   if rif.lower() == "no posee" else f"\n🪪 RIF: {rif}")
    _tg_moderadores(f"apr|{rid}", "🆕 <b>Nueva afiliación</b>", cuerpo, forzar_manual=es_fund)
    estados.pop(to, None)
    wa_api.enviar_texto(
        to, "✅ ¡Listo! Tu punto quedó registrado" +
        (" y ya aparece en la web." if auto_ok else " y está *pendiente de aprobación*. "
         "Te avisamos apenas un coordinador lo confirme."))
    if auto_ok:
        # Siembra en caliente: primero la NECESIDAD más urgente del punto (la web,
        # el botón «Puedo llevar esto» y el puente a acopios viven de esto). El
        # nudge de inventario sale al cerrar este mini-paso (nec1|).
        _pedir_primera_necesidad_wa(to, rid)
    else:
        # Nudge al siguiente paso: sin esto la gente no sabe qué sigue (pedido de Gustavo).
        wa_api.enviar_botones(
            to, "📦 *Siguiente paso:* carga tu inventario (foto, nota de voz, PDF o Excel) "
                "para que donantes y refugios sepan qué tienes.\n\n¿Lo hacemos ahora?",
            [(f"inv|now|{rid}", "📦 Cargar ahora"), (f"inv|later|{rid}", "⏰ Seguir luego")])


def _pedir_primera_necesidad_wa(to, rid):
    """Siembra post-alta (paridad con Telegram): pide EN CALIENTE la primera
    necesidad con meta del punto recién aprobado. El estado lo persiste el
    wrapper del request al salir (patrón warace)."""
    estados[to] = {"flujo": "centro", "paso": "nec1_item", "datos": {"rid": rid}}
    wa_api.enviar_botones(
        to, "🙏 Una pregunta más, la *más importante*:\n\n"
            "¿Qué *insumo le falta más* a tu punto ahora mismo? Escribe UNO "
            "(ej. «agua potable», «pañales talla G»).\n\n"
            "Lo publicamos con barra de avance y botón *«Puedo llevar esto»*, y le "
            "avisamos a los acopios que lo tengan. Así la ayuda llega a donde de "
            "verdad hace falta.",
        [("nec1|skip", "⏭️ Ahora no")])


def _crear_primera_necesidad_wa(to, st, texto_meta):
    """Crea la necesidad sembrada con el MISMO cableado que /necesito: web (barra
    + deep links «Puedo llevar esto») y puente demanda→oferta a los acopios
    (reusa bot_apoyo._avisar_acopios_demanda con import perezoso, como
    _demanda_a_necesidades_wa)."""
    d = st["datos"]
    rep = db.representante(d.get("rid") or 0)
    item = (d.get("nec1_item") or "").strip()
    if not rep or not item:
        estados.pop(to, None)
        return
    meta, unidad = db.parse_cantidad(texto_meta)
    if not meta:
        wa_api.enviar_botones(to, "No entendí la cantidad 🙈 Escribe solo el número "
                                  "(ej. *100*) o número y unidad (ej. *50 kg*).",
                              [("nec1|skip", "⏭️ Saltar esto")])
        return
    nid = db.abrir_necesidad(rep["id"], rep.get("punto_id"), rep.get("punto"),
                             rep.get("tipo_punto"), item, meta=meta, unidad=unidad)
    if nid:
        def _avisar():
            try:
                import bot_apoyo
                bot_apoyo._avisar_acopios_demanda(dict(rep), [(nid, item, meta, unidad)])
            except Exception as e:
                print("puente siembra (wa) error:", e)
        threading.Thread(target=_avisar, daemon=True).start()
    falta = f"{meta} {unidad}".strip() if unidad else str(meta)
    d.pop("nec1_item", None)
    st["paso"] = "nec1_fin"
    wa_api.enviar_botones(
        to, f"✅ Publicado: *{item}* — faltan *{falta}*.\n\n"
            "Ya está en la web y le avisamos a la red. Cuando te llegue algo, "
            "avísame por aquí para registrar el avance.",
        [("nec1|more", "➕ Falta algo más"), ("nec1|done", "✅ Listo")])


def _finalizar_aliado(to, st):
    d = st["datos"]
    aid = db.crear_aliado(None, {"nombre": d["nombre"], "categoria": d["categoria"],
                                 "descripcion": d.get("descripcion"), "zona": d["zona"],
                                 "contacto": to, "horario": None, "lat": None, "lon": None})
    _set_wa_phone("aliados", aid, to)
    if AUTO_APROBAR:
        db.set_estado_aliado(aid, "aprobado")
    _tg_moderadores(f"apra|{aid}", "🤝 <b>Nueva Manos Solidarias</b>",
                    f"{d['nombre']} — {ALIADO_LABEL.get(d['categoria'], d['categoria'])} · {d['zona']}")
    estados.pop(to, None)
    wa_api.enviar_texto(
        to, "✅ ¡Gracias! Quedaste registrado en *Manos Solidarias*" +
        (" y ya apareces en la web. 🤝" if AUTO_APROBAR else ", *pendiente de aprobación*. "
         "Te avisamos apenas lo confirmen. 🤝"))


def _finalizar_voluntario(to, st):
    d = st["datos"]
    vid = db.crear_voluntario(None, {"nombre": d["nombre"], "zona": d["zona"], "contacto": to})
    _set_wa_phone("voluntarios", vid, to)
    if AUTO_APROBAR:
        db.set_estado_voluntario(vid, "aprobado")
    _tg_moderadores(f"aprv|{vid}", "📣 <b>Nuevo Voluntariado</b>", f"{d['nombre']} · {d['zona']}")
    estados.pop(to, None)
    wa_api.enviar_texto(
        to, "✅ ¡Listo! Quedaste registrado como *Voluntariado*" +
        ("." if AUTO_APROBAR else ", *pendiente de aprobación*. Te avisamos apenas lo confirmen. 📣"))


def _referencias_desaparecido(to, lid, persona):
    """HILO best-effort: cruza a la persona buscada con los registros externos
    (fuentes.py: hospitales, rescatados). Si aparece, avisa al solicitante por WA
    y deja nota en el grupo de moderadores. Nunca toca el pedido."""
    try:
        import fuentes
        refs = fuentes.referencias(persona)
        if not refs:
            return
        lineas = ["🔎 *Mientras revisamos tu pedido*, buscamos a *{}* en los registros "
                  "comunitarios de hospitales y rescatados, y encontramos esto:".format(persona)]
        lineas += fuentes.resumen_referencias(refs)
        lineas.append("\n⚠️ Es información cargada por voluntarios (no oficial): "
                      "*confirma llamando al centro*. Tu pedido sigue su curso normal. "
                      "Más resultados: https://redacopioyrefugio.com/buscar.html")
        wa_api.enviar_texto(to, "\n".join(lineas))
        if APOYO_BOT_TOKEN and GRUPO_MODERADORES:
            lineas_m = ([f"🔎 <b>Referencias para el pedido #{lid}</b> "
                         f"(buscan a <b>{_esc(persona)}</b>):"]
                        + fuentes.resumen_referencias(refs, html_out=True)
                        + ["<i>Información comunitaria, no oficial — confirmar llamando "
                           "al centro antes de actuar.</i>"])
            payload = json.dumps({
                "chat_id": GRUPO_MODERADORES, "parse_mode": "HTML",
                "text": "\n".join(lineas_m),
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage",
                data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20)
    except Exception as e:  # noqa: BLE001
        print("referencias desaparecido wa error:", e)


def _finalizar_pedido(to, st):
    """Cierra el flujo 'pedir': crea el llamado + contacto privado. AUTO-PUBLICACIÓN
    (config.AUTO_PUBLICAR_PEDIDOS): el pedido sale a la web al instante, el barrido de
    bot_apoyo.py le avisa a la red y los moderadores reciben reporte + botón Remover;
    los desaparecidos (CATS_PEDIR_MODERADAS) siguen con revisión previa pweb|.
    Si el pedido trae foto (leída por IA o solo adjunta), se guarda en la carpeta
    PRIVADA pedidos_img/<lid>.jpg — build_site.py ya la publica sola al estar el
    pedido publicado, mismo circuito que los pedidos de la web ([[pedir-ayuda-flyer-ia]])."""
    d = st["datos"]
    contacto = to  # el número de WhatsApp del solicitante
    foto_bytes = _foto_tmp_leer(d)
    auto = AUTO_PUBLICAR_PEDIDOS and d["cat"] not in CATS_PEDIR_MODERADAS
    try:
        lid = db.crear_llamado(None, None, d["zona"],
                               "{}: {}".format(CATS_PEDIR_LABEL[d["cat"]], d["descripcion"]), None)
        # Contacto ANTES de publicar: el barrido arma los avisos con solicitudes_web.
        db.guardar_contacto_pedido(lid, d.get("nombre"), contacto, d["cat"])
        if d.get("cat") == "desaparecido" and d.get("persona"):
            db.guardar_persona_desaparecida(lid, d["persona"])
            threading.Thread(target=_referencias_desaparecido,
                             args=(to, lid, d["persona"]), daemon=True).start()
        if auto:
            db.publicar_pedido(lid)
        else:
            db.set_estado_llamado(lid, "pendiente")
    except Exception as e:  # noqa: BLE001
        print("pedido wa error:", e)
        wa_api.enviar_texto(to, "⚠️ No pude guardar tu pedido. Inténtalo de nuevo en un momento 🙏")
        return
    estados.pop(to, None)
    efimero.estado_del(to)  # también puede correr en hilo post-request → borrar en DB directo
    efimero.cooldown_marcar("pedir_wa", to)
    if foto_bytes:
        try:
            (BASE / "pedidos_img").mkdir(exist_ok=True)
            (BASE / "pedidos_img" / f"{lid}.jpg").write_bytes(foto_bytes)
        except Exception as e:  # noqa: BLE001
            print("pedido wa foto error:", e)
            foto_bytes = None  # el pedido sigue sin imagen
    if APOYO_BOT_TOKEN and GRUPO_MODERADORES:
        if auto:
            kb = {"inline_keyboard": [[
                {"text": "✅ Marcar resuelto", "callback_data": f"pat|{lid}|res"},
                {"text": "🗑 Remover de la web", "callback_data": f"pweb|{lid}|no"}]]}
            titulo = "🆘 <b>Pedido de ayuda (WhatsApp)</b> · <i>publicado · puedes removerlo</i>"
        else:
            kb = {"inline_keyboard": [[
                {"text": "✅ Publicar y avisar a la red", "callback_data": f"pweb|{lid}|ok"},
                {"text": "❌ Descartar", "callback_data": f"pweb|{lid}|no"}]]}
            titulo = "🆘 <b>Pedido de ayuda (WhatsApp)</b> · <i>revisar antes de publicar</i>"
        lineas = [titulo,
                  f"🏷️ {_esc(CATS_PEDIR_LABEL[d['cat']])}"]
        if d.get("nombre"):
            lineas.append(f"👤 {_esc(d['nombre'])}")
        if d.get("cat") == "desaparecido" and d.get("persona"):
            lineas.append(f"🔎 Busca a: <b>{_esc(d['persona'])}</b>")
        lineas += [f"✍️ {_esc(d['descripcion'])}", f"📍 {_esc(d['zona'])}",
                   f"📞 {_esc(contacto)}"]
        if foto_bytes:
            lineas.append("📸 <i>Incluye imagen: sale junto al pedido publicado.</i>")
            _tg_foto_moderadores(foto_bytes, "\n".join(lineas), kb)
        else:
            payload = json.dumps({
                "chat_id": GRUPO_MODERADORES, "parse_mode": "HTML",
                "text": "\n".join(lineas), "reply_markup": kb,
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage",
                data=payload, headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=20)
            except Exception as e:  # noqa: BLE001
                print("tg moderadores pedido error:", e)
    if auto:
        wa_api.enviar_texto(
            to, "✅ *¡Pedido publicado!* Ya está en la web y le estamos avisando a la red "
                "de tu zona. Si alguien puede ayudarte, te contactará a este mismo "
                "WhatsApp. 🙏\n\n"
                "Puedes ver los puntos de ayuda en https://redacopioyrefugio.com")
    else:
        wa_api.enviar_texto(
            to, "📨 *¡Pedido recibido!* Está *en revisión* de nuestro equipo. Apenas lo aprueben, "
                "se publica y le avisamos a la red de tu "
                "zona. Si alguien puede ayudarte, te contactará a este mismo WhatsApp. 🙏\n\n"
                "Mientras tanto, puedes ver los puntos de ayuda en https://redacopioyrefugio.com")


def _reencodificar_imagen_pedido(raw):
    """bytes crudos → JPEG re-codificado con Pillow: valida que sea una imagen de
    verdad y BORRA metadatos (EXIF/GPS de quien la mandó). Mismo patrón que
    backend._decodificar_imagen (pedidos de la web), pero de bytes a bytes
    (WhatsApp ya entrega binario, sin el paso de base64 del navegador)."""
    import io
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 40_000_000  # anti "decompression bomb"
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _foto_tmp_guardar(to, data):
    """Deja la foto pendiente de un flujo en un tmp por número dentro de la carpeta
    privada pedidos_img/ (build_site solo busca <id>.jpg ahí; los tmp no le estorban)
    y devuelve el NOMBRE. En el estado va solo el nombre: efimero serializa a JSON
    y unos bytes lo reventarían."""
    carpeta = BASE / "pedidos_img"
    carpeta.mkdir(exist_ok=True)
    ruta = carpeta / f"tmp_wa_{to}.jpg"
    ruta.write_bytes(data)
    return ruta.name


def _foto_tmp_leer(d):
    """Recupera (y borra) la foto pendiente apuntada por el estado; None si no hay.
    Acepta también bytes sueltos en 'foto_pendiente' (mismo request, compat)."""
    data = d.get("foto_pendiente")
    if data is not None:
        return data
    nombre = d.get("foto_tmp")
    if not nombre:
        return None
    ruta = BASE / "pedidos_img" / nombre
    try:
        data = ruta.read_bytes()
        ruta.unlink(missing_ok=True)
        return data
    except Exception:  # noqa: BLE001
        return None


def _pedir_siguiente_paso(to, st):
    """Tras procesar una foto (o de entrada), pregunta el primer campo que
    todavía falte del flujo 'pedir' — cat → descripcion → zona → nombre — o
    finaliza si la IA ya completó todo. El nombre es el único campo donde ''
    es una respuesta válida (usuario escribió «-»), por eso se chequea con
    `in d` y no con `d.get(...)`."""
    d = st["datos"]
    if not d.get("cat"):
        st["paso"] = "categoria"
        wa_api.enviar_lista(
            to, "¿Qué necesitas?", "Elegir",
            [(f"pcat|{k}", t, desc) for k, t, desc in CATS_PEDIR])
    elif d.get("cat") == "desaparecido" and "persona" not in d:
        # Desaparecidos: PRIMERO a quién buscan (orden natural: quién → qué pasó →
        # dónde). El nombre alimenta la sugerencia inmediata (fuentes.py) y el
        # cron depurador que la cruza con hospitales/rescatados.
        st["paso"] = "persona"
        wa_api.enviar_texto(to, "🔎 ¿Cómo se llama la *persona que buscas*? "
                                "(nombre y apellido; si no lo sabes, escribe «-»)")
    elif not d.get("descripcion"):
        st["paso"] = "descripcion"
        if d.get("cat") == "desaparecido":
            wa_api.enviar_texto(to, "✍️ Cuéntame *qué pasó y dónde le viste por última vez* "
                                    "(cualquier seña ayuda: edad, ropa, con quién estaba…). "
                                    "📸 Si tienes el *flyer o una foto*, mándalo y lo leo yo.")
        else:
            wa_api.enviar_texto(to, "✍️ Cuéntame *qué necesitas exactamente* y para cuántas "
                                    "personas. (ej. «agua potable y pañales para 3 familias»)")
    elif not d.get("zona"):
        st["paso"] = "zona"
        if d.get("cat") == "desaparecido":
            wa_api.enviar_texto(to, "📍 ¿En qué *zona* se le vio por última vez? "
                                    "(estado, ciudad y sector)")
        else:
            wa_api.enviar_texto(to, "📍 ¿*Dónde* se necesita? (estado, ciudad y sector — "
                                    "mientras más exacto, más rápido te ubica la red)")
    elif "nombre" not in d:
        st["paso"] = "nombre"
        wa_api.enviar_texto(to, "👤 ¿Cuál es tu *nombre*? (si prefieres no decirlo, escribe «-»)")
    else:
        _finalizar_pedido(to, st)
        return
    # Persistir SIEMPRE que el flujo siga vivo: cuando esto corre en el HILO de
    # media, _despachar ya guardó el snapshot viejo al salir del request — sin esta
    # línea, la siguiente respuesta del usuario llega con el paso rancio y el flujo
    # se queda mudo (visto en prod 2026-07-04 con un flyer en 'pedir').
    efimero.estado_set(to, st)


def _pedido_completo(d):
    """True si al flujo 'pedir' ya no le falta nada — espejo de las condiciones de
    _pedir_siguiente_paso (mantener ambos en sintonía). Evita anunciar «faltan un
    par de datos» cuando la IA completó todo y el cierre llega de inmediato."""
    return bool(d.get("cat") and d.get("descripcion") and d.get("zona")
                and "nombre" in d
                and not (d.get("cat") == "desaparecido" and "persona" not in d))


def _procesar_media_pedir(to, msg, st):
    """Foto o nota de voz durante 'pedir ayuda'. La foto la lee la misma IA que
    /api/leer_flyer de la web (persona desaparecida, edificio dañado, herramienta
    necesaria…) y prellena SOLO los campos vacíos; el contacto nunca se toca,
    siempre es este mismo número. Si la imagen no es legible, igual se adjunta
    al pedido como evidencia visual. Corre en un HILO."""
    tipo = msg.get("type")
    d = st["datos"]
    if tipo == "image":
        try:
            data, _ = wa_api.descargar_media(msg["image"]["id"])
            if data is None:
                raise RuntimeError("no se pudo descargar la imagen")
            if len(data) > MAX_IMG:
                wa_api.enviar_texto(to, "⚠️ Esa imagen es muy pesada. Manda una más liviana 🙏.")
                return
            foto = _reencodificar_imagen_pedido(data)
        except Exception as e:  # noqa: BLE001
            print("procesar_media_pedir descarga error:", e)
            wa_api.enviar_texto(to, "Ups, no pude leer esa imagen 🙏. Intenta de nuevo o "
                                    "sigue por texto.")
            return
        d["foto_tmp"] = _foto_tmp_guardar(to, foto)  # nunca bytes dentro del estado (JSON)
        # Freno anti-costo (mismos topes que /api/leer_flyer de la web): 30s por
        # número + máx 20 lecturas de IA cada 10 min en total. Fuera de eso, la
        # foto igual se guarda y adjunta — solo se salta la lectura automática.
        if not (efimero.cooldown_ok("flyer_wa", to, 30) and efimero.tope_global_ok("flyer", 20, 600)):
            wa_api.enviar_texto(to, "📸 Imagen recibida, va adjunta a tu pedido 🙌"
                                    if _pedido_completo(d) else
                                    "📸 Imagen recibida, la guardo con tu pedido. Sigamos 👇")
            _pedir_siguiente_paso(to, st)
            return
        try:
            lec = captura.leer_flyer(foto)
        except Exception as e:  # noqa: BLE001
            print("leer_flyer wa error:", e)
            wa_api.enviar_texto(to, "📸 Imagen recibida, va adjunta a tu pedido 🙌"
                                    if _pedido_completo(d) else
                                    "📸 Imagen recibida, la guardo con tu pedido. Sigamos 👇")
            _pedir_siguiente_paso(to, st)
            return
        if lec.legible:
            cat = (lec.categoria or "").strip()
            if cat in CATS_PEDIR_LABEL and not d.get("cat"):
                d["cat"] = cat
            if not d.get("descripcion") and (lec.descripcion or "").strip():
                d["descripcion"] = lec.descripcion.strip()[:600]
            if not d.get("zona") and (lec.zona or "").strip():
                d["zona"] = lec.zona.strip()[:120]
            nom_ia = (lec.nombre or "").strip()[:80]
            # El nombre que trae un flyer es el de la persona DEL flyer (beneficiario
            # o buscado), NO el de quien pide: solo sirve como 'persona' en
            # desaparecidos. El nombre del SOLICITANTE se pregunta SIEMPRE
            # (decisión 2026-07-04: un pedido real quedó a nombre del beneficiario).
            if nom_ia and d.get("cat") == "desaparecido" and not d.get("persona"):
                d["persona"] = nom_ia
            txt = ("📸 ¡Leí tu foto! Lo tengo todo — cierro tu pedido ahora mismo 🙌"
                   if _pedido_completo(d) else
                   "📸 ¡Leí tu foto! Ya completé lo que pude — solo faltan un par de datos 👇")
            wa_api.enviar_texto(to, txt)
        else:
            txt = ("📸 Imagen recibida, va adjunta a tu pedido 🙌"
                   if _pedido_completo(d) else
                   "📸 Imagen recibida, la guardo con tu pedido. No logré leer "
                   "texto útil ahí, sigamos con las preguntas 👇")
            wa_api.enviar_texto(to, txt)
        _pedir_siguiente_paso(to, st)
    elif tipo == "audio":
        try:
            data, _ = wa_api.descargar_media(msg["audio"]["id"])
            if data is None:
                raise RuntimeError("no se pudo descargar el audio")
            trans = captura.transcribir(data)
        except Exception as e:  # noqa: BLE001
            print("procesar_media_pedir audio error:", e)
            wa_api.enviar_texto(to, "Ups, hubo un problema con ese audio 🙏. Intenta de nuevo.")
            return
        if not trans:
            wa_api.enviar_texto(to, "No logré entender el audio 🙏. Intenta de nuevo, más claro, "
                                    "o escribe tu pedido en texto.")
            return
        if not d.get("descripcion"):
            d["descripcion"] = trans.strip()[:600]
        _pedir_siguiente_paso(to, st)
    else:
        wa_api.enviar_texto(to, "Para *pedir ayuda* puedo leer una *foto* 📷 o una *nota de voz* "
                                "🎙️. Los documentos no aplican aquí — mejor cuéntame en texto 🙏.")


# ──────────────────── Llamados de apoyo (Voluntariado) ───────────────────────
MAX_TEXTO = 6000               # tope del mensaje del llamado (mismo que Telegram)
MAX_IMG = 12 * 1024 * 1024     # tope de la foto del llamado (mismo que Telegram)


def _iniciar_llamado(to, vol):
    """Arranca un llamado de apoyo (paridad con /llamado de Telegram)."""
    if vol.get("estado") != "aprobado":
        wa_api.enviar_texto(to, "Tu registro de Voluntariado aún está en revisión 🙏. "
                                "Te avisaremos apenas quede aprobado.")
        return
    estados[to] = {"flujo": "llamado", "paso": "sector", "datos": {"vol_id": vol["id"]}}
    wa_api.enviar_texto(
        to, "📣 *Nuevo llamado de apoyo*\n\n"
            "Primero: ¿en qué *sector / zona* es el llamado? "
            "(ej. «El Valle, bloque 5», «Carretera vieja Petare»)\n\n"
            "_Escribe *cancelar* para salir._")


def _pedir_confirmacion_llamado(to, st, texto, foto_data=None):
    """Antes de publicar, muestra una vista previa y pide confirmación con botones
    (evita que un llamado salga por accidente si el usuario quiso cancelar y el
    mensaje no hizo match exacto con la palabra de cancelar)."""
    d = st["datos"]
    texto = (texto or "").strip()
    d["texto_pendiente"] = texto
    # La foto NO viaja dentro del estado (efimero persiste JSON): tmp en disco.
    d["foto_tmp"] = _foto_tmp_guardar(to, foto_data) if foto_data else None
    st["paso"] = "confirmar"
    sector = d.get("sector", "")
    extra = "\n\n📷 _(con foto adjunta)_" if foto_data else ""
    wa_api.enviar_botones(
        to, "👀 *Revisa antes de publicar:*\n\n"
            f"📍 Sector: {sector}\n"
            f"📣 Mensaje: {texto[:500]}{extra}\n\n"
            "¿Lo publico?",
        [("llgo|si", "✅ Sí, publicar"), ("llgo|no", "❌ Cancelar")])
    # Si esto corrió en el hilo de media, nadie más persiste el estado: sin esta
    # línea el botón de confirmar cargaría uno rancio (misma carrera que en 'pedir').
    efimero.estado_set(to, st)


def _finalizar_llamado(to, st):
    """Crea el llamado (sale al instante en la web), guarda la foto si vino y
    dispara la triangulación en segundo plano — igual que en Telegram."""
    d = st["datos"]
    vol = db.voluntario(d.get("vol_id"))
    estados.pop(to, None)
    efimero.estado_del(to)  # corre también en hilo post-request → borrar en DB directo
    if not vol:
        wa_api.enviar_texto(to, "Tu sesión expiró 🙏. Escribe *llamado* para empezar de nuevo.")
        return
    texto = d.get("texto_pendiente", "")
    foto_data = _foto_tmp_leer(d)
    lid = db.crear_llamado(vol["id"], vol.get("telegram_id"), d.get("sector", ""), texto, None)
    if foto_data:
        try:
            img_dir = WEB_DIR / "img"
            img_dir.mkdir(parents=True, exist_ok=True)
            (img_dir / f"llamado_{lid}.jpg").write_bytes(foto_data)
            db.set_foto_llamado(lid, f"img/llamado_{lid}.jpg")
        except Exception as e:  # noqa: BLE001
            print("llamado wa foto error:", e)
    wa_api.enviar_texto(
        to, "✅ *¡Llamado publicado!* Aparecerá en la web en *Llamados de apoyo* en unos "
            "minutos y, si pide insumos, avisamos a los centros de acopio y Manos "
            "Solidarias que puedan cubrirlo. Gracias por levantarlo. 📣\n\n"
            "Puedes crear otro escribiendo *llamado*.")
    threading.Thread(target=_triangular_llamado_wa,
                     args=(lid, d.get("sector", ""), texto, vol), daemon=True).start()


def _triangular_llamado_wa(lid, sector, texto, vol):
    """Reusa la triangulación del bot de Telegram (acopios por inventario + Manos
    Solidarias por categoría + voluntarios por zona). Import PEREZOSO: bot_apoyo
    NO se carga al arrancar este servicio; si el import fallara, el llamado igual
    quedó publicado en la web (difusión mínima garantizada)."""
    try:
        import bot_apoyo
        bot_apoyo._triangular_llamado(lid, sector, texto, vol)
    except Exception as e:  # noqa: BLE001
        print("triangulacion wa error:", e)


def _procesar_media_llamado(to, msg, st):
    """El contenido del llamado llegó como foto o nota de voz. Corre en un HILO."""
    tipo = msg.get("type")
    try:
        if tipo == "image":
            data, _ = wa_api.descargar_media(msg["image"]["id"])
            if data is None:
                raise RuntimeError("no se pudo descargar la imagen")
            texto = (msg.get("image", {}).get("caption") or "").strip()
            _pedir_confirmacion_llamado(to, st, texto,
                               foto_data=data if len(data) <= MAX_IMG else None)
        elif tipo == "audio":
            data, _ = wa_api.descargar_media(msg["audio"]["id"])
            if data is None:
                raise RuntimeError("no se pudo descargar el audio")
            trans = captura.transcribir(data)
            if not trans:
                wa_api.enviar_texto(to, "No logré entender el audio 🙏. Intenta de nuevo, más claro.")
                return
            _pedir_confirmacion_llamado(to, st, trans)
    except Exception as e:  # noqa: BLE001
        print("procesar_media_llamado error:", e)
        wa_api.enviar_texto(to, "Ups, hubo un problema con eso 🙏. Intenta de nuevo en un momento.")


# ─────────────────────────── Retiro voluntario ───────────────────────────────
_ROL_TXT = {"centro": "Centro de acopio", "voluntario": "Voluntariado",
            "aliado": "Manos Solidarias"}
_SET_ESTADO = {"representantes": db.set_estado_representante,
               "aliados": db.set_estado_aliado,
               "voluntarios": db.set_estado_voluntario}
_KW_RETIRO = ("retirarme", "retiro", "darme de baja", "baja", "quiero retirarme",
              "salir de la red")


def _iniciar_retiro(to, ident):
    """Confirmación antes de pasar la ficha a 'retirado' (sale de la web y de los avisos)."""
    reg = ident["registro"]
    if reg.get("estado") == "retirado":
        wa_api.enviar_botones(to, "Tu registro ya está retirado 🕊️. ¿Quieres volver a la red?",
                              [("ret|volver", "🔄 Volver a la red")])
        return
    nombre = reg.get("punto") or reg.get("nombre") or ""
    wa_api.enviar_botones(
        to, "Estás en la red como *" + _ROL_TXT.get(ident["rol"], "miembro") + "*"
            + (f" ({nombre})" if nombre else "") + ".\n\n"
            "Si te retiras, dejas de aparecer en la web y de recibir avisos de la red. "
            "Puedes volver cuando quieras.\n\n¿Confirmas tu retiro?",
        [("ret|si", "✅ Sí, retirarme"), ("ret|no", "↩️ Seguir en la red")])


# ───────────────────────────────── Despacho ──────────────────────────────────
def manejar_texto(to, texto):
    st = estados.get(to)
    # Aceptamos la palabra de cancelar aunque venga con más texto detrás
    # (ej. "cancela me equivoqué"), no solo el match exacto.
    if st and re.match(r"^(cancelar|cancela|salir)\b", (texto or "").strip().lower()):
        # Sin menú persistente en WhatsApp, siempre debe existir una salida.
        estados.pop(to, None)
        wa_api.enviar_texto(to, "Listo, cancelé eso 👍. Escribe *hola* si necesitas otra cosa.")
        return
    # Botones «Puedo ayudar · WhatsApp» de la web: llegan como texto prellenado con un
    # código (LL-<id> = llamado de apoyo, ND-<id> = necesidad de un punto). Vale para
    # cualquiera (registrado o no) y, como un deep link, interrumpe el flujo abierto.
    m_web = re.search(r"\b(LL|ND)-(\d+)\b", texto or "", re.I)
    if m_web:
        estados.pop(to, None)
        if m_web.group(1).upper() == "LL":
            _iniciar_respuesta_llamado_wa(to, int(m_web.group(2)))
        else:
            _iniciar_compromiso_wa(to, int(m_web.group(2)))
        return
    if not st:
        # Palabra clave universal: cualquier persona (registrada o no) puede pedir ayuda.
        if (texto or "").strip().lower() in ("ayuda", "sos", "auxilio", "pedir ayuda",
                                             "necesito ayuda", "emergencia"):
            _iniciar(to, "pedir")
            return
        # Sin conversación abierta → identidad (Opción B) o menú de registro.
        ident = resolver_identidad(to)
        if ident["tipo"] == "vinculado":
            reg = ident["registro"]
            # Ya se retiró: cualquier mensaje ofrece volver (y no lo deja duplicarse).
            if reg.get("estado") == "retirado":
                wa_api.enviar_botones(
                    to, "Tu registro está *retirado* de la plataforma 🕊️. "
                        "¿Quieres volver a la red?",
                    [("ret|volver", "🔄 Volver a la red"), ("rol|pedir", "🆘 Necesito ayuda")])
                return
            # Retiro voluntario: palabra clave disponible para cualquier rol.
            if (texto or "").strip().lower() in _KW_RETIRO:
                _iniciar_retiro(to, ident)
                return
            if ident["rol"] == "centro":
                pid = reg.get("punto_id") or reg["id"]
                # 1) ¿está completando una cantidad faltante? ('pañales 200')
                comp = stock.completar_cantidad(pid, texto)
                if comp:
                    faltan = stock.faltan_cantidad(pid)
                    extra = (f"\n\nQuedan sin cantidad: " + ", ".join(faltan[:5])) if faltan else "\n\n¡Inventario completo! 🎉"
                    wa_api.enviar_texto(to, f"✅ Anotado: *{comp[0]}* = {comp[1]:g}." + extra)
                # 2) ¿quiere ver su inventario?
                elif (texto or "").strip().lower() in ("inventario", "mi inventario", "stock", "ver inventario"):
                    _mostrar_inventario(to, pid)
                # 3) ¿quiere despachar un envío?
                elif (texto or "").strip().lower() in ("despachar", "despacho", "enviar", "envio", "envío"):
                    _iniciar_despacho(to, reg)
                # 4) ¿quiere declarar lo que le hace falta? (paridad /necesito de Telegram)
                elif (texto or "").strip().lower() in ("necesito", "necesito insumos", "donaciones",
                                                       "lo que hace falta", "que necesito",
                                                       "qué necesito", "necesidades",
                                                       "requerimiento", "requerimientos"):
                    _iniciar_necesito(to, reg)
                else:
                    _menu_centro(to, reg)
            elif ident["rol"] == "voluntario":
                if (texto or "").strip().lower() in ("llamado", "llamados", "requerimiento",
                                                     "levantar llamado", "nuevo llamado"):
                    _iniciar_llamado(to, reg)
                else:
                    wa_api.enviar_botones(
                        to, "👋 ¡Hola de nuevo! Estás registrado como *Voluntariado* 📣.\n\n"
                            "• *Levantar llamado*: publica un requerimiento de tu zona "
                            "(insumos, apoyo) y le avisamos a la red que pueda cubrirlo.\n"
                            "• *Necesito ayuda*: pide ayuda para ti.\n\n"
                            "También puedes escribir *llamado* directamente, "
                            "o *retirarme* si deseas salir de la red.",
                        [("vlla|new", "📣 Levantar llamado"), ("rol|pedir", "🆘 Necesito ayuda")])
            else:
                wa_api.enviar_botones(
                    to, "👋 ¡Hola de nuevo! Estás registrado en *Manos Solidarias* 🤝. "
                        "Te escribiremos cuando alguien cerca necesite de tu apoyo.\n\n"
                        "Si tú necesitas ayuda, toca el botón o escribe *ayuda*. "
                        "Si deseas salir de la red, escribe *retirarme*.",
                    [("rol|pedir", "🆘 Necesito ayuda")])
        elif ident["tipo"] == "match":
            reg = ident["registro"]
            nombre = reg.get("punto") or reg.get("nombre") or "tu registro"
            estados[to] = {"flujo": "vincular", "paso": "confirmar",
                           "datos": {"tabla": ident["tabla"], "id": reg["id"]}}
            wa_api.enviar_botones(
                to, f"👀 Este número ya está registrado en Telegram como *{nombre}*.\n\n"
                    "¿Eres tú? Si es tuyo, lo enlazo a este WhatsApp.",
                [("link|si", "✅ Sí, es mío"), ("link|no", "🆕 No, soy nuevo")])
        else:
            _menu_roles(to)
        return

    flujo, paso, d = st["flujo"], st["paso"], st["datos"]

    if flujo == "centro":
        if paso == "nombre":
            d["nombre"] = texto.strip()
            st["paso"] = "tipo"
            wa_api.enviar_lista(to, f"Gracias, {d['nombre'].split()[0]}. ¿Qué *tipo de punto* es?",
                                "Elegir tipo", [(f"tipo|{k}", v) for k, v in TIPOS])
        elif paso == "punto":
            nombre_punto = texto.strip()[:100]
            dueno = db.buscar_punto(nombre_punto)
            if dueno and to not in ((dueno.get("wa_phone") or ""), (dueno.get("telefono") or "")):
                # Punto ya censado → ofrecer sumarse como colaborador (paridad con Telegram).
                d["punto"] = nombre_punto
                d["dueno_punto"] = dueno["punto"]
                d["dueno_tipo"] = dueno.get("tipo_punto")
                d["dueno_punto_id"] = dueno.get("punto_id") or dueno.get("id")
                st["paso"] = "colaborar"
                ubic = (dueno.get("ubicacion") or "").strip()
                loc = f"\n📌 Ubicación registrada: _{ubic}_" if ubic else ""
                wa_api.enviar_botones(
                    to, f"📍 *{dueno['punto']}* ya está registrado como "
                        f"{TIPOS_LABEL.get(dueno.get('tipo_punto'), 'punto')}.{loc}\n\n"
                        "• *Es el mismo punto* → te sumas como colaborador, sin repetir la ficha.\n"
                        "• *Es otro punto* → lo registras aparte, con su propia ficha.\n"
                        "• *Otro nombre* → lo escribiste distinto y quieres corregirlo.",
                    [("wcolab|si", "🤝 Es el mismo punto"),
                     ("wcolab|otro", "📍 Es otro punto"),
                     ("wcolab|no", "✏️ Otro nombre")])
                return
            d["punto"] = nombre_punto
            d["ficha"] = {}
            d["fi"] = 0
            st["paso"] = "ficha"
            wa_api.enviar_texto(to, "🙌 ¡Gracias! Ahora los *datos esenciales* del punto (dirección "
                                    "y horario) para que la ayuda *llegue a donde es*. El resto de la "
                                    "ficha lo completas luego desde el menú, cuando puedas.")
            _ficha_preguntar(to, st)
        elif paso == "colaborar":
            wa_api.enviar_texto(to, "Usa los botones de arriba 👆 para decirme si es el mismo "
                                    "punto o uno distinto (o escribe *cancelar* para salir).")
        elif paso == "ficha":
            _ficha_responder(to, st, texto.strip())
        elif paso == "nec1_item":
            d["nec1_item"] = texto.strip()[:80]
            st["paso"] = "nec1_meta"
            wa_api.enviar_texto(to, f"🔢 ¿*Cuántos* necesitas de «{d['nec1_item']}»? "
                                    "Número y unidad si aplica (ej. «100», «50 kg», «20 cajas»).")
        elif paso == "nec1_meta":
            _crear_primera_necesidad_wa(to, st, texto.strip())
        elif paso == "nec1_fin":
            # Texto suelto tras publicar la primera: lo tomamos como OTRO insumo que falta.
            d["nec1_item"] = texto.strip()[:80]
            st["paso"] = "nec1_meta"
            wa_api.enviar_texto(to, f"🔢 ¿*Cuántos* necesitas de «{d['nec1_item']}»?")

    elif flujo == "aliado":
        if paso == "descripcion":
            d["descripcion"] = texto.strip()
            st["paso"] = "nombre"
            wa_api.enviar_texto(to, "¿Cuál es tu *nombre* (o el de tu grupo)?")
        elif paso == "nombre":
            d["nombre"] = texto.strip()
            st["paso"] = "zona"
            wa_api.enviar_texto(to, "¿En qué *zona* ofreces la ayuda? (estado, ciudad, sector)")
        elif paso == "zona":
            d["zona"] = texto.strip()
            _finalizar_aliado(to, st)

    elif flujo == "voluntario":
        if paso == "nombre":
            d["nombre"] = texto.strip()
            st["paso"] = "zona"
            wa_api.enviar_texto(to, "¿En qué *zona* estás? (estado, ciudad, sector)")
        elif paso == "zona":
            d["zona"] = texto.strip()
            _finalizar_voluntario(to, st)

    elif flujo == "llamado":
        if paso == "sector":
            d["sector"] = texto.strip()[:120]
            st["paso"] = "contenido"
            wa_api.enviar_texto(
                to, "Ahora mándame el *mensaje del llamado*: qué se necesita, para quién y "
                    "cualquier referencia útil (dónde recibir, horarios).\n\n"
                    "Puede ser *texto*, una *foto* (con el texto en el pie) o una "
                    "*nota de voz* 🎙️.")
        elif paso == "contenido":
            if len(texto or "") > MAX_TEXTO:
                wa_api.enviar_texto(to, "⚠️ Ese texto es muy largo. Mándalo más corto.")
                return
            _pedir_confirmacion_llamado(to, st, texto)
        elif paso == "confirmar":
            wa_api.enviar_texto(to, "Usa los botones de arriba para confirmar 👆 "
                                    "(o escribe *cancelar* para descartarlo).")

    elif flujo == "pedir":
        # Cada respuesta guarda su campo y delega en _pedir_siguiente_paso, que sabe
        # qué falta (incluye el paso extra 'persona' cuando es un desaparecido).
        if paso == "descripcion":
            d["descripcion"] = texto.strip()[:600]
            _pedir_siguiente_paso(to, st)
        elif paso == "persona":
            p = texto.strip()[:80]
            d["persona"] = "" if p == "-" else p
            _pedir_siguiente_paso(to, st)
        elif paso == "zona":
            d["zona"] = texto.strip()[:120]
            _pedir_siguiente_paso(to, st)
        elif paso == "nombre":
            n = texto.strip()[:80]
            d["nombre"] = "" if n == "-" else n
            _finalizar_pedido(to, st)

    elif flujo == "despacho":
        if paso == "items":
            items, errores = envios.parse_lineas(d["pid"], texto)
            if errores:
                wa_api.enviar_texto(to, "⚠️ Revisa esto:\n" + "\n".join("• " + x for x in errores) +
                                        "\n\nVuelve a mandarme la lista completa, una línea por insumo.")
                return
            if not items:
                wa_api.enviar_texto(to, "No entendí ningún insumo. Formato: _pañales 40_ "
                                        "(una línea por insumo).")
                return
            d["items"] = items
            inv = {r["id"]: r for r in stock.de_punto(d["pid"])}
            L = ["🚚 *Envío a despachar:*"]
            for sid, cant in items:
                r = inv.get(sid, {})
                L.append(f"• {r.get('item', '?')} — {cant:g} {r.get('unidad') or ''}".rstrip())
            L.append("\nAl despachar se descuenta de tu inventario publicado y se genera el QR de entrega.")
            wa_api.enviar_botones(to, "\n".join(L)[:1024],
                                  [("dgo|si", "🚚 Despachar"), ("dgo|no", "❌ Cancelar")])

    elif flujo == "compromiso":  # alguien se ofreció a llevar un insumo (puente /necesito)
        if paso == "cant":
            digitos = "".join(c for c in (texto or "") if c.isdigit())
            if not digitos:
                wa_api.enviar_texto(to, "Escríbeme solo el número (ej. 20) 🙂")
                return
            _finalizar_compromiso(to, int(digitos))

    elif flujo == "resp_llamado":  # botón web «Puedo ayudar» de un llamado de apoyo
        if paso == "msg":
            _finalizar_respuesta_llamado(to, texto)

    elif flujo == "recibido":  # el centro registra lo recibido de un requerimiento
        if paso == "cant":
            digitos = "".join(c for c in (texto or "") if c.isdigit())
            if not digitos:
                wa_api.enviar_texto(to, "Escríbeme solo el número que recibiste 🙂")
                return
            nid = d["nid"]
            estados.pop(to, None)
            cantr = int(digitos)
            nec2 = db.registrar_recibido(nid, cantr, quien=f"wa:{to}")
            if not nec2:
                wa_api.enviar_texto(to, "No encontré ese requerimiento.")
                return
            if nec2.get("estado") == "resuelta":
                extra = "\n\n🎉 ¡Con esto se cubrió la meta! Ya sale de la lista de «lo que hace falta»."
            else:
                meta = nec2.get("meta")
                falta = max(0, (meta or 0) - (nec2.get("recibido") or 0) - db.en_camino_de(nid)) if meta else None
                extra = (f"\n\nVas en *{nec2.get('recibido') or 0}* recibido(s)"
                         + (f", faltan *{falta}*." if falta is not None else "."))
            wa_api.enviar_texto(to, f"✅ Anotado: recibiste *{cantr}* de *{nec2.get('item') or ''}*.{extra}")


def manejar_interactivo(to, reply_id):
    accion, _, valor = reply_id.partition("|")

    if accion == "rol":
        _iniciar(to, valor)
        return

    if accion == "menu":  # menú del rol centro (lista del "¡Hola de nuevo!")
        ident = resolver_identidad(to)
        reg = ident.get("registro") or {}
        if not (ident.get("tipo") == "vinculado" and ident.get("rol") == "centro"
                and reg.get("estado") == "aprobado"):
            wa_api.enviar_texto(to, "Ese menú es para responsables de un punto *registrado y "
                                    "aprobado*. Escribe *hola* para registrarte. 🙌")
            return
        if valor == "inv":
            # Fija el modo explícito ANTES de la foto/voz (misma regla que /inventario):
            # oferta si el punto publica oferta; stock (privado) para el resto.
            efimero.kv_set("modo_wa", to,
                           "oferta" if reg.get("tipo_punto") in db.TIPOS_OFERTA else "stock")
            wa_api.enviar_texto(to, "📦 ¡Perfecto! Mándame una *foto* de tu lista o del depósito, "
                                    "una *nota de voz* dictándola, *texto*, o un archivo "
                                    "*PDF / Excel / Word*, y yo lo leo y lo organizo. 🙌")
        elif valor == "ver":
            _mostrar_inventario(to, reg.get("punto_id") or reg["id"])
        elif valor == "nec":
            _iniciar_necesito(to, reg)
        elif valor == "desp":
            _iniciar_despacho(to, reg)
        elif valor == "pedir":
            _iniciar(to, "pedir")  # bifurca solo: ¿para tu punto o para otra persona?
        elif valor == "ficha":
            if (reg.get("rol") or "representante") == "colaborador":
                wa_api.enviar_texto(to, "La ficha del punto la gestiona su responsable "
                                        "principal 🙂. Tú puedes cargar inventario y "
                                        "requerimientos directamente.")
            else:
                estados[to] = {"flujo": "centro", "paso": "ficha",
                               "datos": {"ficha": {}, "fi": 0, "tipo": reg.get("tipo_punto"),
                                         "ficha_full": True, "editar_rid": reg["id"]}}
                wa_api.enviar_texto(to, "✏️ Vamos a completar/actualizar la *ficha del punto*. "
                                        "Puedes tocar los botones o escribir la respuesta.")
                _ficha_preguntar(to, estados[to])
        elif valor == "salir":
            _iniciar_retiro(to, ident)
        return

    if accion in ("colok", "colrm"):  # respuesta del dueño al aviso de colaborador nuevo
        rep = db.representante(int(valor)) if valor.isdigit() else None
        if not rep or (rep.get("rol") or "") != "colaborador":
            wa_api.enviar_texto(to, "Ese registro ya no existe 🙂.")
            return
        if accion == "colok":
            wa_api.enviar_texto(to, "👍 ¡Gracias por confirmar! Sigan coordinando en equipo. 💪")
            return
        if rep.get("estado") != "rechazado":
            db.set_estado_representante(rep["id"], "rechazado")
        avisado = (f"Tu vinculación como colaborador de {rep.get('punto') or 'el punto'} fue "
                   "revertida por el responsable del punto. Si crees que es un error, "
                   "comunícate directamente con el punto. 🙏")
        if rep.get("wa_phone") or rep.get("telefono"):
            wa_api.enviar_texto(rep.get("wa_phone") or rep.get("telefono"), avisado)
        elif rep.get("telegram_id"):
            _tg_texto(rep["telegram_id"], avisado)
        _tg_texto(GRUPO_MODERADORES,
                  f"🚫 Colaborador removido por el responsable del punto (WhatsApp): "
                  f"{rep['nombre']} ya no es colaborador de {rep.get('punto') or ''} "
                  "(el dueño no lo reconoció).")
        wa_api.enviar_texto(to, "Listo, quedó removido 🙌. Gracias por avisar — así mantenemos "
                                "la red confiable.")
        return

    if accion == "wcolab":  # registro: el punto tecleado ya existe (colaborador / otro / corregir)
        st = estados.get(to)
        if not st or st.get("flujo") != "centro" or st.get("paso") != "colaborar":
            return
        if valor == "si":
            _registrar_colaborador_wa(to, st)
        elif valor == "otro":
            d = st["datos"]
            d["ficha"] = {}
            d["fi"] = 0
            st["paso"] = "ficha"
            wa_api.enviar_texto(to, "🙌 ¡Entendido! Lo registro como un punto aparte. "
                                    "Ahora unas preguntas rápidas del punto.")
            _ficha_preguntar(to, st)
        else:  # escribir otro nombre
            st["paso"] = "punto"
            wa_api.enviar_texto(to, "✏️ Sin problema. ¿Cómo se llama el punto?")
        return

    if accion == "vlla":  # voluntario: levantar un llamado (botón del saludo)
        if valor == "new":
            ident = resolver_identidad(to)
            if ident.get("tipo") == "vinculado" and ident.get("rol") == "voluntario":
                _iniciar_llamado(to, ident["registro"])
            else:
                wa_api.enviar_texto(to, "Para levantar llamados primero regístrate como "
                                        "*Voluntariado*. Escribe *hola* para empezar. 🙌")
        return

    if accion == "ret":  # retiro voluntario / volver a la red
        ident = resolver_identidad(to)
        if ident.get("tipo") != "vinculado":
            wa_api.enviar_texto(to, "No encontré un registro tuyo 🙂. "
                                    "Escribe *hola* si quieres sumarte a la red.")
            return
        reg, tabla = ident["registro"], ident["tabla"]
        setter = _SET_ESTADO[tabla]
        rol_txt = _ROL_TXT.get(ident["rol"], "miembro")
        nombre = reg.get("punto") or reg.get("nombre") or "(sin nombre)"
        if valor == "si" and reg.get("estado") != "retirado":
            setter(reg["id"], "retirado")
            wa_api.enviar_texto(to, "🕊️ Listo, tu retiro quedó registrado: ya no apareces en la web "
                                    "ni recibirás avisos de la red. Gracias por haber formado parte. "
                                    "Si algún día quieres volver, solo escríbenos *hola*. 💙")
            _tg_texto(GRUPO_MODERADORES,
                      f"🚪 Retiro voluntario (WhatsApp)\n{rol_txt}: {nombre} (id {reg['id']}) "
                      "pidió salir de la plataforma. Ya no aparece en la web ni recibe avisos. "
                      "Si escribe de nuevo, el bot le ofrece volver.")
        elif valor == "volver" and reg.get("estado") == "retirado":
            setter(reg["id"], "aprobado")
            wa_api.enviar_texto(to, "💙 ¡Bienvenido de vuelta! Tu registro está activo otra vez y "
                                    "volverás a aparecer en la web.")
            _tg_texto(GRUPO_MODERADORES,
                      f"💙 {rol_txt} «{nombre}» (id {reg['id']}) volvió a la red "
                      "(reactivación voluntaria por WhatsApp).")
        else:
            wa_api.enviar_texto(to, "💙 ¡Qué bueno que sigues en la red! No cambié nada.")
        return

    if accion == "link":
        st = estados.get(to)
        if valor == "si" and st and st["flujo"] == "vincular":
            _set_wa_phone(st["datos"]["tabla"], st["datos"]["id"], to)
            estados.pop(to, None)
            wa_api.enviar_texto(to, "✅ ¡Enlazado! Ahora puedes usar este WhatsApp. 🙌")
        else:
            estados.pop(to, None)
            _menu_roles(to)
        return

    if accion == "nec1":  # siembra post-alta de la primera necesidad
        st = estados.get(to)
        if not st or not str(st.get("paso", "")).startswith("nec1"):
            return
        if valor == "more":
            st["paso"] = "nec1_item"
            st["datos"].pop("nec1_item", None)
            wa_api.enviar_botones(to, "¿Qué otro *insumo* te falta? (uno a la vez)",
                                  [("nec1|done", "✅ Ya está")])
            return
        rid = st["datos"].get("rid")
        estados.pop(to, None)
        # Cierre del onboarding: nudge de inventario de siempre.
        wa_api.enviar_botones(
            to, "📦 *Cuando puedas:* carga tu inventario (foto, nota de voz, PDF o Excel) "
                "para que la red sepa qué tienes. También puedes completar la ficha del "
                "punto desde el menú (*Completar ficha*).\n\n¿Cargamos el inventario ahora?",
            [(f"inv|now|{rid}", "📦 Cargar ahora"), (f"inv|later|{rid}", "⏰ Seguir luego")])
        return

    if accion == "inv":  # nudge post-registro: cargar inventario ahora / luego
        sub, _, rid_s = valor.partition("|")
        if sub == "now":
            wa_api.enviar_texto(to, "¡Perfecto! 📸 Mándame ahora una *foto* de tu lista, una "
                                    "*nota de voz* o un *PDF/Excel* con tu inventario, y yo lo leo y lo organizo.")
        elif sub == "later" and rid_s.isdigit():
            con = db.conectar()
            con.execute("INSERT OR IGNORE INTO recordatorios_inv (rep_id, canal, destino) VALUES (?,?,?)",
                        (int(rid_s), "wa", to))
            con.commit()
            con.close()
            wa_api.enviar_texto(to, "👍 Sin problema. Te lo recordaré una vez al día hasta que lo cargues. "
                                    "Puedes mandarlo cuando quieras.")
        return

    if accion == "cap":  # inventario: sumar / reemplazar / descartar, o guardar/descartar una necesidad
        cid_s, _, acc = valor.partition("|")
        if not cid_s.isdigit():
            return
        cid = int(cid_s)
        cap = db.captura(cid)
        if cap and cap.get("clase") == "demanda":
            if acc == "ok":
                db.confirmar_captura(cid)
                wa_api.enviar_texto(to, "✅ ¡Guardado! Avisaremos a los acopios que puedan cubrirlo. 🙏")
                threading.Thread(target=_demanda_a_necesidades_wa,
                                 args=(cid, cap["representante_id"]), daemon=True).start()
            else:
                db.descartar_captura(cid)
                wa_api.enviar_texto(to, "🗑️ Descartado. Puedes mandar otra lista cuando quieras.")
            return
        if acc == "add":
            # Auditoría anti-duplicado (lección Ecocamping): si la lista se parece a lo
            # ya publicado, sumar duplicaría → advertir y recomendar Reemplazar.
            coinciden, total = stock.parece_snapshot(cid)
            if total >= 3 and coinciden * 2 >= total:
                wa_api.enviar_botones(
                    to, f"⚠️ *Ojo:* {coinciden} de {total} insumos de esta lista *ya están* en tu "
                        "inventario publicado. Si sumas, se duplican.\n\n¿Qué prefieres?",
                    [(f"cap|{cid}|rep", "🔄 Reemplazar"), (f"cap|{cid}|addf", "➕ Sumar igual")])
                return
            acc = "addf"
        if acc in ("addf", "rep"):
            punto_id = stock.aplicar_captura(cid, reemplazar=(acc == "rep"))
            db.confirmar_captura(cid)
            _apagar_recordatorio(cid)
            _reporte_inventario(to, punto_id, "add" if acc == "addf" else "rep")
        else:
            db.descartar_captura(cid)
            wa_api.enviar_texto(to, "🗑️ Descartado. Puedes mandar otra lista cuando quieras.")
        return

    if accion == "dest":  # despacho: destino elegido de la lista
        st = estados.get(to)
        if st and st.get("flujo") == "despacho" and valor.isdigit():
            st["datos"]["dest"] = int(valor)
            st["paso"] = "items"
            _despacho_pedir_items(to, st)
        return

    if accion == "dgo":  # despacho: confirmar / cancelar
        st = estados.get(to)
        if not (st and st.get("flujo") == "despacho"):
            return
        d = st["datos"]
        if valor != "si" or not d.get("items"):
            estados.pop(to, None)
            wa_api.enviar_texto(to, "❌ Despacho cancelado.")
            return
        try:
            e = envios.despachar(d["pid"], d["rep"], d["dest"], d["dest"], d["items"])
        except ValueError as err:
            wa_api.enviar_texto(to, f"⚠️ {err}. Ajusta la lista y vuelve a intentarlo.")
            return
        estados.pop(to, None)
        env = envios.por_token(e["token"])
        wa_api.enviar_imagen_bytes(to, envios.qr_png(e["token"]),
                                   envios.manifiesto_texto(env) +
                                   "\n\n📱 Imprime o muestra este QR: quien recibe lo escanea y confirma la entrega.")
        wa_api.enviar_texto(to, "🔗 También sirve el enlace directo:\n" + envios.url_entrega(e["token"]))
        envios.notificar_rep(d["dest"], "🚚 " + (env.get("de_punto") or "Un acopio") +
                             " te despachó un envío:\n\n" + envios.manifiesto_texto(env) +
                             "\n\nCuando llegue, revísalo y confírmalo aquí:\n" + envios.url_entrega(e["token"]))
        return

    if accion == "llgo":  # llamado de apoyo: confirmar / cancelar antes de publicar
        st = estados.get(to)
        if not (st and st.get("flujo") == "llamado" and st.get("paso") == "confirmar"):
            return
        if valor != "si":
            estados.pop(to, None)
            wa_api.enviar_texto(to, "❌ Descartado, no se publicó nada. Escribe *llamado* "
                                    "si quieres intentarlo de nuevo.")
            return
        _finalizar_llamado(to, st)
        return

    if accion == "msq":  # solicitud de contacto (web) a una Mano Solidaria
        import contacto_ms
        sid_s, _, dec = valor.partition("|")
        sol = contacto_ms.solicitud(int(sid_s)) if sid_s.isdigit() else None
        if not sol or sol["canal"] != "wa" or (sol["destino"] or "") != to:
            wa_api.enviar_texto(to, "Esa solicitud ya no está disponible 🙂")
            return
        if sol["estado"] != "pendiente":
            wa_api.enviar_texto(to, "Ya respondiste esa solicitud 🙂")
            return
        if dec == "si":
            info = contacto_ms.responder(int(sid_s), True)
            if info:
                texto = ("🤝 ¡Gracias! Este es el contacto de la persona:\n"
                         f"👤 *{info.get('nombre') or ''}*\n"
                         f"📞 *{info.get('contacto') or ''}*")
                if info.get("wa_link"):
                    texto += "\n\nEscríbele con un mensaje ya listo:\n" + info["wa_link"]
                texto += "\n\nElla también recibió tu número. Coordinen en lugares seguros. 💙"
                wa_api.enviar_texto(to, texto)
        else:
            contacto_ms.responder(int(sid_s), False)
            wa_api.enviar_texto(to, "Entendido, gracias por avisar 🙏. Le diremos que "
                                    "no estás disponible por ahora.")
        return

    if accion == "atq":  # opt-in del solicitante para compartir su contacto (relay R1)
        import contacto_ms
        sid_s, _, dec = valor.partition("|")
        sol = contacto_ms.solicitud_atencion(int(sid_s)) if sid_s.isdigit() else None
        if not sol or sol["canal"] != "wa" or (sol["destino"] or "") != to:
            wa_api.enviar_texto(to, "Esa solicitud ya no está disponible 🙂")
            return
        if sol["estado"] != "pendiente":
            wa_api.enviar_texto(to, "Ya respondiste esa solicitud 🙂")
            return
        if dec == "si":
            info = contacto_ms.responder_atencion(int(sid_s), True)
            if info:
                wa_api.enviar_texto(to, "🤝 Listo. Compartimos tu contacto con quien quiere ayudarte.\n\n"
                                        f"👤 *{sol.get('ayuda_nombre') or ''}*\n"
                                        f"📞 *{sol.get('ayuda_contacto') or ''}*")
        else:
            contacto_ms.responder_atencion(int(sid_s), False)
            wa_api.enviar_texto(to, "Entendido. No compartimos tu número con esa persona.")
        return

    if accion == "pedq":  # pedido de un representante: ¿para su punto o para otra persona?
        if valor == "punto":
            ident = resolver_identidad(to)
            if ident["tipo"] == "vinculado" and ident["rol"] == "centro":
                _iniciar_necesito(to, ident["registro"])   # vía rica: necesidades del punto
            else:
                _iniciar(to, "pedir", forzar_publico=True)
        else:
            _iniciar(to, "pedir", forzar_publico=True)
        return

    if accion == "lla":  # aviso de llamado: puedo ayudar / no puedo / ya coordiné
        aid_s, _, resp = valor.partition("|")
        av = db.aviso_llamado(int(aid_s)) if aid_s.isdigit() else None
        if not av or not coincide(av.get("destino_wa") or "", to):
            wa_api.enviar_texto(to, "Esa solicitud ya no está disponible 🙂")
            return
        sector = av.get("sector") or "el sector reportado"
        items = av.get("items") or "lo solicitado"
        quien = av.get("quien") or "un punto de apoyo"
        if resp == "no":
            wa_api.enviar_texto(to, "Entendido, gracias por avisar 🙏. Seguimos buscando "
                                    "quién pueda cubrirlo.")
        elif resp == "si":
            msg = (f"Hola 👋, soy {quien}. Vi tu pedido de ayuda para {sector}. "
                   f"Sí puedo ayudar con {items}. ¿Cómo coordinamos?")
            d = canonico(av.get("contacto") or "")
            if d:
                texto = ("🤝 ¡Gracias! Escríbele directo con este mensaje ya listo:\n\n"
                         "https://wa.me/58" + d + "?text=" + urllib.parse.quote(msg))
            else:
                texto = ("🤝 ¡Gracias! Su contacto: "
                         + (av.get("contacto") or "no disponible") + "\n\n" + msg)
            wa_api.enviar_botones(
                to, texto + "\n\nCuando coordinen la entrega, toca *Ya coordiné* y "
                            "cierro el pedido en la web." + _coletilla(items),
                [(f"lla|{aid_s}|ok", "✅ Ya coordiné")])
        elif resp == "ok":
            lla = db.cerrar_llamado_por_vinculo(av["llamado_id"], quien, None, items)
            if lla:
                wa_api.enviar_texto(to, "🤝 ¡Listo! Registramos el vínculo y el pedido sale "
                                        "de la web en unos minutos. Gracias por ayudar 🙌")
                _tg_texto(GRUPO_MODERADORES,
                          f"🤝 Vínculo hecho (WhatsApp): {quien} cubre el pedido "
                          f"#{av['llamado_id']} ({items}) en {sector}. Se cierra en la web.")
                if lla.get("telegram_id"):
                    _tg_texto(lla["telegram_id"],
                              f"🤝 {quien} confirmó que coordinó la ayuda de tu llamado. "
                              "Lo marcamos como cubierto. 🙌")
            else:
                wa_api.enviar_texto(to, "Ese pedido ya no está disponible 🙂")
        return

    if accion == "nec":  # aviso de demanda (puente /necesito): "puedo ayudar" → cuánto puede llevar
        if valor.isdigit():
            _iniciar_compromiso_wa(to, int(valor))
        return

    if accion == "necno":  # aviso de demanda: declina
        wa_api.enviar_texto(to, "Entendido, gracias por avisar 🙏. Seguimos buscando quién pueda cubrirlo.")
        return

    if accion == "recibi":  # el centro registra lo recibido de un requerimiento (por WA)
        if not valor.isdigit():
            return
        nid = int(valor)
        nec = db.necesidad(nid)
        if not nec:
            wa_api.enviar_texto(to, "No encontré ese requerimiento.")
            return
        estados[to] = {"flujo": "recibido", "paso": "cant", "datos": {"nid": nid}}
        unidad = nec.get("unidad") or "unidades"
        wa_api.enviar_texto(to, f"📥 *{nec.get('item') or ''}* — ¿cuántas *{unidad}* recibiste? "
                                "Escribe el número.")
        return

    st = estados.get(to)
    if not st:
        return
    if accion == "fi" and st["flujo"] == "centro" and st["paso"] == "ficha":
        _ficha_responder(to, st, _FICHA_VAL.get(valor, valor))
    elif accion == "tipo" and st["flujo"] == "centro":
        st["datos"]["tipo"] = valor
        st["paso"] = "punto"
        wa_api.enviar_texto(to, f"¿Cómo se llama el/la *{TIPOS_LABEL.get(valor, 'punto')}*?")
    elif accion == "pcat" and st["flujo"] == "pedir":
        if valor not in CATS_PEDIR_LABEL:
            return
        st["datos"]["cat"] = valor
        # Delegar SIEMPRE en _pedir_siguiente_paso: es quien sabe qué toca preguntar
        # (para desaparecidos pregunta primero a quién buscan, con textos propios).
        _pedir_siguiente_paso(to, st)
    elif accion == "cat" and st["flujo"] == "aliado":
        st["datos"]["categoria"] = valor
        st["paso"] = "descripcion"
        wa_api.enviar_texto(to, "Cuéntame en una línea *qué ofreces exactamente*. (ej. 100 platos de sopa al día)")


# ─────────────────────────────── Ingestión de inventario ─────────────────────
def _resumen_captura(to, cid, lec, trans=None, clase=None):
    """Muestra lo que se extrajo + botones (la captura ya está en la DB como
    'pendiente'; solo se ve en la web al confirmar). Si `clase` es 'demanda'
    (el centro está declarando lo que le hace falta), ofrece Guardar/Descartar
    en vez de Sumar/Reemplazar — no es inventario que TIENE, es lo que NECESITA."""
    L = []
    if trans:
        L.append(f"🎙️ _{trans}_\n")
    L.append(f"📋 *{lec.tipo}*" + (f" · {lec.punto}" if lec.punto else ""))
    if lec.personas:
        L.append(f"\n👥 *{len(lec.personas)} personas*:")
        for p in lec.personas[:10]:
            extra = " · ".join(x for x in [("CI " + p.cedula) if p.cedula else "", p.edad or ""] if x)
            L.append("• " + p.nombre + (f" — {extra}" if extra else ""))
        if len(lec.personas) > 10:
            L.append(f"…y {len(lec.personas) - 10} más")
    if lec.items:
        L.append(f"\n📦 *{len(lec.items)} insumos*:")
        for i in lec.items[:10]:
            d = " ".join(x for x in [i.cantidad or "", f"({i.detalle})" if i.detalle else ""] if x)
            L.append("• " + i.item + (f" — {d}" if d else ""))
        if len(lec.items) > 10:
            L.append(f"…y {len(lec.items) - 10} más")
    if not lec.items and not lec.personas:
        L.append("\n(No detecté insumos ni personas.)")
    if clase == "demanda":
        L.append("\n¿Guardo esto?")
        wa_api.enviar_botones(to, "\n".join(L)[:1024],
                              [(f"cap|{cid}|ok", "✅ Guardar"),
                               (f"cap|{cid}|no", "🗑️ Descartar")])
    else:
        L.append("\n¿Sumar a tu inventario actual o reemplazarlo?")
        wa_api.enviar_botones(to, "\n".join(L)[:1024],
                              [(f"cap|{cid}|add", "➕ Sumar"),
                               (f"cap|{cid}|rep", "🔄 Reemplazar"),
                               (f"cap|{cid}|no", "🗑️ Descartar")])


def _procesar_media(to, msg, rep):
    """Descarga el archivo, lo lee con captura.py y ofrece guardarlo. Corre en un HILO."""
    tipo = msg.get("type")
    try:
        trans = None
        if tipo == "image":
            data, _ = wa_api.descargar_media(msg["image"]["id"])
            if data is None:
                raise RuntimeError("no se pudo descargar la imagen")
            lec = captura.leer_imagen(data)
        elif tipo == "audio":
            data, _ = wa_api.descargar_media(msg["audio"]["id"])
            if data is None:
                raise RuntimeError("no se pudo descargar el audio")
            trans = captura.transcribir(data)
            if not trans:
                wa_api.enviar_texto(to, "No logré entender el audio 🙏. Intenta de nuevo, más claro.")
                return
            lec = captura.leer_texto(trans)
        elif tipo == "document":
            doc = msg["document"]
            mime = (doc.get("mime_type") or "").lower()
            nombre = (doc.get("filename") or "").lower()
            data, _ = wa_api.descargar_media(doc["id"])
            if data is None:
                raise RuntimeError("no se pudo descargar el documento")
            if mime == "application/pdf" or nombre.endswith(".pdf"):
                lec = captura.leer_pdf(data)
            elif "spreadsheetml" in mime or nombre.endswith((".xlsx", ".xls")):
                lec = captura.leer_xlsx(data)
            elif "wordprocessingml" in mime or nombre.endswith(".docx"):
                lec = captura.leer_docx(data)
            elif mime.startswith("image/"):
                lec = captura.leer_imagen(data)
            else:
                wa_api.enviar_texto(to, "Ese tipo de archivo aún no lo entiendo 🙏. Manda foto, PDF, Excel, Word o un audio.")
                return
        else:
            return
        clase = efimero.kv_get("modo_wa", to, ttl=900)
        if clase is None:
            # Captura libre (sin comando): es inventario — oferta para acopio/fundación,
            # stock (privado) para el resto (paridad con Telegram; evita publicar el
            # stock de un refugio como carencia en la web).
            clase = "oferta" if rep.get("tipo_punto") in db.TIPOS_OFERTA else "stock"
        cid = db.crear_captura(rep["id"], tipo, lec.tipo, lec.punto, lec.resumen,
                               [i.model_dump() for i in lec.items],
                               [p.model_dump() for p in lec.personas], clase=clase)
        # Los ítems ya quedaron en la captura; el stock se aplica cuando el centro
        # elige Sumar/Reemplazar (stock.aplicar_captura, compartido con Telegram),
        # salvo que sea una NECESIDAD (clase='demanda'): esa se guarda con ✅ Guardar.
        _resumen_captura(to, cid, lec, trans, clase=clase)
    except Exception as e:  # noqa: BLE001
        print("procesar_media error:", e)
        wa_api.enviar_texto(to, "Ups, hubo un problema procesando eso 🙏. Intenta de nuevo en un momento.")


def _iniciar_necesito(to, reg):
    """Centro declara lo que le hace falta (paridad con /necesito de Telegram): la
    próxima captura que mande (foto/voz/PDF/Excel/Word) se guarda como NECESIDAD en
    vez de sumarse a su inventario. Dispara el puente demanda→oferta: avisamos a los
    acopios que sí la tienen (por Telegram o WhatsApp)."""
    efimero.kv_set("modo_wa", to, "demanda")
    if reg.get("tipo_punto") == "acopio":
        txt = ("🙏 *Donaciones que necesita tu centro de acopio*\n\n"
               "Mándame la lista de *lo que te hace falta* para que el público done "
               "(foto, voz, PDF, Excel o Word). Aparecerá en la web en *«Lo que hace falta»*.")
    else:
        txt = ("🙏 *Lo que necesita tu punto*\n\n"
               "Mándame la lista de *los insumos que te hacen falta* "
               "(foto, voz, PDF, Excel o Word). Así los acopios pueden conectarse contigo.")
    wa_api.enviar_texto(to, txt)


def _demanda_a_necesidades_wa(cid, rep_id):
    """Hilo: confirma la captura de NECESIDAD en la tabla `necesidades` y dispara el
    aviso a los acopios que la cubren. Reusa bot_apoyo._demanda_a_necesidades (import
    perezoso, mismo patrón que _triangular_llamado_wa) pasándole el rep ya resuelto
    (bot_wa no tiene telegram_id para buscarlo)."""
    try:
        import bot_apoyo
        rep = db.representante(rep_id)
        if rep:
            bot_apoyo._demanda_a_necesidades(cid, None, rep=rep)
    except Exception as e:  # noqa: BLE001
        print("demanda a necesidades (wa) error:", e)


def _iniciar_compromiso_wa(to, nid):
    """Alguien por WhatsApp se ofrece a llevar un insumo de una necesidad (mismo
    mecanismo que /start nec_<id> en Telegram, pero como YA está chateando por
    WhatsApp, su número es su contacto: no hace falta pedírselo aparte)."""
    nec = db.necesidad(nid)
    if not nec or nec.get("estado") in ("resuelta", "cancelada"):
        wa_api.enviar_texto(to, "🙏 Esa necesidad ya está cubierta o cerrada. ¡Gracias por querer ayudar!")
        return
    meta = nec.get("meta")
    falta = max(0, (meta or 0) - (nec.get("recibido") or 0) - db.en_camino_de(nid)) if meta else None
    if meta and falta <= 0:
        wa_api.enviar_texto(to, "🙏 Esa necesidad ya está cubierta (todo lo pedido está recibido "
                                "o en camino). ¡Gracias!")
        return
    estados[to] = {"flujo": "compromiso", "paso": "cant", "datos": {"nid": nid}}
    unidad = nec.get("unidad") or "unidades"
    cuanto = f"Faltan *{falta} {unidad}*. " if falta is not None else ""
    wa_api.enviar_texto(
        to, f"🤝 *{nec.get('punto') or 'Un centro'}* necesita: *{nec.get('item') or ''}*.\n{cuanto}\n"
            f"¿Cuántas *{unidad}* puedes llevar? Escribe solo el número.")


def _finalizar_compromiso(to, cant):
    """Cierra el paso 'cuánto puedes llevar': crea el compromiso y avisa al centro
    en segundo plano (llamadas de red, no bloquear la respuesta al webhook)."""
    st = estados.pop(to, None)
    if not st:
        return
    nid = st["datos"]["nid"]
    nombre = "Voluntario (WhatsApp)"
    cmpid, nec2 = db.crear_compromiso(nid, None, nombre, cant)
    if not cmpid:
        wa_api.enviar_texto(to, "🙏 ¡Gracias! Justo se acaba de cubrir lo que faltaba, "
                                "así que no hace falta más por ahora.")
        return
    wa_api.enviar_texto(
        to, f"✅ ¡Gracias! Anotamos que llevarás *{cant}* a "
            f"*{(nec2 or {}).get('punto') or 'el centro'}*.\n\n"
            "El centro recibirá tu WhatsApp para coordinar. Si al final no puedes, no pasa nada: "
            "tu reserva se libera sola en 12 h.")
    threading.Thread(target=_notificar_centro_compromiso_wa,
                     args=(nec2, cant, nombre, to), daemon=True).start()


def _notificar_centro_compromiso_wa(nec2, cant, nombre, to):
    try:
        import bot_apoyo
        bot_apoyo._notificar_centro_compromiso(nec2, cant, nombre, to)
    except Exception as e:  # noqa: BLE001
        print("notificar centro compromiso (wa) error:", e)


def _iniciar_respuesta_llamado_wa(to, lid):
    """Botón «Puedo ayudar» de la web por WhatsApp (paridad con /start lla_<id> de
    Telegram). Como ya está chateando por WhatsApp, su número es su contacto: solo
    se le pregunta cómo puede ayudar."""
    lla = db.llamado(lid)
    if not lla or lla.get("estado") != "activo":
        wa_api.enviar_texto(to, "🙏 Ese llamado ya se cerró. ¡Gracias por querer ayudar! "
                                "Mira otros en redacopioyrefugio.com")
        return
    estados[to] = {"flujo": "resp_llamado", "paso": "msg", "datos": {"lid": lid}}
    sector = lla.get("sector") or "el sector en emergencia"
    wa_api.enviar_texto(
        to, f"🙋 Vas a responder al llamado de *{sector}*:\n"
            f"_{(lla.get('mensaje') or '')[:300]}_\n\n"
            "✍️ ¿*Cómo puedes ayudar*? Escríbelo en una línea (ej. «llevo agua y voy "
            "con camioneta»). Si solo quieres ofrecerte, escribe «-».")


def _finalizar_respuesta_llamado(to, texto):
    """Cierra la respuesta a un llamado: la registra y avisa a quien lo levantó,
    con el WhatsApp del que se ofrece como contacto."""
    st = estados.pop(to, None)
    if not st:
        return
    lid = st["datos"]["lid"]
    m = (texto or "").strip()
    mensaje = None if m == "-" else m[:200]
    nombre = "Voluntario (WhatsApp)"
    try:
        ident = resolver_identidad(to)
        if ident["tipo"] == "vinculado":
            reg = ident["registro"]
            nombre = ((reg.get("nombre") or reg.get("punto") or "").strip() or nombre)[:60]
    except Exception as e:  # noqa: BLE001
        print("resp llamado identidad error:", e)
    contacto = "+" + to
    db.responder_llamado(lid, None, nombre, contacto, mensaje)
    wa_api.enviar_texto(to, "✅ ¡Gracias! Le pasamos tu WhatsApp a quien levantó el llamado "
                            "para que coordine contigo. 🤝")
    threading.Thread(target=_notificar_respuesta_llamado,
                     args=(lid, nombre, contacto, mensaje), daemon=True).start()


def _notificar_respuesta_llamado(lid, nombre, contacto, mensaje):
    """Avisa a quien levantó el llamado que alguien se ofreció. Por Telegram si lo
    levantó ahí (reusa bot_apoyo); si lo levantó por WhatsApp (telegram_id NULL),
    por WhatsApp al wa_phone del voluntario. Best-effort, corre en un hilo."""
    try:
        lla = db.llamado(lid)
        if not lla:
            return
        if lla.get("telegram_id"):
            import bot_apoyo
            bot_apoyo._notificar_voluntario_respuesta(lla, nombre, contacto, mensaje)
            return
        vol = db.voluntario(lla["voluntario_id"]) if lla.get("voluntario_id") else None
        d = canonico((vol or {}).get("wa_phone") or "")
        if not d:
            return
        cuerpo = ("🙋 *" + nombre + "* respondió a tu llamado"
                  + (f" de *{lla.get('sector')}*" if lla.get("sector") else "") + ".\n"
                  + (f"Ofrece: _{mensaje}_\n" if mensaje else "")
                  + f"📱 WhatsApp: *{contacto}*\n\nCoordina con esta persona la ayuda. 🤝")
        wa_api.enviar_texto("58" + d, cuerpo)
    except Exception as e:  # noqa: BLE001
        print("notificar respuesta llamado error:", e)


def _iniciar_despacho(to, reg):
    """Arranca el flujo de despacho: elegir destino de la lista de puntos."""
    pid = reg.get("punto_id") or reg["id"]
    if not stock.de_punto(pid):
        wa_api.enviar_texto(to, "Aún no tienes inventario publicado para despachar. "
                                "Mándame tu lista primero. 📦")
        return
    con = db.conectar()
    dests = con.execute(
        "SELECT id, punto FROM representantes WHERE estado = 'aprobado' AND id != ? "
        "AND punto IS NOT NULL AND TRIM(punto) != '' AND rol = 'representante' "
        "ORDER BY id DESC LIMIT 10", (reg["id"],)).fetchall()
    con.close()
    if not dests:
        wa_api.enviar_texto(to, "No hay otros puntos registrados todavía a quién despachar.")
        return
    estados[to] = {"flujo": "despacho", "paso": "dest", "datos": {"pid": pid, "rep": reg["id"]}}
    wa_api.enviar_lista(to, "🚚 *Despachar un envío*\n\n¿A qué punto va?", "Elegir destino",
                        [(f"dest|{d[0]}", (d[1] or "?")[:24]) for d in dests])


def _despacho_pedir_items(to, st):
    inv = stock.de_punto(st["datos"]["pid"])
    L = ["🚚 Perfecto. Tu inventario disponible:"]
    for r in inv[:30]:
        if r["cantidad"] is None:
            c = "❓"
        else:
            c = f"{r['cantidad'] - (r.get('reservado') or 0):g} {r['unidad'] or ''}".strip()
        L.append(f"• {r['item']} — {c}")
    L.append("\nEscríbeme qué envías, *una línea por insumo*:\n_pañales 40_\n_agua 5_")
    wa_api.enviar_texto(to, "\n".join(L)[:4000])


def _apagar_recordatorio(cid):
    """Al cargar inventario, apaga el recordatorio diario de ese centro."""
    con = db.conectar()
    con.execute("UPDATE recordatorios_inv SET activo = 0 WHERE rep_id = "
                "(SELECT representante_id FROM capturas WHERE id = ?)", (cid,))
    con.commit()
    con.close()


def _mostrar_inventario(to, punto_id):
    """'inventario' → lista lo publicado con cantidades; señala los que faltan."""
    inv = stock.de_punto(punto_id)
    if not inv:
        wa_api.enviar_texto(to, "Aún no tienes inventario publicado. Mándame una *foto*, "
                                "*nota de voz* o *PDF/Excel* con tu lista. 📦")
        return
    L = [f"📦 *Tu inventario publicado* ({len(inv)} insumos):"]
    for r in inv[:40]:
        if r["cantidad"] is not None:
            res = r.get("reservado") or 0
            c = f"{r['cantidad'] - res:g} {r['unidad'] or ''}".strip()
            if res:
                c += f" (+{res:g} en envíos)"
        else:
            c = "❓ sin cantidad"
        L.append(f"• {r['item']} — {c}")
    if len(inv) > 40:
        L.append(f"…y {len(inv) - 40} más")
    faltan = stock.faltan_cantidad(punto_id)
    if faltan:
        L.append(f"\n✍️ Completa los ❓ respondiendo, por ejemplo: _{faltan[0].lower()} 20_")
    wa_api.enviar_texto(to, "\n".join(L)[:4000])


def _reporte_inventario(to, punto_id, modo):
    """Tras Sumar/Reemplazar: confirma y avisa qué ítems quedaron sin cantidad."""
    if punto_id is None:
        wa_api.enviar_texto(to, "✅ Listo.")
        return
    items = stock.de_punto(punto_id)
    faltan = stock.faltan_cantidad(punto_id)
    cab = "➕ *Sumado a tu inventario.*" if modo == "add" else "🔄 *Inventario reemplazado.*"
    L = [cab, f"Ahora tienes *{len(items)} insumos* publicados."]
    if faltan:
        L.append("\n⚠️ Estos quedaron *sin cantidad*. Respóndeme la cantidad de cada uno "
                 "(ej. _pañales 200_) para completarlos:")
        L += ["• " + i for i in faltan[:15]]
        if len(faltan) > 15:
            L.append(f"…y {len(faltan) - 15} más")
    wa_api.enviar_texto(to, "\n".join(L)[:1024])


# ─────────────────────────────────── Webhook ─────────────────────────────────
@app.route("/wa/webhook", methods=["GET"])
def verificar():
    if (request.args.get("hub.mode") == "subscribe" and
            request.args.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403


@app.route("/wa/webhook", methods=["POST"])
def entrante():
    data = request.get_json(silent=True) or {}
    try:
        for entry in data.get("entry", []):
            for ch in entry.get("changes", []):
                for msg in ch.get("value", {}).get("messages", []):
                    _despachar(msg)
                for st in ch.get("value", {}).get("statuses", []):
                    if st.get("status") == "failed":  # motivo exacto de una entrega fallida
                        print("wa status FAILED:", json.dumps(st)[:600])
    except Exception as e:  # noqa: BLE001 — nunca devolver 500 a Meta (reintenta en bucle)
        print("webhook error:", e)
    return "ok", 200


def _despachar(msg):
    """Hidrata el estado de la conversación desde la DB, procesa el mensaje y
    persiste (o borra) el estado al salir. Así los workers de gunicorn comparten
    la conversación y un restart no la pierde; `estados` es solo scratch."""
    to = msg.get("from")
    if not to:
        return
    st_db = efimero.estado_get(to)
    if st_db is not None:
        estados[to] = st_db
    else:
        estados.pop(to, None)
    try:
        _procesar_msg(to, msg)
    finally:
        st = estados.pop(to, None)  # scratch fuera de memoria; la verdad queda en DB
        if st is not None:
            efimero.estado_set(to, st)
        else:
            efimero.estado_del(to)


def _procesar_msg(to, msg):
    wa_api.marcar_leido(msg.get("id"))
    tipo = msg.get("type")
    if tipo == "text":
        manejar_texto(to, msg.get("text", {}).get("body", ""))
    elif tipo == "interactive":
        inter = msg.get("interactive", {})
        rid = (inter.get("button_reply") or inter.get("list_reply") or {}).get("id", "")
        if rid:
            manejar_interactivo(to, rid)
    elif tipo == "button":  # quick reply de una PLANTILLA (aviso de llamado fuera de 24 h)
        payload = (msg.get("button") or {}).get("payload", "")
        if payload:
            manejar_interactivo(to, payload)
    elif tipo in ("image", "document", "audio"):
        st = estados.get(to)
        if st and st.get("flujo") == "llamado":
            # El voluntario está dictando su llamado: foto o nota de voz son contenido.
            if st.get("paso") == "contenido" and tipo in ("image", "audio"):
                wa_api.enviar_texto(to, "📥 ¡Recibido! Dame unos segundos…")
                threading.Thread(target=_procesar_media_llamado,
                                 args=(to, dict(msg), st), daemon=True).start()
            elif st.get("paso") == "contenido":
                wa_api.enviar_texto(to, "Para el llamado mándame *texto*, una *foto* o una "
                                        "*nota de voz* 🙏")
            elif st.get("paso") == "confirmar":
                wa_api.enviar_texto(to, "Usa los botones de arriba para confirmar 👆 "
                                        "(o escribe *cancelar* para descartarlo).")
            else:
                wa_api.enviar_texto(to, "Primero dime el *sector / zona* del llamado 🙂 "
                                        "(o escribe *cancelar* para salir)")
            return
        if st and st.get("flujo") == "pedir":
            # Foto/voz durante 'pedir ayuda': la IA la lee y prellena lo que falte
            # (mismo circuito que /api/leer_flyer de la web). Vale en cualquier paso.
            wa_api.enviar_texto(to, "📥 ¡Recibido! Dame unos segundos…")
            threading.Thread(target=_procesar_media_pedir,
                             args=(to, dict(msg), st), daemon=True).start()
            return
        ident = resolver_identidad(to)
        reg = ident.get("registro") or {}
        if ident.get("tipo") == "vinculado" and ident.get("rol") == "centro" and reg.get("estado") == "aprobado":
            wa_api.enviar_texto(to, "📥 ¡Recibido! Dame unos segundos para leerlo…")
            threading.Thread(target=_procesar_media, args=(to, dict(msg), reg), daemon=True).start()
        else:
            wa_api.enviar_texto(to, "📎 Para enviar inventario primero debes tener tu *centro registrado y aprobado*. "
                                    "Escribe *hola* para registrarte. 🙌")
    elif tipo in ("voice", "video", "sticker"):
        wa_api.enviar_texto(to, "Por ahora entiendo *foto, PDF, Excel, Word o notas de voz* para el inventario. 🙌")
    else:
        manejar_texto(to, "")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8091)
