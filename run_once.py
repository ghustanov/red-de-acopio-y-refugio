"""
Prototipo de una sola corrida:
  1. Lee los mensajes recientes del canal (y baja las fotos).
  2. Clasifica con IA los que aún no estaban guardados (lee texto y volantes).
  3. Genera la página web (web/index.html).

Uso:
    python3 run_once.py
"""
import datetime
import json
import urllib.request

from telethon.errors import FloodWaitError

import db
import reader
import classifier
import build_site
import seguimiento
from config import LIMITE_MENSAJES, APOYO_BOT_TOKEN, GRUPO_MODERADORES

import obs
obs.setup_logging("angeles-run")
obs.init_sentry("angeles-run")
obs.init_error_alerts("angeles-run")
obs.install_excepthook("angeles-run")
log = obs.get_logger("angeles-run")


def _avisar_vencidos(vencidos):
    """Un solo mensaje al grupo de moderadores con lo que acaba de vencer (48h sin
    resolver), cada uno con botón para reactivarlo si el cierre fue un error."""
    if not vencidos or not (APOYO_BOT_TOKEN and GRUPO_MODERADORES):
        return
    lineas = [f"⌛ <b>{len(vencidos)} llamado(s)/pedido(s) vencido(s)</b> "
              "(48h sin resolver, ya salieron de la web):"]
    filas = []
    for v in vencidos:
        etiqueta = "📣 Llamado" if db.es_llamado_de_voluntario(v) else "🆘 Pedido"
        sector = v.get("sector") or "(sin sector)"
        mensaje = (v.get("mensaje") or "")[:60]
        lineas.append(f"#{v['id']} · {etiqueta} · {sector} · {mensaje}")
        filas.append([{"text": f"↩️ Reactivar #{v['id']}", "callback_data": f"pat|{v['id']}|act"}])
    data = json.dumps({"chat_id": GRUPO_MODERADORES, "text": "\n".join(lineas),
                       "parse_mode": "HTML", "reply_markup": {"inline_keyboard": filas}}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage", data=data,
            headers={"Content-Type": "application/json"}), timeout=15)
    except Exception as e:
        print("⚠️ aviso de vencidos falló:", e)


def _expirar_llamados():
    """Depuración: nada debe quedar 'activo' más de 48h sin que alguien lo resuelva."""
    try:
        vencidos = db.expirar_llamados(horas=48)
        if vencidos:
            print(f"⌛ {len(vencidos)} llamado(s)/pedido(s) vencido(s) (48h).")
            _avisar_vencidos(vencidos)
    except Exception as e:
        print(f"   ⚠️  expirar llamados falló (no crítico): {e}")


def _revisar_seguimiento():
    """Avisa a los solicitantes cuyas necesidades ya aparecieron en un acopio cercano."""
    try:
        n = seguimiento.revisar()
        if n:
            print(f"🔔 {n} aviso(s) de seguimiento enviados.")
    except Exception as e:
        print(f"   ⚠️  seguimiento falló (no crítico): {e}")
    try:
        import contacto_ms
        m = contacto_ms.revisar()
        if m:
            print(f"🤝 {m} solicitud(es) de contacto procesadas (recordatorio/expiración).")
    except Exception as e:
        print(f"   ⚠️  contactos MS falló (no crítico): {e}")


def main():
    db.init()

    # Kill-switch: /opt/angeles/INGESTA_OFF pausa la lectura del canal y el
    # clasificador LLM (ahorro de tokens). Borrar el archivo reactiva la ingesta.
    import os as _o
    if _o.path.exists("/opt/angeles/INGESTA_OFF"):
        print("⏸️ Ingesta del canal PAUSADA (INGESTA_OFF presente). Solo mantenimiento.")
        _expirar_llamados()
        _revisar_seguimiento()
        build_site.construir()
        return

    print(f"📥 Leyendo los últimos {LIMITE_MENSAJES} mensajes del canal…")
    try:
        mensajes = reader.obtener_mensajes(limite=LIMITE_MENSAJES)
    except FloodWaitError as e:
        # Telegram pide esperar: NO es un crash. Regeneramos la web con lo que ya
        # hay en la base y salimos limpio (exit 0); el timer reintenta luego.
        print(f"⏳ Telegram pide esperar {e.seconds}s (FloodWait). "
              f"Reconstruyo la web con lo existente y salgo sin error.")
        _expirar_llamados()
        _revisar_seguimiento()
        build_site.construir()
        return
    print(f"   {len(mensajes)} mensajes (con texto y/o foto) encontrados.\n")
    # FIX #3: latido de feed sano (solo en fetch exitoso; el FloodWait retorna antes).
    try:
        with open("/opt/angeles/last_fetch_ok", "w") as _hb:
            _hb.write(datetime.datetime.now().isoformat())
    except Exception:
        pass

    nuevos = 0
    for m in mensajes:
        if db.ya_existe(m["tg_id"]):
            continue
        try:
            c = classifier.clasificar(m["texto"], ruta_imagen=m.get("foto_abs"))
        except Exception:
            log.exception("no se pudo clasificar el mensaje %s", m['tg_id'])
            continue

        db.guardar(
            {
                "tg_id": m["tg_id"],
                "tg_fecha": m["tg_fecha"],
                "texto_original": m["texto"],
                "link": m["link"],
                "relevante": 1 if c.relevante else 0,
                "categoria": c.categoria,
                "titulo": c.titulo,
                "ubicacion": c.ubicacion,
                "detalle": c.detalle,
                "necesidades": c.necesidades,
                "contacto": c.contacto,
                "foto": m.get("foto_rel"),
                "vigencia": c.vigencia,
                "procesado_en": datetime.datetime.now().isoformat(),
            }
        )
        # Centros de acopio (uno por fila)
        if c.centros:
            db.guardar_centros(
                m["tg_id"], m["tg_fecha"], m["link"],
                [centro.model_dump() for centro in c.centros],
            )
        # Estado de servicios básicos (uno por fila)
        if c.servicios:
            db.guardar_servicios(
                m["tg_id"], m["tg_fecha"], m["link"],
                [s.model_dump() for s in c.servicios],
            )
        nuevos += 1
        marca = "✅" if c.relevante else "·"
        print(f"   {marca} [{c.categoria}] {c.titulo}")

    print(f"\n🧮 {nuevos} mensajes nuevos clasificados.")

    # Canal → pedidos de ayuda: los reportes de auxilio CON teléfono entran al
    # cableado de pedidos (contacto privado + fan-out + moderación pweb|).
    # Sin teléfono quedan solo como listado en Ángeles de la Autopista.
    try:
        import canal_pedidos
        canal_pedidos.procesar_nuevos()
    except Exception as e:
        print(f"⚠️ canal→pedidos falló: {e}")

    _expirar_llamados()
    _revisar_seguimiento()
    salida, total = build_site.construir()
    print(f"🌐 Página generada: {salida}")
    print(f"   {total} tarjetas útiles mostradas.")
    print(f"\n👉 Ábrela en tu navegador:  file://{salida}\n")


if __name__ == "__main__":
    main()
