"""
Canal → Pedidos de ayuda (pedido de Gustavo, 2026-07-05).

Los reportes del canal Emergencias Viales que la IA ya clasificó como auxilio
(rescate / heridos / donaciones) Y que traen un teléfono venezolano válido se
suben como PEDIDO DE AYUDA dentro del cableado normal: contacto privado en
solicitudes_web (nunca en la web pública), fan-out del barrido de bot_apoyo a
la red de la zona, y aviso al grupo de moderadores con el mismo botón pweb|
de siempre. Sin teléfono → el reporte queda solo como listado informativo en
la pestaña Ángeles de la Autopista (como hasta ahora).

Idempotente: mensajes.pedido_lid marca lo ya revisado (0 = revisado sin
convertir, >0 = id del llamado creado). La PRIMERA corrida hace backfill:
marca todo lo histórico como revisado — el pipeline aplica solo hacia adelante.
Goteo anti-avalancha: máximo MAX_POR_CORRIDA conversiones por corrida del timer.
"""
import html
import json
import re
import urllib.request

import db
from config import APOYO_BOT_TOKEN, GRUPO_MODERADORES, AUTO_PUBLICAR_PEDIDOS, CATS_PEDIR

# categoría del canal (classifier) → categoría de pedido (CATS_PEDIR)
CATS_CANAL = {"rescate": "rescate", "heridos": "salud", "donaciones": "otro"}
MAX_POR_CORRIDA = 6   # el timer corre cada ~5 min; goteo para no inundar la red

# Teléfonos VE: móviles 04XX-XXXXXXX (con o sin +58 / separadores) y fijos 02XX.
_TEL = re.compile(r"(?:\+?58[-.\s]?)?0?(4\d{2}|2\d{2})[-.\s]?(\d{3})[-.\s]?(\d{4})")
# R3: señales deterministas de intento de prompt injection en texto externo del canal.
_INYECCION = re.compile(
    r"(ignora\s+(?:lo\s+)?anterior|ignora\s+estas\s+reglas|"
    r"clasifica\s+como|act[uú]a\s+como|eres\s+chatgpt|"
    r"system\s*prompt|developer\s*message|prompt\s+injection|"
    r"olvida\s+(?:lo\s+)?anterior|no\s+sigas\s+las\s+instrucciones)",
    re.I,
)


def _telefono_ve(texto):
    """Primer teléfono venezolano válido del texto (prioriza móviles 04XX)."""
    if not texto:
        return None
    moviles, fijos = [], []
    for m in _TEL.finditer(str(texto)):
        num = "0" + m.group(1) + m.group(2) + m.group(3)
        (moviles if num.startswith("04") else fijos).append(num)
    return (moviles or fijos or [None])[0]


def _texto_revision(f):
    return "\n".join(str(f[k] or "") for k in (
        "texto_original", "contacto", "titulo", "detalle", "necesidades"))


def _init_cola_revision():
    con = db.conectar()
    con.execute(
        "CREATE TABLE IF NOT EXISTS cola_revision_canal ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "mensaje_id INTEGER, tg_id INTEGER, motivo TEXT, categoria TEXT, contacto TEXT, "
        "texto TEXT, link TEXT, creado_en TEXT DEFAULT CURRENT_TIMESTAMP, "
        "estado TEXT DEFAULT 'pendiente', resuelto_en TEXT)")
    con.commit()
    con.close()


def _encolar_revision(f, motivo, tel=None):
    _init_cola_revision()
    con = db.conectar()
    con.execute(
        "INSERT INTO cola_revision_canal "
        "(mensaje_id, tg_id, motivo, categoria, contacto, texto, link) "
        "VALUES (?,?,?,?,?,?,?)",
        (f["id"], f["tg_id"], motivo, f["categoria"], tel or f["contacto"],
         _texto_revision(f)[:1200], f["link"]))
    con.execute("UPDATE mensajes SET pedido_lid = 0 WHERE id = ?", (f["id"],))
    con.commit()
    con.close()
    if GRUPO_MODERADORES and APOYO_BOT_TOKEN:
        _tg_moderadores(
            "⚠️ <b>Pedido del canal requiere revisión manual</b>"
            + f"\nMotivo: {html.escape(motivo)}"
            + f"\nCategoría: {html.escape(f['categoria'] or '(sin categoría)')}"
            + f"\n🆘 {html.escape(f['titulo'] or '(sin título)')}"
            + (f"\n🔗 {html.escape(f['link'])}" if f["link"] else ""))


def _validar_prepublicacion(f, cat, tel):
    if (f["categoria"] or "").strip().lower() not in CATS_CANAL:
        return False, "categoria fuera de whitelist"
    if cat not in CATS_PEDIR:
        return False, "categoria destino fuera de CATS_PEDIR"
    if not tel:
        return False, "sin telefono venezolano valido"
    if _INYECCION.search(_texto_revision(f)):
        return False, "senal de prompt injection en contenido externo"
    return True, ""


def _tg_moderadores(texto, lid=None):
    """Aviso al grupo con los botones pweb| que bot_apoyo ya procesa."""
    if not (APOYO_BOT_TOKEN and GRUPO_MODERADORES):
        return
    kb = None
    if lid:
        if AUTO_PUBLICAR_PEDIDOS:
            kb = {"inline_keyboard": [[
                {"text": "✅ Marcar resuelto", "callback_data": f"pat|{lid}|res"},
                {"text": "🗑 Remover de la web", "callback_data": f"pweb|{lid}|no"}]]}
        else:
            kb = {"inline_keyboard": [[{"text": "✅ Publicar y avisar a la red", "callback_data": f"pweb|{lid}|ok"},
                                       {"text": "🗑 Descartar", "callback_data": f"pweb|{lid}|no"}]]}
    data = json.dumps({"chat_id": GRUPO_MODERADORES, "text": texto,
                       "parse_mode": "HTML", "reply_markup": kb,
                       "disable_web_page_preview": True}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage", data=data,
            headers={"Content-Type": "application/json"}), timeout=15)
    except Exception as e:
        print("   ⚠️ canal→pedidos: aviso a moderadores falló:", e)


def procesar_nuevos():
    """Barrido idempotente sobre los mensajes sin revisar. Devuelve # creados."""
    con = db.conectar()
    ya_corrio = con.execute(
        "SELECT COUNT(*) FROM mensajes WHERE pedido_lid IS NOT NULL").fetchone()[0]
    if not ya_corrio:
        # Backfill one-shot: lo histórico ya envejeció; solo aplicamos hacia adelante.
        con.execute("UPDATE mensajes SET pedido_lid = 0 WHERE pedido_lid IS NULL")
        con.commit()
        con.close()
        print("   📡 canal→pedidos: primer arranque — histórico marcado como revisado.")
        return 0
    filas = con.execute(
        "SELECT id, tg_id, link, categoria, titulo, ubicacion, detalle, necesidades, "
        "contacto, texto_original FROM mensajes WHERE pedido_lid IS NULL "
        "ORDER BY id ASC").fetchall()
    con.close()

    creados = 0
    for f in filas:
        cat = CATS_CANAL.get((f["categoria"] or "").strip().lower())
        tel = _telefono_ve(f["contacto"]) or _telefono_ve(f["texto_original"])
        convertir = bool(cat and tel)
        sospechoso = bool(cat and _INYECCION.search(_texto_revision(f)))
        if sospechoso:
            _encolar_revision(f, "senal de prompt injection en contenido externo", tel)
            continue
        if convertir and creados >= MAX_POR_CORRIDA:
            break   # los demás quedan sin marcar → los toma la próxima corrida
        lid = 0
        if convertir:
            valido, motivo = _validar_prepublicacion(f, cat, tel)
            if not valido:
                _encolar_revision(f, motivo, tel)
                continue
            try:
                partes = [p for p in (f["titulo"], f["detalle"], f["necesidades"]) if p]
                mensaje = (" — ".join(partes) or (f["texto_original"] or ""))[:600]
                mensaje += "\n\n📡 Reporte del canal Emergencias Viales"
                lid = db.crear_llamado(None, None, (f["ubicacion"] or "").strip()[:120] or None,
                                       mensaje, origen="pedido")
                # Contacto PRIVADO (solicitudes_web): solo lo ve quien atiende el pedido.
                db.guardar_contacto_pedido(lid, "Canal Emergencias Viales", tel, cat)
                if AUTO_PUBLICAR_PEDIDOS:
                    db.publicar_pedido(lid)   # estado web + triangulado=0 → fan-out del barrido
                _tg_moderadores(
                    "📡 <b>Pedido de ayuda desde el canal</b>"
                    + (" · ✅ <i>publicado</i>" if AUTO_PUBLICAR_PEDIDOS else " · ⏳ <i>pendiente</i>")
                    + f"\n🆘 {html.escape(f['titulo'] or '(sin título)')}"
                    + f"\n📍 {html.escape(f['ubicacion'] or 'sin zona')}"
                    + f"\n📞 {html.escape(tel)}"
                    + (f"\n🔗 {html.escape(f['link'])}" if f["link"] else ""), lid)
                creados += 1
            except Exception as e:
                print(f"   ⚠️ canal→pedido falló (msg {f['tg_id']}):", e)
                lid = 0
        con = db.conectar()
        con.execute("UPDATE mensajes SET pedido_lid = ? WHERE id = ?", (lid, f["id"]))
        con.commit()
        con.close()
    if creados:
        print(f"   📡 canal→pedidos: {creados} pedido(s) de ayuda creados y avisados.")
    return creados
