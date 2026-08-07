"""
Backend del botón público "Reportar novedad".
Recibe el reporte del público y NOTIFICA a los moderadores por Telegram
(con botones para confirmar). NO cambia el estado por sí solo: eso lo decide
un moderador con un toque (lo maneja bot.py).

Corre detrás de nginx, escuchando solo en 127.0.0.1 (no expuesto directo).
NO tiene acceso a la sesión de Telegram. La API de Anthropic se usa SOLO en
/api/leer_flyer (leer la imagen de un pedido de ayuda), vía captura.leer_flyer.
"""
import base64
import datetime
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, Response
from werkzeug.exceptions import HTTPException

import db
import efimero
import obs
from config import (TELEGRAM_BOT_TOKEN, GRUPO_MODERADORES, BASE, PANEL_USER, PANEL_PASS,
                    APOYO_BOT_TOKEN, AUTO_APROBAR, INSTITUCION_API_KEY,
                    AUTO_PUBLICAR_PEDIDOS, CATS_PEDIR_MODERADAS)

app = Flask(__name__)
# Tope de payload: la imagen de un pedido llega en base64 (el navegador ya la
# reduce a ~≤900 KB); todo lo demás son JSON chicos. OJO: nginx también limita
# (client_max_body_size) — mantener ambos en sintonía.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
API_APOYO = f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}"  # bot de afiliación (maneja apra|)

db.init()  # asegura que existan las tablas (incluye contador de visitas)

# --- Observabilidad: logging a journald + Sentry opcional (apagado sin SENTRY_DSN) ---
obs.setup_logging("angeles-web")
obs.init_sentry("angeles-web")
obs.init_error_alerts("angeles-web")
log = obs.get_logger("angeles-web")


@app.errorhandler(Exception)
def _obs_unhandled(e):
    # Deja pasar 404/405/HTTP normales; captura solo errores de servidor reales y los
    # devuelve como JSON (antes: 500 HTML rompia r.json() en el navegador).
    if isinstance(e, HTTPException):
        return e
    log.exception("no manejada: %s %s", request.method, request.path)
    obs.capture_exception(e)
    return jsonify(ok=False, error="error interno"), 500

# --- Contador de visitas (sin cookies, sin guardar IPs) ---
SALT_FILE = BASE / "visita_salt"


def _secret_salt():
    """Salt aleatoria persistente; si no existe, se crea (chmod 600)."""
    try:
        return SALT_FILE.read_bytes()
    except FileNotFoundError:
        s = secrets.token_bytes(32)
        SALT_FILE.write_bytes(s)
        try:
            os.chmod(SALT_FILE, 0o600)
        except OSError:
            pass
        return s


def _hoy_vet():
    return datetime.datetime.now(ZoneInfo("America/Caracas")).strftime("%Y-%m-%d")


def _huella(ip, ua, dia):
    """Hash irreversible de (día+IP+navegador). La IP NUNCA se guarda; el día
    va dentro, así que el mismo visitante en días distintos no es vinculable."""
    if not ip:
        return None
    msg = f"{dia}|{ip}|{ua}".encode()
    return hmac.new(_secret_salt(), msg, hashlib.sha256).hexdigest()[:32]
# Cooldowns/topes anti-spam: viven en efimero (SQLite) — compartidos entre los
# workers de gunicorn (antes cada worker tenía su copia → límite real ~3×).
GLOBAL_GAP = 8          # mínimo 8 s entre notificaciones al grupo
GLOBAL_MAX_10MIN = 15   # máximo 15 notificaciones por cada 10 min


def _tg(metodo, _api=API, **params):
    payload = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
               for k, v in params.items() if v is not None}
    data = urllib.parse.urlencode(payload).encode()
    with urllib.request.urlopen(f"{_api}/{metodo}", data=data, timeout=20) as r:
        return json.load(r)


def _tg_foto(chat_id, foto_bytes, _api=API, **params):
    """sendPhoto subiendo la imagen (multipart). No se manda por URL: la imagen del
    pedido vive en una carpeta PRIVADA (no expuesta por nginx) hasta que un moderador
    lo publique, así que Telegram no podría bajarla. Mismo patrón que bot_apoyo."""
    boundary = "----angped" + str(int(time.time() * 1000))
    pre = []

    def campo(nombre, valor):
        pre.append(("--" + boundary + "\r\n"
                    'Content-Disposition: form-data; name="' + nombre + '"\r\n\r\n'
                    + str(valor) + "\r\n").encode())

    campo("chat_id", chat_id)
    for k, v in params.items():
        if v is not None:
            campo(k, json.dumps(v) if isinstance(v, (dict, list)) else v)
    pre.append(("--" + boundary + "\r\n"
                'Content-Disposition: form-data; name="photo"; filename="pedido.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n").encode())
    body = b"".join(pre) + foto_bytes + ("\r\n--" + boundary + "--\r\n").encode()
    req = urllib.request.Request(f"{_api}/sendPhoto", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.get("/api/datos.json")
def datos_json():
    """API pública de lectura (SIN datos personales): centros de acopio, sismos y
    conteos. CORS abierto para que otras plataformas puedan integrar estos datos."""
    try:
        centros = db.resumen_centros()
    except Exception:
        centros = []
    try:
        sismos = json.loads((BASE / "sismos_cache.json").read_text())
        if not isinstance(sismos, list):
            sismos = []
    except Exception:
        sismos = []
    try:
        visitas = db.resumen_visitas(_hoy_vet())["total"]
    except Exception:
        visitas = None
    payload = {
        "proyecto": "Ángeles de la Autopista",
        "sitio": "https://angelesdelaautopista.com",
        "actualizado": datetime.datetime.now(ZoneInfo("America/Caracas")).isoformat(),
        "centros": centros,
        "sismos": sismos,
        "conteos": {
            "centros": len(centros),
            "recibiendo": sum(1 for c in centros if c.get("estado") == "disponible"),
            "no_recibe": sum(1 for c in centros if c.get("estado") == "lleno"),
            "sismos": len(sismos),
            "visitas": visitas,
        },
        "nota": ("Datos públicos del canal de emergencias, organizados con IA. "
                 "No incluye datos personales. Verifica siempre en la fuente."),
    }
    resp = jsonify(payload)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "public, max-age=120"
    return resp


# ---------------------------------------------------------------------------
# API institucional (lectura, con API key): mismos datos que se publican en la
# web (red de colaboradores — registrados por Telegram Y WhatsApp, ambos caen
# en las mismas tablas). NUNCA los export_* del panel: esos incluyen fichas
# incompletas y necesidades ya cerradas que jamás llegaron a mostrarse al
# público. Aquí solo lo que un visitante ve hoy en la web.
# ---------------------------------------------------------------------------
INST_TOPE_VENTANA = 60      # segundos
INST_TOPE_MAX = 30          # máx. 30 requests/min en total (todas las claves juntas)


def _institucion_auth_ok():
    if not INSTITUCION_API_KEY:
        return False  # API desactivada si no hay key configurada
    return hmac.compare_digest(request.headers.get("X-API-Key", ""), INSTITUCION_API_KEY)


def _institucional_envelope(datos):
    payload = {
        "proyecto": "Red de Acopio y Refugio",
        "actualizado": datetime.datetime.now(ZoneInfo("America/Caracas")).isoformat(),
        "nota": "Mismos datos que se publican en la web pública. Sin datos personales (nombres/cédulas).",
        "datos": datos,
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.before_request
def _inst_rate_limit():
    if request.path.startswith("/api/institucional/"):
        if not _institucion_auth_ok():
            return Response(json.dumps({"error": "API key inválida o ausente (header X-API-Key)"}),
                             401, {"Content-Type": "application/json"})
        if not efimero.tope_global_ok("institucional", INST_TOPE_MAX, INST_TOPE_VENTANA):
            return Response(json.dumps({"error": "Demasiadas solicitudes, intenta en unos segundos"}),
                             429, {"Content-Type": "application/json"})


@app.get("/api/institucional/v1/puntos.json")
def institucional_puntos():
    """Centros de acopio, refugios y hospitales publicados (aprobados, ficha completa)."""
    return _institucional_envelope(db.resumen_directorio())


@app.get("/api/institucional/v1/oferta.json")
def institucional_oferta():
    """Inventario disponible en centros de acopio (lo que TIENEN)."""
    return _institucional_envelope(db.resumen_oferta())


@app.get("/api/institucional/v1/necesidades.json")
def institucional_necesidades():
    """Lo que NECESITAN refugios/hospitales, con conteo de personas (sin datos personales)."""
    return _institucional_envelope(db.resumen_necesidades())


@app.get("/api/institucional/v1/seguimiento.json")
def institucional_seguimiento():
    """Necesidades en seguimiento: meta, recibido, en camino (compromisos activos) y estado."""
    return _institucional_envelope(db.necesidades_web())


@app.get("/api/institucional/v1/situacion.json")
def institucional_situacion():
    """Situación consolidada de refugios: censo agregado por refugio + serie histórica."""
    return _institucional_envelope(db.resumen_situacion())


def _esc(s):
    return html.escape("" if s is None else str(s))


def _panel_auth_ok(header):
    # Exige usuario Y contraseña configurados: sin ambos, el panel queda cerrado.
    if not PANEL_USER or not PANEL_PASS or not header.startswith("Basic "):
        return False
    try:
        u, _, p = base64.b64decode(header[6:]).decode("utf-8", "ignore").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(u, PANEL_USER or "") and hmac.compare_digest(p, PANEL_PASS)


def _tabla(headers, filas):
    if not filas:
        return "<p class=m>—</p>"
    h = "<table><tr>" + "".join(f"<th>{_esc(x)}</th>" for x in headers) + "</tr>"
    for f in filas:
        h += "<tr>" + "".join(f"<td>{_esc(x)}</td>" for x in f) + "</tr>"
    return h + "</table>"


_PANEL_HTML = """<!doctype html><html lang=es><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>
<title>Panel Master</title><style>
body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#eef2f7;color:#0f1f33}
.top{background:linear-gradient(135deg,#0b2a4a,#163e6b);color:#fff;padding:20px 18px}
.top h1{margin:0;font-size:19px}.top p{margin:4px 0 0;font-size:13px;color:#cfe0f3}
.wrap{max-width:760px;margin:0 auto;padding:16px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;margin:14px 0}
.card h2{margin:0 0 6px;font-size:18px}.card h3{margin:14px 0 6px;font-size:14px;color:#334155}
table{width:100%;border-collapse:collapse;font-size:13.5px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef2f7;vertical-align:top}
th{color:#64748b;font-weight:700;font-size:11px;text-transform:uppercase}.m{color:#94a3b8;font-size:13px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
.s{flex:1;min-width:120px;background:#fff;border-radius:12px;padding:14px;text-align:center}
.s b{font-size:26px;display:block;color:#0b2a4a}.s span{font-size:12px;color:#64748b}
</style></head><body>
<div class=top><h1>🔐 Panel Master · capturas</h1><p>Privado · datos reportados por representantes. No es información oficial.</p></div>
<div class=wrap>__RESUMEN__ __SECCIONES__</div></body></html>"""


@app.get("/api/panel")
def panel():
    if not _panel_auth_ok(request.headers.get("Authorization", "")):
        return Response("Autenticacion requerida", 401, {"WWW-Authenticate": 'Basic realm="Panel Master"'})
    con = db.conectar()
    caps = con.execute("SELECT c.id,c.tipo_entrada,c.tipo_lectura,c.creado_en, COALESCE(r.punto,'(sin punto)') punto, r.punto_id pid, r.nombre rep, COALESCE(r.cedula,'') ced FROM capturas c LEFT JOIN representantes r ON r.id=c.representante_id WHERE c.estado='confirmado' ORDER BY c.creado_en DESC").fetchall()
    inv = con.execute("SELECT COALESCE(r.punto,'(sin punto)') punto, r.punto_id pid, i.categoria,i.item,i.cantidad,i.detalle FROM inventario i JOIN capturas c ON c.id=i.captura_id LEFT JOIN representantes r ON r.id=c.representante_id WHERE c.estado='confirmado'").fetchall()
    per = con.execute("SELECT COALESCE(r.punto,'(sin punto)') punto, r.punto_id pid, p.nombre,p.cedula,p.edad,p.detalle FROM censo_personas p JOIN capturas c ON c.id=p.captura_id LEFT JOIN representantes r ON r.id=c.representante_id WHERE c.estado='confirmado'").fetchall()
    con.close()
    # Agrupado por punto_id (identidad estable): dos puntos con el mismo nombre no se mezclan.
    def _key(r):
        return r["pid"] if r["pid"] is not None else "n:" + (r["punto"] or "(sin punto)")
    puntos = {}
    for r in caps:
        puntos.setdefault(_key(r), {"punto": r["punto"], "caps": [], "inv": [], "per": []})["caps"].append(r)
    for r in inv:
        puntos.setdefault(_key(r), {"punto": r["punto"], "caps": [], "inv": [], "per": []})["inv"].append(r)
    for r in per:
        puntos.setdefault(_key(r), {"punto": r["punto"], "caps": [], "inv": [], "per": []})["per"].append(r)
    secciones = ""
    for key in sorted(puntos, key=lambda k: puntos[k]["punto"]):
        d = puntos[key]
        secciones += f"<div class=card><h2>📍 {_esc(d['punto'])}</h2>"
        if d["per"]:
            secciones += f"<h3>👥 Personas ({len(d['per'])})</h3>" + _tabla(
                ["Nombre", "Cédula", "Edad", "Detalle"],
                [(x["nombre"], x["cedula"], x["edad"], x["detalle"]) for x in d["per"]])
        if d["inv"]:
            secciones += f"<h3>📦 Insumos ({len(d['inv'])})</h3>" + _tabla(
                ["Categoría", "Ítem", "Cant.", "Detalle"],
                [(x["categoria"], x["item"], x["cantidad"], x["detalle"]) for x in d["inv"]])
        secciones += f"<h3>🧾 Capturas ({len(d['caps'])})</h3>" + _tabla(
            ["Quién", "Cédula", "Vía", "Tipo", "Cuándo"],
            [(x["rep"], x["ced"], x["tipo_entrada"], x["tipo_lectura"], (x["creado_en"] or "")[:16]) for x in d["caps"]])
        secciones += "</div>"
    resumen = (f"<div class=stats><div class=s><b>{len(caps)}</b><span>capturas</span></div>"
               f"<div class=s><b>{len(puntos)}</b><span>puntos</span></div>"
               f"<div class=s><b>{len(per)}</b><span>personas</span></div>"
               f"<div class=s><b>{len(inv)}</b><span>insumos</span></div></div>"
               '<div class=card><h2>📊 Exportar base de datos (Excel)</h2>'
               '<p class=m>«Segura» = sin datos personales (para compartir con instituciones). '
               '«Completa» = incluye el censo de personas (uso interno).</p>'
               '<p><a href="/api/export?modo=segura">⬇️ Descargar versión SEGURA</a> &nbsp;·&nbsp; '
               '<a href="/api/export?modo=completa">⬇️ Descargar versión COMPLETA (con censo)</a></p></div>')
    pagina = (_PANEL_HTML.replace("__RESUMEN__", resumen)
              .replace("__SECCIONES__", secciones or "<p class=m>Aún no hay capturas confirmadas.</p>"))
    return Response(pagina, mimetype="text/html", headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.get("/api/export")
def export_maestro():
    """Excel maestro (mismo login del panel). ?modo=completa incluye el censo de personas."""
    if not _panel_auth_ok(request.headers.get("Authorization", "")):
        return Response("Autenticacion requerida", 401, {"WWW-Authenticate": 'Basic realm="Panel Master"'})
    import export
    completa = request.args.get("modo") == "completa"
    try:
        data = export.maestro_bytes(completa=completa)
    except Exception as e:
        log.exception("export maestro fallo")
        return Response(f"No se pudo generar el Excel: {e}", 500)
    nombre = "angeles_maestro_" + ("completo" if completa else "seguro") + ".xlsx"
    return Response(
        data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"',
                 "Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.post("/api/visita")
def visita():
    """Cuenta una visita y devuelve el acumulado. Solo lo llaman navegadores
    reales (corre por JS), así que los bots/scanners no inflan el conteo."""
    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr)
    ua = request.headers.get("User-Agent", "")[:200]
    dia = _hoy_vet()
    cutoff = (datetime.date.fromisoformat(dia) - datetime.timedelta(days=60)).isoformat()
    try:
        db.registrar_visita(dia, _huella(ip, ua, dia), cutoff)
        r = db.resumen_visitas(dia)
    except Exception:
        log.exception("visita fallo")
        return jsonify(ok=False), 500
    resp = jsonify(ok=True, **r)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/reportar")
def reportar():
    body = request.get_json(silent=True) or {}
    tg_id = body.get("tg_id")
    if tg_id is None:
        return jsonify(ok=False, error="falta tg_id"), 400

    personas = {p["tg_id"]: p for p in db.listar_desaparecidos()}
    persona = personas.get(int(tg_id)) if str(tg_id).lstrip("-").isdigit() else None
    if not persona:
        return jsonify(ok=False, error="no encontrado"), 404

    # Anti-spam: máximo un reporte por persona cada 60 s (compartido entre workers)
    if not efimero.cooldown_ok("reporte_persona", str(tg_id), 60):
        return jsonify(ok=True, msg="ya recibido")

    # Tope GLOBAL: protege al grupo de moderadores de una inundación de reportes
    if (not efimero.cooldown_ok("reporte_global", "*", GLOBAL_GAP)
            or not efimero.tope_global_ok("reporte", GLOBAL_MAX_10MIN, 600)):
        return jsonify(ok=True, msg="recibido")

    botones = {"inline_keyboard": [
        [{"text": "✅ Apareció", "callback_data": f"set|{tg_id}|aparecio"},
         {"text": "🕯️ Fallecida", "callback_data": f"set|{tg_id}|fallecida"}],
        [{"text": "❌ Ignorar reporte", "callback_data": f"ign|{tg_id}|x"}],
    ]}
    texto = (f"🔔 <b>Reporte del público</b>\n"
             f"Desaparecido/a: <b>{html.escape(persona['titulo'])}</b>\n"
             f"Alguien indica que hay una novedad.\n\n¿Qué confirmas?")
    try:
        _tg("sendMessage", chat_id=GRUPO_MODERADORES, text=texto,
            parse_mode="HTML", reply_markup=botones)
    except Exception:
        log.exception("reportar: no se pudo notificar a moderadores")
        return jsonify(ok=False, error="no se pudo notificar"), 502

    return jsonify(ok=True)


# --- Manos Solidarias por la WEB (formulario público /ofrecer.html) -------------
# Guarda como 'pendiente' y avisa a los moderadores con el bot de APOYO (para que el
# botón "apra|" lo maneje bot_apoyo.py, igual que las inscripciones por Telegram).
CATS_OFRECER = {
    "comida": "🍲 Comida", "agua": "💧 Agua", "salud": "🩹 Salud / primeros auxilios",
    "transporte": "🚐 Transporte", "carga": "🔌 Carga de teléfonos",
    "hospedaje": "🛏️ Hospedaje temporal", "herramientas": "🔦 Herramientas/linternas",
    "remoto": "💻 Remoto / En Línea", "estructural": "🏗️ Estructural / Inspección",
    "oficios": "🛠️ Oficios", "otro": "📦 Otra ayuda",
}
# Cooldown por IP y tope global: en efimero (compartidos entre workers).
OFRECER_GAP_IP = 30       # mínimo 30 s entre envíos de la misma IP
OFRECER_GLOBAL_MAX = 30   # máximo 30 envíos por cada 10 min


@app.post("/api/ofrecer")
def ofrecer():
    body = request.get_json(silent=True) or {}
    # Honeypot: los bots rellenan el campo oculto 'website'. Fingimos éxito y descartamos.
    if (body.get("website") or "").strip():
        return jsonify(ok=True)
    nombre = (body.get("nombre") or "").strip()[:80]
    categoria = (body.get("categoria") or "").strip()
    descripcion = (body.get("descripcion") or "").strip()[:600]
    zona = (body.get("zona") or "").strip()[:120]
    contacto = (body.get("contacto") or "").strip()[:60]
    horario = (body.get("horario") or "").strip()[:80]
    if categoria not in CATS_OFRECER:
        return jsonify(ok=False, error="categoría inválida"), 400
    if not (nombre and descripcion and zona and contacto):
        return jsonify(ok=False, error="faltan datos"), 400

    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if not efimero.cooldown_ok("ofrecer_ip", ip, OFRECER_GAP_IP):
        return jsonify(ok=True, msg="recibido")
    if not efimero.tope_global_ok("ofrecer", OFRECER_GLOBAL_MAX, 600):
        return jsonify(ok=True, msg="recibido")

    datos = {"nombre": nombre, "categoria": categoria, "descripcion": descripcion,
             "zona": zona, "contacto": contacto, "horario": horario or None,
             "lat": None, "lon": None}
    # Si ya existe un aliado con este mismo teléfono (registrado por web, Telegram o
    # WhatsApp), se actualiza su ficha en vez de crear un duplicado — así reenviar el
    # formulario para corregir algo no genera una segunda ficha. Solo una ficha NUEVA
    # se auto-publica y avisa a moderadores: una existente conserva su estado, por lo
    # que un aliado quitado de la web (denunciado) que reenvía NO vuelve solo.
    try:
        existente = db.aliado_por_contacto(contacto)
        es_nuevo = existente is None
        if existente:
            aid = existente["id"]
            db.actualizar_aliado(aid, datos)
        else:
            aid = db.crear_aliado(None, datos)
    except Exception:
        log.exception("ofrecer: no se pudo guardar aliado")
        return jsonify(ok=False, error="no se pudo guardar"), 500
    # Manos Solidarias se PUBLICA de una (auto-publicada). El aviso al grupo solo ofrece
    # QUITARLA, por si se denuncia como indebida o estafa.
    if es_nuevo:
        try:
            db.set_estado_aliado(aid, "aprobado")
        except Exception:
            pass

    if es_nuevo and GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        kb = {"inline_keyboard": [[{"text": "❌ Quitar de la web", "callback_data": f"apra|{aid}|no"}]]}
        cab = "🤝 <b>Nueva Manos Solidarias (web)</b> · ✅ <i>publicada</i> — quítala si es indebida o estafa"
        lineas = [cab, f"👤 {_esc(nombre)}", f"🏷️ {_esc(CATS_OFRECER[categoria])}",
                  f"✍️ {_esc(descripcion)}", f"📍 {_esc(zona)}", f"📞 {_esc(contacto)}"]
        if horario:
            lineas.append(f"🕐 {_esc(horario)}")
        try:
            _tg("sendMessage", _api=API_APOYO, chat_id=GRUPO_MODERADORES,
                text="\n".join(lineas), parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass  # ya quedó publicada; el aviso es solo para poder quitarla

    return jsonify(ok=True)


# --- Pedir ayuda por la WEB (formulario público /pedir.html) --------------------
# Crea un llamado en estado 'pendiente' (NO sale a la web) + guarda el contacto en la
# tabla privada solicitudes_web (jamás público) + avisa a los moderadores con botones
# ✅ Publicar / ❌ Descartar (bot APOYO, callback pweb|<lid>|ok/no). Al Publicar, el bot
# lo pasa a estado 'web' (sale anonimizado) y abanica a los acopios/MS de la zona.
from config import CATS_PEDIR  # categorías canónicas compartidas con los bots
# Cooldown por IP y tope global de /api/solicitar: en efimero.

# Carpeta PRIVADA para la imagen de un pedido (flyer de desaparecido, etc.).
# NO está bajo web/ → nadie la puede ver por URL mientras el pedido siga sin
# moderar. Al publicarse (estado 'web'), build_site la copia a web/img/.
PEDIDOS_IMG = BASE / "pedidos_img"
# La imagen llega en base64; el navegador ya la reduce (canvas → JPEG ≤ ~900 KB).
MAX_IMG_B64 = 1_400_000


def _decodificar_imagen(b64):
    """base64 → JPEG re-codificado con Pillow. Re-codificar valida que sea una
    imagen de verdad y BORRA los metadatos (EXIF/GPS de quien la subió)."""
    import io
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 40_000_000  # anti "decompression bomb"
    raw = base64.b64decode(b64, validate=True)
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@app.post("/api/leer_flyer")
def leer_flyer():
    """Lee con IA la imagen que alguien sube en pedir.html (flyer de persona
    desaparecida, aviso, lista) y devuelve los campos para PRELLENAR el formulario.
    No guarda nada: el pedido se crea después, en /api/solicitar, ya confirmado
    por la persona. Cada llamada cuesta (visión) → límites estrictos."""
    body = request.get_json(silent=True) or {}
    if (body.get("website") or "").strip():   # honeypot
        return jsonify(ok=True, legible=False)
    b64 = (body.get("imagen") or "").strip()
    if not b64 or len(b64) > MAX_IMG_B64:
        return jsonify(ok=False, error="imagen inválida o demasiado grande"), 400

    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if not efimero.cooldown_ok("flyer_ip", ip, 30):
        return jsonify(ok=False, error="espera un momento e intenta de nuevo"), 429
    if not efimero.tope_global_ok("flyer", 20, 600):
        return jsonify(ok=False, error="hay mucha demanda, intenta en unos minutos"), 429

    try:
        foto = _decodificar_imagen(b64)
    except Exception:
        return jsonify(ok=False, error="no parece una imagen válida"), 400
    try:
        import captura
        lec = captura.leer_flyer(foto)
    except Exception:
        log.exception("leer_flyer fallo")
        return jsonify(ok=False, error="no se pudo leer la imagen"), 502

    cat = (lec.categoria or "").strip()
    return jsonify(ok=True, legible=bool(lec.legible),
                   categoria=cat if cat in CATS_PEDIR else "",
                   descripcion=(lec.descripcion or "").strip()[:600],
                   zona=(lec.zona or "").strip()[:120],
                   contacto=(lec.contacto or "").strip()[:60],
                   nombre=(lec.nombre or "").strip()[:80])


# --- Buscador federado de personas (desaparecidos ↔ hospitales/rescatados) ------
# Consulta EN VIVO fuentes comunitarias externas (fuentes.py: Buscador Multiorigen,
# Hospitales en Venezuela, Red Ayuda Venezuela). Solo referencia, nunca confirmación;
# cédulas/teléfonos salen enmascarados desde fuentes.py. Lo usan web/buscar.html y
# la sugerencia en vivo de pedir.html (categoría desaparecido).


@app.get("/api/buscar_persona")
def buscar_persona():
    q = (request.args.get("q") or "").strip()[:80]
    if len(q) < 3:
        return jsonify(ok=False, error="escribe al menos 3 letras"), 400
    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    # Las fuentes son de terceros y gratis: frenos generosos pero firmes.
    if not efimero.cooldown_ok("buscar_ip", ip, 4):
        return jsonify(ok=False, error="espera un momento e intenta de nuevo"), 429
    if not efimero.tope_global_ok("buscar", 90, 600):
        return jsonify(ok=False, error="hay mucha demanda, intenta en unos minutos"), 429
    try:
        import fuentes
        r = fuentes.buscar_todo(q)
    except Exception:
        log.exception("buscar_persona fallo")
        return jsonify(ok=False, error="fuentes no disponibles"), 502
    if r.get("error"):
        return jsonify(ok=False, error=r["error"]), 400
    return jsonify(ok=True, **r)


def _avisar_referencias_moderadores(lid, persona):
    """Best-effort, en HILO: si la persona buscada aparece en las fuentes externas,
    manda un 2.º mensaje al grupo de moderadores (junto al aviso pweb| del pedido)
    para que lo vean ANTES de publicar. No toca el pedido ni decide nada."""
    try:
        import fuentes
        refs = fuentes.referencias(persona)
        if not refs:
            return
        lineas = ([f"🔎 <b>Referencias para el pedido #{lid}</b> "
                   f"(buscan a <b>{html.escape(persona)}</b>):"]
                  + fuentes.resumen_referencias(refs, html_out=True)
                  + ["<i>Información comunitaria, no oficial — confirmar llamando "
                     "al centro antes de actuar.</i>"])
        _tg("sendMessage", _api=API_APOYO, chat_id=GRUPO_MODERADORES,
            text="\n".join(lineas), parse_mode="HTML")
    except Exception as e:
        print("avisar_referencias error:", e)


@app.post("/api/solicitar")
def solicitar():
    body = request.get_json(silent=True) or {}
    # Honeypot: los bots rellenan el campo oculto 'website'. Fingimos éxito y descartamos.
    if (body.get("website") or "").strip():
        return jsonify(ok=True)
    nombre = (body.get("nombre") or "").strip()[:80]
    categoria = (body.get("categoria") or "").strip()
    descripcion = (body.get("descripcion") or "").strip()[:600]
    zona = (body.get("zona") or "").strip()[:120]
    contacto = (body.get("contacto") or "").strip()[:60]
    persona = (body.get("persona") or "").strip()[:80]  # a quién buscan (desaparecido)
    if categoria not in CATS_PEDIR:
        return jsonify(ok=False, error="categoría inválida"), 400
    if not (descripcion and zona and contacto):
        return jsonify(ok=False, error="faltan datos"), 400

    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if not efimero.cooldown_ok("pedir_ip", ip, 30):
        return jsonify(ok=True, msg="recibido")
    if not efimero.tope_global_ok("pedir", 30, 600):
        return jsonify(ok=True, msg="recibido")

    # Imagen opcional (flyer de desaparecido, etc.). Se valida ANTES de crear el
    # pedido para no dejar un llamado sin la imagen que lo sustenta.
    img_b64 = (body.get("imagen") or "").strip()
    foto_bytes = None
    if img_b64:
        if len(img_b64) > MAX_IMG_B64:
            return jsonify(ok=False, error="la imagen es demasiado grande"), 400
        try:
            foto_bytes = _decodificar_imagen(img_b64)
        except Exception:
            return jsonify(ok=False, error="no parece una imagen válida"), 400

    # Mensaje PÚBLICO anonimizado (sin nombre ni contacto). AUTO-PUBLICACIÓN
    # (config.AUTO_PUBLICAR_PEDIDOS): nace 'pendiente' y unos renglones más abajo —
    # con el contacto privado ya guardado — pasa a 'web' con db.publicar_pedido; el
    # barrido de bot_apoyo.py le avisa a la red y los moderadores reciben reporte +
    # botón Remover. Desaparecidos (CATS_PEDIR_MODERADAS): quedan 'pendiente' hasta
    # que un moderador los publique (datos de un tercero).
    mensaje = "{}: {}".format(CATS_PEDIR[categoria], descripcion)
    auto = AUTO_PUBLICAR_PEDIDOS and categoria not in CATS_PEDIR_MODERADAS
    try:
        lid = db.crear_llamado(None, None, zona, mensaje, None)
        db.set_estado_llamado(lid, "pendiente")
    except Exception:
        log.exception("solicitar: no se pudo guardar el pedido de ayuda")
        return jsonify(ok=False, error="no se pudo guardar"), 500
    # Contacto PRIVADO (jamás a la web): mismo helper que usan los bots (tabla solicitudes_web).
    try:
        db.guardar_contacto_pedido(lid, nombre, contacto, categoria)
    except Exception:
        pass
    # Desaparecido: se guarda a QUIÉN buscan (para el cron depurador) y se revisa en
    # segundo plano si ya aparece en hospitales/rescatados (aviso extra a moderadores).
    if categoria == "desaparecido" and persona:
        try:
            db.guardar_persona_desaparecida(lid, persona)
        except Exception:
            pass
        threading.Thread(target=_avisar_referencias_moderadores,
                         args=(lid, persona), daemon=True).start()
    # Publicar DESPUÉS de guardar el contacto: el barrido arma los avisos leyendo
    # solicitudes_web. Si esto falla, el pedido queda 'pendiente' (circuito viejo).
    if auto:
        try:
            db.publicar_pedido(lid)
        except Exception:
            auto = False
    # Imagen a la carpeta PRIVADA. build_site solo la copia a web/img/ cuando el
    # pedido esté publicado; si el moderador lo descarta, el bot la borra.
    if foto_bytes:
        try:
            PEDIDOS_IMG.mkdir(exist_ok=True)
            (PEDIDOS_IMG / f"{lid}.jpg").write_bytes(foto_bytes)
        except Exception:
            foto_bytes = None  # sin imagen, pero el pedido sigue

    if GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        if auto:
            kb = {"inline_keyboard": [[
                {"text": "✅ Marcar resuelto", "callback_data": f"pat|{lid}|res"},
                {"text": "🗑 Remover de la web", "callback_data": f"pweb|{lid}|no"}]]}
            titulo = "🆘 <b>Pedido de ayuda (web)</b> · <i>publicado · puedes removerlo</i>"
        else:
            kb = {"inline_keyboard": [[
                {"text": "✅ Publicar y avisar a la red", "callback_data": f"pweb|{lid}|ok"},
                {"text": "❌ Descartar", "callback_data": f"pweb|{lid}|no"}]]}
            titulo = "🆘 <b>Pedido de ayuda (web)</b> · <i>revisar antes de publicar</i>"
        lineas = [titulo,
                  f"🏷️ {_esc(CATS_PEDIR[categoria])}"]
        if nombre:
            lineas.append(f"👤 {_esc(nombre)}")
        if categoria == "desaparecido" and persona:
            lineas.append(f"🔎 Busca a: <b>{_esc(persona)}</b>")
        lineas += [f"✍️ {_esc(descripcion)}", f"📍 {_esc(zona)}", f"📞 {_esc(contacto)}"]
        if foto_bytes:
            lineas.append("📸 <i>Incluye imagen: sale junto al pedido publicado.</i>")
        try:
            if foto_bytes:
                # El caption de Telegram admite máx. 1024 chars → se recorta con aviso.
                cap = "\n".join(lineas)
                if len(cap) > 1000:
                    cap = cap[:990] + "…"
                _tg_foto(GRUPO_MODERADORES, foto_bytes, _api=API_APOYO,
                         caption=cap, parse_mode="HTML", reply_markup=kb)
            else:
                _tg("sendMessage", _api=API_APOYO, chat_id=GRUPO_MODERADORES,
                    text="\n".join(lineas), parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass  # el pedido ya quedó guardado; el aviso es best-effort

    # `publicado` le dice al formulario qué mensaje de éxito mostrar (al instante
    # en la web vs. en revisión — desaparecidos).
    return jsonify(ok=True, publicado=auto)


# --- Atender un pedido de ayuda (desde la web) ----------------------------------
# Modelo A (autoservicio con registro): quien va a ayudar deja SU nombre + WhatsApp →
# se guarda en 'atenciones', el pedido pasa a estado 'atencion', se avisa a los
# moderadores (trazabilidad) y se le DEVUELVE el WhatsApp del solicitante para que lo
# contacte. Cerrar el pedido: /api/resolver (con el token) o el moderador desde Telegram.
# Cooldown por IP y tope global de /api/atender: en efimero.


def _tabla_atenciones():
    con = db.conectar()
    con.execute(
        "CREATE TABLE IF NOT EXISTS atenciones ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, llamado_id INTEGER, nombre TEXT, "
        "contacto TEXT, nota TEXT, token TEXT, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)")
    con.commit()
    con.close()


@app.post("/api/atender")
def atender():
    import secrets
    body = request.get_json(silent=True) or {}
    if (body.get("website") or "").strip():
        return jsonify(ok=True)
    try:
        lid = int(body.get("llamado_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="id inválido"), 400
    nombre = (body.get("nombre") or "").strip()[:80]
    contacto = (body.get("contacto") or "").strip()[:60]
    nota = (body.get("nota") or "").strip()[:300]
    if not (nombre and contacto):
        return jsonify(ok=False, error="faltan datos"), 400

    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if not efimero.cooldown_ok("atender_ip", ip, 20):
        return jsonify(ok=False, error="espera un momento"), 429
    if not efimero.tope_global_ok("atender", 40, 600):
        return jsonify(ok=False, error="intenta más tarde"), 429

    # El pedido debe existir y estar publicado ('web') o ya en atención ('atencion').
    con = db.conectar()
    row = con.execute("SELECT estado, sector, mensaje FROM llamados WHERE id = ?", (lid,)).fetchone()
    con.close()
    if not row or row["estado"] not in ("web", "atencion"):
        return jsonify(ok=False, error="ese pedido ya no está disponible"), 404

    # R1: el contacto PRIVADO del solicitante NO se revela; se usa para relay opt-in.
    try:
        import contacto_ms
        sol = contacto_ms.crear_atencion(lid, nombre, contacto, nota or None)
    except Exception:
        log.exception("atender: no se pudo registrar la intencion de atencion")
        return jsonify(ok=False, error="no se pudo registrar"), 500
    if not sol:
        return jsonify(ok=False, error="no hay contacto para este pedido"), 404

    token = secrets.token_urlsafe(8)
    try:
        _tabla_atenciones()
        con = db.conectar()
        con.execute(
            "INSERT INTO atenciones (llamado_id, nombre, contacto, nota, token) VALUES (?,?,?,?,?)",
            (lid, nombre, contacto, nota or None, token))
        con.commit()
        con.close()
        db.set_estado_llamado(lid, "atencion")
    except Exception:
        log.exception("atender: no se pudo registrar la atencion")
        return jsonify(ok=False, error="no se pudo registrar"), 500

    try:
        contacto_ms.notificar_solicitante_atencion(sol["id"])
    except Exception:
        pass

    if GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        kb = {"inline_keyboard": [[
            {"text": "✅ Marcar resuelto", "callback_data": f"pat|{lid}|res"},
            {"text": "🗑️ Quitar", "callback_data": f"pat|{lid}|del"}]]}
        lineas = ["🤝 <b>Alguien pidió atender un pedido</b>",
                  f"🆘 {_esc(row['mensaje'] or '')}",
                  f"📍 {_esc(row['sector'] or '')}",
                  f"🙋 Ayuda: <b>{_esc(nombre)}</b> · 📞 {_esc(contacto)}",
                  f"🔐 Relay: solicitud #{sol['id']} enviada al solicitante para opt-in."]
        if nota:
            lineas.append(f"✍️ {_esc(nota)}")
        try:
            _tg("sendMessage", _api=API_APOYO, chat_id=GRUPO_MODERADORES,
                text="\n".join(lineas), parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    return jsonify(ok=True, token=token, relay=True, solicitud_id=sol["id"])


# --- 🤝 Solicitar contacto de una Mano Solidaria (aliado) ---------------------
# La web NUNCA muestra el número del aliado. El interesado llena el mini-formulario
# (build_site.py → abrirContactoMS): aquí se registra en bitácora (contacto_ms.crear)
# y se le avisa al aliado por su canal (Telegram/WhatsApp) con botones ✅/❌. El
# cruce de números lo hace bot_apoyo.py / bot_wa.py al recibir la respuesta.
@app.post("/api/contactar-ms")
def contactar_ms():
    import contacto_ms
    body = request.get_json(silent=True) or {}
    # Honeypot: los bots rellenan el campo oculto 'website'. Fingimos éxito y descartamos.
    if (body.get("website") or "").strip():
        return jsonify(ok=True, token=secrets.token_urlsafe(16))
    try:
        aliado_id = int(body.get("aliado_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="id inválido"), 400
    nombre = (body.get("nombre") or "").strip()[:80]
    contacto = (body.get("contacto") or "").strip()[:60]
    mensaje = (body.get("mensaje") or "").strip()[:300]
    if not (nombre and contacto):
        return jsonify(ok=False, error="faltan datos"), 400

    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if not efimero.cooldown_ok("contactar_ms_ip", ip, 20):
        return jsonify(ok=False, error="espera un momento"), 429
    if not efimero.tope_global_ok("contactar_ms", 40, 600):
        return jsonify(ok=False, error="intenta más tarde"), 429

    try:
        sol = contacto_ms.crear(aliado_id, nombre, contacto, mensaje)
    except Exception:
        log.exception("contactar_ms: no se pudo registrar la solicitud")
        return jsonify(ok=False, error="no se pudo registrar"), 500
    if not sol:
        return jsonify(ok=False, error="esa Mano Solidaria ya no está disponible"), 404

    # La solicitud ya quedó en bitácora; el aviso al aliado es best-effort (si el I/O
    # falla, run_once.py/contacto_ms.revisar reintenta el recordatorio a las 12 h).
    try:
        contacto_ms.notificar_aliado(sol["id"])
    except Exception:
        pass
    return jsonify(ok=True, token=sol["token"])


# Página de estado de una solicitud (el token es privado del interesado).
@app.get("/api/contacto-ms/<token>")
def contacto_ms_estado(token):
    import contacto_ms
    sol = contacto_ms.por_token(token)
    if not sol:
        return Response("<h1>Solicitud no encontrada</h1>", status=404, mimetype="text/html")
    estado = sol["estado"]
    ali_nombre = _esc(sol.get("aliado_nombre") or "la Mano Solidaria")
    detalle = ""
    if estado == "aceptada":
        ali = db.aliado(sol["aliado_id"]) or {}
        num = (ali.get("contacto") or ali.get("wa_phone") or "").strip()
        dest = contacto_ms._wa_dest(num)  # mismo cableado de números que el resto del flujo
        titulo, color = "✅ ¡Solicitud aceptada!", "#1E9C94"
        cuerpo = (f"{ali_nombre} aceptó compartir su contacto contigo. "
                  "También te avisamos por WhatsApp.")
        if dest:
            detalle = (f'<a class="btn ok" style="text-decoration:none;text-align:center;'
                       f'box-sizing:border-box" href="https://wa.me/{dest}" target="_blank" '
                       f'rel="noopener">💬 Escribir por WhatsApp</a>')
        elif num:
            detalle = f'<p class="fin">📞 {_esc(num)}</p>'
    elif estado == "rechazada":
        titulo, color = "🙏 No disponible por ahora", "#C53030"
        cuerpo = (f"{ali_nombre} no puede atender tu solicitud por ahora. "
                  "Puedes intentar con otra Mano Solidaria en la web.")
    elif estado == "expirada":
        titulo, color = "⌛ Solicitud vencida", "#718096"
        cuerpo = (f"{ali_nombre} no respondió en 24 horas, así que cerramos la solicitud. "
                  "Puedes intentar con otra Mano Solidaria en la web.")
    else:  # pendiente
        titulo, color = "🕊️ Solicitud enviada", "#B7791F"
        cuerpo = (f"Le avisamos a {ali_nombre}. Tiene hasta 24 horas para responder. "
                  "Si acepta, te escribimos por WhatsApp y esta página se actualiza.")
    refresca = '<meta http-equiv="refresh" content="30">' if estado == "pendiente" else ""
    html_doc = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresca}
<title>Solicitud de contacto — Red de Acopio y Refugio</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
<style>
 body{{font-family:Poppins,sans-serif;margin:0;background:#f4f7f9;color:#1a2b3c}}
 .top{{background:#003C67;color:#fff;padding:18px 16px}} .top h1{{margin:0;font-size:1.1rem}}
 .card{{max-width:520px;margin:16px auto;background:#fff;border-radius:14px;
        box-shadow:0 2px 10px rgba(0,60,103,.08);padding:22px}}
 .badge{{display:inline-block;padding:3px 12px;border-radius:99px;color:#fff;
        font-size:.8rem;background:{color}}}
 h2{{margin:6px 0 10px;font-size:1.2rem}} p{{line-height:1.5;font-size:.95rem}}
 .btn{{display:block;width:100%;border:0;border-radius:10px;padding:13px;margin-top:14px;
      font-family:inherit;font-size:1rem;font-weight:600;cursor:pointer}}
 .ok{{background:#1E9C94;color:#fff}} .fin{{text-align:center;font-weight:600;margin-top:12px}}
 a.red{{display:block;text-align:center;margin-top:16px;color:#1E9C94;font-weight:600}}
</style></head><body>
<div class="top"><h1>🤝 Solicitud de contacto</h1>
<div style="font-size:.85rem;opacity:.85">Red de Acopio y Refugio · <span class="badge">{titulo}</span></div></div>
<div class="card">
<h2>{titulo}</h2>
<p>{cuerpo}</p>
{detalle}
<a class="red" href="https://redacopioyrefugio.com/#manos">🌐 Ver Manos Solidarias</a>
</div></body></html>"""
    return Response(html_doc, mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.post("/api/resolver")
def resolver():
    body = request.get_json(silent=True) or {}
    try:
        lid = int(body.get("llamado_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="id inválido"), 400
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify(ok=False, error="falta token"), 400
    try:
        con = db.conectar()
        m = con.execute(
            "SELECT id FROM atenciones WHERE llamado_id = ? AND token = ?", (lid, token)).fetchone()
        con.close()
    except Exception:
        m = None
    if not m:
        return jsonify(ok=False, error="no autorizado"), 403
    try:
        db.set_estado_llamado(lid, "resuelto")
    except Exception:
        log.exception("resolver fallo")
        return jsonify(ok=False, error="no se pudo"), 500
    if GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        try:
            _tg("sendMessage", _api=API_APOYO, chat_id=GRUPO_MODERADORES,
                text=f"✅ <b>Pedido #{lid} marcado como resuelto</b> por quien lo atendió.",
                parse_mode="HTML")
        except Exception:
            pass
    return jsonify(ok=True)


# --- Registro de CENTROS por la web (tercera vía, paridad con los bots) ---------
# Crea el representante con la MISMA ficha esencial del alta corta de los bots
# (ubicación, horario, contacto) y el MISMO circuito: AUTO_APROBAR (fundaciones
# siempre a revisión manual), aviso al grupo con los botones apr| que bot_apoyo ya
# procesa, y wa_phone canónico para que bot_wa reconozca el número cuando escriba
# (continuidad entre las 3 vías sin re-registro). Cooldowns en efimero.

TIPOS_CENTRO_WEB = {"acopio": "📦 Centro de acopio", "refugio": "🏠 Refugio",
                    "hospital": "🏥 Hospital", "cdi": "🩺 CDI",
                    "maternidad": "🤰 Maternidad", "clinica": "➕ Clínica",
                    "fundacion": "🏛 Fundación", "otro": "📍 Otro"}


@app.post("/api/registrar_centro")
def registrar_centro():
    body = request.get_json(silent=True) or {}
    if (body.get("website") or "").strip():
        return jsonify(ok=True)
    nombre = (body.get("nombre") or "").strip()[:80]
    tipo = (body.get("tipo") or "").strip()
    punto = (body.get("punto") or "").strip()[:100]
    ubicacion = (body.get("ubicacion") or "").strip()[:240]
    horario = (body.get("horario") or "").strip()[:120]
    contacto = (body.get("contacto") or "").strip()[:60]
    cedula = (body.get("cedula") or "").strip()[:20]
    rif = (body.get("rif") or "").strip()[:30]
    cobertura = (body.get("cobertura") or "").strip()[:120]
    areas = (body.get("areas") or "").strip()[:200]
    nec_item = (body.get("nec_item") or "").strip()[:80]
    nec_cantidad = (body.get("nec_cantidad") or "").strip()[:30]
    canal_nombre = (body.get("canal_nombre") or "").strip()[:100]
    if tipo not in TIPOS_CENTRO_WEB:
        return jsonify(ok=False, error="tipo inválido"), 400
    if not (nombre and punto and ubicacion and horario and contacto):
        return jsonify(ok=False, error="faltan datos"), 400
    if tipo == "fundacion" and not (cobertura and areas):
        return jsonify(ok=False, error="para fundaciones, cobertura y áreas son obligatorias"), 400

    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if not efimero.cooldown_ok("regcentro_ip", ip, 30):
        return jsonify(ok=False, error="espera un momento e intenta de nuevo"), 429
    if not efimero.tope_global_ok("regcentro", 20, 600):
        return jsonify(ok=False, error="intenta más tarde"), 429

    # Punto ya censado → NO duplicar desde la web: sumarse como colaborador va por bot
    # (necesita el failsafe del dueño, que vive en los bots).
    dueno = db.buscar_punto(punto)
    if dueno:
        return jsonify(ok=False, existe=True,
                       error="Ese punto ya está en la red. Si trabajas ahí, súmate como "
                             "colaborador por WhatsApp o Telegram (sin repetir la ficha)."), 409

    ficha = {"ubicacion": ubicacion, "horario": horario, "contacto": contacto}
    if tipo == "fundacion":
        ficha.update({"rif": rif or "No posee", "cobertura": cobertura, "areas": areas})
    rid = db.crear_representante(None, nombre, tipo, punto, contacto,
                                 ficha=ficha, cedula=cedula or None)
    # wa_phone canónico: cuando esta persona escriba al bot de WhatsApp desde ese
    # número, resolver_identidad la reconoce como su centro (queda vinculada).
    try:
        from wa_util import canonico
        wa = canonico(contacto)
        if wa:
            con = db.conectar()
            con.execute("UPDATE representantes SET wa_phone = ? WHERE id = ?", (wa, rid))
            con.commit()
            con.close()
    except Exception:
        pass

    es_fund = (tipo == "fundacion")
    auto_ok = AUTO_APROBAR and not es_fund
    if auto_ok:
        db.set_estado_representante(rid, "aprobado")

    # Autoregistro desde el canal: si venía RECLAMANDO un punto detectado en
    # Emergencias Viales, vincula todos sus reportes al nuevo responsable (y la
    # sección «Detectados en el canal» deja de mostrarlo en el próximo build).
    canal_n = 0
    if canal_nombre:
        try:
            canal_n = db.reclamar_centros_canal(canal_nombre, rid)
        except Exception:
            canal_n = 0

    # Siembra opcional: las primeras necesidades con meta (hasta 5 en el mismo
    # registro; mismo cableado que /necesito: web con barra + botón «Lo llevo»,
    # y puente demanda→oferta a los acopios que tengan cada insumo).
    pedidos_nec = body.get("necesidades") or []
    if not isinstance(pedidos_nec, list):
        pedidos_nec = []
    if nec_item:   # compat con el formato de un solo insumo
        pedidos_nec.append({"item": nec_item, "cantidad": nec_cantidad})
    creadas = []
    if auto_ok:
        for n in pedidos_nec[:5]:
            try:
                item = str((n or {}).get("item") or "").strip()[:80]
                if not item:
                    continue
                meta, unidad = db.parse_cantidad(str((n or {}).get("cantidad") or ""))
                nid = db.abrir_necesidad(rid, rid, punto, tipo, item, meta=meta, unidad=unidad)
                if nid:
                    creadas.append((nid, item, meta, unidad))
            except Exception:
                continue
        if any(c[2] for c in creadas):
            def _puente():
                try:
                    import bot_apoyo
                    rep = db.representante(rid)
                    bot_apoyo._avisar_acopios_demanda(dict(rep), [c for c in creadas if c[2]])
                except Exception as e:
                    print("puente registro web error:", e)
            threading.Thread(target=_puente, daemon=True).start()

    if GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        if auto_ok:
            kb = {"inline_keyboard": [[{"text": "❌ Quitar de la web", "callback_data": f"apr|{rid}|no"}]]}
            cab = "🆕 <b>Nueva afiliación (web)</b> · ✅ <i>aprobada automáticamente</i>"
        else:
            kb = {"inline_keyboard": [[{"text": "✅ Aprobar", "callback_data": f"apr|{rid}|ok"},
                                       {"text": "❌ Rechazar", "callback_data": f"apr|{rid}|no"}]]}
            cab = "🆕 <b>Nueva afiliación (web)</b> · ⏳ <i>pendiente</i>"
        lineas = [cab, f"👤 {_esc(nombre)}"]
        lineas.append(f"🪪 CI: {_esc(cedula)}" if cedula else "🪪 <b>Sin cédula</b> (registro web)")
        lineas.append(f"{TIPOS_CENTRO_WEB.get(tipo, 'Punto')}: <b>{_esc(punto)}</b>")
        lineas.append(f"📍 {_esc(ubicacion)}")
        lineas.append(f"🕐 {_esc(horario)} · 📞 {_esc(contacto)}")
        if es_fund:
            lineas.append(f"🪪 RIF: {_esc(rif or 'No posee')} · 🗺️ {_esc(cobertura)} · 🎯 {_esc(areas)}")
        if canal_n:
            lineas.append(f"📡 <b>Reclamó un punto del canal</b> ({canal_n} reporte{'s' if canal_n != 1 else ''} vinculados)")
        if creadas:
            det = ", ".join(f"{i} ({m:g}{(' ' + u) if u else ''})" if m else i
                            for _, i, m, u in creadas)
            lineas.append(f"🙏 Necesidades declaradas: {_esc(det)}")
        try:
            _tg("sendMessage", _api=API_APOYO, chat_id=GRUPO_MODERADORES,
                text="\n".join(lineas), parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass

    return jsonify(ok=True, aprobado=bool(auto_ok), necesidades=len(creadas))


# --- Compromiso desde la web con una necesidad ("Lo llevo", sin salir de la web) -
# Mismo circuito que el deep link nec_ de los bots: db.crear_compromiso (reserva
# que vence sola en 12 h, sin sobre-comprometer) + aviso al dueño del punto por SU
# canal (Telegram o WhatsApp, con el mismo botón recibi| que ambos bots procesan)
# + trazabilidad al grupo de moderadores. Al donante se le devuelven dirección,
# horario y contacto del punto (datos ya públicos en el directorio): ve y entrega.

def _avisar_centro_compromiso_web(nec, cant, nombre, wa_donante):
    """Best-effort, corre en hilo. Paridad con bot_apoyo._notificar_centro_compromiso."""
    dueno = db.dueno_de_punto(nec.get("punto_id")) if nec else None
    if not dueno:
        return
    item = nec.get("item") or ""
    if dueno.get("telegram_id"):
        kb = {"inline_keyboard": [[{"text": "✅ Ya lo recibí", "callback_data": f"recibi|{nec['id']}"}]]}
        try:
            _tg("sendMessage", _api=API_APOYO, chat_id=dueno["telegram_id"], parse_mode="HTML",
                text=(f"🤝 <b>{_esc(nombre)}</b> se comprometió por la web a llevar <b>{cant}</b> "
                      f"de <b>{_esc(item)}</b> a tu punto.\n📱 WhatsApp: <b>{_esc(wa_donante)}</b>\n\n"
                      "Coordina la entrega. Cuando lo recibas, toca «Ya lo recibí» para actualizar la barra."),
                reply_markup=kb)
        except Exception:
            pass
        return
    if dueno.get("wa_phone"):
        try:
            import wa_api
            wa_api.enviar_botones(
                dueno["wa_phone"],
                f"🤝 *{nombre}* se comprometió por la web a llevar *{cant}* de *{item}* a tu punto.\n"
                f"📱 WhatsApp: *{wa_donante}*\n\n"
                "Coordina la entrega. Cuando lo recibas, toca «Ya lo recibí».",
                [(f"recibi|{nec['id']}", "✅ Ya lo recibí")])
        except Exception:
            pass


@app.post("/api/comprometer")
def comprometer():
    body = request.get_json(silent=True) or {}
    if (body.get("website") or "").strip():
        return jsonify(ok=True)
    try:
        nid = int(body.get("necesidad_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="id inválido"), 400
    nombre = (body.get("nombre") or "").strip()[:80]
    contacto = (body.get("contacto") or "").strip()[:60]
    try:
        cantidad = max(1, int(str(body.get("cantidad") or "").strip()))
    except (TypeError, ValueError):
        cantidad = 0
    if not (nombre and contacto and cantidad):
        return jsonify(ok=False, error="faltan datos"), 400

    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if not efimero.cooldown_ok("comprometer_ip", ip, 20):
        return jsonify(ok=False, error="espera un momento"), 429
    if not efimero.tope_global_ok("comprometer", 40, 600):
        return jsonify(ok=False, error="intenta más tarde"), 429

    cid, nec = db.crear_compromiso(nid, None, nombre, cantidad)
    if not cid:
        return jsonify(ok=False, error="esa necesidad ya está cubierta o no está disponible"), 409

    threading.Thread(target=_avisar_centro_compromiso_web,
                     args=(dict(nec), cantidad, nombre, contacto), daemon=True).start()

    if GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        try:
            _tg("sendMessage", _api=API_APOYO, chat_id=GRUPO_MODERADORES, parse_mode="HTML",
                text=(f"🚚 <b>Compromiso desde la web</b>\n"
                      f"🙋 {_esc(nombre)} · 📱 {_esc(contacto)}\n"
                      f"Lleva <b>{cantidad}</b> de <b>{_esc(nec.get('item') or '')}</b> a "
                      f"<b>{_esc(nec.get('punto') or '')}</b> (la reserva vence sola en 12 h)."))
        except Exception:
            pass

    dueno = db.dueno_de_punto(nec.get("punto_id")) or {}
    centro = {"punto": nec.get("punto"), "ubicacion": dueno.get("ubicacion"),
              "horario": dueno.get("horario"), "contacto": dueno.get("telefono")}
    return jsonify(ok=True, centro=centro)


# --- Respuesta desde la web a un LLAMADO de apoyo (ayuda puntual, sin registro) --
# Paridad con el deep link lla_ de los bots: db.responder_llamado + aviso a quien
# levantó el llamado por SU canal (Telegram directo; o WhatsApp vía su registro de
# voluntario). El respondiente no necesita cuenta: nombre + WhatsApp y listo.

def _avisar_voluntario_respuesta_web(lla, nombre, contacto, mensaje):
    """Best-effort, corre en hilo."""
    try:
        if lla.get("telegram_id"):
            cuerpo = (f"🙋 <b>{_esc(nombre)}</b> respondió a tu llamado"
                      + (f" de <b>{_esc(lla.get('sector'))}</b>" if lla.get("sector") else "")
                      + " (desde la web).\n"
                      + (f"Ofrece: <i>{_esc(mensaje)}</i>\n" if mensaje else "")
                      + f"📱 WhatsApp: <b>{_esc(contacto)}</b>\n\nCoordina con esta persona la ayuda. 🤝")
            _tg("sendMessage", _api=API_APOYO, chat_id=lla["telegram_id"],
                parse_mode="HTML", text=cuerpo)
            return
        vol = None
        if lla.get("voluntario_id"):
            con = db.conectar()
            vol = con.execute("SELECT * FROM voluntarios WHERE id = ?",
                              (lla["voluntario_id"],)).fetchone()
            con.close()
        dwa = vol["wa_phone"] if (vol and vol["wa_phone"]) else None
        if dwa:
            import wa_api
            wa_api.enviar_texto(
                dwa, f"🙋 *{nombre}* respondió a tu llamado"
                     + (f" de *{lla.get('sector')}*" if lla.get("sector") else "")
                     + " (desde la web).\n"
                     + (f"Ofrece: _{mensaje}_\n" if mensaje else "")
                     + f"📱 WhatsApp: *{contacto}*\n\nCoordina con esta persona la ayuda. 🤝")
    except Exception:
        pass


@app.post("/api/responder_llamado")
def responder_llamado_web():
    body = request.get_json(silent=True) or {}
    if (body.get("website") or "").strip():
        return jsonify(ok=True)
    try:
        lid = int(body.get("llamado_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="id inválido"), 400
    nombre = (body.get("nombre") or "").strip()[:80]
    contacto = (body.get("contacto") or "").strip()[:60]
    mensaje = (body.get("mensaje") or "").strip()[:300]
    if not (nombre and contacto):
        return jsonify(ok=False, error="faltan datos"), 400

    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if not efimero.cooldown_ok("respllam_ip", ip, 20):
        return jsonify(ok=False, error="espera un momento"), 429
    if not efimero.tope_global_ok("respllam", 40, 600):
        return jsonify(ok=False, error="intenta más tarde"), 429

    lla = db.llamado(lid)
    # Solo llamados de VOLUNTARIO activos: los pedidos de ayuda van por /api/atender.
    if not lla or (lla.get("origen") or "voluntario") == "pedido" or lla.get("estado") != "activo":
        return jsonify(ok=False, error="ese llamado ya no está activo"), 404

    db.responder_llamado(lid, None, nombre, contacto, mensaje or None)
    threading.Thread(target=_avisar_voluntario_respuesta_web,
                     args=(dict(lla), nombre, contacto, mensaje), daemon=True).start()

    if GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        try:
            _tg("sendMessage", _api=API_APOYO, chat_id=GRUPO_MODERADORES, parse_mode="HTML",
                text=(f"🙋 <b>Respuesta a llamado desde la web</b>\n"
                      f"📣 {_esc(lla.get('sector') or '')} (LL-{lid})\n"
                      f"🙋 {_esc(nombre)} · 📱 {_esc(contacto)}"
                      + (f"\n✍️ {_esc(mensaje)}" if mensaje else "")))
        except Exception:
            pass

    return jsonify(ok=True)


# --- Denuncia pública de una publicación (Manos Solidarias o Centro) ------------
# El público NO puede quitar nada. Solo DENUNCIA → llega un aviso al grupo de
# moderadores con el botón Quitar, para que ellos revisen y decidan.
# Cooldown por IP y tope global de /api/denunciar: en efimero.


@app.post("/api/denunciar")
def denunciar():
    body = request.get_json(silent=True) or {}
    tipo = (body.get("tipo") or "").strip()
    try:
        rid = int(body.get("id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="id inválido"), 400
    if tipo not in ("aliado", "centro"):
        return jsonify(ok=False, error="tipo inválido"), 400

    ip = (request.headers.get("X-Real-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "")
    if not efimero.cooldown_ok("denuncia_ip", ip, 20):
        return jsonify(ok=True, msg="recibido")
    if not efimero.tope_global_ok("denuncia", 30, 600):
        return jsonify(ok=True, msg="recibido")

    # Buscar el nombre y armar el callback de Quitar correcto (apra| aliado / apr| centro)
    if tipo == "aliado":
        row = db.aliado(rid)
        nombre = row.get("nombre") if row else None
        quitar_cb = f"apra|{rid}|no"
        etiqueta = "Manos Solidarias"
    else:
        con = db.conectar()
        r = con.execute("SELECT punto FROM representantes WHERE id = ?", (rid,)).fetchone()
        con.close()
        nombre = (r["punto"] if r else None)
        quitar_cb = f"apr|{rid}|no"
        etiqueta = "Centro de la red"
    if not nombre:
        return jsonify(ok=True, msg="no encontrado")  # ya no existe; nada que quitar

    if GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        kb = {"inline_keyboard": [[{"text": "❌ Quitar de la web", "callback_data": quitar_cb}]]}
        texto = (f"🚩 <b>Denuncia del público</b> · {etiqueta}\n"
                 f"Sobre: <b>{_esc(nombre)}</b>\n"
                 f"Alguien la marcó como indebida o estafa. Revisen y decidan si la quitan.")
        try:
            _tg("sendMessage", _api=API_APOYO, chat_id=GRUPO_MODERADORES,
                text=texto, parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.exception("denunciar: no se pudo notificar a moderadores")
            return jsonify(ok=False, error="no se pudo notificar"), 502

    return jsonify(ok=True)


# --- Entregas verificadas (Fase C): la página del QR ---------------------------
# El QR de un envío despachado apunta a /api/entrega/<token>. El responsable que
# recibe escanea, revisa el manifiesto y confirma: completo, o incompleto por ítem
# con motivo. El token largo es la llave (inadivinable). No expone teléfonos.

_MOTIVOS_WEB = [("menos", "📉 Llegó menos cantidad"), ("danado", "📦 Llegó dañado/vencido"),
                ("equivocado", "❌ Ítem equivocado"), ("no_llego", "🚫 No llegó")]


@app.get("/api/entrega/<token>.png")
def entrega_qr(token):
    import envios
    if not envios.por_token(token):
        return Response("no existe", status=404)
    return Response(envios.qr_png(token), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/entrega/<token>")
def entrega_ver(token):
    import envios
    e = envios.por_token(token)
    if not e:
        return Response("<h1>Entrega no encontrada</h1>", status=404, mimetype="text/html")
    filas = []
    for it in e["items"]:
        cant = f"{it['cant_enviada']:g} {it['unidad'] or ''}".strip()
        if e["estado"] == "despachado":
            ops = "".join(f'<option value="{k}">{v}</option>' for k, v in _MOTIVOS_WEB)
            filas.append(
                f'<tr data-id="{it["id"]}" data-env="{it["cant_enviada"]:g}">'
                f'<td>{_esc(it["item"])}</td><td>{_esc(cant)}</td>'
                f'<td class="inc" hidden><input type="number" min="0" max="{it["cant_enviada"]:g}" '
                f'step="any" value="{it["cant_enviada"]:g}"> '
                f'<select hidden><option value="">motivo…</option>{ops}</select></td></tr>')
        else:
            rec = f"{(it['cant_recibida'] or 0):g}"
            mot = dict(_MOTIVOS_WEB).get(it["motivo"] or "", "")
            ok = (it["cant_recibida"] or 0) >= (it["cant_enviada"] or 0)
            filas.append(f'<tr><td>{_esc(it["item"])}</td><td>{_esc(cant)}</td>'
                         f'<td>{"✅" if ok else "⚠️"} {rec}{(" · " + mot) if mot else ""}</td></tr>')
    abierto = e["estado"] == "despachado"
    badge = {"despachado": ("En camino", "#B7791F"), "recibido": ("Recibido ✓", "#1E9C94"),
             "incompleto": ("Incompleto", "#C53030"), "cancelado": ("Cancelado", "#718096")}[e["estado"]]
    html_doc = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Entrega — Red de Acopio y Refugio</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
<style>
 body{{font-family:Poppins,sans-serif;margin:0;background:#f4f7f9;color:#1a2b3c}}
 .top{{background:#003C67;color:#fff;padding:18px 16px}} .top h1{{margin:0;font-size:1.1rem}}
 .card{{max-width:520px;margin:16px auto;background:#fff;border-radius:14px;
        box-shadow:0 2px 10px rgba(0,60,103,.08);padding:18px}}
 .badge{{display:inline-block;padding:3px 12px;border-radius:99px;color:#fff;
        font-size:.8rem;background:{badge[1]}}}
 table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:.92rem}}
 td{{padding:7px 6px;border-bottom:1px solid #e3ecf2}} input{{width:70px;padding:5px}}
 select{{margin-top:4px;padding:5px;max-width:170px}}
 .btn{{display:block;width:100%;border:0;border-radius:10px;padding:13px;margin-top:10px;
      font-family:inherit;font-size:1rem;font-weight:600;cursor:pointer}}
 .ok{{background:#1E9C94;color:#fff}} .warn{{background:#fff;color:#C53030;border:2px solid #C53030}}
 .send{{background:#003C67;color:#fff}} textarea{{width:100%;box-sizing:border-box;margin-top:8px;padding:8px}}
 .fin{{text-align:center;font-weight:600;margin-top:12px}}
</style></head><body>
<div class="top"><h1>📦 Entrega de {_esc(e.get("de_punto") or "acopio")}</h1>
<div style="font-size:.85rem;opacity:.85">para {_esc(e.get("a_punto") or "destino")} ·
<span class="badge">{badge[0]}</span></div></div>
<div class="card">
<table>{"".join(filas)}</table>
{('''<div id="acciones">
 <button class="btn ok" onclick="confirmar(true)">✅ Recibí todo completo</button>
 <button class="btn warn" onclick="modoInc()">⚠️ Llegó incompleto</button></div>
<div id="inc" hidden>
 <p style="font-size:.88rem">Ajusta <b>cuánto llegó</b> de cada insumo y elige el motivo en los que falten:</p>
 <textarea id="nota" rows="2" placeholder="Nota para el centro de acopio (opcional)"></textarea>
 <button class="btn send" onclick="confirmar(false)">Enviar reporte</button></div>
<div id="fin" class="fin" hidden></div>''') if abierto else
 f'<p class="fin">Confirmada el {_esc((e.get("cerrado_en") or "")[:16])} UTC. ¡Gracias!</p>'}
</div>
<script>
function modoInc(){{document.getElementById('acciones').hidden=true;
 document.getElementById('inc').hidden=false;
 document.querySelectorAll('.inc').forEach(c=>{{c.hidden=false;}});
 document.querySelectorAll('.inc input').forEach(i=>{{i.oninput=()=>{{
   const tr=i.closest('tr'), s=tr.querySelector('select');
   s.hidden = parseFloat(i.value||0) >= parseFloat(tr.dataset.env);}};}});}}
async function confirmar(completo){{
 const body={{completo:completo, items:{{}}, nota:(document.getElementById('nota')||{{}}).value||''}};
 if(!completo) document.querySelectorAll('tr[data-id]').forEach(tr=>{{
   body.items[tr.dataset.id]={{recibida:tr.querySelector('input').value,
                              motivo:tr.querySelector('select').value}};}});
 const r=await fetch(location.pathname+'/confirmar',{{method:'POST',
   headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
 const j=await r.json().catch(()=>({{}}));
 document.getElementById('acciones')&&(document.getElementById('acciones').hidden=true);
 document.getElementById('inc')&&(document.getElementById('inc').hidden=true);
 const f=document.getElementById('fin'); f.hidden=false;
 f.textContent = j.ok ? (j.estado==='recibido' ? '✅ ¡Entrega confirmada! Gracias.'
   : '⚠️ Reporte enviado. El centro de acopio fue notificado.') : '⚠️ No se pudo confirmar. Recarga la página.';
}}
</script></body></html>"""
    return Response(html_doc, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/entrega/<token>/confirmar")
def entrega_confirmar(token):
    import envios
    e = envios.por_token(token)
    if not e:
        return jsonify(ok=False, error="no existe"), 404
    if e["estado"] != "despachado":
        return jsonify(ok=False, error="ya confirmada"), 409
    data = request.get_json(silent=True) or {}
    recepciones = {}
    if not data.get("completo"):
        for it in e["items"]:
            d = (data.get("items") or {}).get(str(it["id"])) or {}
            try:
                rec = float(d.get("recibida", it["cant_enviada"]))
            except (TypeError, ValueError):
                rec = it["cant_enviada"]
            rec = max(0.0, min(rec, it["cant_enviada"]))  # nunca más de lo enviado
            motivo = d.get("motivo") if rec < it["cant_enviada"] else None
            if motivo not in envios.MOTIVOS:
                motivo = "menos" if rec < it["cant_enviada"] else None
            recepciones[it["id"]] = (rec, motivo)
    nota = (data.get("nota") or "").strip()[:300] or None
    res = envios.recibir(token, recepciones, nota=nota)
    if not res:
        return jsonify(ok=False, error="ya confirmada"), 409
    # Notificar al acopio por su canal (cierre del ciclo)
    if res["estado"] == "recibido":
        txt = (f"✅ {res.get('a_punto') or 'El destino'} confirmó tu entrega COMPLETA.\n\n"
               + envios.manifiesto_texto(res))
    else:
        faltas = [f"• {it['item']}: llegó {(it['cant_recibida'] or 0):g} de {it['cant_enviada']:g}"
                  + (f" ({envios.MOTIVOS.get(it['motivo'], '')})" if it.get("motivo") else "")
                  for it in res["items"]
                  if (it["cant_recibida"] or 0) < (it["cant_enviada"] or 0)]
        txt = (f"⚠️ {res.get('a_punto') or 'El destino'} reportó tu entrega INCOMPLETA:\n"
               + "\n".join(faltas))
        if nota:
            txt += f"\n📝 Nota: {nota}"
    try:
        envios.notificar_rep(res["de_rep"], txt)
    except Exception:
        pass
    return jsonify(ok=True, estado=res["estado"])
