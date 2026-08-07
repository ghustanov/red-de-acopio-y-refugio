"""
Bot de afiliación de representantes — "Ángeles · Apoyo" (@angelesapoyove_bot).

Un responsable de un punto (centro de acopio, hospital, clínica o refugio) se
afilia conversando con el bot; un coordinador lo aprueba con un toque desde el
grupo de moderadores. Corre siempre (long-poll). Aislado del resto.
"""
import html
import json
import math
import re
import socket
import threading
import time
import urllib.parse
import urllib.request

import db
import efimero
import captura
import envios
import stock
import wa_api
from config import (BASE, APOYO_BOT_TOKEN, GRUPO_MODERADORES, AUTO_APROBAR, WEB_DIR,
                    WA_TPL_LLAMADO)

# Forzar IPv4: desde este VPS algunas conexiones IPv6 a Telegram se congelan
# hasta agotar el timeout (~70s), trabando el bot. La ruta IPv4 es estable.
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4(host, *args, **kwargs):
    res = _orig_getaddrinfo(host, *args, **kwargs)
    solo_v4 = [r for r in res if r[0] == socket.AF_INET]
    return solo_v4 or res
socket.getaddrinfo = _getaddrinfo_ipv4

API = f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}"
TIPOS = {
    "acopio": "Centro de acopio", "refugio": "Refugio", "hospital": "Hospital",
    "cdi": "CDI", "maternidad": "Maternidad", "clinica": "Clínica",
    "fundacion": "Fundación", "otro": "Otro punto",
}

# Ficha informativa del punto: se pregunta una a una después de los datos de la
# persona y el nombre del punto. Si algo no corresponde (un centro de acopio sin
# baños/cocina/camas), el representante toca "No aplica / No posee".
#   modo: "sino"    → botones Sí / No
#         "sino_na" → botones Sí / No / No aplica
#         "no_posee"/"no_aplica" → un botón para saltar
#         None      → solo texto
#   solo / salvo: la pregunta aplica SOLO a esos tipos / a todos SALVO esos.
#   (Los campos nuevos van al FINAL de la lista: ficha_i persiste en efimero y
#   un índice corrido re-preguntaría mal a quien esté a mitad de registro.)
FICHA = [
    {"campo": "tipo_inmueble",    "preg": "🏚️ ¿Qué <b>tipo de inmueble</b> es? (escuela, iglesia, galpón, clínica, casa…)", "modo": None, "salvo": ("fundacion",)},
    {"campo": "aforo",            "preg": "👥 ¿Cuál es el <b>aforo</b>? (cuántas personas caben)",                          "modo": "no_aplica", "salvo": ("fundacion",)},
    {"campo": "banos",            "preg": "🚻 ¿Cuántos <b>baños</b> tiene?",                                                "modo": "no_posee", "salvo": ("fundacion",)},
    {"campo": "duchas",           "preg": "🚿 ¿Cuántas <b>duchas/regaderas</b> tiene?",                                     "modo": "no_posee", "salvo": ("fundacion",)},
    {"campo": "cocina",           "preg": "🍳 ¿Posee <b>cocina</b>?",                                                       "modo": "sino", "salvo": ("fundacion",)},
    {"campo": "agua_potable",     "preg": "🚰 ¿Tiene <b>agua potable</b>?",                                                 "modo": "sino", "salvo": ("fundacion",)},
    {"campo": "electricidad",     "preg": "⚡ ¿Tiene <b>electricidad</b>?",                                                  "modo": "sino", "salvo": ("fundacion",)},
    {"campo": "planta",           "preg": "🔋 ¿Tiene <b>planta o generador</b> eléctrico?",                                 "modo": "sino", "salvo": ("fundacion",)},
    {"campo": "camas",            "preg": "🛏️ ¿Cuántas <b>camas disponibles</b> hay?",                                      "modo": "no_aplica", "salvo": ("fundacion",)},
    {"campo": "acceso_vehiculos", "preg": "🚛 ¿Tiene <b>acceso para vehículos</b>? (ambulancias, camiones)",                "modo": "sino"},
    {"campo": "accesibilidad",    "preg": "♿ ¿Es <b>accesible para personas con discapacidad</b>? (rampas)",                "modo": "sino", "salvo": ("fundacion",)},
    {"campo": "almacenamiento",   "preg": "📦 ¿Qué <b>capacidad de almacenamiento</b> tiene? (para acopio)",                "modo": "no_aplica"},
    {"campo": "refrigeracion",    "preg": "❄️ ¿Tiene <b>refrigeración / cadena de frío</b>?",                               "modo": "sino_na"},
    {"campo": "ubicacion",        "preg": "📍 ¿Cuál es la <b>dirección exacta</b> del punto?\n\nEscríbela lo <b>más precisa posible</b>: estado, ciudad o municipio, urbanización/sector, calle y número — y un <b>punto de referencia</b> cercano (una plaza, un comercio, una esquina conocida).\n\n👉 Mientras más precisa, más fácil que donantes y voluntarios <b>lleguen a ti</b>.",                      "modo": None, "core": True},
    {"campo": "horario",          "preg": "🕐 ¿Cuál es el <b>horario de atención</b>? (ej. 24 horas, 8am-6pm)",             "modo": None, "core": True},
    {"campo": "responsable",      "preg": "🧑‍💼 ¿Quién es la <b>persona responsable</b> del punto?",                        "modo": None},
    {"campo": "contacto",         "preg": "📞 ¿Cuál es el <b>número de contacto</b>?",                                      "modo": None, "core": True},
    {"campo": "rif",              "preg": "🪪 ¿Cuál es el <b>RIF</b> de la fundación? (ej. J-12345678-9)\n\nSi aún no están constituidos legalmente, toca <i>No posee</i> — igual pueden participar.", "modo": "no_posee", "solo": ("fundacion",), "core": True},
    {"campo": "cobertura",        "preg": "🗺️ ¿En qué <b>zonas puede entregar o servir</b> la fundación? (ej. Caracas y Miranda; todo el país)", "modo": None, "solo": ("fundacion",), "core": True},
    {"campo": "areas",            "preg": "🎯 ¿Qué <b>áreas atiende</b> la fundación? Nómbralas separadas por comas (ej. animales, salud, niños, alimentación, rescate, adultos mayores).\n\n👉 Con esto te avisamos los <b>pedidos de ayuda y llamados</b> de tu área, aunque no tengas inventario cargado.", "modo": None, "solo": ("fundacion",), "core": True},
]
FICHA_LABEL = {
    "tipo_inmueble": "Tipo de inmueble", "aforo": "Aforo", "banos": "Baños",
    "duchas": "Duchas", "cocina": "Cocina", "agua_potable": "Agua potable",
    "electricidad": "Electricidad", "planta": "Planta/generador",
    "camas": "Camas disponibles", "acceso_vehiculos": "Acceso vehículos",
    "accesibilidad": "Accesibilidad", "almacenamiento": "Almacenamiento",
    "refrigeracion": "Refrigeración", "ubicacion": "Ubicación", "horario": "Horario",
    "responsable": "Responsable", "contacto": "Contacto",
    "rif": "RIF", "cobertura": "Zona de cobertura", "areas": "Áreas de atención",
}


def _ficha_aplica(q, tipo):
    """¿Esta pregunta de ficha corresponde al tipo de punto? (solo/salvo)."""
    if q.get("solo") and tipo not in q["solo"]:
        return False
    if q.get("salvo") and tipo in q["salvo"]:
        return False
    return True


def _ficha_en_alta(q, st):
    """En el ALTA solo va la ficha esencial (core: dirección, horario, contacto y,
    para fundaciones, RIF/cobertura/áreas — su filtro de verificación). El resto
    (aforo, baños, camas…) se completa después con /ficha, que pregunta todo."""
    return bool(q.get("core")) or bool(st.get("editar_rid"))
_FICHA_VAL = {"si": "Sí", "no": "No", "np": "No posee", "na": "No aplica"}  # token -> texto guardado

# MANOS SOLIDARIAS: categorías de ayuda gratuita que un aliado puede ofrecer.
ALIADO_CATS = [
    ("comida", "🍲 Comida"),
    ("herramientas", "🔦 Herramientas/linternas"),
    ("transporte", "🚐 Transporte"),
    ("agua", "💧 Agua"),
    ("carga", "🔌 Carga de teléfonos"),
    ("salud", "🩹 Salud / primeros auxilios"),
    ("hospedaje", "🛏️ Hospedaje temporal"),
    ("remoto", "💻 Remoto / En Línea"),
    ("estructural", "🏗️ Estructural / Inspección"),
    ("oficios", "🛠️ Oficios"),
    ("otro", "📦 Otra ayuda"),
]
ALIADO_CAT_LABEL = {k: v for k, v in ALIADO_CATS}

# telegram_id -> {"paso": ..., "nombre": ..., "tipo": ..., "punto": ..., "ficha": {...}, "ficha_i": int}
# El dict es solo el scratch DEL UPDATE en curso: _con_estado lo hidrata desde la
# DB (efimero.tg_estados) al entrar y lo persiste al salir → las conversaciones
# sobreviven a los restarts/deploys (antes cada deploy botaba al que iba a mitad
# de la ficha). El main-loop procesa updates en serie → sin carreras.
estados = {}


def _con_estado(chat_id, fn, arg):
    """Envuelve un handler: hidrata estados[chat_id], corre fn(arg), persiste."""
    if chat_id is None:
        fn(arg)
        return
    st_db = efimero.estado_tg_get(str(chat_id))
    if st_db is not None:
        estados[chat_id] = st_db
    else:
        estados.pop(chat_id, None)
    try:
        fn(arg)
    finally:
        st = estados.pop(chat_id, None)  # scratch fuera de memoria; la verdad, en DB
        if st is not None:
            efimero.estado_tg_set(str(chat_id), st)
        else:
            efimero.estado_tg_del(str(chat_id))

# Seguridad: límites de tamaño y rate-limit por persona (anti-spam / anti-costo de IA)
MAX_IMG = 12 * 1024 * 1024
MAX_AUDIO = 20 * 1024 * 1024
MAX_DOC = 20 * 1024 * 1024   # PDF / Excel / Word
MAX_TEXTO = 6000
_rate = {}  # telegram_id -> [timestamps de capturas]
# Modo de la próxima captura (según el último comando): 'oferta' (acopio /inventario),
# 'stock' (centro de salud /inventario, privado), o 'demanda' (/necesito). Caduca a
# los 15 min. Vive en efimero (kv) → un deploy entre el comando y la foto ya no
# hace clasificar mal la captura.


def _clase_captura(chat_id):
    return efimero.kv_get("modo_tg", str(chat_id), ttl=900)


def _rate_ok(chat_id, max_n=25, ventana=3600, gap=3):
    ahora = time.time()
    h = [t for t in _rate.get(chat_id, []) if ahora - t < ventana]
    if (h and ahora - h[-1] < gap) or len(h) >= max_n:
        _rate[chat_id] = h
        return False
    h.append(ahora)
    _rate[chat_id] = h
    return True


def _tg(metodo, **params):
    payload = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
               for k, v in params.items() if v is not None}
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(f"{API}/{metodo}", data=data, timeout=70) as r:
            return json.load(r)
    except Exception as e:
        # answerCallbackQuery es best-effort: la query de Telegram expira en pocos
        # segundos; un 400 "query is too old" no afecta la operacion realizada.
        if metodo != "answerCallbackQuery":
            print("tg error:", metodo, e)
        return {}


def _kb(filas):
    return {"inline_keyboard": filas}


def _notif_botones(prefijo, cabecera, forzar_manual=False):
    """Botones + cabecera del aviso al grupo de moderadores. Con AUTO_APROBAR, el
    registro ya entró solo → solo se ofrece bajarlo de la web ('❌ Quitar'); sin él,
    se pide aprobar/rechazar. `prefijo` es el callback base (ej. 'apr|7' o 'apra|3').
    `forzar_manual`: el registro quedó pendiente aunque AUTO_APROBAR esté activo
    (fundaciones: verificación de RIF caso por caso) → botones Aprobar/Rechazar."""
    if AUTO_APROBAR and not forzar_manual:
        kb = _kb([[{"text": "❌ Quitar de la web", "callback_data": f"{prefijo}|no"}]])
        return kb, cabecera + " · ✅ <i>aprobado automáticamente</i>"
    kb = _kb([[{"text": "✅ Aprobar", "callback_data": f"{prefijo}|ok"},
               {"text": "❌ Rechazar", "callback_data": f"{prefijo}|no"}]])
    return kb, cabecera


def _enviar_documento(chat_id, filename, data, caption=None):
    """Sube un archivo al chat (multipart/form-data). Para mandar el Excel del inventario."""
    boundary = "----ang" + str(int(time.time() * 1000))
    pre = []

    def campo(nombre, valor):
        pre.append(("--" + boundary + "\r\n"
                    'Content-Disposition: form-data; name="' + nombre + '"\r\n\r\n'
                    + str(valor) + "\r\n").encode())

    campo("chat_id", chat_id)
    if caption:
        campo("caption", caption)
    pre.append(("--" + boundary + "\r\n"
                'Content-Disposition: form-data; name="document"; filename="' + filename + '"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode())
    body = b"".join(pre) + data + ("\r\n--" + boundary + "--\r\n").encode()
    req = urllib.request.Request(API + "/sendDocument", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except Exception as e:
        print("sendDocument error:", e)
        return {}


def _enviar_foto(chat_id, data, caption=None):
    """Sube una foto (multipart), p. ej. el QR de una entrega. No se manda por URL:
    detrás de Cloudflare el fetcher de Telegram puede ser bloqueado y la foto
    nunca llegar (mismo patrón que _enviar_documento)."""
    boundary = "----angfoto" + str(int(time.time() * 1000))
    pre = []

    def campo(nombre, valor):
        pre.append(("--" + boundary + "\r\n"
                    'Content-Disposition: form-data; name="' + nombre + '"\r\n\r\n'
                    + str(valor) + "\r\n").encode())

    campo("chat_id", chat_id)
    if caption:
        campo("caption", caption)
    pre.append(("--" + boundary + "\r\n"
                'Content-Disposition: form-data; name="photo"; filename="qr.png"\r\n'
                "Content-Type: image/png\r\n\r\n").encode())
    body = b"".join(pre) + data + ("\r\n--" + boundary + "--\r\n").encode()
    req = urllib.request.Request(API + "/sendPhoto", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except Exception as e:
        print("sendPhoto error:", e)
        return {}


def _pedir_cedula(chat_id):
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🪪 ¿Cuál es tu <b>número de cédula</b>? (ej. V-12345678)\n\n"
              "<i>Es solo para verificar tu registro; no se publica.</i>"))


def _pedir_cedula_final(chat_id, st):
    """Cierra el alta pidiendo la cédula. Es OMITIBLE (bajar fricción sin perder el
    filtro: el grupo de moderadores ve quién no la dio). Para FUNDACIONES es
    obligatoria — su verificación es caso por caso."""
    st["paso"] = "cedula"
    kb = None
    if st.get("tipo") != "fundacion":
        kb = _kb([[{"text": "⏭️ Prefiero darla luego", "callback_data": "ced|skip"}]])
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🪪 Ya casi terminamos: ¿cuál es tu <b>número de cédula</b>? (ej. V-12345678)\n\n"
              "<i>No se publica; es solo para verificar tu registro.</i>"),
        reply_markup=kb)


def _normalizar_cedula(texto):
    """Valida/normaliza una cédula venezolana de persona (V o E, 6 a 8 dígitos). Acepta
    'V-12.345.678', '12345678', 'E12345678', etc. Devuelve (True, 'V-12345678') o
    (False, motivo) con motivo en {'larga','corta','formato'}."""
    s = (texto or "").strip().upper().replace(".", "").replace("-", "").replace(" ", "")
    prefijo = ""
    if s[:1] in ("V", "E"):
        prefijo, s = s[0], s[1:]
    if not s.isdigit():
        return (False, "formato")
    if len(s) > 8:
        return (False, "larga")
    if len(s) < 6:
        return (False, "corta")
    return (True, f"{prefijo or 'V'}-{s}")


def _aviso_cedula_invalida(chat_id, motivo):
    if motivo == "larga":
        txt = "Esa cédula tiene <b>demasiados dígitos</b> (el máximo son 8). Revísala y vuelve a escribirla."
    elif motivo == "corta":
        txt = "Esa cédula tiene <b>muy pocos dígitos</b>. Revísala (ej. <b>V-12345678</b>)."
    else:
        txt = "Esa cédula no parece válida. Escríbela así: <b>V-12345678</b> (solo el número también vale)."
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML", text=txt)


def _validar_cedula(chat_id, st, texto):
    """Valida formato (V/E, 6-8 dígitos) y reuso bajo otro nombre. Si algo falla avisa y
    devuelve None; si está OK devuelve la cédula normalizada."""
    ok, res = _normalizar_cedula(texto)
    if not ok:
        _aviso_cedula_invalida(chat_id, res)
        return None
    otro = db.cedula_otro_nombre(res, st.get("nombre", ""), excluir_tgid=chat_id)
    if otro:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=(f"⚠️ La cédula <b>{html.escape(res)}</b> ya está registrada a nombre de "
                  f"<b>{html.escape(otro)}</b>. No puedes afiliarte con la cédula de otra persona.\n\n"
                  "Si crees que es un error, verifica el número o escribe a un coordinador."))
        return None
    return res


def _pedir_tipo(chat_id):
    kb = _kb([
        [{"text": "📦 Acopio", "callback_data": "tipo|acopio"},
         {"text": "🏠 Refugio", "callback_data": "tipo|refugio"}],
        [{"text": "🏥 Hospital", "callback_data": "tipo|hospital"},
         {"text": "🩺 CDI", "callback_data": "tipo|cdi"}],
        [{"text": "🤰 Maternidad", "callback_data": "tipo|maternidad"},
         {"text": "➕ Clínica", "callback_data": "tipo|clinica"}],
        [{"text": "🏛 Fundación", "callback_data": "tipo|fundacion"},
         {"text": "📍 Otro", "callback_data": "tipo|otro"}],
    ])
    _tg("sendMessage", chat_id=chat_id, text="¿Qué tipo de punto representas?", reply_markup=kb)


GPS_SKIP = "🕓 Lo hago luego (no estoy en el sitio)"


def _pedir_gps(chat_id):
    """Pide la ubicación con el botón nativo de Telegram (pin GPS). Aclara que solo debe
    tocarlo si está físicamente en el punto: Telegram envía la ubicación ACTUAL del teléfono."""
    kb = {"keyboard": [[{"text": "📍 Estoy en el punto: compartir ubicación", "request_location": True}],
                       [{"text": GPS_SKIP}]],
          "resize_keyboard": True, "one_time_keyboard": True}
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("📍 <b>Ubicación de tu punto</b>\n\n"
              "Toca <b>«Estoy en el punto»</b> <u>solo si en este momento te encuentras físicamente "
              "allí</u> — Telegram comparte tu ubicación <b>actual</b>, así que ese es el pin que "
              "quedará registrado.\n\n"
              "Si <b>no estás en el sitio ahora</b>, toca <b>«Lo hago luego»</b>; cuando llegues al "
              "punto escribe /ubicacion y deja el pin correcto. Así conectamos por <b>cercanía</b>. 👍"),
        reply_markup=kb)


def _enviar_pregunta_ficha(chat_id, q):
    """Envía UNA pregunta de ficha (con sus botones según el modo)."""
    c = q["campo"]
    botones = None
    if q["modo"] in ("sino", "sino_na"):
        botones = [[{"text": "✅ Sí", "callback_data": f"ficha|{c}|si"},
                    {"text": "🚫 No", "callback_data": f"ficha|{c}|no"}]]
        if q["modo"] == "sino_na":
            botones.append([{"text": "➖ No aplica", "callback_data": f"ficha|{c}|na"}])
    elif q["modo"] == "no_posee":
        botones = [[{"text": "🚫 No posee", "callback_data": f"ficha|{c}|np"}]]
    elif q["modo"] == "no_aplica":
        botones = [[{"text": "➖ No aplica", "callback_data": f"ficha|{c}|na"}]]
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML", text=q["preg"],
        reply_markup=_kb(botones) if botones else None)


def _preguntar_ficha(chat_id, st):
    """Hace la pregunta de ficha que toca (según st['ficha_i']), saltando las que
    no aplican al tipo de punto (solo/salvo) y, en el alta, las que no son core.
    Cuando ya no quedan: en el alta pide la cédula (omitible) y luego confirma."""
    i = st.get("ficha_i", 0)
    while i < len(FICHA) and not (_ficha_aplica(FICHA[i], st.get("tipo"))
                                  and _ficha_en_alta(FICHA[i], st)):
        i += 1
    st["ficha_i"] = i
    if i >= len(FICHA):
        if not st.get("editar_rid") and not st.get("cedula") and not st.get("cedula_omitida"):
            _pedir_cedula_final(chat_id, st)
        else:
            _ir_a_confirmar(chat_id, st)
        return
    _enviar_pregunta_ficha(chat_id, FICHA[i])


def _registrar_ficha(chat_id, st, valor):
    """Guarda la respuesta y avanza. En modo corrección guarda solo ese campo y
    vuelve al resumen, en vez de seguir con las siguientes preguntas."""
    valor = (valor or "").strip()[:120]
    if st.get("corrigiendo"):
        st.setdefault("ficha", {})[st["corrigiendo"]] = valor
        st.pop("corrigiendo", None)
        _ir_a_confirmar(chat_id, st)
        return
    q = FICHA[st["ficha_i"]]
    st.setdefault("ficha", {})[q["campo"]] = valor
    # "Otro punto con el mismo nombre": al escribir la ubicación, la IA verifica si en
    # realidad es el mismo lugar que el punto homónimo ya registrado.
    if q["campo"] == "ubicacion" and st.get("ref_punto") and valor:
        misma, zona = captura.comparar_ubicacion(valor, st.get("ref_ubicacion"))
        st["zona"] = zona   # etiqueta de sector para desambiguar el nombre al crear
        if misma:
            _avisar_ubicacion_duplicada(chat_id, st)
            return
    st["ficha_i"] += 1
    _preguntar_ficha(chat_id, st)


# --- Corrección puntual (elegir un solo dato a arreglar desde el resumen) ---
CORR_ITEMS = ([("nombre", "👤 Nombre"), ("cedula", "🪪 Cédula"),
               ("tipo", "🏷️ Tipo de punto"), ("punto", "🏛️ Nombre del punto")]
              + [(q["campo"], FICHA_LABEL[q["campo"]]) for q in FICHA])


def _menu_correccion(chat_id, st):
    """Muestra la lista de datos para que el rep elija cuál corregir."""
    st["paso"] = "menu_corr"
    st.pop("corrigiendo", None)
    # No ofrecer corregir campos que la ficha no preguntó para este tipo de punto
    # (ni los que el alta corta ya no pregunta — esos se completan con /ficha).
    fichables = {q["campo"] for q in FICHA
                 if _ficha_aplica(q, st.get("tipo")) and _ficha_en_alta(q, st)}
    filas, fila = [], []
    for key, lbl in CORR_ITEMS:
        if key not in ("nombre", "cedula", "tipo", "punto") and key not in fichables:
            continue
        fila.append({"text": lbl, "callback_data": f"corr|{key}"})
        if len(fila) == 2:
            filas.append(fila); fila = []
    if fila:
        filas.append(fila)
    filas.append([{"text": "✅ Está todo bien, confirmar", "callback_data": "corr|__ok__"}])
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text="¿Qué dato quieres <b>corregir</b>? Toca uno y te lo vuelvo a preguntar (lo demás se conserva):",
        reply_markup=_kb(filas))


def _preguntar_un_campo(chat_id, st, key):
    """Re-pregunta un solo dato (para corregirlo)."""
    st["corrigiendo"] = key
    if key == "nombre":
        st["paso"] = "corr_nombre"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML", text="¿Cuál es tu <b>nombre y apellido</b>?")
    elif key == "cedula":
        st["paso"] = "corr_cedula"
        _pedir_cedula(chat_id)
    elif key == "punto":
        st["paso"] = "corr_punto"
        _tg("sendMessage", chat_id=chat_id,
            text=f"¿Cómo se llama el/la {TIPOS.get(st.get('tipo'), 'punto').lower()}?")
    elif key == "tipo":
        st["paso"] = "tipo"
        _pedir_tipo(chat_id)
    else:
        st["paso"] = "ficha"
        q = next((x for x in FICHA if x["campo"] == key), None)
        if q:
            _enviar_pregunta_ficha(chat_id, q)


def _ir_a_confirmar(chat_id, st):
    """Muestra el resumen completo (datos de la persona + ficha del punto)."""
    st["paso"] = "confirmar"
    f = st.get("ficha", {})
    lineas = ["Revisa la <b>ficha del punto</b>:\n",
              f"👤 Te registras: <b>{html.escape(st['nombre'])}</b>"]
    if st.get("cedula"):
        lineas.append(f"🪪 Cédula: <b>{html.escape(st['cedula'])}</b>")
    lineas.append(f"{TIPOS.get(st['tipo'], 'Punto')}: <b>{html.escape(st['punto'])}</b>")
    for campo, etiqueta in FICHA_LABEL.items():
        val = f.get(campo)
        if val:
            lineas.append(f"• {etiqueta}: <b>{html.escape(val)}</b>")
    kb = _kb([[{"text": "✅ Confirmar", "callback_data": "conf|ok"},
               {"text": "✏️ Corregir", "callback_data": "conf|edit"}]])
    _tg("sendMessage", chat_id=chat_id, text="\n".join(lineas), parse_mode="HTML", reply_markup=kb)


def _iniciar_ficha(chat_id, st, nombre_punto):
    """Arranca la ficha obligatoria del punto (registro de un punto NUEVO)."""
    st["punto"] = nombre_punto
    st["ficha"] = {}
    st["ficha_i"] = 0
    st["paso"] = "ficha"
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("📝 Ahora los <b>datos esenciales del punto</b> (dirección, horario y "
              "contacto) para que la ayuda <b>llegue a donde es</b>.\n\n"
              "El resto de la ficha (aforo, baños, camas…) lo completas luego "
              "con /ficha, cuando puedas."))
    _preguntar_ficha(chat_id, st)


def _ofrecer_colaborar(chat_id, st, dueno, nombre_tecleado):
    """El punto que tecleó YA está registrado: ofrece (1) sumarse como colaborador,
    (2) registrar OTRO punto con el mismo nombre (otra ubicación), o (3) escribir otro nombre."""
    st["paso"] = "colaborar"
    st["dueno_punto"] = dueno["punto"]
    st["dueno_tipo"] = dueno.get("tipo_punto")
    st["dueno_ubicacion"] = dueno.get("ubicacion")
    st["dueno_punto_id"] = dueno.get("punto_id") or dueno.get("id")
    st["nombre_tecleado"] = nombre_tecleado
    tipo_txt = TIPOS.get(dueno.get("tipo_punto"), "punto").lower()
    ubic = (dueno.get("ubicacion") or "").strip()
    loc = f"\n📌 Ubicación registrada: <i>{html.escape(ubic)}</i>" if ubic else ""
    kb = _kb([
        [{"text": "🤝 Es el mismo punto (soy colaborador)", "callback_data": "colab|si"}],
        [{"text": "📍 Es otro punto con el mismo nombre", "callback_data": "colab|otro"}],
        [{"text": "➕ No, escribir otro nombre", "callback_data": "colab|no"}],
    ])
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"📍 <b>{html.escape(dueno['punto'])}</b> ya está registrado como {tipo_txt}.{loc}\n\n"
              "¿Qué corresponde a tu caso?\n\n"
              "• <b>Es el mismo punto</b> → te sumas como colaborador (no repites la ficha).\n"
              "• <b>Es otro con el mismo nombre</b> → lo registras aparte (verifico la ubicación).\n"
              "• <b>Otro nombre</b> → te equivocaste y escribes otro."),
        reply_markup=kb)


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
        kb = _kb([[{"text": "✅ Lo conozco", "callback_data": f"colok|{rid}"},
                   {"text": "🚫 No lo conozco", "callback_data": f"colrm|{rid}"}]])
        _tg("sendMessage", chat_id=dueno["telegram_id"], parse_mode="HTML",
            text=(f"🤝 <b>Se sumó un colaborador a tu punto</b> {html.escape(rep.get('punto') or '')}:\n"
                  f"👤 <b>{html.escape(rep['nombre'])}</b> (vía {via}{tel})\n"
                  "Ya puede cargar inventario y necesidades del punto.\n\n"
                  "¿Lo reconoces? Si no sabes quién es, toca «🚫 No lo conozco» y lo removemos."),
            reply_markup=kb)
    elif dueno.get("wa_phone") or dueno.get("telefono"):
        wa_api.enviar_botones(
            dueno.get("wa_phone") or dueno.get("telefono"),
            f"🤝 *Se sumó un colaborador a tu punto* {rep.get('punto') or ''}:\n"
            f"👤 *{rep['nombre']}* (vía {via}{tel})\n"
            "Ya puede cargar inventario y necesidades del punto.\n\n"
            "¿Lo reconoces? Si no sabes quién es, toca *No lo conozco* y lo removemos.",
            [(f"colok|{rid}", "✅ Lo conozco"), (f"colrm|{rid}", "🚫 No lo conozco")])


def _registrar_colaborador(chat_id, st, punto, tipo, punto_id):
    """Crea un colaborador del punto dado, avisa al rep y notifica al grupo (si hay)."""
    rid = db.crear_colaborador(chat_id, st["nombre"],
                               {"punto": punto, "tipo_punto": tipo, "punto_id": punto_id},
                               cedula=st.get("cedula"))
    if AUTO_APROBAR:
        db.set_estado_representante(rid, "aprobado")
        _set_commands_rol(chat_id, "centro", con_ficha=False)  # colaborador: sin /ficha
    estados.pop(chat_id, None)
    if AUTO_APROBAR:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=(f"✅ ¡Listo! Quedaste como <b>colaborador de {html.escape(punto or 'el punto')}</b> "
                  "y ya estás activo. Ya puedes mandarme listas, fotos o audios con los requerimientos e "
                  "inventario del punto."))
    else:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=(f"🙌 ¡Gracias! Quedaste como <b>colaborador de {html.escape(punto or 'el punto')}</b>, "
                  "pendiente de aprobación. Te avisamos apenas un coordinador lo confirme."))
    rep = db.representante(rid)
    if GRUPO_MODERADORES:
        kb, cab = _notif_botones(f"apr|{rid}", "🤝 <b>Nuevo colaborador</b>")
        ci = f"\n🪪 CI: {html.escape(rep['cedula'])}" if rep.get("cedula") else ""
        txt = (f"{cab}\n👤 {html.escape(rep['nombre'])}{ci}\n"
               f"Se suma a <b>{html.escape(rep['punto'] or '')}</b> "
               f"({TIPOS.get(rep['tipo_punto'], 'punto')})")
        _tg("sendMessage", chat_id=GRUPO_MODERADORES, text=txt, parse_mode="HTML", reply_markup=kb)
    _avisar_dueno_colab(rid, "Telegram")


def _avisar_ubicacion_duplicada(chat_id, st):
    """La IA detectó que la ubicación coincide con el punto homónimo ya registrado."""
    st["paso"] = "ubic_dup"
    kb = _kb([
        [{"text": "🤝 Es el mismo, soy colaborador", "callback_data": "dup|colab"}],
        [{"text": "✏️ No, corregir la ubicación", "callback_data": "dup|corr"}],
    ])
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"⚠️ <b>{html.escape(st.get('ref_punto') or 'Ese punto')}</b> ya fue registrado con "
              "<b>esa misma ubicación</b> — parece ser el mismo lugar.\n\n¿Qué hacemos?"),
        reply_markup=kb)


# --- PEDIR AYUDA: cualquier persona (sin registro) pide ayuda a la red. Reusa el
# circuito de los pedidos web: contacto privado en solicitudes_web + AUTO-PUBLICACIÓN
# (el pedido sale anonimizado a la web al instante, _barrido_triangulador abanica a
# acopios/MS/voluntarios y el grupo de moderadores recibe reporte + botón Remover).
# Excepción: CATS_PEDIR_MODERADAS (desaparecidos) siguen con revisión previa pweb|. ---
# Categorías canónicas y flags compartidos con backend.py y bot_wa.py:
from config import CATS_PEDIR, AUTO_PUBLICAR_PEDIDOS, CATS_PEDIR_MODERADAS
# Freno anti-spam de /pedir: vive en efimero.rl_cooldowns (sobrevive restarts).


def _iniciar_pedido(chat_id, forzar_publico=False):
    # Si quien pide REPRESENTA un punto (fundación, refugio, acopio…), su pedido
    # "para mi punto" va por la vía rica de /necesito (metas, compromisos, puente
    # demanda→oferta), no por el formulario público. Se le pregunta para quién es.
    if not forzar_publico:
        rep = db.representante_por_tgid(chat_id)
        if rep and rep.get("estado") == "aprobado":
            tipo_lbl = TIPOS.get(rep.get("tipo_punto"), "punto")
            estados.pop(chat_id, None)
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=(f"Tú representas <b>{html.escape(rep.get('punto') or 'un punto')}</b> "
                      f"({html.escape(tipo_lbl)}). ¿Para quién es este pedido?\n\n"
                      f"• <b>Para tu {html.escape(tipo_lbl.lower())}</b> → declaras sus insumos "
                      "con seguimiento (metas, «en camino») y la red con inventario recibe el aviso.\n"
                      "• <b>Para otra persona</b> → sale como pedido de ayuda al público."),
                reply_markup=_kb([[{"text": f"📦 Para mi {tipo_lbl.lower()}"[:38], "callback_data": "pedq|punto"}],
                                  [{"text": "🧍 Para otra persona", "callback_data": "pedq|otro"}]]))
            return
    if time.time() - efimero.cooldown_ts("pedir_tg", str(chat_id)) < 600:
        _tg("sendMessage", chat_id=chat_id,
            text="⏳ Ya recibimos tu pedido hace poco y está en camino a la red. "
                 "Si necesitas agregar algo, espera unos minutos e inténtalo de nuevo 🙏")
        return
    estados[chat_id] = {"paso": "pd_cat", "pedido": {}}
    filas, fila = [], []
    for k, lbl in CATS_PEDIR.items():
        fila.append({"text": lbl, "callback_data": f"pcat|{k}"})
        if len(fila) == 2:
            filas.append(fila); fila = []
    if fila:
        filas.append(fila)
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🆘 <b>Pedir ayuda</b>\n\nVamos a avisarle a la red (acopios, Manos "
              "Solidarias y voluntarios de tu zona). Tu pedido sale <b>anonimizado</b> "
              "en la web; tu contacto solo se comparte en privado con quien puede "
              "ayudarte.\n\n¿Qué tipo de ayuda necesitas?"),
        reply_markup=_kb(filas))


def _referencias_desaparecido_tg(chat_id, lid, persona):
    """HILO best-effort: cruza a la persona buscada con los registros externos
    (fuentes.py: hospitales, rescatados). Si aparece, avisa al solicitante y deja
    nota en el grupo de moderadores. Nunca toca el pedido."""
    try:
        import fuentes
        refs = fuentes.referencias(persona)
        if not refs:
            return
        lineas = [f"🔎 <b>Mientras revisamos tu pedido</b>, buscamos a "
                  f"<b>{html.escape(persona)}</b> en los registros comunitarios de "
                  "hospitales y rescatados, y encontramos esto:"]
        lineas += fuentes.resumen_referencias(refs, html_out=True)
        lineas.append("\n⚠️ Es información cargada por voluntarios (no oficial): "
                      "<b>confirma llamando al centro</b>. Tu pedido sigue su curso normal. "
                      "Más resultados: https://redacopioyrefugio.com/buscar.html")
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="\n".join(lineas), disable_web_page_preview=True)
        if GRUPO_MODERADORES:
            lineas_m = ([f"🔎 <b>Referencias para el pedido #{lid}</b> "
                         f"(buscan a <b>{html.escape(persona)}</b>):"]
                        + fuentes.resumen_referencias(refs, html_out=True)
                        + ["<i>Información comunitaria, no oficial — confirmar llamando "
                           "al centro antes de actuar.</i>"])
            _tg("sendMessage", chat_id=GRUPO_MODERADORES, parse_mode="HTML",
                text="\n".join(lineas_m))
    except Exception as e:
        print("referencias desaparecido tg error:", e)


_PREGUNTA_PD = {   # pregunta pendiente por paso de Pedir ayuda (se repite si llega algo sin texto)
    "pd_persona": "🔎 ¿Cómo se llama la persona que buscas? (nombre y apellido; si no lo sabes, escribe «-»)",
    "pd_desc": "✍️ ¿Qué necesitas exactamente y para cuántas personas? (también puedes mandar una foto o flyer)",
    "pd_zona": "📍 ¿Dónde se necesita? (estado, ciudad y sector)",
    "pd_tel": "📞 ¿A qué número de teléfono / WhatsApp te contactan?",
    "pd_nombre": "👤 ¿Cuál es tu nombre? (si prefieres no decirlo, escribe «-»)",
}

_PREGUNTA_PD_DESAP = {  # variantes cuando lo que se pide es BUSCAR A UNA PERSONA
    "pd_desc": "✍️ ¿Qué pasó y dónde le viste por última vez? (también puedes mandar el flyer o una foto)",
    "pd_zona": "📍 ¿En qué zona se le vio por última vez? (estado, ciudad y sector)",
}


def _pd_foto(chat_id, st, msg):
    """Foto en el paso de descripción de Pedir ayuda: queda adjunta al pedido y se lee
    con la misma IA del flyer de la web/WhatsApp (captura.leer_flyer) para prellenar
    descripción y zona. Corre en hilo aparte (la lectura tarda varios segundos)."""
    if st.get("paso") != "pd_desc":
        return
    fotos = msg.get("photo") or []
    file_id = fotos[-1].get("file_id") if fotos else None
    if not file_id:
        return
    d = st["pedido"]
    d["foto_file_id"] = file_id
    cap = (msg.get("caption") or "").strip()
    if cap:
        d["descripcion"] = cap[:600]
    lec = None
    # Frenos anti-costo compartidos con la web y WhatsApp: 30 s por chat + máx 20
    # lecturas de IA cada 10 min en total. Fuera de tope, la foto igual queda adjunta.
    if efimero.cooldown_ok("flyer_tg", str(chat_id), 30) and efimero.tope_global_ok("flyer", 20, 600):
        _tg("sendMessage", chat_id=chat_id, text="🔎 Leyendo tu imagen…")
        try:
            data = _descargar(file_id)
            if data and len(data) <= MAX_IMG:
                lec = captura.leer_flyer(data)
        except Exception as e:  # noqa: BLE001
            print("pedido tg flyer error:", e)
    legible = lec is not None and getattr(lec, "legible", False)
    if legible:
        if not d.get("descripcion") and (lec.descripcion or "").strip():
            d["descripcion"] = lec.descripcion.strip()[:600]
        if not d.get("zona") and (lec.zona or "").strip():
            d["zona"] = lec.zona.strip()[:120]
        # Flyer de desaparecido: el nombre leído es el de la PERSONA BUSCADA
        # (mismo criterio que la web y WhatsApp).
        if (d.get("cat") == "desaparecido" and not d.get("persona")
                and (lec.nombre or "").strip()):
            d["persona"] = lec.nombre.strip()[:80]
    if st.get("paso") != "pd_desc":
        return  # el usuario siguió escribiendo mientras leíamos: la foto ya quedó adjunta
    if not d.get("descripcion"):
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="📸 Recibí tu foto y va adjunta al pedido. Ahora cuéntame en <b>texto</b> "
                 "qué necesitas exactamente y para cuántas personas 🙏")
        return
    intro = ("📸 ¡Leí tu foto y completé lo que pude!" if legible
             else "📸 Foto recibida, va adjunta a tu pedido.")
    if d.get("zona"):
        st["paso"] = "pd_tel"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=intro + " 📞 ¿A qué <b>número de teléfono / WhatsApp</b> te contactan? "
                         "(no se publica; solo lo ve quien puede ayudarte)")
    else:
        st["paso"] = "pd_zona"
        if d.get("cat") == "desaparecido":
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=intro + " 📍 ¿En qué <b>zona</b> se le vio por última vez? "
                             "(estado, ciudad y sector)")
        else:
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=intro + " 📍 ¿<b>Dónde</b> se necesita? (estado, ciudad y sector — "
                             "mientras más exacto, más rápido te ubica la red)")


def _finalizar_pedido(chat_id, st):
    d = st["pedido"]
    # AUTO-PUBLICACIÓN: sale a la web al instante y el barrido le avisa a la red; los
    # moderadores reciben reporte + botón Remover. Desaparecidos: revisión previa.
    # Publicar DESPUÉS de guardar el contacto: el barrido arma los avisos leyendo
    # solicitudes_web (si publicara antes, el aviso podría salir sin contacto).
    auto = AUTO_PUBLICAR_PEDIDOS and d["cat"] not in CATS_PEDIR_MODERADAS
    try:
        lid = db.crear_llamado(None, None, d["zona"],
                               "{}: {}".format(CATS_PEDIR[d["cat"]], d["descripcion"]), None)
        db.guardar_contacto_pedido(lid, d.get("nombre"), d["contacto"], d["cat"])
        if d.get("cat") == "desaparecido" and d.get("persona"):
            db.guardar_persona_desaparecida(lid, d["persona"])
            threading.Thread(target=_referencias_desaparecido_tg,
                             args=(chat_id, lid, d["persona"]), daemon=True).start()
        if auto:
            db.publicar_pedido(lid)
        else:
            db.set_estado_llamado(lid, "pendiente")
    except Exception as e:
        print("pedido tg error:", e)
        _tg("sendMessage", chat_id=chat_id,
            text="⚠️ No pude guardar tu pedido. Inténtalo de nuevo en un momento 🙏")
        return
    estados.pop(chat_id, None)
    efimero.cooldown_marcar("pedir_tg", str(chat_id))
    # Foto del pedido (si la hubo): a la carpeta PRIVADA pedidos_img/<lid>.jpg —
    # build_site.py la publica solo con el pedido en estado 'web' y el botón Remover
    # (pweb|no) la borra (mismo circuito que los pedidos de la web y de WhatsApp).
    fid = d.get("foto_file_id")
    con_foto = False
    if fid:
        try:
            data = _descargar(fid)
            if data:
                (BASE / "pedidos_img").mkdir(exist_ok=True)
                (BASE / "pedidos_img" / f"{lid}.jpg").write_bytes(data)
                con_foto = True
        except Exception as e:  # noqa: BLE001
            print("pedido tg foto error:", e)
    if GRUPO_MODERADORES:
        if auto:
            kb = _kb([[{"text": "🗑 Remover de la web", "callback_data": f"pweb|{lid}|no"}]])
            titulo = "🆘 <b>Pedido de ayuda (bot Telegram)</b> · <i>publicado · puedes removerlo</i>"
        else:
            kb = _kb([[{"text": "✅ Publicar y avisar a la red", "callback_data": f"pweb|{lid}|ok"},
                       {"text": "❌ Descartar", "callback_data": f"pweb|{lid}|no"}]])
            titulo = "🆘 <b>Pedido de ayuda (bot Telegram)</b> · <i>revisar antes de publicar</i>"
        lineas = [titulo,
                  f"🏷️ {html.escape(CATS_PEDIR[d['cat']])}"]
        if d.get("nombre"):
            lineas.append(f"👤 {html.escape(d['nombre'])}")
        if d.get("cat") == "desaparecido" and d.get("persona"):
            lineas.append(f"🔎 Busca a: <b>{html.escape(d['persona'])}</b>")
        lineas += [f"✍️ {html.escape(d['descripcion'])}",
                   f"📍 {html.escape(d['zona'])}", f"📞 {html.escape(d['contacto'])}"]
        if con_foto:
            # El aviso sale como FOTO con caption: el handler pweb| ya sabe sellar
            # captions con editMessageCaption (límite 1024, igual que _sellar).
            _tg("sendPhoto", chat_id=GRUPO_MODERADORES, photo=fid, parse_mode="HTML",
                caption="\n".join(lineas)[:1024], reply_markup=kb)
        else:
            _tg("sendMessage", chat_id=GRUPO_MODERADORES, parse_mode="HTML",
                text="\n".join(lineas), reply_markup=kb)
    if auto:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=("✅ <b>¡Pedido publicado!</b> Ya está en la web y le estamos avisando "
                  "a la red de tu zona; si alguien puede ayudarte, te contactará al "
                  "número que dejaste. 🙏\n\n"
                  "Puedes ver los puntos de ayuda en https://redacopioyrefugio.com"))
    else:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=("📨 <b>¡Pedido recibido!</b> Está <b>en revisión</b> de nuestro equipo. "
                  "Apenas lo aprueben, se publica y le avisamos a la red de tu zona; si "
                  "alguien puede ayudarte, te contactará al número que dejaste. 🙏\n\n"
                  "Mientras tanto, puedes ver los puntos de ayuda en "
                  "https://redacopioyrefugio.com"))


def iniciar(chat_id):
    """Primer paso: el usuario elige CÓMO se registra (la bifurcación)."""
    estados[chat_id] = {"paso": "elegir_modo"}
    kb = _kb([
        [{"text": "🆘 Necesito ayuda", "callback_data": "modo|pedir"}],
        [{"text": "🏠 Represento un centro", "callback_data": "modo|centro"}],
        [{"text": "🤝 Manos Solidarias (ayuda gratis)", "callback_data": "modo|aliado"}],
        [{"text": "📣 Voluntariado", "callback_data": "modo|voluntario"}],
    ])
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🕊️ <b>Ángeles · Apoyo</b>\n\n¿Cómo quieres participar?\n\n"
              "🆘 <b>Necesito ayuda</b> — pide comida, agua, medicinas, refugio o rescate; "
              "le avisamos a la red de tu zona.\n\n"
              "🏠 <b>Represento un centro</b> — eres responsable de un centro de acopio, "
              "hospital, clínica o refugio (te haremos la ficha del punto).\n\n"
              "🤝 <b>Manos Solidarias</b> — ofreces ayuda <b>gratis</b> a la comunidad "
              "(comida, linternas, transporte…) sin ser un centro fijo.\n\n"
              "📣 <b>Voluntariado</b> — estás en terreno y quieres <b>levantar llamados de "
              "apoyo</b> de sectores en emergencia (publicas foto + sector + mensaje)."),
        reply_markup=kb)


def _iniciar_centro(chat_id):
    """Camino de representante de un centro (flujo de siempre): empieza por el nombre."""
    estados[chat_id] = {"paso": "nombre"}
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🏠 Te vas a afiliar como <b>representante de tu punto</b> "
              "(centro de acopio, hospital, clínica o refugio) para ayudar a coordinar mejor "
              "la ayuda.\n\nSon <b>unas pocas preguntas (~3 minutos)</b> y tu punto queda "
              "en la red.\n\nPrimero: ¿cuál es tu <b>nombre y apellido</b>?"))


# --- MANOS SOLIDARIAS: flujo corto de ayuda gratuita ---
def _kb_cats_aliado():
    filas, fila = [], []
    for k, lbl in ALIADO_CATS:
        fila.append({"text": lbl, "callback_data": f"acat|{k}"})
        if len(fila) == 2:
            filas.append(fila); fila = []
    if fila:
        filas.append(fila)
    return _kb(filas)


def _iniciar_aliado(chat_id):
    """Camino Manos Solidarias: ayuda gratuita. Flujo corto, empieza por la categoría."""
    prev = db.aliado_por_tgid(chat_id)
    if prev and prev["estado"] == "aprobado":
        estados.pop(chat_id, None)
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="✅ Ya estás registrado como <b>Manos Solidarias</b>. ¡Gracias por tu ayuda! 🤝")
        return
    if prev and prev["estado"] == "pendiente":
        estados.pop(chat_id, None)
        _tg("sendMessage", chat_id=chat_id,
            text="⏳ Tu registro como Manos Solidarias ya está enviado y pendiente de aprobación. Te avisamos apenas lo confirmen.")
        return
    estados[chat_id] = {"paso": "al_cat", "aliado": {}}
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🤝 <b>Manos Solidarias</b> — gracias por querer ayudar 🙏\n\n"
              "Son unas preguntas rápidas. Primero: ¿<b>qué tipo de ayuda</b> ofreces?"),
        reply_markup=_kb_cats_aliado())


def _pedir_gps_aliado(chat_id):
    """Pide ubicación (opcional) para el pin del aliado en el mapa."""
    kb = {"keyboard": [[{"text": "📍 Estoy aquí: compartir ubicación", "request_location": True}],
                       [{"text": GPS_SKIP}]],
          "resize_keyboard": True, "one_time_keyboard": True}
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("📍 <b>Ubicación (opcional)</b>\n\nSi quieres aparecer en el <b>mapa</b>, comparte tu "
              "ubicación con <b>«Estoy aquí»</b> (solo si estás en el lugar desde donde ayudas).\n\n"
              "Si prefieres no compartirla, toca <b>«Lo hago luego»</b> — igual apareces en la lista."),
        reply_markup=kb)


def _ir_a_confirmar_aliado(chat_id, st):
    """Resumen del registro de Manos Solidarias antes de enviar."""
    st["paso"] = "al_conf"
    a = st["aliado"]
    lineas = ["Revisa tu registro de <b>Manos Solidarias</b>:\n",
              f"🏷️ Ayuda: <b>{html.escape(ALIADO_CAT_LABEL.get(a.get('categoria',''), a.get('categoria','')))}</b>",
              f"👤 Nombre: <b>{html.escape(a.get('nombre',''))}</b>",
              f"✍️ Ofreces: <b>{html.escape(a.get('descripcion',''))}</b>",
              f"📍 Zona: <b>{html.escape(a.get('zona',''))}</b>",
              f"📞 Contacto: <b>{html.escape(a.get('contacto',''))}</b>"]
    if a.get("horario"):
        lineas.append(f"🕐 Disponibilidad: <b>{html.escape(a['horario'])}</b>")
    lineas.append("📌 Ubicación en el mapa: <b>" + ("sí" if a.get("lat") is not None else "pendiente") + "</b>")
    kb = _kb([[{"text": "✅ Confirmar", "callback_data": "aconf|ok"},
               {"text": "✏️ Empezar de nuevo", "callback_data": "aconf|edit"}]])
    _tg("sendMessage", chat_id=chat_id, text="\n".join(lineas), parse_mode="HTML", reply_markup=kb)


def _crear_aliado_final(chat_id, st):
    """Crea el aliado y lo PUBLICA de una (Manos Solidarias es auto-publicada). Avisa al
    grupo con la única opción de QUITAR, por si se denuncia como indebida o estafa."""
    a = st["aliado"]
    aid = db.crear_aliado(chat_id, a)
    db.set_estado_aliado(aid, "aprobado")
    _set_commands_rol(chat_id, "aliado")
    estados.pop(chat_id, None)
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text="✅ ¡Listo! Quedaste registrado en <b>Manos Solidarias</b> y ya apareces en la web. ¡Gracias por ayudar! 🤝",
        reply_markup={"remove_keyboard": True})
    if GRUPO_MODERADORES:
        kb = _kb([[{"text": "❌ Quitar de la web", "callback_data": f"apra|{aid}|no"}]])
        lineas = ["🤝 <b>Nueva Manos Solidarias</b> · ✅ <i>publicada</i> — quítala si es indebida o estafa",
                  f"👤 {html.escape(a.get('nombre',''))}",
                  f"🏷️ {html.escape(ALIADO_CAT_LABEL.get(a.get('categoria',''), a.get('categoria','')))}",
                  f"✍️ {html.escape(a.get('descripcion',''))}",
                  f"📍 {html.escape(a.get('zona',''))}",
                  f"📞 {html.escape(a.get('contacto',''))}"]
        _tg("sendMessage", chat_id=GRUPO_MODERADORES, text="\n".join(lineas),
            parse_mode="HTML", reply_markup=kb)


# --- VOLUNTARIADO: registro corto + levantar llamados de apoyo ---
def _iniciar_voluntario(chat_id):
    """Camino Voluntariado: flujo corto. Empieza por el nombre."""
    prev = db.voluntario_por_tgid(chat_id)
    if prev and prev["estado"] == "aprobado":
        estados.pop(chat_id, None)
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="✅ Ya estás registrado como <b>Voluntariado</b>. Usa /llamado para publicar un llamado de apoyo. 📣",
            reply_markup=_menu_voluntario())
        return
    if prev and prev["estado"] == "pendiente":
        estados.pop(chat_id, None)
        _tg("sendMessage", chat_id=chat_id,
            text="⏳ Tu registro de Voluntariado ya está enviado y pendiente de aprobación. Te avisamos apenas lo confirmen.")
        return
    estados[chat_id] = {"paso": "vo_nombre", "vol": {}}
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("📣 <b>Voluntariado</b> — gracias por sumar 🙏\n\n"
              "Con esto vas a poder <b>levantar llamados de apoyo</b> de sectores en "
              "emergencia (foto + sector + mensaje), que se publican en la web.\n\n"
              "Son tres preguntas rápidas. Primero: ¿cuál es tu <b>nombre y apellido</b>?"))


def _ir_a_confirmar_voluntario(chat_id, st):
    """Resumen del registro de Voluntariado antes de enviar."""
    st["paso"] = "vo_conf"
    v = st["vol"]
    lineas = ["Revisa tu registro de <b>Voluntariado</b>:\n",
              f"👤 Nombre: <b>{html.escape(v.get('nombre',''))}</b>",
              f"📍 Zona: <b>{html.escape(v.get('zona',''))}</b>",
              f"📞 Contacto (privado): <b>{html.escape(v.get('contacto',''))}</b>",
              "\n<i>Tu contacto NO se publica en la web. Solo se usa, en privado, para conectar "
              "un llamado que pida insumos con un centro de acopio que los tenga.</i>"]
    kb = _kb([[{"text": "✅ Confirmar", "callback_data": "vconf|ok"},
               {"text": "✏️ Empezar de nuevo", "callback_data": "vconf|edit"}]])
    _tg("sendMessage", chat_id=chat_id, text="\n".join(lineas), parse_mode="HTML", reply_markup=kb)


def _crear_voluntario_final(chat_id, st):
    """Crea el voluntario en la DB, avisa y notifica al grupo de moderadores."""
    v = st["vol"]
    vid = db.crear_voluntario(chat_id, v)
    if AUTO_APROBAR:
        db.set_estado_voluntario(vid, "aprobado")
        _set_commands_rol(chat_id, "voluntario")
    estados.pop(chat_id, None)
    if AUTO_APROBAR:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="✅ ¡Listo! Quedaste registrado como <b>Voluntariado</b>. Usa /llamado para publicar "
                 "tu primer llamado de apoyo. 📣",
            reply_markup=_menu_voluntario())
    else:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=("🙌 ¡Gracias! Tu registro de <b>Voluntariado</b> quedó <b>pendiente de aprobación</b>. "
                  "Te avisamos apenas un coordinador lo confirme."),
            reply_markup={"remove_keyboard": True})
    if GRUPO_MODERADORES:
        kb, cab = _notif_botones(f"aprv|{vid}", "📣 <b>Nuevo Voluntariado</b>")
        lineas = [cab,
                  f"👤 {html.escape(v.get('nombre',''))}",
                  f"📍 {html.escape(v.get('zona',''))}",
                  f"📞 {html.escape(v.get('contacto',''))}"]
        _tg("sendMessage", chat_id=GRUPO_MODERADORES, text="\n".join(lineas),
            parse_mode="HTML", reply_markup=kb)


def _menu_voluntario():
    """Menú de acciones para un voluntario aprobado."""
    return _kb([[{"text": "📣 Levantar llamado de apoyo", "callback_data": "cmd|lla"}]])


def _descargar(file_id):
    info = _tg("getFile", file_id=file_id)
    path = (info.get("result") or {}).get("file_path")
    if not path:
        return None
    url = f"https://api.telegram.org/file/bot{APOYO_BOT_TOKEN}/{path}"
    with urllib.request.urlopen(url, timeout=90) as r:
        return r.read()


def _ofrecer_guardar(chat_id, rep, lec, tipo_entrada, transcripcion=None):
    clase = _clase_captura(chat_id)  # 'oferta'/'stock'/'demanda' según el último comando
    if clase is None:
        # Captura libre (sin /inventario ni /necesito): es inventario — como en
        # /inventario, oferta para acopio/fundación y stock (privado) para el resto.
        # Sin la marca, la web deduce 'demanda' y publica el stock de un refugio
        # como si fuera una carencia.
        clase = "oferta" if rep.get("tipo_punto") in db.TIPOS_OFERTA else "stock"
    cid = db.crear_captura(rep["id"], tipo_entrada, lec.tipo, lec.punto, lec.resumen,
                           [i.model_dump() for i in lec.items], [p.model_dump() for p in lec.personas],
                           clase=clase)
    L = []
    if transcripcion:
        L.append(f"🎙️ <i>{html.escape(transcripcion)}</i>\n")
    L.append(f"📋 <b>{html.escape(lec.tipo)}</b>" + (f" · {html.escape(lec.punto)}" if lec.punto else ""))
    if lec.personas:
        L.append(f"\n👥 <b>{len(lec.personas)} personas</b>:")
        for p in lec.personas[:12]:
            extra = " · ".join(x for x in [("CI " + p.cedula) if p.cedula else "", p.edad or ""] if x)
            L.append("• " + html.escape(p.nombre) + (f" — {html.escape(extra)}" if extra else ""))
        if len(lec.personas) > 12:
            L.append(f"…y {len(lec.personas) - 12} más")
    if lec.items:
        L.append(f"\n📦 <b>{len(lec.items)} insumos</b>:")
        for i in lec.items[:12]:
            d = " ".join(x for x in [i.cantidad or "", f"({i.detalle})" if i.detalle else ""] if x)
            L.append("• " + html.escape(i.item) + (f" — {html.escape(d)}" if d else ""))
        if len(lec.items) > 12:
            L.append(f"…y {len(lec.items) - 12} más")
    # Inventario (no demanda): preguntar SIEMPRE si suma o reemplaza — la gente suele
    # mandar la foto de TODO su inventario (snapshot); sumar a ciegas lo infla/duplica.
    if lec.items and clase != "demanda":
        L.append("\n¿Esto se <b>suma</b> a tu inventario publicado o lo <b>reemplaza</b>?")
        kb = _kb([[{"text": "➕ Sumar", "callback_data": f"cap|{cid}|add"},
                   {"text": "🔄 Reemplazar", "callback_data": f"cap|{cid}|rep"}],
                  [{"text": "✏️ Editar", "callback_data": f"cap|{cid}|edit"},
                   {"text": "🗑️ Descartar", "callback_data": f"cap|{cid}|no"}]])
    else:
        L.append("\n¿Guardo esto?")
        kb = _kb([[{"text": "✅ Guardar", "callback_data": f"cap|{cid}|ok"}],
                  [{"text": "✏️ Editar", "callback_data": f"cap|{cid}|edit"},
                   {"text": "🗑️ Descartar", "callback_data": f"cap|{cid}|no"}]])
    _tg("sendMessage", chat_id=chat_id, text="\n".join(L), parse_mode="HTML", reply_markup=kb)


# --- Editor de captura: corregir un dato (persona/insumo) antes de guardar ---
# La captura ya está en la BD como 'pendiente' (invisible en la web hasta confirmar),
# así que se edita en sitio. Solo se corrigen personas e insumos: son los renglones
# que de verdad fluyen (la web agrupa por el punto registrado del rep, no por la captura).
PAGE_SIZE = 8
_CAMPO_P = {"n": ("nombre", "Nombre"), "c": ("cedula", "Cédula"),
            "e": ("edad", "Edad"), "d": ("detalle", "Detalle")}
_CAMPO_I = {"t": ("item", "Insumo"), "q": ("cantidad", "Cantidad"),
            "g": ("categoria", "Categoría"), "d": ("detalle", "Detalle")}


def _corta(s, n=28):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _buscar_fila(det, tipo, rid):
    fuente = det["personas"] if tipo == "p" else det["items"]
    return next((x for x in fuente if x["id"] == rid), None)


def _editor_lista(chat_id, cid, page=0, message_id=None):
    """Lista las personas e insumos de la captura, cada uno con su botón para corregir."""
    det = db.captura_detalle(cid)
    if not det:
        _tg("sendMessage", chat_id=chat_id, text="Esa captura ya no está disponible.")
        return
    filas_obj = ([("p", p) for p in det["personas"]] + [("i", it) for it in det["items"]])
    total = len(filas_obj)
    paginas = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, paginas - 1))
    trozo = filas_obj[page * PAGE_SIZE:page * PAGE_SIZE + PAGE_SIZE]
    botones = []
    for tipo, row in trozo:
        if tipo == "p":
            botones.append([{"text": "👤 " + _corta(row.get("nombre") or "(sin nombre)"),
                             "callback_data": f"ce|{cid}|p|{row['id']}"}])
        else:
            botones.append([{"text": "📦 " + _corta(row.get("item") or "(sin nombre)"),
                             "callback_data": f"ce|{cid}|i|{row['id']}"}])
    if paginas > 1:
        nav = []
        if page > 0:
            nav.append({"text": "◀", "callback_data": f"ce|{cid}|L|{page - 1}"})
        nav.append({"text": f"{page + 1}/{paginas}", "callback_data": f"ce|{cid}|L|{page}"})
        if page < paginas - 1:
            nav.append({"text": "▶", "callback_data": f"ce|{cid}|L|{page + 1}"})
        botones.append(nav)
    botones.append([{"text": "✅ Guardar", "callback_data": f"ce|{cid}|ok"},
                    {"text": "🗑️ Descartar", "callback_data": f"ce|{cid}|no"}])
    txt = ("✏️ <b>Corregir captura</b>\nToca el dato que quieres arreglar; lo demás se conserva."
           if total else "No hay renglones para corregir. Puedes guardarlo o descartarlo.")
    if message_id:
        _tg("editMessageText", chat_id=chat_id, message_id=message_id, text=txt,
            parse_mode="HTML", reply_markup=_kb(botones))
    else:
        _tg("sendMessage", chat_id=chat_id, text=txt, parse_mode="HTML", reply_markup=_kb(botones))


def _editor_fila(chat_id, cid, tipo, rid, message_id=None):
    """Menú de los campos de un renglón (persona o insumo) para corregir uno."""
    det = db.captura_detalle(cid)
    if not det:
        _tg("sendMessage", chat_id=chat_id, text="Esa captura ya no está disponible.")
        return
    row = _buscar_fila(det, tipo, rid)
    if not row:
        _editor_lista(chat_id, cid, message_id=message_id)
        return
    campos = _CAMPO_P if tipo == "p" else _CAMPO_I
    cod = "ep" if tipo == "p" else "ei"
    botones = [[{"text": f"{lbl}: {_corta(row.get(col) or '—', 24)}",
                 "callback_data": f"ce|{cid}|{cod}|{rid}|{f}"}]
               for f, (col, lbl) in campos.items()]
    botones.append([{"text": "🗑️ Borrar este renglón", "callback_data": f"ce|{cid}|d{tipo}|{rid}"}])
    botones.append([{"text": "← Volver a la lista", "callback_data": f"ce|{cid}|L|0"}])
    txt = ("👤 Persona" if tipo == "p" else "📦 Insumo") + " — toca el campo a corregir:"
    if message_id:
        _tg("editMessageText", chat_id=chat_id, message_id=message_id, text=txt, reply_markup=_kb(botones))
    else:
        _tg("sendMessage", chat_id=chat_id, text=txt, reply_markup=_kb(botones))


def _capturar(chat_id, rep, msg, texto):
    if not _rate_ok(chat_id):
        _tg("sendMessage", chat_id=chat_id, text="⏳ Vas muy rápido. Espera un momento y vuelve a mandarlo.")
        return
    foto = msg.get("photo")
    voz = msg.get("voice") or msg.get("audio")
    doc = msg.get("document")
    mime = str((doc or {}).get("mime_type", "")).lower()
    nombre = str((doc or {}).get("file_name", "")).lower()
    es_imagen = bool(foto) or bool(doc and mime.startswith("image/"))
    es_pdf = bool(doc and (mime == "application/pdf" or nombre.endswith(".pdf")))
    es_xlsx = bool(doc and ("spreadsheetml" in mime or nombre.endswith(".xlsx")))
    es_docx = bool(doc and ("wordprocessingml" in mime or nombre.endswith(".docx")))
    es_viejo = bool(doc and (nombre.endswith(".xls") or nombre.endswith(".doc")))
    try:
        if es_imagen:
            _tg("sendMessage", chat_id=chat_id, text="🔎 Leyendo tu imagen…")
            data = _descargar(foto[-1]["file_id"] if foto else doc["file_id"])
            if not data or len(data) > MAX_IMG:
                _tg("sendMessage", chat_id=chat_id, text="⚠️ Esa imagen es muy grande o no se pudo bajar. Mándala más liviana.")
                return
            _ofrecer_guardar(chat_id, rep, captura.leer_imagen(data), "foto")
        elif es_pdf:
            _tg("sendMessage", chat_id=chat_id, text="📄 Leyendo tu PDF…")
            data = _descargar(doc["file_id"])
            if not data or len(data) > MAX_DOC:
                _tg("sendMessage", chat_id=chat_id, text="⚠️ Ese PDF es muy grande (máx. 20 MB). Mándalo más liviano o por partes.")
                return
            _ofrecer_guardar(chat_id, rep, captura.leer_pdf(data), "pdf")
        elif es_xlsx:
            _tg("sendMessage", chat_id=chat_id, text="📊 Leyendo tu Excel…")
            data = _descargar(doc["file_id"])
            if not data or len(data) > MAX_DOC:
                _tg("sendMessage", chat_id=chat_id, text="⚠️ Ese archivo es muy grande (máx. 20 MB).")
                return
            _ofrecer_guardar(chat_id, rep, captura.leer_xlsx(data), "excel")
        elif es_docx:
            _tg("sendMessage", chat_id=chat_id, text="📝 Leyendo tu Word…")
            data = _descargar(doc["file_id"])
            if not data or len(data) > MAX_DOC:
                _tg("sendMessage", chat_id=chat_id, text="⚠️ Ese archivo es muy grande (máx. 20 MB).")
                return
            _ofrecer_guardar(chat_id, rep, captura.leer_docx(data), "word")
        elif es_viejo:
            _tg("sendMessage", chat_id=chat_id,
                text="⚠️ Ese formato antiguo (.doc/.xls) no lo puedo leer. Guárdalo como .docx o .xlsx, o mándame un PDF o una foto 🙂")
        elif voz:
            _tg("sendMessage", chat_id=chat_id, text="🎙️ Transcribiendo tu audio…")
            data = _descargar(voz["file_id"])
            if not data or len(data) > MAX_AUDIO:
                _tg("sendMessage", chat_id=chat_id, text="⚠️ Ese audio es muy grande. Mándalo más corto.")
                return
            txt = captura.transcribir(data)
            if not txt:
                _tg("sendMessage", chat_id=chat_id, text="No logré entender el audio. Intenta de nuevo, más claro.")
                return
            _ofrecer_guardar(chat_id, rep, captura.leer_texto(txt), "audio", transcripcion=txt)
        elif texto and not texto.startswith("/"):
            if len(texto) > MAX_TEXTO:
                _tg("sendMessage", chat_id=chat_id, text="⚠️ Ese texto es muy largo. Mándalo en partes.")
                return
            _tg("sendMessage", chat_id=chat_id, text="🔎 Procesando…")
            _ofrecer_guardar(chat_id, rep, captura.leer_texto(texto), "texto")
        else:
            _tg("sendMessage", chat_id=chat_id, text="Mándame tu lista como foto, nota de voz, texto, o archivo (PDF, Excel o Word) 🙂")
    except Exception as e:
        print("captura error:", e)
        _tg("sendMessage", chat_id=chat_id, text="⚠️ No pude leer eso. Inténtalo de nuevo (con buena luz o más claro).")


def _hint_rol(chat_id):
    """Pista honesta según el rol que la persona YA tiene, para no decirle
    'no estás afiliado' a quien sí lo está (en otro rol)."""
    rep = db.representante_por_tgid(chat_id)
    vol = db.voluntario_por_tgid(chat_id)
    al = db.aliado_por_tgid(chat_id)
    if rep and rep["estado"] == "aprobado":
        return "Tú estás registrado como <b>centro de acopio</b> 🏠"
    if vol and vol["estado"] == "aprobado":
        return "Tú estás registrado como <b>Voluntariado</b> 📣 — usa /llamado para levantar un llamado de apoyo."
    if al and al["estado"] == "aprobado":
        return "Tú estás registrado como <b>Manos Solidarias</b> 🤝 — usa /ubicacion para aparecer en el mapa."
    if (rep and rep["estado"] == "pendiente") or (vol and vol["estado"] == "pendiente") or (al and al["estado"] == "pendiente"):
        return "Tu registro está <b>pendiente de aprobación</b> ⏳. Te avisamos apenas lo confirmen."
    return "Si aún no te registras, escribe /start 🙂"


def _cmd_ficha(chat_id):
    rep = db.representante_por_tgid(chat_id)
    if not rep or rep["estado"] != "aprobado":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🔒 <b>/ficha</b> es solo para <b>centros de acopio</b> (el responsable del punto).\n\n" + _hint_rol(chat_id))
        return
    if (rep.get("rol") or "representante") == "colaborador":
        _tg("sendMessage", chat_id=chat_id,
            text="La ficha de este punto la gestiona su representante principal. Tú puedes "
                 "enviar requerimientos e inventario directamente (foto, audio, texto o archivo) 🙂")
        return
    estados[chat_id] = {"paso": "ficha", "ficha": {}, "ficha_i": 0,
                        "editar_rid": rep["id"], "nombre": rep["nombre"],
                        "tipo": rep["tipo_punto"], "punto": rep["punto"]}
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"✏️ Vamos a completar/actualizar la <b>ficha de {html.escape(rep['punto'] or 'tu punto')}</b>. "
              "Responde las preguntas; si algo no corresponde, toca <i>No aplica / No posee</i>."))
    _preguntar_ficha(chat_id, estados[chat_id])


def _cmd_ubicacion(chat_id):
    rep = db.representante_por_tgid(chat_id)
    if rep and rep["estado"] != "rechazado":
        _pedir_gps(chat_id)
        return
    al = db.aliado_por_tgid(chat_id)
    if al and al["estado"] == "aprobado":
        _pedir_gps_aliado(chat_id)
        return
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text="📍 <b>/ubicacion</b> es para <b>centros de acopio</b> y <b>Manos Solidarias</b>.\n\n" + _hint_rol(chat_id))


def _cmd_inventario(chat_id):
    rep = db.representante_por_tgid(chat_id)
    if not rep or rep["estado"] != "aprobado":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🔒 <b>/inventario</b> es solo para <b>centros de acopio</b>.\n\n" + _hint_rol(chat_id))
        return
    es_oferta = rep.get("tipo_punto") in db.TIPOS_OFERTA   # acopio o fundación: publica lo que TIENE
    efimero.kv_set("modo_tg", str(chat_id), "oferta" if es_oferta else "stock")  # /inventario = lo que TIENE
    if rep.get("tipo_punto") == "fundacion":
        txt = ("📦 <b>Inventario de tu fundación</b>\n\n"
               "Mándame la lista de <b>lo que pueden entregar o donar</b> "
               "(agua, medicinas, ropa, comida, pañales…). Puede ser:\n"
               "• una <b>foto</b> de la lista o del depósito,\n"
               "• una <b>nota de voz</b> dictándolo,\n"
               "• <b>texto</b>, o un archivo <b>PDF / Excel / Word</b>.\n\n"
               "Cuando un acopio o refugio necesite algo que tienen, la red los conecta contigo.")
    elif es_oferta:
        txt = ("📦 <b>Inventario de tu centro de acopio</b>\n\n"
               "Mándame la lista de <b>lo que tienes disponible para dar</b> "
               "(agua, medicinas, ropa, comida, pañales…). Puede ser:\n"
               "• una <b>foto</b> de la lista o del depósito,\n"
               "• una <b>nota de voz</b> dictándolo,\n"
               "• <b>texto</b>, o un archivo <b>PDF / Excel / Word</b>.\n\n"
               "Aparece en <b>«Disponible para entregar»</b> para que quien lo necesite te encuentre.")
    else:
        txt = ("📦 <b>Inventario de tu punto</b>\n\n"
               "Mándame la lista de <b>lo que tienes</b> (tu stock). Puede ser foto, nota de voz, "
               "texto o archivo PDF/Excel/Word.\n\n"
               "Se guarda para tus <b>datos y el censo</b> (no se publica como disponible). Para pedir "
               "lo que te falta, usa /necesito.")
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML", text=txt)
    if rep.get("lat") is None:   # sin ubicación aún → clave para el match por cercanía
        _tg("sendMessage", chat_id=chat_id,
            text="📍 Tip: comparte la ubicación de tu punto con /ubicacion — sirve para conectar por cercanía.")


def _cmd_necesito(chat_id):
    """Declara una NECESIDAD (clase demanda). Para un acopio = donaciones que pide; para un
    centro de ayuda = insumos que necesita. La próxima lista que mande se guarda como demanda."""
    rep = db.representante_por_tgid(chat_id)
    if not rep or rep["estado"] != "aprobado":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🔒 <b>/necesito</b> es solo para <b>centros de acopio</b>.\n\n" + _hint_rol(chat_id))
        return
    efimero.kv_set("modo_tg", str(chat_id), "demanda")
    if rep.get("tipo_punto") == "acopio":
        txt = ("🙏 <b>Donaciones que necesita tu centro de acopio</b>\n\n"
               "Mándame la lista de <b>lo que te hace falta</b> para que el público done "
               "(foto, voz, texto o PDF/Excel/Word). Aparecerá en la web en <b>«Lo que hace falta»</b>.\n\n"
               "<i>(Tu inventario de lo que TIENES se carga aparte con /inventario.)</i>")
    else:
        txt = ("🙏 <b>Lo que necesita tu punto</b>\n\n"
               "Mándame la lista de <b>los insumos que te hacen falta</b> "
               "(foto, voz, texto o PDF/Excel/Word). Así los acopios pueden conectarse contigo.")
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML", text=txt)


def _cmd_despachar(chat_id):
    """Despacho de un envío con QR de entrega (paridad con WhatsApp): elegir destino,
    listar el stock, escribir 'insumo cantidad' por línea, confirmar → reserva + QR."""
    rep = db.representante_por_tgid(chat_id)
    if not rep or rep["estado"] != "aprobado":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🔒 <b>/despachar</b> es solo para <b>centros de acopio</b>.\n\n" + _hint_rol(chat_id))
        return
    pid = rep.get("punto_id") or rep["id"]
    if not stock.de_punto(pid):
        _tg("sendMessage", chat_id=chat_id,
            text="Aún no tienes inventario publicado para despachar. Usa /inventario primero 📦")
        return
    con = db.conectar()
    dests = con.execute(
        "SELECT id, punto FROM representantes WHERE estado = 'aprobado' AND id != ? "
        "AND punto IS NOT NULL AND TRIM(punto) != '' AND rol = 'representante' "
        "ORDER BY id DESC LIMIT 10", (rep["id"],)).fetchall()
    con.close()
    if not dests:
        _tg("sendMessage", chat_id=chat_id, text="No hay otros puntos registrados todavía.")
        return
    kb = _kb([[{"text": (d["punto"] or "?")[:40], "callback_data": f"dsp|d|{d['id']}"}] for d in dests])
    _tg("sendMessage", chat_id=chat_id, text="🚚 Despachar un envío\n\n¿A qué punto va?", reply_markup=kb)


def _cmd_stock(chat_id):
    """Vista rápida del inventario numérico publicado (paridad con WhatsApp)."""
    rep = db.representante_por_tgid(chat_id)
    if not rep or rep["estado"] != "aprobado":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🔒 <b>/stock</b> es solo para <b>centros de acopio</b>.\n\n" + _hint_rol(chat_id))
        return
    pid = rep.get("punto_id") or rep["id"]
    inv = stock.de_punto(pid)
    if not inv:
        _tg("sendMessage", chat_id=chat_id,
            text="Aún no tienes inventario publicado. Usa /inventario y mándame tu lista 📦")
        return
    L = [f"📦 <b>Tu inventario publicado</b> ({len(inv)} insumos):"]
    for r in inv[:60]:
        c = (f"{r['cantidad']:g} {r['unidad'] or ''}".strip()
             if r["cantidad"] is not None else "❓ sin cantidad")
        L.append(f"• {html.escape(r['item'])} — {html.escape(c)}")
    if len(inv) > 60:
        L.append(f"…y {len(inv) - 60} más")
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML", text="\n".join(L)[:4000])


def _cmd_miinventario(chat_id):
    """Manda al colaborador su inventario en Excel, listo para reenviar."""
    rep = db.representante_por_tgid(chat_id)
    if not rep or rep["estado"] != "aprobado":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🔒 <b>/miinventario</b> es solo para <b>centros de acopio</b>.\n\n" + _hint_rol(chat_id))
        return
    pid = rep.get("punto_id") or rep["id"]
    if not db.inventario_de_punto(pid):
        _tg("sendMessage", chat_id=chat_id,
            text="Tu punto aún no tiene inventario cargado. Usa /inventario (lo que tienes) o /necesito 🙂")
        return
    _tg("sendMessage", chat_id=chat_id, text="📄 Generando tu inventario en Excel…")
    threading.Thread(target=_generar_y_enviar_inv, args=(chat_id, rep, pid), daemon=True).start()


def _generar_y_enviar_inv(chat_id, rep, pid):
    try:
        import export
        data = export.inventario_punto_bytes(pid, rep.get("punto") or "")
    except Exception as e:
        print("export error:", e)
        _tg("sendMessage", chat_id=chat_id, text="⚠️ No pude generar el Excel. Intenta de nuevo.")
        return
    base = re.sub(r"[^a-z0-9]+", "_", (rep.get("punto") or "punto").lower()).strip("_") or "punto"
    _enviar_documento(chat_id, f"inventario_{base}.xlsx", data,
                      caption=f"📦 Inventario de {rep.get('punto') or 'tu punto'} — puedes reenviarlo.")


def _dist_km(a_lat, a_lon, b_lat, b_lon):
    """Distancia en km entre dos coordenadas (haversine). None si falta alguna."""
    if None in (a_lat, a_lon, b_lat, b_lon):
        return None
    R = 6371.0
    dlat = math.radians(b_lat - a_lat)
    dlon = math.radians(b_lon - a_lon)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def _wa_link(tel):
    """Teléfono venezolano -> enlace wa.me (https) para escribir/llamar directo. None si no sirve."""
    d = re.sub(r"\D", "", tel or "")
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("0"):
        d = "58" + d[1:]            # 0412xxxxxxx -> 58412xxxxxxx
    elif len(d) == 10 and d[0] == "4":
        d = "58" + d               # 412xxxxxxx -> 58412xxxxxxx
    return f"https://wa.me/{d}" if len(d) >= 11 else None


def _wa_btn(label, tel):
    """Fila de teclado con botón de WhatsApp (contacto directo), o None si el número no sirve."""
    link = _wa_link(tel)
    return [{"text": label, "url": link}] if link else None


def _wa_text_link(tel, texto):
    """Enlace wa.me con un mensaje ya escrito (se abre WhatsApp listo para enviar)."""
    base = _wa_link(tel)
    if not base:
        return None
    return base + "?text=" + urllib.parse.quote(texto)


def _botones_responder(av):
    """Tres respuestas rápidas para que un acopio/MS conteste al voluntario por WhatsApp con
    el mensaje ya redactado: tengo disponible / tengo pero sin transporte / se me agotó."""
    tel = av.get("contacto")
    quien = av.get("quien") or "un punto de apoyo"
    sector = av.get("sector") or "el sector reportado"
    items = av.get("items") or "lo solicitado"
    if av.get("tipo") in ("ms", "vol"):
        opciones = [
            ("✅ Sí, puedo ayudar",
             f"Hola 👋, soy {quien}. Vi tu llamado de apoyo para {sector}. Sí puedo ayudar con {items}. ¿Cómo coordinamos?"),
            ("🛵 Puedo ayudar, sin transporte",
             f"Hola 👋, soy {quien}. Vi tu llamado para {sector}. Puedo ayudar con {items}, pero no cuento con vehículo ni moto. ¿Coordinamos el retiro o el transporte entre los dos?"),
            ("🛑 Ahora no puedo",
             f"Hola 👋, soy {quien}. Vi tu llamado para {sector}. Por ahora no puedo cubrirlo, disculpa. Te aviso si la situación cambia."),
        ]
    else:
        opciones = [
            ("✅ Sí, tengo disponible",
             f"Hola 👋, soy {quien}. Vi tu llamado de apoyo para {sector}. Sí tengo disponible {items}. ¿Cómo coordinamos la entrega?"),
            ("🛵 Tengo, pero sin transporte",
             f"Hola 👋, soy {quien}. Vi tu llamado para {sector}. Tengo disponible {items}, pero no cuento con vehículo ni moto para llevarlo. ¿Podemos coordinar el retiro o conseguir transporte entre los dos?"),
            ("🛑 Se me agotó",
             f"Hola 👋, soy {quien}. Vi tu llamado para {sector}. Lamentablemente {items} ya se me agotó y por ahora no puedo cubrirlo. Te aviso si logro reponer."),
        ]
    filas = []
    for label, texto in opciones:
        link = _wa_text_link(tel, texto)
        if link:
            filas.append([{"text": label, "url": link}])
    return filas


RADIO_CERCANO = 25.0  # km; más allá de esto se considera "lejos"


def _cmd_solicitar(chat_id):
    rep = db.representante_por_tgid(chat_id)
    if not rep or rep["estado"] != "aprobado":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🔒 <b>/solicitar</b> es solo para <b>centros de acopio</b>.\n\n" + _hint_rol(chat_id))
        return
    estados[chat_id] = {"paso": "sol_item"}
    extra = ("" if rep.get("lat") is not None
             else "\n\n📍 Tip: comparte tu ubicación con /ubicacion y te ordeno los acopios por cercanía.")
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🔎 <b>Buscar insumos en los centros de acopio</b>\n\n"
              "Escríbeme qué <b>necesitas</b>. Puede ser <b>uno o varios</b>, separados por coma "
              "o uno por línea:\n<i>pañales, agua, amoxicilina</i>\n\n"
              "Te muestro qué acopio tiene cada cosa, del más cercano al más lejano." + extra))


def _parse_pedido(texto):
    """Separa el texto en una lista de insumos (por coma, salto de línea o ';'), sin repetidos."""
    out, vistos = [], set()
    for p in re.split(r"[,\n;]+", texto or ""):
        p = " ".join(p.split())
        k = db._norm(p)
        if len(p) >= 2 and k and k not in vistos:
            vistos.add(k)
            out.append(p)
        if len(out) >= 30:
            break
    return out


def _notificar_acopio(dt, solicitante, item, sid):
    """Avisa al dueño del acopio que le solicitan `item`, con botones de respuesta."""
    quien = solicitante.get("punto") or solicitante.get("nombre") or "Un punto"
    tipo_txt = TIPOS.get(solicitante.get("tipo_punto"), "")
    cont_sol = solicitante.get("telefono")
    filas = [[{"text": "✅ Puedo ayudar", "callback_data": f"solr|{sid}|ok"},
              {"text": "❌ No tengo ahora", "callback_data": f"solr|{sid}|no"}]]
    wa = _wa_btn("💬 WhatsApp al solicitante", cont_sol)
    if wa:
        filas.append(wa)
    _tg("sendMessage", chat_id=dt, parse_mode="HTML",
        text=(f"📩 <b>Nueva solicitud de insumos</b>\n\nTe piden: <b>{html.escape(item)}</b>\n"
              f"De: <b>{html.escape(quien)}</b>" + (f" ({tipo_txt})" if tipo_txt else "")
              + (f"\n📞 Contacto: <b>{html.escape(cont_sol)}</b>" if cont_sol else "")
              + "\n\n¿Puedes ayudar con esto?"),
        reply_markup=_kb(filas))


def _plan_minimo(items_cerca, acopios_elegibles):
    """Combinación MÍNIMA de acopios (greedy set cover) que cubre los insumos disponibles cerca:
    en cada paso elige el acopio que cubre más insumos aún faltantes (desempata por cercanía)."""
    faltan = set(items_cerca)
    plan = []
    while faltan:
        mejor, mejor_cov, mejor_d = None, 0, 9e9
        for a in acopios_elegibles:
            cov = len(set(a["cubre"]) & faltan)
            d = a["dist"] if a["dist"] is not None else 0
            if cov > mejor_cov or (cov == mejor_cov and cov > 0 and d < mejor_d):
                mejor, mejor_cov, mejor_d = a, cov, d
        if not mejor or mejor_cov == 0:
            break
        plan.append(mejor)
        faltan -= set(mejor["cubre"])
    return plan


def _buscar_y_mostrar(chat_id, st, texto):
    pedido = _parse_pedido(texto)
    if not pedido:
        _tg("sendMessage", chat_id=chat_id,
            text="Escríbeme al menos un insumo (2+ letras). Puedes poner varios separados por coma 🙂")
        return
    rep = db.representante_por_tgid(chat_id)
    mylat = rep.get("lat") if rep else None
    mylon = rep.get("lon") if rep else None

    # Por cada insumo pedido, busca acopios; acumula por acopio QUÉ pedidos cubre.
    acopios = {}
    for it in pedido:
        for r in db.buscar_oferta(it):
            a = acopios.get(r["punto_id"])
            if not a:
                a = dict(r)
                a["cubre"] = []
                a["dist"] = _dist_km(mylat, mylon, r.get("lat"), r.get("lon"))
                acopios[r["punto_id"]] = a
            if it not in a["cubre"]:
                a["cubre"].append(it)

    def _cerca(a):  # elegible como "cercano" (dentro del radio, o sin GPS no se puede medir)
        return a["dist"] is None or a["dist"] <= RADIO_CERCANO

    # Por insumo: acopios que lo tienen, más cercano primero.
    por_item = {}
    for it in pedido:
        por_item[it] = sorted([a for a in acopios.values() if it in a["cubre"]],
                              key=lambda a: (a["dist"] is None, a["dist"] if a["dist"] is not None else 9e9))
    cerca = {it for it in pedido if any(_cerca(a) for a in por_item[it])}
    sin_cerca = [it for it in pedido if it not in cerca]

    lista = sorted(acopios.values(),
                   key=lambda a: (-len(a["cubre"]), a["dist"] is None,
                                  a["dist"] if a["dist"] is not None else 9e9))
    plan = _plan_minimo(cerca, [a for a in lista if _cerca(a)])

    st["paso"] = "sol_pick"
    st["pedido"] = pedido
    st["sin_cerca"] = sin_cerca
    st["ops"] = {str(a["punto_id"]): a for a in lista}
    st["plan_min"] = [str(a["punto_id"]) for a in plan]

    N = len(pedido)
    L = [f"🔎 Tu pedido ({N}): ✅ {len(cerca)} disponible{'s' if len(cerca) != 1 else ''}"
         + (f" · ⚠️ {len(sin_cerca)} sin acopio cercano" if sin_cerca else "")]
    if mylat is None:
        L.append("<i>(comparte /ubicacion para ordenar por cercanía)</i>")
    L.append("\n📋 <b>Dónde está cada cosa:</b>")
    for it in pedido:
        avs = por_item[it]
        if not avs:
            L.append(f"  • {html.escape(it)} → ⚠️ ninguno")
        else:
            a0 = avs[0]
            dist = f" ({a0['dist']:.1f} km)" if a0["dist"] is not None else ""
            mas = f" +{len(avs) - 1} más" if len(avs) > 1 else ""
            lejos_tag = "" if _cerca(a0) else " ⚠️ lejos"
            L.append(f"  • {html.escape(it)} → {html.escape(a0['punto'] or 'Acopio')}{dist}{mas}{lejos_tag}")

    botones = []
    if plan:
        L.append("\n📩 <b>Solicitar a todos</b> contactará: " + html.escape(", ".join(a["punto"] or "Acopio" for a in plan)))
        botones.append([{"text": f"📩 Solicitar a todos los necesarios ({len(plan)})", "callback_data": "sola|all"}])
    for a in lista[:6]:  # individuales por si quieres elegir
        et = (f"📩 Solo {a['punto'] or 'acopio'}")[:40] + (f" · {a['dist']:.0f}km" if a["dist"] is not None else "")
        botones.append([{"text": et, "callback_data": f"sol|{a['punto_id']}"}])
    if sin_cerca:
        botones.append([{"text": "📢 Avisar a coordinadores y publicar lo que falta",
                         "callback_data": "solb|esc"}])
    if not acopios and not sin_cerca:
        L.append("\n😕 No encontré nada. Intenta con otras palabras.")
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML", text="\n".join(L), reply_markup=_kb(botones))


def _cmd_llamado(chat_id):
    """Inicia un llamado de apoyo (solo voluntarios aprobados): pide el sector."""
    vol = db.voluntario_por_tgid(chat_id)
    if not vol or vol["estado"] != "aprobado":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🔒 <b>/llamado</b> es solo para <b>Voluntariado</b>.\n\n" + _hint_rol(chat_id))
        return
    estados[chat_id] = {"paso": "lla_sector", "lla": {"vol_id": vol["id"]}}
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("📣 <b>Nuevo llamado de apoyo</b>\n\n"
              "Primero: ¿en qué <b>sector / zona</b> es el llamado? "
              "(ej. «El Valle, bloque 5», «Carretera vieja Petare»)"))


def _recibir_llamado(chat_id, st, msg):
    """Recibe el contenido del llamado: una foto (con texto) o solo texto. Lo publica al
    instante en la web y dispara la triangulación con los acopios si pide insumos."""
    foto = msg.get("photo")
    texto = (msg.get("caption") or msg.get("text") or "").strip()
    if not foto and not texto:
        _tg("sendMessage", chat_id=chat_id,
            text="Mándame el <b>mensaje del llamado</b> (puede ir con una <b>foto</b>) 🙂", parse_mode="HTML")
        return
    if len(texto) > MAX_TEXTO:
        _tg("sendMessage", chat_id=chat_id, text="⚠️ Ese texto es muy largo. Mándalo más corto.")
        return
    lla = st.get("lla", {})
    sector = lla.get("sector", "")
    vol = db.voluntario(lla.get("vol_id")) or db.voluntario_por_tgid(chat_id)
    if not vol:
        estados.pop(chat_id, None)
        _tg("sendMessage", chat_id=chat_id, text="Tu sesión expiró. Usa /llamado de nuevo 🙂")
        return
    # 1) Guardar el llamado (sale al instante; la web lo toma en el próximo ciclo).
    lid = db.crear_llamado(vol["id"], chat_id, sector, texto or "", None)
    # 2) Guardar la foto en web/img para que se vea en la página.
    if foto:
        try:
            data = _descargar(foto[-1]["file_id"])
            if data and len(data) <= MAX_IMG:
                img_dir = WEB_DIR / "img"
                img_dir.mkdir(parents=True, exist_ok=True)
                (img_dir / f"llamado_{lid}.jpg").write_bytes(data)
                db.set_foto_llamado(lid, f"img/llamado_{lid}.jpg")
        except Exception as e:
            print("llamado foto error:", e)
    estados.pop(chat_id, None)
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("✅ ¡Llamado publicado! Aparecerá en la web en <b>Llamados de apoyo</b> en unos minutos. "
              "Gracias por levantarlo. 📣\n\nPuedes crear otro con /llamado."),
        reply_markup=_menu_voluntario())
    # 3) Triangular en segundo plano (la IA puede tardar): si pide insumos, avisar a acopios.
    threading.Thread(target=_triangular_llamado, args=(lid, sector, texto, vol),
                     daemon=True).start()


AVISO_SEGURO_HTML = ("\n\n⚠️ <i>La Red solo conecta; no verifica ni supervisa la ayuda. "
                     "Coordina con precaución y en lugares seguros.</i>")
AVISO_SALUD_HTML = ("\n🧠 <i>Apoyo voluntario entre personas: no sustituye atención médica "
                    "ni psicológica profesional. En crisis, 911.</i>")


def _es_salud(items):
    t = (items or "").lower()
    return any(k in t for k in ("salud", "medicin", "psic", "emocional", "mental"))


def _coletilla(items):
    """Disclaimer para los avisos de match (HTML de Telegram)."""
    return AVISO_SEGURO_HTML + (AVISO_SALUD_HTML if _es_salud(items) else "")


def _coletilla_wa(items):
    """Disclaimer para los avisos de match por WhatsApp (texto plano)."""
    txt = ("\n\n⚠️ La Red solo conecta; no verifica ni supervisa la ayuda. "
           "Coordina con precaución y en lugares seguros.")
    if _es_salud(items):
        txt += ("\n🧠 Apoyo voluntario entre personas: no sustituye atención médica "
                "ni psicológica profesional. En crisis, 911.")
    return txt


def _notificar_acopio_llamado(dt, acopio, sector, items_cubre, vol, llamado_id):
    """Avisa a un acopio que un llamado de apoyo pide insumos que él tiene. El contacto del
    voluntario viaja PRIVADO (nunca a la web); el acopio responde con un toque por WhatsApp."""
    cont = vol.get("contacto")
    nombre_pto = acopio.get("punto") or ""
    items_txt = ", ".join(items_cubre)
    quien = ("del centro de acopio " + nombre_pto).strip() if nombre_pto else "del centro de acopio"
    aid = db.crear_aviso_llamado(llamado_id, dt, "acopio", quien, items_txt, cont, sector)
    kb = _kb([[{"text": "💬 Responder al voluntario", "callback_data": f"lresp|{aid}"}],
              [{"text": "🚫 No puedo ayudar", "callback_data": f"lno|{aid}"}]]) if cont else None
    _tg("sendMessage", chat_id=dt, parse_mode="HTML",
        text=(f"📣 <b>Llamado de apoyo que puedes cubrir</b>\n\n"
              f"En <b>{html.escape(sector or 'un sector en emergencia')}</b> piden: "
              f"<b>{html.escape(items_txt)}</b>\n"
              f"Tú tienes esos insumos en <b>{html.escape(nombre_pto or 'tu acopio')}</b>.\n\n"
              f"Reportado por <b>{html.escape(vol.get('nombre') or 'un voluntario')}</b>"
              + (f"\n📞 Contacto: <b>{html.escape(cont)}</b>" if cont else "")
              + "\n\nToca <b>Responder al voluntario</b> para escribirle por WhatsApp con un mensaje listo."
              + _coletilla(items_txt)),
        reply_markup=kb)


# Mapea lo que pide un llamado (nombre + categoría del ítem, normalizado SIN acentos) a las
# categorías de Manos Solidarias. Las claves van en forma normalizada (minúscula, sin tildes).
_MS_KEYWORDS = [
    ("agua",         ("agua", "bebida", "hidrata", "suero")),
    ("comida",       ("comida", "aliment", "viver", "merienda", "almuerzo", "cena", "desayuno", "enlatad")),
    ("salud",        ("salud", "medic", "medicament", "farmac", "curaci", "insumo medic", "primeros auxilios",
                      "botiqu", "vendaj", "antibi", "analges", "gasa", "alcohol")),
    ("transporte",   ("transport", "traslad", "vehicul", "flete", "movil", "lancha", "bote")),
    ("herramientas", ("herramient", "linterna", "bateria", "pila", "vela", "generador", "planta", "cuerda")),
    ("carga",        ("carga", "cargar tel", "power bank", "powerbank", "energia tel")),
    ("hospedaje",    ("hospedaje", "alojamiento", "refugio", "dormir", "albergue", "techo", "carpa", "colchon")),
]


def _ms_categorias(items):
    """Categorías de Manos Solidarias que calzan con los ítems pedidos en el llamado."""
    cats = set()
    for it in (items or []):
        blob = db._norm((getattr(it, "item", "") or "") + " " + (getattr(it, "categoria", "") or ""))
        for cat, kws in _MS_KEYWORDS:
            if any(k in blob for k in kws):
                cats.add(cat)
    return cats


def _notificar_ms_llamado(dt, aliado, sector, pedidos, vol, llamado_id):
    """Avisa a un Manos Solidarias que un llamado pide algo de su categoría. El contacto del
    voluntario viaja PRIVADO; responde con un toque por WhatsApp."""
    cont = vol.get("contacto")
    nombre = aliado.get("nombre") or ""
    items_txt = ", ".join(pedidos[:8])
    cat_lbl = ALIADO_CAT_LABEL.get(aliado.get("categoria"), aliado.get("categoria") or "ayuda")
    quien = ("de Manos Solidarias (" + nombre + ")") if nombre else "de Manos Solidarias"
    aid = db.crear_aviso_llamado(llamado_id, dt, "ms", quien, items_txt, cont, sector)
    kb = _kb([[{"text": "💬 Responder al voluntario", "callback_data": f"lresp|{aid}"}],
              [{"text": "🚫 No puedo ayudar", "callback_data": f"lno|{aid}"}]]) if cont else None
    _tg("sendMessage", chat_id=dt, parse_mode="HTML",
        text=(f"📣 <b>Llamado de apoyo donde puedes ayudar</b>\n\n"
              f"En <b>{html.escape(sector or 'un sector en emergencia')}</b> necesitan: "
              f"<b>{html.escape(items_txt)}</b>\n"
              f"Tú ofreces <b>{html.escape(cat_lbl)}</b>.\n\n"
              f"Reportado por <b>{html.escape(nombre or 'un voluntario')}</b>"
              + (f"\n📞 Contacto: <b>{html.escape(cont)}</b>" if cont else "")
              + "\n\nToca <b>Responder al voluntario</b> para escribirle por WhatsApp con un mensaje listo."
              + _coletilla(items_txt)),
        reply_markup=kb)


def _notificar_fundacion_llamado(dt, fund, sector, items_txt, vol, llamado_id):
    """Avisa a una fundación que un pedido/llamado calza con sus ÁREAS de atención
    (aunque no tenga inventario cargado). Mismo modelo que Manos Solidarias:
    contacto privado + respuesta con un toque."""
    cont = vol.get("contacto")
    punto = fund.get("punto") or "tu fundación"
    quien = "de la fundación " + (fund.get("punto") or "")
    aid = db.crear_aviso_llamado(llamado_id, dt, "fund", quien.strip(), items_txt, cont, sector)
    kb = _kb([[{"text": "💬 Responder al solicitante", "callback_data": f"lresp|{aid}"}],
              [{"text": "🚫 No puedo ayudar", "callback_data": f"lno|{aid}"}]]) if cont else None
    _tg("sendMessage", chat_id=dt, parse_mode="HTML",
        text=(f"📣 <b>Pedido de ayuda del área de tu fundación</b>\n\n"
              f"En <b>{html.escape(sector or 'un sector en emergencia')}</b> necesitan: "
              f"<b>{html.escape(items_txt)}</b>\n"
              f"<b>{html.escape(punto)}</b> atiende: <i>{html.escape(fund.get('areas') or '')}</i>."
              + (f"\n📞 Contacto: <b>{html.escape(cont)}</b>" if cont else "")
              + "\n\nToca <b>Responder al solicitante</b> para escribirle por WhatsApp con un mensaje listo."
              + _coletilla(items_txt)),
        reply_markup=kb)


def _wa_dest(tel):
    """Número canónico VE → formato de la Cloud API (58 + 10 dígitos), o None."""
    from wa_util import canonico
    d = canonico(tel or "")
    return ("58" + d) if d else None


def _notificar_wa_llamado(phone, tipo, quien, sector, items_txt, vol, llamado_id):
    """Aviso por WhatsApp (Cloud API) a un acopio/MS/voluntario registrado por ese canal.
    Mismo modelo que el aviso de Telegram: contacto privado + respuesta con un toque.
    Best-effort: si Meta rechaza (ventana de 24 h cerrada), solo queda en el log."""
    dest = _wa_dest(phone)
    if not dest:
        return False
    aid = db.crear_aviso_llamado(llamado_id, None, tipo, quien, items_txt,
                                 vol.get("contacto"), sector, destino_wa=dest)
    cuerpo = ("📣 *Llamado de apoyo que puedes cubrir*\n\n"
              f"En *{sector or 'un sector en emergencia'}* piden: *{items_txt}*.\n"
              f"Pide: {vol.get('nombre') or 'una persona de la zona'}."
              "\n\nSi puedes ayudar, te paso su contacto con un mensaje listo para enviar."
              + _coletilla_wa(items_txt))
    botones = [(f"lla|{aid}|si", "🤝 Puedo ayudar"),
               (f"lla|{aid}|no", "🛑 Ahora no puedo")]
    try:
        if WA_TPL_LLAMADO:
            # Plantilla aprobada: llega SIEMPRE, aunque la ventana de 24 h esté cerrada.
            r = wa_api.enviar_plantilla(
                dest, WA_TPL_LLAMADO,
                variables=(sector or "un sector en emergencia", items_txt,
                           vol.get("nombre") or "una persona de la zona"),
                payloads=[b[0] for b in botones])
            if r.get("messages"):
                return True
            print("plantilla rechazada, fallback a mensaje normal:", r)
        wa_api.enviar_botones(dest, cuerpo, botones)
        return True
    except Exception as e:  # noqa: BLE001
        print("aviso wa llamado error:", e)
        return False


def _notificar_vol_llamado(dt, volrow, sector, pedidos, vol, llamado_id):
    """Avisa por Telegram a un voluntario de la zona que un llamado pide ayuda cerca."""
    cont = vol.get("contacto")
    nombre = volrow.get("nombre") or ""
    items_txt = ", ".join(pedidos[:8])
    quien = ("del voluntariado (" + nombre + ")") if nombre else "del voluntariado"
    aid = db.crear_aviso_llamado(llamado_id, dt, "vol", quien, items_txt, cont, sector)
    kb = _kb([[{"text": "💬 Responder al solicitante", "callback_data": f"lresp|{aid}"}],
              [{"text": "🚫 No puedo ayudar", "callback_data": f"lno|{aid}"}]]) if cont else None
    _tg("sendMessage", chat_id=dt, parse_mode="HTML",
        text=(f"📣 <b>Llamado de apoyo en tu zona</b>\n\n"
              f"En <b>{html.escape(sector or 'un sector en emergencia')}</b> necesitan: "
              f"<b>{html.escape(items_txt)}</b>\n"
              f"Tú eres voluntario cerca ({html.escape(volrow.get('zona') or 'tu zona')})."
              + (f"\n📞 Contacto: <b>{html.escape(cont)}</b>" if cont else "")
              + "\n\nToca <b>Responder al solicitante</b> para escribirle por WhatsApp con un mensaje listo."
              + _coletilla(items_txt)),
        reply_markup=kb)


def _triangular_llamado(lid, sector, texto, vol):
    """Lee el mensaje del llamado; si pide insumos, avisa por Telegram (con el contacto
    privado del voluntario) a los acopios que los tengan Y a los Manos Solidarias cuya
    categoría calce. Si no hay insumos detectables o nadie calza, no hace nada (el llamado
    igual se publicó: difusión en la web)."""
    if not texto:
        return
    try:
        lec = captura.leer_texto(texto)
    except Exception as e:
        print("triangulacion lectura error:", e)
        return
    pedidos = []
    vistos = set()
    for it in (lec.items or []):
        nombre = (it.item or "").strip()
        k = db._norm(nombre)
        if len(nombre) >= 2 and k and k not in vistos:
            vistos.add(k)
            pedidos.append(nombre)
    # OJO: aunque la IA no detecte insumos (rescate, desaparecidos, transporte…), se
    # sigue: las fundaciones matchean por ÁREAS con el texto (paso 2b). Los pasos por
    # inventario/categoría/zona (1, 2 y 3) solo corren si hay insumos, como siempre.
    enviados = 0
    avisados = set()
    # 1) Centros de acopio y fundaciones que TIENEN lo pedido (match por inventario).
    acopios = {}
    for it in pedidos:
        for r in db.buscar_oferta(it):
            a = acopios.setdefault(r["punto_id"], {**r, "cubre": []})
            if it not in a["cubre"]:
                a["cubre"].append(it)
    for a in acopios.values():
        if not a.get("cubre"):
            continue
        if a.get("dueno_tgid"):
            _notificar_acopio_llamado(a["dueno_tgid"], a, sector, a["cubre"], vol, lid)
            enviados += 1
        else:
            wa = db.wa_de_rep(a.get("dueno_rep_id"))
            quien = ("del centro de acopio " + (a.get("punto") or "")).strip()
            if wa and _notificar_wa_llamado(wa, "acopio", quien, sector,
                                            ", ".join(a["cubre"]), vol, lid):
                enviados += 1
    # 2) Manos Solidarias cuya CATEGORÍA calce con lo pedido (comida, agua, salud…).
    ms_cats = _ms_categorias(lec.items)
    for al in db.aliados_para_notificar(sorted(ms_cats)):
        tg, wa = al.get("telegram_id"), al.get("wa_phone")
        if str(tg) == str(vol.get("telegram_id")):
            continue
        if tg and tg not in avisados:
            avisados.add(tg)
            _notificar_ms_llamado(tg, al, sector, pedidos, vol, lid)
            enviados += 1
        elif wa and ("wa:" + wa) not in avisados:
            avisados.add("wa:" + wa)
            quien = ("de Manos Solidarias (" + al["nombre"] + ")") if al.get("nombre") else "de Manos Solidarias"
            if _notificar_wa_llamado(wa, "ms", quien, sector, ", ".join(pedidos[:8]), vol, lid):
                enviados += 1
    # 2b) FUNDACIONES cuyas ÁREAS de atención calzan con el texto del pedido/llamado
    # (además de las que ya calzaron por inventario en el paso 1). Corre incluso si
    # la IA no detectó insumos (rescate, desaparecidos…): ahí decide el texto.
    items_txt_fund = ", ".join(pedidos[:8]) or (texto or "").strip()[:90]
    for fu in db.fundaciones_para_notificar(texto):
        if fu.get("punto_id") in acopios:    # ya avisada por inventario en el paso 1
            continue
        tg, wa = fu.get("dueno_tgid"), fu.get("wa_phone")
        if str(tg) == str(vol.get("telegram_id")):
            continue
        if tg and tg not in avisados:
            avisados.add(tg)
            _notificar_fundacion_llamado(tg, fu, sector, items_txt_fund, vol, lid)
            enviados += 1
        elif wa and ("wa:" + wa) not in avisados:
            avisados.add("wa:" + wa)
            quien = ("de la fundación " + (fu.get("punto") or "")).strip()
            if _notificar_wa_llamado(wa, "fund", quien, sector, items_txt_fund, vol, lid):
                enviados += 1
    # 3) Voluntarios aprobados cuya ZONA calza con el sector del llamado (solo si el
    # llamado pide insumos concretos, como siempre).
    for v in (db.voluntarios_para_notificar(sector) if pedidos else []):
        tg, wa = v.get("telegram_id"), v.get("wa_phone")
        if str(tg) == str(vol.get("telegram_id")):
            continue
        if tg and tg not in avisados:
            avisados.add(tg)
            _notificar_vol_llamado(tg, v, sector, pedidos, vol, lid)
            enviados += 1
        elif wa and ("wa:" + wa) not in avisados:
            avisados.add("wa:" + wa)
            quien = ("del voluntariado (" + v["nombre"] + ")") if v.get("nombre") else "del voluntariado"
            if _notificar_wa_llamado(wa, "vol", quien, sector, ", ".join(pedidos[:8]), vol, lid):
                enviados += 1
    if enviados and vol.get("telegram_id"):
        _tg("sendMessage", chat_id=vol["telegram_id"], parse_mode="HTML",
            text=f"🔗 Tu llamado se cruzó con <b>{enviados}</b> contacto(s) que pueden ayudar "
                 "(centros de acopio, fundaciones, Manos Solidarias y/o voluntariado). Les avisamos con tu contacto para coordinar. 🙌")
    elif enviados and vol.get("wa_phone"):
        # Voluntario registrado por WhatsApp (bot_wa reusa esta triangulación).
        try:
            wa_api.enviar_texto("58" + vol["wa_phone"],
                                f"🔗 Tu llamado se cruzó con *{enviados}* contacto(s) que pueden "
                                "ayudar (centros de acopio, fundaciones, Manos Solidarias y/o voluntariado). "
                                "Les avisamos con tu contacto para coordinar. 🙌")
        except Exception as e:  # noqa: BLE001
            print("aviso vol wa error:", e)


def _menu_inline(es_dueno):
    """Menú de acciones tappable (se adjunta al mensaje de 'ya afiliado')."""
    filas = [[{"text": "📦 Tengo", "callback_data": "cmd|inv"},
              {"text": "🙏 Necesito", "callback_data": "cmd|nec"}],
             [{"text": "📩 Solicitar insumo", "callback_data": "cmd|sol"},
              {"text": "📄 Mi inventario", "callback_data": "cmd|mid"}],
             [{"text": "📍 Ubicación", "callback_data": "cmd|ubi"}]]
    if es_dueno:
        filas[-1].append({"text": "📝 Ficha", "callback_data": "cmd|fic"})
    return _kb(filas)


def manejar_mensaje(msg):
    """Punto de entrada del main-loop: corre el handler con estado persistente."""
    _con_estado(msg.get("chat", {}).get("id"), _manejar_mensaje, msg)


def _manejar_mensaje(msg):
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return  # la afiliación es solo por chat privado
    chat_id = chat.get("id")
    texto = (msg.get("text") or "").strip()

    # Pin de ubicación (botón nativo de Telegram): guarda las coordenadas del punto.
    loc = msg.get("location")
    if loc:
        st_al = estados.get(chat_id)
        if st_al and st_al.get("paso") == "al_gps":   # Manos Solidarias en curso
            st_al["aliado"]["lat"] = loc.get("latitude")
            st_al["aliado"]["lon"] = loc.get("longitude")
            _tg("sendMessage", chat_id=chat_id, text="📍 ¡Ubicación recibida!",
                reply_markup={"remove_keyboard": True})
            _ir_a_confirmar_aliado(chat_id, st_al)
            return
        rep = db.representante_por_tgid(chat_id)
        if rep and rep["estado"] != "rechazado":
            db.guardar_gps(rep["id"], loc.get("latitude"), loc.get("longitude"))
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="✅ <b>Ubicación de tu punto guardada</b>. ¡Gracias! 📍",
                reply_markup={"remove_keyboard": True})
            return
        al = db.aliado_por_tgid(chat_id)
        if al and al["estado"] == "aprobado":
            db.guardar_gps_aliado(al["id"], loc.get("latitude"), loc.get("longitude"))
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="✅ <b>¡Ubicación guardada!</b> Ya apareces en el mapa de Manos Solidarias. ¡Gracias! 📍",
                reply_markup={"remove_keyboard": True})
            return
        _tg("sendMessage", chat_id=chat_id,
            text="Para guardar tu ubicación, primero regístrate con /start 🙂",
            reply_markup={"remove_keyboard": True})
        return

    if texto == GPS_SKIP:   # "Lo hago luego": no está en el sitio, pospone el pin
        st_al = estados.get(chat_id)
        if st_al and st_al.get("paso") == "al_gps":   # aliado: sigue sin ubicación
            _tg("sendMessage", chat_id=chat_id, text="👍 Listo, sin ubicación por ahora.",
                reply_markup={"remove_keyboard": True})
            _ir_a_confirmar_aliado(chat_id, st_al)
            return
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=("👍 Listo. Cuando estés <b>en tu punto</b>, escribe /ubicacion y toca el botón "
                  "para dejar registrada la ubicación exacta."),
            reply_markup={"remove_keyboard": True})
        return

    if texto.startswith("/start"):
        partes_s = texto.split(maxsplit=1)
        if len(partes_s) == 2 and partes_s[1].startswith("nec_"):
            _iniciar_compromiso(chat_id, partes_s[1], msg.get("from", {}) or {})
            return
        if len(partes_s) == 2 and partes_s[1].startswith("lla_"):
            _iniciar_respuesta_llamado(chat_id, partes_s[1], msg.get("from", {}) or {})
            return
        prev = db.representante_por_tgid(chat_id)
        if prev and prev["estado"] == "aprobado":
            es_dueno = (prev.get("rol") or "representante") != "colaborador"
            extra = ("\n📝 /ficha — completar o actualizar la ficha de tu punto" if es_dueno else "")
            _set_commands_rol(chat_id, "centro", con_ficha=es_dueno)
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=("✅ Ya estás afiliado. Mándame tus listas cuando quieras "
                      "(foto, voz, texto o PDF/Excel/Word).\n\n"
                      "📦 /inventario — cargar lo que TIENES (acopio)\n"
                      "🙏 /necesito — declarar lo que NECESITAS / donaciones\n"
                      "🔎 /solicitar — buscar un insumo en los acopios cercanos\n"
                      "📍 /ubicacion — compartir la ubicación de tu punto" + extra +
                      "\n\nUsa los botones de abajo o el menú ☰ del chat."),
                reply_markup=_menu_inline(es_dueno))
            return
        if prev and prev["estado"] == "pendiente":
            _tg("sendMessage", chat_id=chat_id,
                text="⏳ Tu afiliación ya está enviada y pendiente de aprobación. Te avisamos apenas la confirmen.")
            return
        vol = db.voluntario_por_tgid(chat_id)
        if vol and vol["estado"] == "aprobado":
            _set_commands_rol(chat_id, "voluntario")
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=("✅ Estás registrado como <b>Voluntariado</b>.\n\n"
                      "📣 /llamado — levantar un llamado de apoyo (foto + sector + mensaje)\n\n"
                      "Usa el botón de abajo o el menú ☰ del chat."),
                reply_markup=_menu_voluntario())
            return
        if vol and vol["estado"] == "pendiente":
            _tg("sendMessage", chat_id=chat_id,
                text="⏳ Tu registro de Voluntariado ya está enviado y pendiente de aprobación. Te avisamos apenas lo confirmen.")
            return
        al = db.aliado_por_tgid(chat_id)
        if al and al["estado"] == "aprobado":
            _set_commands_rol(chat_id, "aliado")
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=("✅ Estás registrado como <b>Manos Solidarias</b>. ¡Gracias por ayudar! 🤝\n\n"
                      "📍 /ubicacion — compartir tu ubicación para aparecer en el mapa\n\n"
                      "Usa el menú ☰ del chat."))
            return
        if al and al["estado"] == "pendiente":
            _tg("sendMessage", chat_id=chat_id,
                text="⏳ Tu registro como Manos Solidarias ya está enviado y pendiente de aprobación. Te avisamos apenas lo confirmen.")
            return
        iniciar(chat_id)
        return

    if texto.startswith("/ficha"):
        _cmd_ficha(chat_id)
        return

    if texto.startswith("/ubicacion"):
        _cmd_ubicacion(chat_id)
        return

    if texto.startswith("/inventario"):
        _cmd_inventario(chat_id)
        return

    if texto.startswith("/necesito"):
        _cmd_necesito(chat_id)
        return

    if texto.startswith("/recibi"):
        _cmd_recibi(chat_id)
        return

    if texto.startswith("/miinventario"):
        _cmd_miinventario(chat_id)
        return

    if texto.startswith("/stock"):
        _cmd_stock(chat_id)
        return

    if texto.startswith("/despachar"):
        _cmd_despachar(chat_id)
        return

    if texto.startswith("/solicitar"):
        _cmd_solicitar(chat_id)
        return

    if texto.startswith("/llamado"):
        _cmd_llamado(chat_id)
        return

    if texto.startswith("/pedir"):
        _iniciar_pedido(chat_id)
        return

    if texto.startswith("/retirarme"):
        _cmd_retirarme(chat_id)
        return

    # Líneas de un despacho en curso ('pañales 40' por línea) → resumen + confirmar.
    st_dsp = estados.get(chat_id)
    if st_dsp and st_dsp.get("paso") == "dsp_items" and texto:
        d = st_dsp["dsp"]
        items, errores = envios.parse_lineas(d["pid"], texto)
        if errores:
            _tg("sendMessage", chat_id=chat_id,
                text="⚠️ Revisa esto:\n" + "\n".join("• " + x for x in errores) +
                     "\n\nVuelve a mandarme la lista completa, una línea por insumo.")
            return
        if not items:
            _tg("sendMessage", chat_id=chat_id,
                text="No entendí ningún insumo. Formato: pañales 40 (una línea por insumo).")
            return
        d["items"] = items
        inv = {r["id"]: r for r in stock.de_punto(d["pid"])}
        L = ["🚚 <b>Envío a despachar:</b>"]
        for sid, cant in items:
            r = inv.get(sid, {})
            L.append(("• " + html.escape(r.get("item", "?")) +
                      f" — {cant:g} " + html.escape(r.get("unidad") or "")).rstrip())
        L.append("\nAl despachar se descuenta de tu inventario publicado y se genera el QR de entrega.")
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML", text="\n".join(L),
            reply_markup=_kb([[{"text": "🚚 Despachar", "callback_data": "dsp|go"},
                               {"text": "❌ Cancelar", "callback_data": "dsp|no"}]]))
        return

    # Contenido de un llamado de apoyo: puede llegar como foto (con texto) o solo texto,
    # así que se intercepta aquí (antes del motor de pasos por texto).
    st_ll = estados.get(chat_id)
    if st_ll and st_ll.get("paso") == "lla_contenido":
        _recibir_llamado(chat_id, st_ll, msg)
        return

    st = estados.get(chat_id)
    if not st:
        rep = db.representante_por_tgid(chat_id)
        if rep and rep["estado"] == "aprobado":
            # En hilo: la lectura con IA/whisper puede tardar y, si bloquea el loop,
            # los toques de boton se encolan y su callback expira (error 400).
            threading.Thread(target=_capturar, args=(chat_id, rep, msg, texto),
                             daemon=True).start()
        else:
            _tg("sendMessage", chat_id=chat_id, text="Para afiliarte, escribe /start 🙂")
        return

    paso = st["paso"]
    # Pedir ayuda: una FOTO en el paso de descripción vale como contenido (se lee
    # con la misma IA del flyer web/WhatsApp y queda adjunta para moderación). En
    # hilo: la lectura tarda segundos y bloquearía el loop (mismo motivo que _capturar).
    if paso == "pd_desc" and msg.get("photo"):
        threading.Thread(target=_pd_foto, args=(chat_id, st, msg), daemon=True).start()
        return
    if paso in ("nombre", "cedula", "punto", "ficha", "corr_nombre", "corr_cedula",
                "corr_punto", "edit_captura_campo", "sol_item",
                "al_nombre", "al_desc", "al_zona", "al_tel", "al_horario",
                "vo_nombre", "vo_zona", "vo_tel", "lla_sector",
                "pd_persona", "pd_desc", "pd_zona", "pd_tel", "pd_nombre") and not texto:
        # En los pasos de Pedir ayuda se repite la pregunta pendiente: sin ella el
        # usuario pierde el hilo y las respuestas llegan corridas (visto en prod).
        extra = _PREGUNTA_PD.get(paso, "")
        if st.get("pedido", {}).get("cat") == "desaparecido":
            extra = _PREGUNTA_PD_DESAP.get(paso, extra)
        _tg("sendMessage", chat_id=chat_id,
            text="Por favor, escríbelo como texto (o usa el botón si no aplica) 🙂" +
                 ("\n\n" + extra if extra else ""))
        return

    if paso == "corr_nombre":
        st["nombre"] = texto[:80]
        st.pop("corrigiendo", None)
        _ir_a_confirmar(chat_id, st)
    elif paso == "corr_punto":
        st["punto"] = texto[:100]
        st.pop("corrigiendo", None)
        _ir_a_confirmar(chat_id, st)
    elif paso == "nombre":
        st["nombre"] = texto[:80]
        st["paso"] = "tipo"   # la cédula se pide al FINAL del alta (omitible)
        _pedir_tipo(chat_id)
    elif paso == "cedula":
        ced = _validar_cedula(chat_id, st, texto)
        if not ced:
            return
        st["cedula"] = ced
        if st.get("punto"):   # flujo nuevo: la cédula cierra el alta → resumen
            _ir_a_confirmar(chat_id, st)
        else:                 # venía a mitad del flujo viejo (deploy): sigue como antes
            st["paso"] = "tipo"
            _pedir_tipo(chat_id)
    elif paso == "corr_cedula":
        ced = _validar_cedula(chat_id, st, texto)
        if not ced:
            return
        st["cedula"] = ced
        st.pop("corrigiendo", None)
        _ir_a_confirmar(chat_id, st)
    elif paso == "punto":
        nombre_punto = texto[:100]
        dueno = db.buscar_punto(nombre_punto)
        if dueno and str(dueno.get("telegram_id")) != str(chat_id):
            _ofrecer_colaborar(chat_id, st, dueno, nombre_punto)   # punto ya censado → ofrecer colaborar
        else:
            _iniciar_ficha(chat_id, st, nombre_punto)  # punto nuevo → ficha obligatoria
    elif paso == "ficha":
        _registrar_ficha(chat_id, st, texto)
    elif paso == "nec1_item":
        st["nec1_item"] = texto[:80]
        st["paso"] = "nec1_meta"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=(f"🔢 ¿<b>Cuántos</b> necesitas de «{html.escape(st['nec1_item'])}»? "
                  "Número y unidad si aplica (ej. «100», «50 kg», «20 cajas»)."))
    elif paso == "nec1_meta":
        _crear_primera_necesidad(chat_id, st, texto)
    elif paso == "nec1_fin":
        # Texto suelto tras publicar la primera: lo tomamos como OTRO insumo que falta.
        st["nec1_item"] = texto[:80]
        st["paso"] = "nec1_meta"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=f"🔢 ¿<b>Cuántos</b> necesitas de «{html.escape(st['nec1_item'])}»?")
    elif paso == "pd_persona":
        p = texto.strip()[:80]
        st["pedido"]["persona"] = "" if p == "-" else p
        st["paso"] = "pd_desc"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="✍️ Cuéntame <b>qué pasó y dónde la viste por última vez</b> "
                 "(cualquier seña ayuda: edad, ropa, con quién estaba…). 📸 Si tienes "
                 "el <b>flyer o una foto</b>, mándalo y lo leo yo.")
    elif paso == "pd_desc":
        st["pedido"]["descripcion"] = texto[:600]
        st["paso"] = "pd_zona"
        if st["pedido"].get("cat") == "desaparecido":
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="📍 ¿En qué <b>zona</b> se le vio por última vez? "
                     "(estado, ciudad y sector)")
        else:
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="📍 ¿<b>Dónde</b> se necesita? (estado, ciudad y sector — mientras más "
                     "exacto, más rápido te ubica la red)")
    elif paso == "pd_zona":
        st["pedido"]["zona"] = texto[:120]
        st["paso"] = "pd_tel"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="📞 ¿A qué <b>número de teléfono / WhatsApp</b> te contactan? "
                 "(no se publica; solo lo ve quien puede ayudarte)")
    elif paso == "pd_tel":
        st["pedido"]["contacto"] = texto[:60]
        st["paso"] = "pd_nombre"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="👤 ¿Cuál es tu <b>nombre</b>? (si prefieres no decirlo, escribe «-»)")
    elif paso == "pd_nombre":
        n = texto.strip()[:80]
        st["pedido"]["nombre"] = "" if n == "-" else n
        _finalizar_pedido(chat_id, st)
    elif paso == "al_nombre":
        st.setdefault("aliado", {})["nombre"] = texto[:80]
        st["paso"] = "al_desc"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="✍️ ¿<b>Qué ofreces</b> exactamente? (ej. «almuerzos calientes para rescatistas», «presto linternas y baterías»)")
    elif paso == "al_desc":
        st["aliado"]["descripcion"] = texto[:200]
        st["paso"] = "al_zona"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="📍 ¿En qué <b>zona</b> ayudas? ¿Llevas a domicilio o van a ti? (ej. «San Francisco · llevo a domicilio»)")
    elif paso == "al_zona":
        st["aliado"]["zona"] = texto[:120]
        st["paso"] = "al_tel"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="📞 ¿Cuál es tu <b>número de contacto / WhatsApp</b>?")
    elif paso == "al_tel":
        st["aliado"]["contacto"] = texto[:40]
        st["paso"] = "al_horario"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🕐 ¿Cuándo estás <b>disponible</b>? (ej. «7am-3pm», «todo el día»). Si no aplica, escribe «-».")
    elif paso == "al_horario":
        h = texto.strip()[:60]
        st["aliado"]["horario"] = "" if h == "-" else h
        st["paso"] = "al_gps"
        _pedir_gps_aliado(chat_id)
    elif paso == "vo_nombre":
        st.setdefault("vol", {})["nombre"] = texto[:80]
        st["paso"] = "vo_zona"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="📍 ¿En qué <b>zona / sector</b> estás ayudando? (ej. «El Valle», «La Guaira, sector La Veguita»)")
    elif paso == "vo_zona":
        st["vol"]["zona"] = texto[:120]
        st["paso"] = "vo_tel"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="📞 ¿Cuál es tu <b>número de contacto / WhatsApp</b>? <i>(Es privado: no se publica en la web.)</i>")
    elif paso == "vo_tel":
        st["vol"]["contacto"] = texto[:40]
        _ir_a_confirmar_voluntario(chat_id, st)
    elif paso == "lla_sector":
        st.setdefault("lla", {})["sector"] = texto[:120]
        st["paso"] = "lla_contenido"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=("✍️ Ahora mándame el <b>mensaje del llamado</b>: qué se necesita y para qué. "
                  "Puedes adjuntar una <b>foto</b> (con el texto como pie de foto).\n\n"
                  "<i>Si pide insumos, lo cruzamos en privado con un acopio que los tenga.</i>"))
    elif paso == "sol_item":
        _buscar_y_mostrar(chat_id, st, texto)
    elif paso == "edit_captura_campo":
        valor = texto.strip()[:120]
        if valor == "-":
            valor = None
        cid, obj, rid, campo = st["cap_id"], st["obj"], st["rid"], st["campo"]
        if obj == "p":
            db.actualizar_persona_captura(rid, campo, valor)
        else:
            db.actualizar_item_captura(rid, campo, valor)
        estados.pop(chat_id, None)
        _tg("sendMessage", chat_id=chat_id, text="✅ Corregido.")
        _editor_fila(chat_id, cid, obj, rid)
    elif paso == "comp_cant":
        m = re.search(r"\d+", texto or "")
        if not m:
            _tg("sendMessage", chat_id=chat_id, text="Escríbeme solo el número (ej. 20) 🙂")
            return
        st["cant"] = int(m.group(0))
        st["paso"] = "comp_wa"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="📱 Déjame tu <b>WhatsApp</b> (con código de país, ej. +58 412...) para que el centro coordine la entrega contigo.")
    elif paso == "comp_wa":
        wa = (texto or "").strip()[:40]
        if sum(c.isdigit() for c in wa) < 7:
            _tg("sendMessage", chat_id=chat_id, text="Ese número no parece válido. Escríbelo con código de país (ej. +58 412 1234567) 🙂")
            return
        nid = st["nid"]
        cant = st.get("cant", 1)
        nombre = st.get("nombre", "Voluntario")
        estados.pop(chat_id, None)
        cmpid, nec2 = db.crear_compromiso(nid, chat_id, nombre, cant)
        if not cmpid:
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="🙏 ¡Gracias! Justo se acaba de cubrir lo que faltaba, así que no hace falta más por ahora.")
            return
        _notificar_centro_compromiso(nec2, cant, nombre, wa)
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=(f"✅ ¡Gracias, {html.escape(nombre)}! Anotamos que llevarás <b>{cant}</b> a "
                  f"<b>{html.escape((nec2 or {}).get('punto') or 'el centro')}</b>.\n\n"
                  "El centro recibirá tu WhatsApp para coordinar. Si al final no puedes, no pasa nada: "
                  "tu reserva se libera sola en 12 h."))
    elif paso == "recibi_cant":
        m = re.search(r"\d+", texto or "")
        if not m:
            _tg("sendMessage", chat_id=chat_id, text="Escríbeme solo el número que recibiste 🙂")
            return
        nid = st["nid"]
        estados.pop(chat_id, None)
        cantr = int(m.group(0))
        nec2 = db.registrar_recibido(nid, cantr, quien=f"tg:{chat_id}")
        if not nec2:
            _tg("sendMessage", chat_id=chat_id, text="No encontré ese requerimiento.")
            return
        if nec2.get("estado") == "resuelta":
            extra = "\n\n🎉 ¡Con esto se cubrió la meta! Ya sale de la lista de «lo que hace falta»."
        else:
            meta = nec2.get("meta")
            falta = max(0, (meta or 0) - (nec2.get("recibido") or 0) - db.en_camino_de(nid)) if meta else None
            extra = (f"\n\nVas en <b>{nec2.get('recibido') or 0}</b> recibido(s)"
                     + (f", faltan <b>{falta}</b>." if falta is not None else "."))
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=f"✅ Anotado: recibiste <b>{cantr}</b> de <b>{html.escape(nec2.get('item') or '')}</b>.{extra}")
    elif paso == "resp_lla_msg":
        m = (texto or "").strip()
        st["mensaje"] = None if m == "-" else m[:200]
        st["paso"] = "resp_lla_wa"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="📱 Déjame tu <b>WhatsApp</b> (con código de país) para que el voluntario coordine contigo.")
    elif paso == "resp_lla_wa":
        wa = (texto or "").strip()[:40]
        if sum(c.isdigit() for c in wa) < 7:
            _tg("sendMessage", chat_id=chat_id, text="Ese número no parece válido. Escríbelo con código de país (ej. +58 412 1234567) 🙂")
            return
        lid = st["lid"]
        nombre = st.get("nombre", "Voluntario")
        mensaje = st.get("mensaje")
        estados.pop(chat_id, None)
        db.responder_llamado(lid, chat_id, nombre, wa, mensaje)
        _notificar_voluntario_respuesta(db.llamado(lid), nombre, wa, mensaje)
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=(f"✅ ¡Gracias, {html.escape(nombre)}! Le pasamos tu WhatsApp a quien levantó el llamado "
                  "para que coordine contigo. 🤝"))
    else:
        _tg("sendMessage", chat_id=chat_id, text="Usa los botones de arriba para continuar 🙂")


# ---------------------------------------------------------------------------
# INVENTARIO DE 3 ESTADOS: el centro publica meta (vía /necesito), confirma recibido
# (/recibi), y cualquiera desde la web se compromete a llevar (deep link /start nec_<id>).
# ---------------------------------------------------------------------------
def _pedir_primera_necesidad(chat_id, rid):
    """Siembra post-alta: pide EN CALIENTE la primera necesidad con meta del punto.
    Es lo que mueve la red (la web con barra, el botón «Puedo llevar esto» y el
    puente demanda→oferta solo actúan sobre necesidades publicadas con meta).
    Persiste el estado directo en efimero: puede llamarse desde el chat del grupo
    de moderadores (aprobación manual), donde _con_estado no cubre este chat_id."""
    estados[chat_id] = {"paso": "nec1_item", "nec1_rid": rid}
    efimero.estado_tg_set(str(chat_id), estados[chat_id])
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=("🙏 Una pregunta más, la <b>más importante</b>:\n\n"
              "¿Qué <b>insumo le falta más</b> a tu punto ahora mismo? Escribe UNO "
              "(ej. «agua potable», «pañales talla G»).\n\n"
              "Lo publicamos con barra de avance y botón <b>«Puedo llevar esto»</b>, "
              "y le avisamos a los acopios que lo tengan. Así la ayuda llega a donde "
              "de verdad hace falta."),
        reply_markup=_kb([[{"text": "⏭️ Ahora no", "callback_data": "nec1|skip"}]]))


def _crear_primera_necesidad(chat_id, st, texto_meta):
    """Crea la necesidad sembrada con el MISMO cableado que /necesito: queda en la
    web (barra tricolor + deep link «Puedo llevar esto») y el puente demanda→oferta
    avisa a los acopios que tienen el insumo."""
    rep = db.representante(st.get("nec1_rid") or 0) or db.representante_por_tgid(chat_id)
    item = (st.get("nec1_item") or "").strip()
    if not rep or not item:
        estados.pop(chat_id, None)
        return
    meta, unidad = db.parse_cantidad(texto_meta)
    if not meta:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="No entendí la cantidad 🙈 Escribe solo el número (ej. <b>100</b>) "
                 "o número y unidad (ej. <b>50 kg</b>).",
            reply_markup=_kb([[{"text": "⏭️ Saltar esto", "callback_data": "nec1|skip"}]]))
        return
    nid = db.abrir_necesidad(rep["id"], rep.get("punto_id"), rep.get("punto"),
                             rep.get("tipo_punto"), item, meta=meta, unidad=unidad)
    if nid:
        # Puente demanda→oferta: mismo hilo best-effort que /necesito.
        threading.Thread(target=_avisar_acopios_demanda,
                         args=(dict(rep), [(nid, item, meta, unidad)]), daemon=True).start()
    falta = f"{meta} {unidad}".strip() if unidad else str(meta)
    st["paso"] = "nec1_fin"
    st.pop("nec1_item", None)
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"✅ Publicado: <b>{html.escape(item)}</b> — faltan <b>{html.escape(str(falta))}</b>.\n\n"
              "Ya está en la web y le avisamos a la red. Cuando te llegue algo, "
              "regístralo con /recibi para que el avance se vea."),
        reply_markup=_kb([[{"text": "➕ Me falta algo más", "callback_data": "nec1|more"},
                           {"text": "✅ Listo", "callback_data": "nec1|done"}]]))


def _demanda_a_necesidades(cid, chat_id, rep=None):
    """Tras confirmar una captura de DEMANDA, vuelca sus ítems a la tabla `necesidades`
    con meta/unidad (parseadas de la cantidad que leyó la IA). No duplica (abrir_necesidad).
    `rep` opcional: pásalo cuando quien confirma no tiene telegram_id (ej. bot_wa.py)."""
    cap = db.captura(int(cid))
    if not cap or cap.get("clase") != "demanda":
        return 0
    rep = rep or db.representante_por_tgid(chat_id)
    det = db.captura_detalle(int(cid))
    if not rep or not det:
        return 0
    n = 0
    creadas = []
    for it in det["items"]:
        item = (it.get("item") or "").strip()
        if not item:
            continue
        meta, unidad = db.parse_cantidad(it.get("cantidad"))
        nid = db.abrir_necesidad(rep["id"], rep.get("punto_id"), rep.get("punto"),
                                 rep.get("tipo_punto"), item,
                                 meta=meta, unidad=unidad, categoria=it.get("categoria"))
        n += 1
        if nid:
            creadas.append((nid, item, meta, unidad))
    if creadas:
        # Puente demanda→oferta: avisar a los acopios que TIENEN estos insumos
        # (antes la necesidad solo quedaba publicada en la web esperando visitas).
        threading.Thread(target=_avisar_acopios_demanda, args=(dict(rep), creadas),
                         daemon=True).start()
    return n


def _avisar_acopios_demanda(rep, creadas):
    """Puente demanda→oferta: al declarar necesidades (/necesito) avisa a los
    acopios cuyo inventario cubre cada ítem (mismo match de /solicitar:
    db.buscar_oferta, cercanos primero si hay GPS). Corre en un HILO (varios
    sendMessage; NO toca `estados`). Anti-spam: máx 5 acopios por necesidad y
    1 aviso por (necesidad, acopio) cada 24 h (efimero). Avisa por Telegram O
    WhatsApp (plantilla aprobada si el acopio está fuera de la ventana de 24h,
    mismo patrón que _triangular_llamado). Best-effort."""
    try:
        mylat, mylon = rep.get("lat"), rep.get("lon")
        avisos = {}  # (canal, destino) -> [(nid, item, meta, unidad)]
        for nid, item, meta, unidad in creadas:
            ofertas = [a for a in db.buscar_oferta(item)
                       if (a.get("dueno_tgid") or a.get("dueno_rep_id"))
                       and a.get("punto_id") != rep.get("punto_id")]
            for a in ofertas:
                a["dist"] = _dist_km(mylat, mylon, a.get("lat"), a.get("lon"))
            ofertas.sort(key=lambda a: (a["dist"] is None,
                                        a["dist"] if a["dist"] is not None else 9e9))
            for a in ofertas[:5]:
                if not efimero.cooldown_ok("nec_aviso", f"{nid}|{a['punto_id']}", 86400):
                    continue
                if a.get("dueno_tgid"):
                    clave = ("tg", a["dueno_tgid"])
                else:
                    wa = db.wa_de_rep(a.get("dueno_rep_id"))
                    if not wa:
                        continue
                    clave = ("wa", wa)
                avisos.setdefault(clave, []).append((nid, item, meta, unidad))
        for (canal, destino), necs in avisos.items():
            if canal == "tg":
                _avisar_demanda_tg(destino, rep, necs)
            else:
                _avisar_demanda_wa(destino, rep, necs)
    except Exception as e:  # noqa: BLE001
        print("avisar_acopios_demanda error:", e)


def _avisar_demanda_tg(dt, rep, necs):
    L = [f"📦 <b>{html.escape(rep.get('punto') or 'Un punto de la red')}</b> "
         "necesita insumos que tú tienes publicados:"]
    filas = []
    for nid, item, meta, unidad in necs[:6]:
        cant = f" — meta {meta:g} {unidad or 'unidades'}" if meta else ""
        L.append(f"• <b>{html.escape(item)}</b>{html.escape(cant)}")
        filas.append([{"text": ("🤝 Puedo llevar: " + item)[:40],
                       "callback_data": f"nec|{nid}"}])
    L.append("\nToca el insumo que puedas cubrir y te digo cuánto falta."
             + _coletilla(", ".join(x[1] for x in necs)))
    # Salida silenciosa (paridad con WhatsApp): quien no puede, cierra y ya —
    # ni el punto que necesita ni nadie más se entera de la negativa.
    filas.append([{"text": "🛑 Ahora no puedo ayudar", "callback_data": "necno|0"}])
    _tg("sendMessage", chat_id=dt, parse_mode="HTML",
        text="\n".join(L)[:4000], reply_markup=_kb(filas))


def _avisar_demanda_wa(wa, rep, necs):
    """Mismo aviso que _avisar_demanda_tg pero por WhatsApp. La plantilla aprobada
    solo trae 2 botones de texto FIJO ('Puedo ayudar'/'Ahora no puedo', no se puede
    personalizar por insumo), así que se manda UN mensaje por insumo (para llevar el
    nid exacto en el payload); son pocos por demanda declarada y hay cooldown de
    24h por (necesidad, acopio) ya aplicado arriba."""
    dest = _wa_dest(wa)
    if not dest:
        return
    punto = rep.get("punto") or "Un punto de la red"
    for nid, item, meta, unidad in necs[:6]:
        cant = f" (meta {meta:g} {unidad or 'unidades'})" if meta else ""
        botones = [(f"nec|{nid}", "🤝 Puedo ayudar"), (f"necno|{nid}", "🛑 Ahora no puedo")]
        try:
            if WA_TPL_LLAMADO:
                r = wa_api.enviar_plantilla(
                    dest, WA_TPL_LLAMADO,
                    variables=(punto, item + cant, "un punto de la red"),
                    payloads=[b[0] for b in botones])
                if r.get("messages"):
                    continue
                print("demanda wa: plantilla rechazada, fallback a mensaje normal:", r)
            cuerpo = (f"📦 *{punto}* necesita *{item}*{cant} y tú lo tienes publicado.\n\n"
                      "Si puedes llevarlo, toca el botón y te digo cuánto falta.")
            wa_api.enviar_botones(dest, cuerpo, botones)
        except Exception as e:  # noqa: BLE001
            print("avisar demanda wa error:", e)


def _iniciar_compromiso(chat_id, payload, frm):
    """Deep link desde la web: /start nec_<id>. Cualquiera puede ofrecerse a llevar, sin registro."""
    try:
        nid = int(payload.split("_", 1)[1])
    except (ValueError, IndexError):
        _tg("sendMessage", chat_id=chat_id, text="Enlace no válido. Abre el botón desde la web 🙂")
        return
    nec = db.necesidad(nid)
    if not nec or nec.get("estado") in ("resuelta", "cancelada"):
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🙏 Esa necesidad ya está <b>cubierta</b> o cerrada. ¡Gracias por querer ayudar! Mira otras en redacopioyrefugio.com")
        return
    meta = nec.get("meta")
    falta = max(0, (meta or 0) - (nec.get("recibido") or 0) - db.en_camino_de(nid)) if meta else None
    if meta and falta <= 0:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🙏 Esa necesidad ya está <b>cubierta</b> (todo lo pedido ya está recibido o en camino). ¡Gracias!")
        return
    nombre = ((frm.get("first_name") or "").strip() or "Voluntario")[:60]
    estados[chat_id] = {"paso": "comp_cant", "nid": nid, "nombre": nombre}
    unidad = nec.get("unidad") or "unidades"
    cuanto = (f"Faltan <b>{falta} {html.escape(unidad)}</b>. " if falta is not None else "")
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"🤝 <b>{html.escape(nec.get('punto') or 'Un centro')}</b> necesita: "
              f"<b>{html.escape(nec.get('item') or '')}</b>.\n{cuanto}\n"
              f"¿<b>Cuántas {html.escape(unidad)}</b> puedes llevar? Escribe solo el número."))


def _notificar_centro_compromiso(nec, cant, nombre, wa):
    """Avisa al centro que alguien va a llevar `cant`, con el WhatsApp del voluntario.
    Por Telegram si el centro tiene telegram_id; si no, por WhatsApp (mismo botón
    'Ya lo recibí' — bot_wa.py ya sabe procesar el payload recibi|)."""
    if not nec or nec.get("punto_id") is None:
        return
    dueno = db.dueno_de_punto(nec.get("punto_id"))
    if not dueno:
        return
    dt = dueno.get("telegram_id")
    if dt:
        filas = []
        wabtn = _wa_btn(f"💬 WhatsApp a {nombre}", wa)
        if wabtn:
            filas.append(wabtn)
        filas.append([{"text": "✅ Ya lo recibí", "callback_data": f"recibi|{nec['id']}"}])
        _tg("sendMessage", chat_id=dt, parse_mode="HTML",
            text=(f"🤝 <b>{html.escape(nombre)}</b> se ofreció a llevar <b>{cant}</b> de "
                  f"<b>{html.escape(nec.get('item') or '')}</b> a tu punto.\n"
                  f"📱 WhatsApp: <b>{html.escape(wa)}</b>\n\n"
                  "Coordina la entrega. Cuando lo recibas, toca «Ya lo recibí» para actualizar la barra."),
            reply_markup=_kb(filas))
        return
    dwa = _wa_dest(dueno.get("wa_phone") or "")
    if not dwa:
        return
    try:
        cuerpo = (f"🤝 *{nombre}* se ofreció a llevar *{cant}* de *{nec.get('item') or ''}* a tu punto.\n"
                  f"📱 WhatsApp: *{wa}*\n\n"
                  "Coordina la entrega directo con ese número. Cuando lo recibas, toca «Ya lo recibí».")
        wa_api.enviar_botones(dwa, cuerpo, [(f"recibi|{nec['id']}", "✅ Ya lo recibí")])
    except Exception as e:  # noqa: BLE001
        print("notificar centro compromiso wa error:", e)


def _cmd_recibi(chat_id):
    rep = db.representante_por_tgid(chat_id)
    if not rep or rep["estado"] != "aprobado":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🔒 <b>/recibi</b> es solo para <b>centros</b>.\n\n" + _hint_rol(chat_id))
        return
    necs = [n for n in db.necesidades_de_punto(rep_id=rep["id"], punto_id=rep.get("punto_id")) if n.get("meta")]
    if not necs:
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="No tienes requerimientos con meta abiertos. Publica lo que necesitas con /necesito 🙂")
        return
    filas = [[{"text": f"{(n['item'] or '')[:32]} ({n.get('recibido') or 0}/{n['meta']})",
               "callback_data": f"recibi|{n['id']}"}] for n in necs[:20]]
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text="📥 <b>¿Qué recibiste?</b> Toca el requerimiento para registrar lo que llegó.",
        reply_markup=_kb(filas))


def _iniciar_respuesta_llamado(chat_id, payload, frm):
    """Deep link desde la web: /start lla_<id>. Cualquiera puede ofrecerse a atender un llamado."""
    try:
        lid = int(payload.split("_", 1)[1])
    except (ValueError, IndexError):
        _tg("sendMessage", chat_id=chat_id, text="Enlace no válido. Abre el botón desde la web 🙂")
        return
    lla = db.llamado(lid)
    if not lla or lla.get("estado") != "activo":
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🙏 Ese llamado ya se cerró. ¡Gracias por querer ayudar! Mira otros en redacopioyrefugio.com")
        return
    nombre = ((frm.get("first_name") or "").strip() or "Voluntario")[:60]
    estados[chat_id] = {"paso": "resp_lla_msg", "lid": lid, "nombre": nombre}
    sector = lla.get("sector") or "el sector en emergencia"
    _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
        text=(f"🙋 Vas a responder al llamado de <b>{html.escape(sector)}</b>:\n"
              f"<i>{html.escape((lla.get('mensaje') or '')[:300])}</i>\n\n"
              "✍️ ¿<b>Cómo puedes ayudar</b>? Escríbelo en una línea (ej. «llevo agua y voy con camioneta»). "
              "Si solo quieres ofrecerte, escribe «-»."))


def _notificar_voluntario_respuesta(lla, nombre, contacto, mensaje):
    """Avisa al voluntario que levantó el llamado que alguien se ofreció, con su WhatsApp."""
    if not lla or not lla.get("telegram_id"):
        return
    filas = []
    wabtn = _wa_btn(f"💬 WhatsApp a {nombre}", contacto)
    if wabtn:
        filas.append(wabtn)
    cuerpo = (f"🙋 <b>{html.escape(nombre)}</b> respondió a tu llamado"
              + (f" de <b>{html.escape(lla.get('sector'))}</b>" if lla.get("sector") else "") + ".\n"
              + (f"Ofrece: <i>{html.escape(mensaje)}</i>\n" if mensaje else "")
              + f"📱 WhatsApp: <b>{html.escape(contacto)}</b>\n\nCoordina con esta persona la ayuda. 🤝" + _coletilla(mensaje))
    if filas:
        _tg("sendMessage", chat_id=lla["telegram_id"], parse_mode="HTML", text=cuerpo, reply_markup=_kb(filas))
    else:
        _tg("sendMessage", chat_id=lla["telegram_id"], parse_mode="HTML", text=cuerpo)


def _solicitud_web(lid):
    """Datos PRIVADOS de un pedido web (nombre + contacto). Nunca van a la web pública."""
    con = db.conectar()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS solicitudes_web ("
            "llamado_id INTEGER PRIMARY KEY, nombre TEXT, contacto TEXT, "
            "categoria TEXT, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)")
        con.commit()
        row = con.execute(
            "SELECT * FROM solicitudes_web WHERE llamado_id = ?", (lid,)).fetchone()
    except Exception:
        row = None
    con.close()
    return dict(row) if row else None


def _llamado_row(lid):
    con = db.conectar()
    row = con.execute("SELECT * FROM llamados WHERE id = ?", (lid,)).fetchone()
    con.close()
    return dict(row) if row else None


# --- Retiro voluntario de la plataforma (estado 'retirado': sale de la web y de los avisos) ---
_ROLES_RETIRO = (
    ("centro", "🏠 Centro de acopio", db.representante_por_tgid, db.set_estado_representante),
    ("voluntario", "📣 Voluntariado", db.voluntario_por_tgid, db.set_estado_voluntario),
    ("aliado", "🤝 Manos Solidarias", db.aliado_por_tgid, db.set_estado_aliado),
)


def _fichas_retiro(chat_id, estados_busca):
    """Fichas de este chat cuyo estado esté en estados_busca: [(rol, etiqueta, ficha, setter)]."""
    out = []
    for rol, etiqueta, getter, setter in _ROLES_RETIRO:
        f = getter(chat_id)
        if f and f.get("estado") in estados_busca:
            out.append((rol, etiqueta, f, setter))
    return out


def _cmd_retirarme(chat_id):
    """Pide confirmación antes de retirar; si ya está retirado, ofrece volver."""
    activas = _fichas_retiro(chat_id, ("aprobado", "pendiente"))
    if activas:
        lineas = []
        for rol, etiqueta, f, _st in activas:
            nombre = f.get("punto") or f.get("nombre") or ""
            lineas.append("• " + etiqueta + (f" — {html.escape(nombre)}" if nombre else ""))
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="Estás en la red como:\n" + "\n".join(lineas) +
                 "\n\nSi te retiras, dejas de aparecer en la web y de recibir avisos de la red. "
                 "Puedes volver cuando quieras.\n\n¿Confirmas tu retiro?",
            reply_markup=_kb([[{"text": "✅ Sí, retirarme", "callback_data": "ret|si"},
                               {"text": "↩️ Seguir en la red", "callback_data": "ret|no"}]]))
        return
    if _fichas_retiro(chat_id, ("retirado",)):
        _tg("sendMessage", chat_id=chat_id,
            text="Tu registro ya está retirado de la plataforma 🕊️. ¿Quieres volver a activarlo?",
            reply_markup=_kb([[{"text": "🔄 Volver a la red", "callback_data": "ret|volver"}]]))
        return
    _tg("sendMessage", chat_id=chat_id,
        text="No encontré un registro activo tuyo en la plataforma 🙂. "
             "Si quieres sumarte, escribe /start.")


def manejar_callback(cq):
    """Punto de entrada del main-loop: corre el handler con estado persistente."""
    _con_estado((cq.get("message") or {}).get("chat", {}).get("id"),
                _manejar_callback, cq)


def _manejar_callback(cq):
    data = cq.get("data", "")
    msg = cq.get("message", {}) or {}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    cqid = cq["id"]

    if data.startswith("necno|"):  # aviso de demanda: declina EN SILENCIO (paridad con WA)
        _tg("answerCallbackQuery", callback_query_id=cqid, text="Cerrado 🙏")
        _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
            text=(msg.get("text", "") + "\n\n🚫 Cerraste este aviso — gracias por revisar. "
                  "Seguimos buscando quién pueda cubrirlo."))
        return

    if data.startswith("nec|"):  # acopio avisado: "puedo llevar" → flujo de compromiso
        _tg("answerCallbackQuery", callback_query_id=cqid)
        _iniciar_compromiso(chat_id, "nec_" + data.split("|", 1)[1], cq.get("from") or {})
        return

    if data.startswith("recibi|"):   # el centro registra lo recibido de un requerimiento
        try:
            _, nid = data.split("|")
        except ValueError:
            return
        nec = db.necesidad(int(nid))
        if not nec:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No encontrado")
            return
        _tg("answerCallbackQuery", callback_query_id=cqid)
        estados[chat_id] = {"paso": "recibi_cant", "nid": int(nid)}
        unidad = nec.get("unidad") or "unidades"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=f"📥 <b>{html.escape(nec.get('item') or '')}</b> — ¿<b>cuántas {html.escape(unidad)}</b> recibiste? Escribe el número.")
        return

    if data.startswith("cmd|"):  # menú inline de comandos (mismo efecto que escribirlos)
        _tg("answerCallbackQuery", callback_query_id=cqid)
        accion = data.split("|", 1)[1]
        if accion == "inv":
            _cmd_inventario(chat_id)
        elif accion == "nec":
            _cmd_necesito(chat_id)
        elif accion == "ubi":
            _cmd_ubicacion(chat_id)
        elif accion == "sol":
            _cmd_solicitar(chat_id)
        elif accion == "mid":
            _cmd_miinventario(chat_id)
        elif accion == "fic":
            _cmd_ficha(chat_id)
        elif accion == "lla":
            _cmd_llamado(chat_id)
        return

    if data.startswith("lno|"):  # el avisado NO puede ayudar → el aviso muere aquí, en silencio.
        # NO se notifica al solicitante ni a moderadores: solo cierra el ciclo puntual de
        # esta notificación (decisión de Gustavo 2026-07-03; mismo patrón que WA «Ahora no puedo»).
        aid = data.split("|", 1)[1]
        av = db.aviso_llamado(int(aid)) if aid.isdigit() else None
        if not av or str(chat_id) != str(av.get("destino_tgid")):
            _tg("answerCallbackQuery", callback_query_id=cqid)
            return
        _tg("answerCallbackQuery", callback_query_id=cqid, text="Cerrado 🙏")
        _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
            text=(msg.get("text", "") + "\n\n🚫 Cerraste este aviso — gracias por revisar. "
                  "Seguimos buscando quién pueda cubrirlo."))
        return

    if data.startswith("lvin|"):  # acopio/MS/voluntario confirma el vínculo → cierra el pedido
        _tg("answerCallbackQuery", callback_query_id=cqid)
        aid = data.split("|", 1)[1]
        av = db.aviso_llamado(int(aid)) if aid.isdigit() else None
        if not av:
            _tg("sendMessage", chat_id=chat_id, text="Esta solicitud ya no está disponible.")
            return
        if str(chat_id) != str(av.get("destino_tgid")):
            _tg("sendMessage", chat_id=chat_id, text="No autorizado.")
            return
        lla = db.cerrar_llamado_por_vinculo(av["llamado_id"], av.get("quien"), None, av.get("items"))
        if not lla:
            _tg("sendMessage", chat_id=chat_id, text="Ese pedido ya no está disponible.")
            return
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="🤝 ¡Gracias! Registramos que coordinaste la ayuda. El pedido queda cerrado "
                 "y saldrá de la web en unos minutos. 🙌")
        if lla.get("telegram_id"):
            _tg("sendMessage", chat_id=lla["telegram_id"], parse_mode="HTML",
                text=f"🤝 <b>{html.escape(av.get('quien') or 'Alguien de la red')}</b> confirmó que "
                     "coordinó la ayuda de tu llamado. Lo marcamos como cubierto. 🙌")
        if GRUPO_MODERADORES:
            _tg("sendMessage", chat_id=GRUPO_MODERADORES, parse_mode="HTML",
                text=f"🤝 Vínculo hecho: <b>{html.escape(av.get('quien') or '?')}</b> cubre el pedido "
                     f"#{av['llamado_id']} ({html.escape(av.get('items') or '')}) en "
                     f"{html.escape(av.get('sector') or '¿?')}. Se cierra en la web.")
        return

    if data.startswith("lresp|"):  # acopio/MS toca "Responder al voluntario" → respuestas WhatsApp
        _tg("answerCallbackQuery", callback_query_id=cqid)
        aid = data.split("|", 1)[1]
        av = db.aviso_llamado(int(aid)) if aid.isdigit() else None
        if not av:
            _tg("sendMessage", chat_id=chat_id, text="Esta solicitud ya no está disponible.")
            return
        if str(chat_id) != str(av.get("destino_tgid")):
            _tg("sendMessage", chat_id=chat_id, text="No autorizado.")
            return
        filas = _botones_responder(av)
        if not filas:
            _tg("sendMessage", chat_id=chat_id, text="No hay un número de WhatsApp válido para responder.")
            return
        # «Ya coordiné» aparece recién AQUÍ (después de aceptar responder), no en el
        # aviso inicial: quien no tiene nada que ver cierra con «No puedo ayudar».
        filas.append([{"text": "🤝 Ya coordiné la ayuda", "callback_data": f"lvin|{aid}"}])
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=("Elige tu respuesta — se abrirá WhatsApp con el mensaje ya escrito para quien pide. "
                  "Cuando coordinen la entrega, toca <b>Ya coordiné la ayuda</b> y cierro el pedido en la web:"
                  + _coletilla(av.get("items"))),
            reply_markup=_kb(filas))
        return

    if data.startswith("sol|"):  # el solicitante elige a qué acopio pedirle (los ítems que cubre)
        _tg("answerCallbackQuery", callback_query_id=cqid)
        st = estados.get(chat_id)
        pid = data.split("|", 1)[1]
        if not st or st.get("paso") != "sol_pick" or pid not in (st.get("ops") or {}):
            _tg("sendMessage", chat_id=chat_id, text="La búsqueda expiró. Usa /solicitar de nuevo 🙂")
            return
        enviadas = st.setdefault("enviadas", [])
        if pid in enviadas:
            _tg("sendMessage", chat_id=chat_id, text="Ya le enviaste una solicitud a ese acopio 🙂")
            return
        r = st["ops"][pid]
        item = ", ".join(r.get("cubre") or [])
        solicitante = db.representante_por_tgid(chat_id)
        if not solicitante:
            return
        sid = db.crear_solicitud(solicitante["id"], r.get("dueno_rep_id"), r["punto_id"], item)
        enviadas.append(pid)
        contacto = r.get("contacto")
        wa = _wa_btn(f"💬 WhatsApp a {r['punto'] or 'acopio'}"[:40], contacto)
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=(f"✅ Solicitud de <b>{html.escape(item)}</b> enviada a <b>{html.escape(r['punto'] or 'el acopio')}</b>.\n"
                  + (f"📞 Su contacto: <b>{html.escape(contacto)}</b>\n" if contacto else "")
                  + "Te aviso por aquí si responden. Puedes solicitar a otro acopio con los botones de arriba."),
            reply_markup=_kb([wa]) if wa else None)
        if r.get("dueno_tgid"):
            _notificar_acopio(r["dueno_tgid"], solicitante, item, sid)
        return

    if data.startswith("sola|"):  # "Solicitar a todos los necesarios" (plan mínimo de acopios)
        _tg("answerCallbackQuery", callback_query_id=cqid)
        st = estados.get(chat_id)
        if not st or st.get("paso") != "sol_pick":
            _tg("sendMessage", chat_id=chat_id, text="La búsqueda expiró. Usa /solicitar de nuevo 🙂")
            return
        plan = st.get("plan_min") or []
        solicitante = db.representante_por_tgid(chat_id)
        if not solicitante or not plan:
            _tg("sendMessage", chat_id=chat_id, text="No hay acopios cercanos que solicitar 🙂")
            return
        enviadas = st.setdefault("enviadas", [])
        hechos, filas = [], []
        for pid in plan:
            if pid in enviadas:
                continue
            r = (st.get("ops") or {}).get(pid)
            if not r:
                continue
            item = ", ".join(r.get("cubre") or [])
            sid = db.crear_solicitud(solicitante["id"], r.get("dueno_rep_id"), r["punto_id"], item)
            enviadas.append(pid)
            hechos.append(r.get("punto") or "Acopio")
            wa = _wa_btn(f"💬 WhatsApp a {r.get('punto') or 'acopio'}"[:40], r.get("contacto"))
            if wa:
                filas.append(wa)
            if r.get("dueno_tgid"):
                _notificar_acopio(r["dueno_tgid"], solicitante, item, sid)
        if hechos:
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=("✅ Solicitudes enviadas a: <b>" + html.escape(", ".join(hechos)) + "</b>.\n"
                      "Te aviso por aquí si responden. Contáctalos directo:"),
                reply_markup=_kb(filas) if filas else None)
        else:
            _tg("sendMessage", chat_id=chat_id, text="Ya habías solicitado a esos acopios 🙂")
        return

    if data.startswith("solb|"):  # Plan B: avisar a coordinadores + publicar lo que no hay cerca
        _tg("answerCallbackQuery", callback_query_id=cqid)
        st = estados.get(chat_id)
        if not st or st.get("paso") != "sol_pick":
            _tg("sendMessage", chat_id=chat_id, text="La búsqueda expiró. Usa /solicitar de nuevo 🙂")
            return
        if st.get("escalado"):
            _tg("sendMessage", chat_id=chat_id, text="Ya avisé a los coordinadores y publiqué lo que falta 👍")
            return
        falta = st.get("sin_cerca") or st.get("pedido") or []
        if not falta:
            _tg("sendMessage", chat_id=chat_id, text="No hay nada pendiente por escalar 🙂")
            return
        rep = db.representante_por_tgid(chat_id)
        if not rep:
            return
        st["escalado"] = True
        falta_txt = ", ".join(falta)
        # 1) poner cada insumo EN SEGUIMIENTO (aparece en la web 🔎 Buscando; se avisa si aparece)
        for it in falta:
            db.abrir_necesidad(rep["id"], rep.get("punto_id"), rep.get("punto"), rep.get("tipo_punto"), it)
        # 2) registrar la solicitud escalada
        sid = db.crear_solicitud(rep["id"], None, None, falta_txt, detalle="sin acopio cercano")
        db.set_estado_solicitud(sid, "escalada")
        # 3) avisar al grupo de coordinadores (si hay)
        if GRUPO_MODERADORES:
            ubic = rep.get("ubicacion") or ""
            cont = rep.get("telefono") or ""
            tipo_txt = TIPOS.get(rep.get("tipo_punto"), "Punto")
            _tg("sendMessage", chat_id=GRUPO_MODERADORES, parse_mode="HTML",
                text=(f"📢 <b>Necesidad sin acopio cercano</b>\n\n"
                      f"{tipo_txt}: <b>{html.escape(rep.get('punto') or '')}</b>\n"
                      f"Necesita: <b>{html.escape(falta_txt)}</b>"
                      + (f"\n📍 {html.escape(ubic)}" if ubic else "")
                      + (f"\n📞 {html.escape(cont)}" if cont else "")))
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=(f"📢 Listo. Dejé <b>{html.escape(falta_txt)}</b> <b>en seguimiento</b> (aparece en la web)"
                  + (" y avisé a los coordinadores" if GRUPO_MODERADORES else "")
                  + ".\n🔔 Te aviso automáticamente si aparece en un acopio cercano. También puedes "
                  "contactar a los acopios más lejanos de la lista."))
        return

    if data.startswith("msq|"):  # solicitud de contacto (web) a una Mano Solidaria
        try:
            _, sid, dec = data.split("|")
        except ValueError:
            return
        import contacto_ms
        sol = contacto_ms.solicitud(int(sid))
        if not sol or sol["canal"] != "tg" or str(sol["destino"]) != str(chat_id):
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Ya no disponible")
            return
        if sol["estado"] != "pendiente":
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Ya respondiste esta solicitud")
            return
        base = msg.get("text", "")
        if dec == "si":
            info = contacto_ms.responder(int(sid), True)
            _tg("answerCallbackQuery", callback_query_id=cqid, text="¡Gracias!")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + "\n\n✅ Aceptaste — contactos compartidos")
            if info:
                kb = (_kb([[{"text": "💬 Escribirle por WhatsApp", "url": info["wa_link"]}]])
                      if info.get("wa_link") else None)
                _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                    text=("🤝 ¡Gracias! Este es el contacto de la persona:\n"
                          f"👤 <b>{html.escape(info.get('nombre') or '')}</b>\n"
                          f"📞 <b>{html.escape(info.get('contacto') or '')}</b>\n\n"
                          "Ella también recibió tu número. Coordinen en lugares seguros. 💙"),
                    reply_markup=kb)
        else:
            contacto_ms.responder(int(sid), False)
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Anotado")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + "\n\n❌ Respondiste: ahora no puedo")
        return

    if data.startswith("solr|"):  # el acopio responde a una solicitud
        try:
            _, sid, dec = data.split("|")
        except ValueError:
            return
        sol = db.solicitud(int(sid))
        rep = db.representante_por_tgid(chat_id)
        if not sol or not rep or rep["id"] != sol["a_rep"]:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No válido")
            return
        base = msg.get("text", "")
        solicitante = db.representante(sol["de_rep"])
        acopio = db.dueno_de_punto(sol["a_punto_id"]) or {}
        if dec == "ok":
            db.set_estado_solicitud(int(sid), "aceptada")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="¡Gracias!")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + "\n\n✅ Respondiste: Puedo ayudar")
            if solicitante and solicitante.get("telegram_id"):
                cont = acopio.get("telefono")
                wa = _wa_btn(f"💬 WhatsApp a {acopio.get('punto') or 'acopio'}"[:40], cont)
                _tg("sendMessage", chat_id=solicitante["telegram_id"], parse_mode="HTML",
                    text=(f"✅ <b>{html.escape(acopio.get('punto') or 'El acopio')}</b> puede ayudarte con "
                          f"<b>{html.escape(sol['item'] or '')}</b>."
                          + (f"\n📞 Contáctalo: <b>{html.escape(cont)}</b>" if cont else "")),
                    reply_markup=_kb([wa]) if wa else None)
        else:
            db.set_estado_solicitud(int(sid), "rechazada")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Anotado")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + "\n\n❌ Respondiste: No tengo ahora")
            # El acopio no tiene → queda EN SEGUIMIENTO: el sistema avisará si aparece cerca.
            if solicitante:
                for it in [x.strip() for x in (sol.get("item") or "").split(",") if x.strip()]:
                    db.abrir_necesidad(solicitante["id"], solicitante.get("punto_id"),
                                       solicitante.get("punto"), solicitante.get("tipo_punto"), it)
            if solicitante and solicitante.get("telegram_id"):
                _tg("sendMessage", chat_id=solicitante["telegram_id"], parse_mode="HTML",
                    text=(f"ℹ️ <b>{html.escape(acopio.get('punto') or 'El acopio')}</b> no tiene "
                          f"<b>{html.escape(sol['item'] or '')}</b> disponible ahora. "
                          "Lo dejé <b>en seguimiento</b>: te aviso si aparece en un acopio cercano. 🔔"))
        return

    if data.startswith("seg|"):  # respuesta del solicitante a "apareció lo que necesitabas"
        try:
            _, nid, dec = data.split("|")
        except ValueError:
            return
        nec = db.necesidad(int(nid))
        rep = db.representante_por_tgid(chat_id)
        if not nec or not rep or rep["id"] != nec["rep_id"]:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No válido")
            return
        base = msg.get("text", "")
        acopio = db.dueno_de_punto(nec.get("match_punto_id")) or {}
        if dec == "coord":
            db.set_estado_necesidad(int(nid), "coordinando")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="¡Listo!")
            wa = _wa_btn(f"💬 WhatsApp a {acopio.get('punto') or 'acopio'}"[:40], acopio.get("telefono"))
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + "\n\n🤝 <b>Coordinando la entrega.</b> Contáctalos:", parse_mode="HTML",
                reply_markup=_kb([wa]) if wa else None)
            dt = acopio.get("telegram_id")
            if dt:
                quien = rep.get("punto") or rep.get("nombre") or "Un punto"
                wa2 = _wa_btn(f"💬 WhatsApp a {quien}"[:40], rep.get("telefono"))
                _tg("sendMessage", chat_id=dt, parse_mode="HTML",
                    text=(f"🤝 <b>{html.escape(quien)}</b> quiere <b>coordinar la entrega</b> de "
                          f"<b>{html.escape(nec.get('item') or '')}</b>."
                          + (f"\n📞 Contacto: <b>{html.escape(rep.get('telefono'))}</b>" if rep.get("telefono") else "")),
                    reply_markup=_kb([wa2]) if wa2 else None)
        else:  # ya no la necesito
            db.set_estado_necesidad(int(nid), "resuelta")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Anotado")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + "\n\n✅ Listo, la quité de seguimiento.")
        return

    if data.startswith("inv|"):  # nudge post-registro: cargar inventario ahora / luego
        partes = data.split("|")
        sub = partes[1] if len(partes) > 1 else ""
        rid_s = partes[2] if len(partes) > 2 else ""
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if sub == "now":
            rep_n = db.representante_por_tgid(chat_id) or {}
            efimero.kv_set("modo_tg", str(chat_id),  # inventario: oferta solo si el punto publica oferta
                           "oferta" if rep_n.get("tipo_punto") in db.TIPOS_OFERTA else "stock")
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="¡Perfecto! 📸 Mándame una <b>foto</b> de tu lista, un <b>audio</b> o un "
                     "<b>PDF/Excel</b> con tu inventario, y yo lo leo y lo organizo.")
        elif sub == "later" and rid_s.isdigit():
            con = db.conectar()
            con.execute("INSERT OR IGNORE INTO recordatorios_inv (rep_id, canal, destino) VALUES (?,?,?)",
                        (int(rid_s), "tg", str(chat_id)))
            con.commit()
            con.close()
            _tg("sendMessage", chat_id=chat_id,
                text="👍 Sin problema. Te lo recordaré una vez al día hasta que lo cargues.")
        return

    if data.startswith("dsp|"):  # despacho de envío (elegir destino / confirmar / cancelar)
        partes = data.split("|")
        acc = partes[1] if len(partes) > 1 else ""
        _tg("answerCallbackQuery", callback_query_id=cqid)
        rep = db.representante_por_tgid(chat_id)
        if not rep or rep["estado"] != "aprobado":
            return
        pid = rep.get("punto_id") or rep["id"]
        if acc == "d" and len(partes) > 2 and partes[2].isdigit():
            estados[chat_id] = {"paso": "dsp_items",
                                "dsp": {"dest": int(partes[2]), "pid": pid, "rep": rep["id"]}}
            inv = stock.de_punto(pid)
            L = ["🚚 Tu inventario disponible:"]
            for r in inv[:30]:
                if r["cantidad"] is None:
                    c = "❓"
                else:
                    c = f"{r['cantidad'] - (r['reservado'] or 0):g} {r['unidad'] or ''}".strip()
                L.append(f"• {html.escape(r['item'])} — {html.escape(c)}")
            L.append("\nEscríbeme qué envías, <b>una línea por insumo</b>:\n<i>pañales 40\nagua 5</i>")
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML", text="\n".join(L)[:4000])
        elif acc == "go":
            st = estados.get(chat_id) or {}
            d = st.get("dsp") or {}
            if not d.get("items"):
                return
            try:
                e = envios.despachar(d["pid"], d["rep"], d["dest"], d["dest"], d["items"])
            except ValueError as err:
                _tg("sendMessage", chat_id=chat_id, text=f"⚠️ {err}. Ajusta la lista y vuelve a intentar.")
                return
            estados.pop(chat_id, None)
            env = envios.por_token(e["token"])
            _enviar_foto(chat_id, envios.qr_png(e["token"]),
                         caption=(envios.manifiesto_texto(env) +
                                  "\n\n📱 Imprime o muestra este QR: quien recibe lo escanea y confirma.\n"
                                  + envios.url_entrega(e["token"]))[:1024])
            envios.notificar_rep(d["dest"], "🚚 " + (env.get("de_punto") or "Un acopio") +
                                 " te despachó un envío:\n\n" + envios.manifiesto_texto(env) +
                                 "\n\nCuando llegue, revísalo y confírmalo aquí:\n"
                                 + envios.url_entrega(e["token"]))
        elif acc == "no":
            estados.pop(chat_id, None)
            _tg("sendMessage", chat_id=chat_id, text="❌ Despacho cancelado.")
        return

    if data.startswith("cap|"):  # guardar / descartar una captura (en el chat del rep)
        try:
            _, cid, dec = data.split("|")
        except ValueError:
            return
        cap = db.captura(int(cid))
        rep = db.representante_por_tgid(chat_id)
        if not cap or not rep or cap["representante_id"] != rep["id"]:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No válido")
            return
        base = msg.get("text", "")
        if dec == "add":
            # Auditoría anti-duplicado (lección Ecocamping): si la lista se parece a lo
            # ya publicado, sumar duplicaría → advertir y recomendar Reemplazar.
            coinciden, total = stock.parece_snapshot(int(cid))
            if total >= 3 and coinciden * 2 >= total:
                _tg("answerCallbackQuery", callback_query_id=cqid)
                _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"], parse_mode="HTML",
                    text=f"⚠️ <b>Ojo:</b> {coinciden} de {total} insumos de esta lista <b>ya están</b> en tu "
                         "inventario publicado. Si sumas, se duplican.\n\n¿Qué prefieres?",
                    reply_markup=_kb([[{"text": "🔄 Reemplazar", "callback_data": f"cap|{cid}|rep"},
                                       {"text": "➕ Sumar igual", "callback_data": f"cap|{cid}|addf"}]]))
                return
            dec = "addf"
        if dec in ("ok", "addf", "rep"):
            db.confirmar_captura(int(cid))
            if dec in ("addf", "rep"):
                # Alimenta el stock numérico compartido (mismo inventario que WhatsApp)
                stock.aplicar_captura(int(cid), reemplazar=(dec == "rep"))
                con = db.conectar()
                con.execute("UPDATE recordatorios_inv SET activo = 0 WHERE rep_id = ?", (rep["id"],))
                con.commit()
                con.close()
            _demanda_a_necesidades(int(cid), chat_id)
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Guardado ✅")
            fin = {"ok": "✅ Guardado, ¡gracias!",
                   "addf": "➕ Sumado a tu inventario, ¡gracias!",
                   "rep": "🔄 Inventario reemplazado, ¡gracias!"}[dec]
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"], text=base + "\n\n" + fin)
        elif dec == "edit":
            estados.pop(chat_id, None)
            _tg("answerCallbackQuery", callback_query_id=cqid)
            _tg("editMessageReplyMarkup", chat_id=chat_id, message_id=msg["message_id"], reply_markup=_kb([]))
            _editor_lista(chat_id, int(cid))
        else:
            db.descartar_captura(int(cid))
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Descartado")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"], text=base + "\n\n🗑️ Descartado. Mándalo de nuevo cuando quieras.")
        return

    if data.startswith("ce|"):  # editor de captura (corregir dato por dato)
        estados.pop(chat_id, None)  # cancela cualquier edición de texto a medias
        partes = data.split("|")
        cid = partes[1]
        cap = db.captura(int(cid))
        rep = db.representante_por_tgid(chat_id)
        if not cap or not rep or cap["representante_id"] != rep["id"]:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No válido")
            return
        accion = partes[2]
        mid = msg.get("message_id")
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if accion == "L":
            _editor_lista(chat_id, int(cid), page=int(partes[3]), message_id=mid)
        elif accion in ("p", "i"):
            _editor_fila(chat_id, int(cid), accion, int(partes[3]), message_id=mid)
        elif accion in ("ep", "ei"):
            obj = "p" if accion == "ep" else "i"
            rid, f = int(partes[3]), partes[4]
            col, lbl = (_CAMPO_P if obj == "p" else _CAMPO_I).get(f, (None, None))
            if not col:
                return
            det = db.captura_detalle(int(cid))
            fila = _buscar_fila(det, obj, rid) if det else None
            actual = (fila or {}).get(col) or "—"
            estados[chat_id] = {"paso": "edit_captura_campo", "cap_id": int(cid),
                                "obj": obj, "rid": rid, "campo": col}
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text=(f"Escríbeme <b>{lbl}</b> corregido.\nActual: <i>{html.escape(str(actual))}</i>\n\n"
                      "(Manda un guion «-» para dejarlo vacío.)"))
        elif accion in ("dp", "di"):
            rid = int(partes[3])
            if accion == "dp":
                db.borrar_persona_captura(rid)
            else:
                db.borrar_item_captura(rid)
            _editor_lista(chat_id, int(cid), message_id=mid)
        elif accion == "ok":
            det = db.captura_detalle(int(cid)) or {}
            if det.get("items") and (cap.get("clase") != "demanda"):
                # Inventario editado: misma pregunta que el flujo normal (paridad con WhatsApp)
                _tg("editMessageText", chat_id=chat_id, message_id=mid, parse_mode="HTML",
                    text="¿Esto se <b>suma</b> a tu inventario publicado o lo <b>reemplaza</b>?",
                    reply_markup=_kb([[{"text": "➕ Sumar", "callback_data": f"cap|{cid}|add"},
                                       {"text": "🔄 Reemplazar", "callback_data": f"cap|{cid}|rep"}]]))
            else:
                db.confirmar_captura(int(cid))
                _demanda_a_necesidades(int(cid), chat_id)
                _tg("editMessageText", chat_id=chat_id, message_id=mid, text="✅ Guardado, ¡gracias!")
        elif accion == "no":
            db.descartar_captura(int(cid))
            _tg("editMessageText", chat_id=chat_id, message_id=mid,
                text="🗑️ Descartado. Mándalo de nuevo cuando quieras.")
        return

    if data.startswith("apr|"):  # aprobación desde el grupo de moderadores
        if str(chat_id) != str(GRUPO_MODERADORES):
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No autorizado")
            return
        try:
            _, rid, decision = data.split("|")
        except ValueError:
            return
        rep = db.representante(int(rid))
        if not rep:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No encontrado")
            return
        quien = cq.get("from", {}).get("first_name", "coordinador")
        base = msg.get("text", "")
        if decision == "ok":
            db.set_estado_representante(int(rid), "aprobado")
            _set_commands_rol(rep["telegram_id"], "centro",
                              con_ficha=(rep.get("rol") or "representante") != "colaborador")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Aprobado ✅")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n✅ Aprobado por {quien}")
            _tg("sendMessage", chat_id=rep["telegram_id"],
                text="✅ ¡Tu afiliación fue aprobada! Ya puedes mandarme tus listas, inventario o necesidades por foto, audio o texto.")
            if (rep.get("rol") or "representante") == "representante" and rep.get("telegram_id"):
                # Onboarding: siembra la primera necesidad; al cerrarla (nec1|)
                # salen el GPS (si no hay pin) y el nudge de inventario.
                _pedir_primera_necesidad(rep["telegram_id"], int(rid))
        else:
            db.set_estado_representante(int(rid), "rechazado")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Quitado de la web")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n🗑️ Quitado de la web por {quien}")
        return

    if data.startswith("apra|"):  # aprobación de Manos Solidarias desde el grupo de moderadores
        if str(chat_id) != str(GRUPO_MODERADORES):
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No autorizado")
            return
        try:
            _, aid, decision = data.split("|")
        except ValueError:
            return
        al = db.aliado(int(aid))
        if not al:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No encontrado")
            return
        quien = cq.get("from", {}).get("first_name", "coordinador")
        base = msg.get("text", "")
        if decision == "ok":
            db.set_estado_aliado(int(aid), "aprobado")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Aprobado ✅")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n✅ Aprobado por {quien}")
            # Los que se inscribieron por la WEB no tienen chat de Telegram (telegram_id None):
            # no se les fija menú ni se les avisa por Telegram (se enteran al aparecer en la web).
            if al.get("telegram_id"):
                _set_commands_rol(al["telegram_id"], "aliado")
                _tg("sendMessage", chat_id=al["telegram_id"], parse_mode="HTML",
                    text="✅ ¡Tu registro en <b>Manos Solidarias</b> fue aprobado! Ya apareces en la web para que la gente te encuentre. ¡Gracias por ayudar! 🤝")
        else:
            db.set_estado_aliado(int(aid), "rechazado")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Quitado de la web")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n🗑️ Quitado de la web por {quien}")
        return

    if data.startswith("ret|"):  # retiro voluntario del propio usuario (o volver a la red)
        valor = data.split("|", 1)[1]
        if valor == "si":
            activas = _fichas_retiro(chat_id, ("aprobado", "pendiente"))
            if not activas:
                _tg("answerCallbackQuery", callback_query_id=cqid, text="Sin registro activo")
                _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                    text="No encontré un registro activo tuyo 🙂.")
                return
            for rol, etiqueta, f, setter in activas:
                setter(f["id"], "retirado")
                if GRUPO_MODERADORES:
                    nombre = f.get("punto") or f.get("nombre") or "(sin nombre)"
                    t = {"centro": "r", "voluntario": "v", "aliado": "a"}[rol]
                    _tg("sendMessage", chat_id=GRUPO_MODERADORES, parse_mode="HTML",
                        text="🚪 <b>Retiro voluntario</b> (Telegram)\n" + etiqueta +
                             f": <b>{html.escape(nombre)}</b> (id {f['id']}) pidió salir de la "
                             "plataforma. Ya no aparece en la web ni recibe avisos.",
                        reply_markup=_kb([[{"text": "🔄 Reactivar",
                                            "callback_data": f"retr|{t}|{f['id']}"}]]))
            # El menú ☰ vuelve al básico (sin comandos de rol).
            _tg("setMyCommands", commands=[_CMD_START, _CMD_PEDIR],
                scope={"type": "chat", "chat_id": chat_id})
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Retiro registrado")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text="🕊️ Listo, tu retiro quedó registrado. Gracias por haber formado parte de la red. "
                     "Si algún día quieres volver, escribe /retirarme y toca «Volver a la red», "
                     "o regístrate de nuevo con /start. 💙")
        elif valor == "volver":
            retiradas = _fichas_retiro(chat_id, ("retirado",))
            if not retiradas:
                _tg("answerCallbackQuery", callback_query_id=cqid, text="Nada que reactivar")
                return
            for rol, etiqueta, f, setter in retiradas:
                setter(f["id"], "aprobado")
                _set_commands_rol(chat_id, rol,
                                  con_ficha=(f.get("rol") or "representante") != "colaborador")
                if GRUPO_MODERADORES:
                    nombre = f.get("punto") or f.get("nombre") or "(sin nombre)"
                    _tg("sendMessage", chat_id=GRUPO_MODERADORES,
                        text=f"💙 {etiqueta} «{nombre}» (id {f['id']}) volvió a la red "
                             "(reactivación voluntaria).")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="¡De vuelta! 💙")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text="💙 ¡Bienvenido de vuelta! Tu registro está activo otra vez y "
                     "volverás a aparecer en la web.")
        else:
            _tg("answerCallbackQuery", callback_query_id=cqid)
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text="💙 ¡Qué bueno que sigues en la red! No cambié nada.")
        return

    if data.startswith("retr|"):  # un moderador reactiva a alguien que se retiró
        if str(chat_id) != str(GRUPO_MODERADORES):
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No autorizado")
            return
        try:
            _, t, rid = data.split("|")
            rid = int(rid)
        except ValueError:
            return
        conf = {"r": (db.representante, db.set_estado_representante, "centro"),
                "v": (db.voluntario, db.set_estado_voluntario, "voluntario"),
                "a": (db.aliado, db.set_estado_aliado, "aliado")}.get(t)
        if not conf:
            return
        getter, setter, rol = conf
        f = getter(rid)
        if not f:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No encontrado")
            return
        setter(rid, "aprobado")
        quien = cq.get("from", {}).get("first_name", "coordinador")
        _tg("answerCallbackQuery", callback_query_id=cqid, text="Reactivado ✅")
        _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
            text=(msg.get("text", "") + f"\n\n🔄 Reactivado por {quien}"))
        if f.get("telegram_id"):
            _set_commands_rol(f["telegram_id"], rol,
                              con_ficha=(f.get("rol") or "representante") != "colaborador")
            _tg("sendMessage", chat_id=f["telegram_id"],
                text="💙 Tu registro en la red fue reactivado por el equipo. ¡Bienvenido de vuelta!")
        elif f.get("wa_phone"):
            try:
                import wa_api
                wa_api.enviar_texto("58" + f["wa_phone"],
                                    "💙 Tu registro en la Red de Acopio y Refugio fue reactivado "
                                    "por el equipo. ¡Bienvenido de vuelta!")
            except Exception as e:  # noqa: BLE001
                print("aviso reactivación WA error:", e)
        return

    if data.startswith("pweb|"):  # moderar un PEDIDO DE AYUDA llegado por la web
        if str(chat_id) != str(GRUPO_MODERADORES):
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No autorizado")
            return
        try:
            _, lid, decision = data.split("|")
        except ValueError:
            return
        lid = int(lid)
        lla = _llamado_row(lid)
        if not lla:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No encontrado")
            return
        quien = cq.get("from", {}).get("first_name", "coordinador")
        # El aviso puede ser texto o FOTO con caption (pedido con imagen/flyer):
        # una foto se sella con editMessageCaption (editMessageText fallaría).
        base = msg.get("text") or msg.get("caption") or ""

        def _sellar(sufijo, teclado=None):
            if "photo" in msg:
                cap = base + sufijo
                if len(cap) > 1024:  # límite de caption de Telegram
                    cap = cap[:1023] + "…"
                _tg("editMessageCaption", chat_id=chat_id, message_id=msg["message_id"],
                    caption=cap, reply_markup=teclado)
            else:
                _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                    text=base + sufijo, reply_markup=teclado)
        if decision == "ok":
            # Publica y deja el aviso a la red en manos de _barrido_triangulador (≤45 s),
            # el MISMO camino que la auto-publicación → un solo punto de fan-out, sin
            # avisos duplicados. El contacto privado viaja por Telegram, nunca a la web.
            db.publicar_pedido(lid)
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Publicado ✅")
            # Deja el mismo aviso con botón de cierre (pat|res reusa el handler de
            # abajo, ya usado para pedidos atendidos desde la web) — así un pedido
            # resuelto por WhatsApp/otro canal también se puede cerrar sin pasar por
            # /api/atender, sin depender de un ID escrito a mano.
            _sellar(f"\n\n✅ Publicado y avisado a la red por {quien}",
                    teclado=_kb([[{"text": "✅ Marcar resuelto", "callback_data": f"pat|{lid}|res"},
                                  {"text": "🗑️ Quitar", "callback_data": f"pat|{lid}|del"}]]))
        else:
            db.set_estado_llamado(lid, "descartado")
            # La imagen privada del pedido (si la hay) se borra: descartado = no se publica.
            try:
                (BASE / "pedidos_img" / f"{lid}.jpg").unlink(missing_ok=True)
            except Exception:
                pass
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Descartado")
            _sellar(f"\n\n🗑️ Descartado por {quien}")
        return

    if data.startswith("dcz|"):  # cruce del DEPURADOR: la persona buscada apareció en registros
        if str(chat_id) != str(GRUPO_MODERADORES):
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No autorizado")
            return
        try:
            _, lid, decision = data.split("|")
            lid = int(lid)
        except ValueError:
            return
        quien = cq.get("from", {}).get("first_name", "coordinador")
        base = msg.get("text") or ""
        if decision == "ok":
            # El moderador confirmó que la persona apareció → el pedido se marca
            # resuelto y deja de salir en la web (depuración con confirmación humana).
            try:
                db.set_estado_llamado(lid, "resuelto")
            except Exception as e:
                print("dcz resolver error:", e)
                _tg("answerCallbackQuery", callback_query_id=cqid, text="Error al marcar")
                return
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Marcado resuelto ✅")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n✅ Confirmado por {quien}: la persona apareció — "
                            "pedido RESUELTO, sale de la web.")
        else:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Se mantiene activo")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n👁 {quien} decidió MANTENER el pedido activo "
                            "(la coincidencia no se confirmó).")
        return

    if data.startswith("pat|"):  # moderar la ATENCIÓN de un pedido web (resolver / quitar)
        if str(chat_id) != str(GRUPO_MODERADORES):
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No autorizado")
            return
        try:
            _, lid, accion = data.split("|")
        except ValueError:
            return
        lid = int(lid)
        lla = _llamado_row(lid)
        if not lla:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No encontrado")
            return
        quien = cq.get("from", {}).get("first_name", "coordinador")
        base = msg.get("text", "")
        if accion == "res":
            db.set_estado_llamado(lid, "resuelto")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Resuelto ✅")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n✅ Marcado resuelto por {quien}")
        elif accion == "act":  # reactivar uno marcado 'vencido' por error (aviso de expiración)
            destino = "activo" if db.es_llamado_de_voluntario(lla) else "web"
            db.set_estado_llamado(lid, destino)
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Reactivado ↩️")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n↩️ Reactivado por {quien}")
        else:
            db.set_estado_llamado(lid, "descartado")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Quitado")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n🗑️ Quitado de la web por {quien}")
        return

    if data.startswith("aprv|"):  # aprobación de Voluntariado desde el grupo de moderadores
        if str(chat_id) != str(GRUPO_MODERADORES):
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No autorizado")
            return
        try:
            _, vid, decision = data.split("|")
        except ValueError:
            return
        vol = db.voluntario(int(vid))
        if not vol:
            _tg("answerCallbackQuery", callback_query_id=cqid, text="No encontrado")
            return
        quien = cq.get("from", {}).get("first_name", "coordinador")
        base = msg.get("text", "")
        if decision == "ok":
            db.set_estado_voluntario(int(vid), "aprobado")
            _set_commands_rol(vol["telegram_id"], "voluntario")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Aprobado ✅")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n✅ Aprobado por {quien}")
            _tg("sendMessage", chat_id=vol["telegram_id"], parse_mode="HTML",
                text="✅ ¡Tu registro de <b>Voluntariado</b> fue aprobado! Ya puedes levantar llamados de "
                     "apoyo con /llamado. ¡Gracias por sumar! 📣", reply_markup=_menu_voluntario())
        else:
            db.set_estado_voluntario(int(vid), "rechazado")
            _tg("answerCallbackQuery", callback_query_id=cqid, text="Quitado de la web")
            _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                text=base + f"\n\n🗑️ Quitado de la web por {quien}")
        return

    if chat.get("type") != "private":
        return
    st = estados.get(chat_id)

    if data.startswith("modo|"):  # bifurcación inicial: centro / Manos Solidarias / Voluntariado
        _tg("answerCallbackQuery", callback_query_id=cqid)
        modo = data.split("|")[1]
        if modo == "aliado":
            _iniciar_aliado(chat_id)
        elif modo == "voluntario":
            _iniciar_voluntario(chat_id)
        elif modo == "pedir":
            _iniciar_pedido(chat_id)
        else:
            _iniciar_centro(chat_id)
        return

    if data.startswith("pedq|"):  # Pedir ayuda de un representante: ¿para su punto o para otro?
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if data.split("|")[1] == "punto":
            _cmd_necesito(chat_id)          # vía rica: necesidades del punto + puente
        else:
            _iniciar_pedido(chat_id, forzar_publico=True)
        return

    if data.startswith("pcat|"):  # Pedir ayuda: eligió el tipo de necesidad
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if not st or st.get("paso") != "pd_cat":
            return
        cat = data.split("|")[1]
        if cat not in CATS_PEDIR:
            return
        st["pedido"]["cat"] = cat
        if cat == "desaparecido":
            # Nombre de la PERSONA BUSCADA: alimenta la sugerencia inmediata
            # (fuentes.py) y el cron depurador que la cruza con hospitales/rescatados.
            st["paso"] = "pd_persona"
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="🔎 ¿Cómo se llama la <b>persona que buscas</b>? "
                     "(nombre y apellido; si no lo sabes, escribe «-»)")
        else:
            st["paso"] = "pd_desc"
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="✍️ Cuéntame <b>qué necesitas exactamente</b> y para cuántas personas "
                     "(ej. «agua potable y pañales para 3 familias»). 📸 Si tienes una "
                     "<b>foto o flyer</b> de la necesidad, mándala y la leo yo.")
        return

    if data.startswith("acat|"):  # Manos Solidarias eligió categoría de ayuda
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if not st or st.get("paso") != "al_cat":
            return
        cat = data.split("|")[1]
        st.setdefault("aliado", {})["categoria"] = cat
        st["paso"] = "al_nombre"
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text="👤 ¿Cuál es tu <b>nombre</b> (o el de tu emprendimiento)?")
        return

    if data.startswith("aconf|"):  # confirmar / reiniciar registro de Manos Solidarias
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if data.split("|")[1] == "edit":
            _iniciar_aliado(chat_id)
            return
        if not st or st.get("paso") != "al_conf":
            return
        _crear_aliado_final(chat_id, st)
        return

    if data.startswith("vconf|"):  # confirmar / reiniciar registro de Voluntariado
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if data.split("|")[1] == "edit":
            _iniciar_voluntario(chat_id)
            return
        if not st or st.get("paso") != "vo_conf":
            return
        _crear_voluntario_final(chat_id, st)
        return

    if data.startswith(("colok|", "colrm|")):   # respuesta del dueño al aviso de colaborador
        accion_c, _, rid_s = data.partition("|")
        _tg("answerCallbackQuery", callback_query_id=cqid)
        rep = db.representante(int(rid_s)) if rid_s.isdigit() else None
        if not rep or (rep.get("rol") or "") != "colaborador":
            _tg("sendMessage", chat_id=chat_id, text="Ese registro ya no existe 🙂.")
            return
        if accion_c == "colok":
            _tg("sendMessage", chat_id=chat_id,
                text="👍 ¡Gracias por confirmar! Sigan coordinando en equipo. 💪")
            return
        if rep.get("estado") != "rechazado":
            db.set_estado_representante(rep["id"], "rechazado")
        avisado = (f"Tu vinculación como colaborador de {rep.get('punto') or 'el punto'} fue "
                   "revertida por el responsable del punto. Si crees que es un error, "
                   "comunícate directamente con el punto. 🙏")
        if rep.get("telegram_id"):
            _tg("sendMessage", chat_id=rep["telegram_id"], text=avisado)
        elif rep.get("wa_phone") or rep.get("telefono"):
            wa_api.enviar_texto(rep.get("wa_phone") or rep.get("telefono"), avisado)
        if GRUPO_MODERADORES:
            _tg("sendMessage", chat_id=GRUPO_MODERADORES,
                text=(f"🚫 Colaborador removido por el responsable del punto: {rep['nombre']} "
                      f"ya no es colaborador de {rep.get('punto') or ''} (el dueño no lo reconoció)."))
        _tg("sendMessage", chat_id=chat_id, text="Listo, quedó removido 🙌. Gracias por avisar — "
                                                 "así mantenemos la red confiable.")
        return

    if data.startswith("colab|"):  # respondió a "punto ya registrado"
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if not st or st.get("paso") != "colaborar":
            return
        dec = data.split("|")[1]
        if dec == "si":   # es el mismo punto → colaborador
            _registrar_colaborador(chat_id, st, st.get("dueno_punto"), st.get("dueno_tipo"),
                                   st.get("dueno_punto_id"))
        elif dec == "otro":   # otro punto con el mismo nombre → ficha + chequeo de ubicación
            st["ref_punto"] = st.get("dueno_punto")
            st["ref_ubicacion"] = st.get("dueno_ubicacion")
            st["ref_tipo"] = st.get("dueno_tipo")
            st["ref_punto_id"] = st.get("dueno_punto_id")
            _iniciar_ficha(chat_id, st, st.get("nombre_tecleado") or st.get("dueno_punto"))
        else:   # se equivocó → escribir otro nombre
            st["paso"] = "punto"
            for k in ("dueno_punto", "dueno_tipo", "dueno_ubicacion", "nombre_tecleado"):
                st.pop(k, None)
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="Sin problema 🙂 Dime el <b>nombre del otro punto</b> que quieres registrar:")
        return

    if data.startswith("dup|"):  # respondió al aviso "misma ubicación que el homónimo"
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if not st or st.get("paso") != "ubic_dup":
            return
        if data.split("|")[1] == "colab":   # es el mismo lugar → colaborador
            _registrar_colaborador(chat_id, st, st.get("ref_punto"), st.get("ref_tipo"),
                                   st.get("ref_punto_id"))
        else:   # corregir la ubicación: re-pregunta el mismo campo (ficha_i no avanzó)
            st["paso"] = "ficha"
            _enviar_pregunta_ficha(chat_id, FICHA[st["ficha_i"]])
        return

    if data == "ced|skip":  # alta: dejó la cédula para luego (fundación no tiene este botón)
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if not st or st.get("paso") != "cedula" or st.get("tipo") == "fundacion":
            return
        st["cedula_omitida"] = True
        _ir_a_confirmar(chat_id, st)
        return

    if data.startswith("nec1|"):  # siembra post-alta de la primera necesidad
        sub = data.split("|", 1)[1]
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if not st or not str(st.get("paso", "")).startswith("nec1"):
            return
        if sub == "more":
            st["paso"] = "nec1_item"
            st.pop("nec1_item", None)
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="¿Qué otro <b>insumo</b> te falta? (uno a la vez)",
                reply_markup=_kb([[{"text": "✅ Ya está", "callback_data": "nec1|done"}]]))
            return
        rid_s = st.get("nec1_rid")
        estados.pop(chat_id, None)
        # Cierre del onboarding: ahora sí GPS (si no hay pin) + nudge de inventario.
        rep_n = (db.representante(rid_s) if rid_s else None) or db.representante_por_tgid(chat_id)
        if rep_n and rep_n.get("lat") is None and (rep_n.get("rol") or "representante") == "representante":
            _pedir_gps(chat_id)
        _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
            text=("📦 <b>Cuando puedas:</b> carga tu inventario (foto, audio, PDF o Excel) "
                  "para que la red sepa qué tienes, y completa la ficha del punto con "
                  "/ficha (aforo, camas, servicios…)."),
            reply_markup=_kb([[{"text": "📦 Cargar ahora", "callback_data": f"inv|now|{rid_s}"},
                               {"text": "⏰ Seguir luego", "callback_data": f"inv|later|{rid_s}"}]]))
        return

    if data.startswith("ficha|"):
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if not st or st.get("paso") != "ficha":
            return
        try:
            _, campo, tok = data.split("|")
        except ValueError:
            return
        if st.get("corrigiendo"):  # corrigiendo un solo campo
            if st["corrigiendo"] != campo:
                return
        elif FICHA[st["ficha_i"]]["campo"] != campo:  # botón viejo / fuera de orden
            return
        _registrar_ficha(chat_id, st, _FICHA_VAL.get(tok, tok))
        return

    if data.startswith("corr|"):  # eligió qué dato corregir desde el resumen
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if not st:
            return
        key = data.split("|", 1)[1]
        if key == "__ok__":
            _ir_a_confirmar(chat_id, st)
        else:
            _preguntar_un_campo(chat_id, st, key)
        return

    if data.startswith("tipo|"):
        if not st:
            _tg("answerCallbackQuery", callback_query_id=cqid)
            return
        st["tipo"] = data.split("|")[1]
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if st.get("corrigiendo") == "tipo":  # solo corrigiendo el tipo
            st.pop("corrigiendo", None)
            _ir_a_confirmar(chat_id, st)
            return
        st["paso"] = "punto"
        _tg("sendMessage", chat_id=chat_id,
            text=f"¿Cómo se llama el/la {TIPOS.get(st['tipo'], 'punto').lower()}? (ej. Pérez Carreño)")
        return

    if data == "conf|edit":
        _tg("answerCallbackQuery", callback_query_id=cqid)
        if st:
            _menu_correccion(chat_id, st)
        else:
            iniciar(chat_id)
        return

    if data == "conf|ok":
        if not st or st.get("paso") != "confirmar":
            _tg("answerCallbackQuery", callback_query_id=cqid)
            return
        f = st.get("ficha", {})
        if st.get("editar_rid"):  # actualización de ficha de un rep ya aprobado (/ficha)
            db.actualizar_ficha_representante(st["editar_rid"], f)
            estados.pop(chat_id, None)
            _tg("answerCallbackQuery", callback_query_id=cqid, text="¡Actualizado!")
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="✅ ¡Listo! La ficha de tu punto quedó <b>actualizada</b>. Gracias 🙌")
            return
        # Homónimos: si el nombre choca EXACTO con un punto ya registrado, se le agrega el
        # sector (de la ubicación) para que en la web no se mezclen dos puntos distintos.
        nombre_punto = st["punto"]
        if db.existe_punto_exacto(nombre_punto):
            zona = (st.get("zona") or "").strip()
            if not zona:
                _, zona = captura.comparar_ubicacion(f.get("ubicacion", ""), "")
                zona = (zona or "").strip()
            if not zona:
                zona = (f.get("ubicacion", "") or "").strip()[:24]
            if zona:
                nombre_punto = f"{st['punto']} — {zona}"
        rid = db.crear_representante(chat_id, st["nombre"], st["tipo"], nombre_punto,
                                    f.get("contacto", ""), ficha=f, cedula=st.get("cedula"))
        # Las fundaciones SIEMPRE pasan por aprobación manual (verificación de RIF
        # caso por caso), aunque AUTO_APROBAR esté activo para el resto de puntos.
        auto_ok = AUTO_APROBAR and st["tipo"] != "fundacion"
        if auto_ok:
            db.set_estado_representante(rid, "aprobado")
            _set_commands_rol(chat_id, "centro", con_ficha=True)  # responsable: con /ficha
        estados.pop(chat_id, None)
        _tg("answerCallbackQuery", callback_query_id=cqid, text="¡Enviado!")
        if auto_ok:
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="✅ ¡Listo! Tu afiliación quedó <b>aprobada</b> y ya apareces en la web. Ya puedes mandarme tus "
                     "listas, inventario o necesidades por foto, audio, texto o PDF/Excel/Word.")
            # Siembra en caliente: primero la NECESIDAD más urgente del punto (la web,
            # el botón «Puedo llevar esto» y el puente a acopios viven de esto).
            # Al cerrarla (nec1|) salen el GPS y el nudge de inventario de siempre.
            _pedir_primera_necesidad(chat_id, rid)
        else:
            _tg("sendMessage", chat_id=chat_id, parse_mode="HTML",
                text="🙌 ¡Gracias! Tu afiliación quedó <b>pendiente de aprobación</b>. Te avisamos apenas un coordinador la confirme.")
        rep = db.representante(rid)
        if GRUPO_MODERADORES:
            kb, cab = _notif_botones(f"apr|{rid}", "🆕 <b>Nueva afiliación</b>",
                                     forzar_manual=(rep.get("tipo_punto") == "fundacion"))
            lineas = [f"{cab}\n👤 {html.escape(rep['nombre'])}"]
            if rep.get("cedula"):
                lineas.append(f"🪪 CI: {html.escape(rep['cedula'])}")
            else:
                lineas.append("🪪 <b>Sin cédula</b> (eligió darla luego)")
            lineas.append(f"{TIPOS.get(rep['tipo_punto'], 'Punto')}: <b>{html.escape(rep['punto'] or '')}</b>")
            if rep.get("tipo_punto") == "fundacion" and (f.get("rif") or "").strip().lower() == "no posee":
                lineas.append("⚠️ <b>Declaró no poseer RIF</b> (colectivo informal) — aprobar caso por caso.")
            for campo, etiqueta in FICHA_LABEL.items():
                val = f.get(campo)
                if val:
                    lineas.append(f"• {etiqueta}: {html.escape(val)}")
            _tg("sendMessage", chat_id=GRUPO_MODERADORES, text="\n".join(lineas),
                parse_mode="HTML", reply_markup=kb)
        return


# --- Menú ☰ de Telegram, separado por rol (scope por chat) ---
_CMD_START = {"command": "start", "description": "🕊️ Afiliarte / ver tu menú"}
_CMD_PEDIR = {"command": "pedir", "description": "🆘 Pedir ayuda a la red"}
_CMD_RETIRO = {"command": "retirarme", "description": "🚪 Retirarme de la plataforma"}


def _cmds_centro(con_ficha=True):
    """Comandos del menú ☰ de un centro de acopio. /ficha solo para el responsable."""
    cmds = [
        {"command": "inventario", "description": "📦 Cargar lo que TIENES (acopio)"},
        {"command": "necesito", "description": "🙏 Declarar lo que NECESITAS / donaciones"},
        {"command": "recibi", "description": "📥 Registrar lo que ya recibiste"},
        {"command": "solicitar", "description": "🔎 Buscar y solicitar un insumo a un acopio"},
        {"command": "stock", "description": "👀 Ver tu inventario publicado"},
        {"command": "despachar", "description": "🚚 Despachar un envío con QR de entrega"},
        {"command": "miinventario", "description": "📄 Recibir tu inventario en Excel"},
        {"command": "ubicacion", "description": "📍 Compartir la ubicación de tu punto"},
    ]
    if con_ficha:
        cmds.append({"command": "ficha", "description": "📝 Completar o actualizar la ficha del punto"})
    cmds.append(_CMD_RETIRO)
    cmds.append(_CMD_START)
    return cmds


_CMDS_VOLUNTARIO = [
    {"command": "llamado", "description": "📣 Levantar un llamado de apoyo"},
    _CMD_RETIRO,
    _CMD_START,
]
_CMDS_ALIADO = [
    {"command": "ubicacion", "description": "📍 Compartir tu ubicación en el mapa"},
    _CMD_RETIRO,
    _CMD_START,
]


def _set_commands():
    """Menú ☰ global por defecto: /start + /pedir (para quien todavía no se registra).
    Cada rol recibe su propia lista por chat con _set_commands_rol()."""
    _tg("setMyCommands", commands=[_CMD_START, _CMD_PEDIR])


def _set_commands_rol(chat_id, rol, con_ficha=True):
    """Fija el menú ☰ de ESTE chat según el rol, con scope por chat (BotCommandScopeChat),
    para que cada quien vea solo SUS comandos. rol: 'centro' | 'voluntario' | 'aliado'."""
    if rol == "centro":
        cmds = _cmds_centro(con_ficha)
    elif rol == "voluntario":
        cmds = _CMDS_VOLUNTARIO
    elif rol == "aliado":
        cmds = _CMDS_ALIADO
    else:
        cmds = [_CMD_START]
    _tg("setMyCommands", commands=cmds, scope={"type": "chat", "chat_id": chat_id})


def _barrido_triangulador():
    """AUTO-PUBLICACIÓN DE PEDIDOS: único punto de aviso a la red. Los 3 canales
    (web/WhatsApp/Telegram) y el botón ✅ del moderador publican con db.publicar_pedido
    (estado 'web' + triangulado=0); este barrido recoge esos pedidos y abanica
    _triangular_llamado con el contacto privado de solicitudes_web. Reclamar
    (triangulado=1) ANTES de enviar evita avisos duplicados, incluso si el bot se
    reinicia a mitad de barrido (se pierde ese aviso, nunca se duplica)."""
    while True:
        try:
            for p in db.pedidos_por_triangular():
                if not db.reclamar_triangulado(p["id"]):
                    continue
                sol = _solicitud_web(p["id"]) or {}
                vol = {"nombre": sol.get("nombre") or "Un vecino",
                       "contacto": sol.get("contacto")}
                try:
                    _triangular_llamado(p["id"], p.get("sector"), p.get("mensaje"), vol)
                except Exception as e:  # noqa: BLE001
                    print("barrido triangulador pedido", p["id"], "error:", e)
        except Exception as e:  # noqa: BLE001
            print("barrido triangulador error:", e)
        time.sleep(45)


def main():
    print("Bot de afiliación (Ángeles · Apoyo) escuchando…")
    db.init()
    _set_commands()
    threading.Thread(target=_barrido_triangulador, daemon=True).start()
    offset = None
    while True:
        resp = _tg("getUpdates", offset=offset, timeout=60,
                   allowed_updates=["message", "callback_query"])
        if not resp:
            time.sleep(5)
            continue
        for u in resp.get("result", []):
            offset = u["update_id"] + 1
            try:
                if u.get("message"):
                    manejar_mensaje(u["message"])
                elif u.get("callback_query"):
                    manejar_callback(u["callback_query"])
            except Exception as e:
                print("error manejando update:", e)


if __name__ == "__main__":
    main()
