#!/usr/bin/env python3
"""FIX #3: vigila que el feed del canal siga vivo. Si run_once no logra un fetch
exitoso en >UMBRAL min (FloodWait largo, cuenta limitada, reader caído), avisa por
DM a Gustavo. Anti-spam: 1 aviso por episodio + aviso al recuperarse.

+ Vigilancia del feed de OFERTAS: si no entra ninguna captura de inventario de
  oferta confirmada en >UMBRAL_OFERTA_H, el emparejador necesidad->acopio queda a
  ciegas SIN lanzar error (las necesidades se congelan en 'buscando'). Mismo
  patron de anti-spam con su propio flag."""
import datetime, os, sys, urllib.request, urllib.parse
sys.path.insert(0, "/opt/angeles")
from config import TELEGRAM_BOT_TOKEN

import obs
obs.setup_logging("angeles-monitor")
obs.init_sentry("angeles-monitor")
obs.init_error_alerts("angeles-monitor")
obs.install_excepthook("angeles-monitor")

DESTINO = os.getenv("ALERTAS_TG_IDS", "").split(",")[0]
HEART = "/opt/angeles/last_fetch_ok"
FLAG  = "/opt/angeles/.feed_alerted"
UMBRAL_MIN = 30

FLAG_OFERTA     = "/opt/angeles/.oferta_alerted"
UMBRAL_OFERTA_H = 96   # el matcher descarta ofertas a las 72h (168h fundacion)

def enviar(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": DESTINO, "text": texto,
                                   "parse_mode": "Markdown"}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=20)
    except Exception as e:
        print("no pude enviar DM:", e)

def edad_min():
    # Pausa intencional de la ingesta (ver run_once.py): con INGESTA_OFF
    # presente el feed del canal no debe alertar; se reporta como fresco.
    if os.path.exists("/opt/angeles/INGESTA_OFF"):
        return 0.0
    if not os.path.exists(HEART):
        return None
    return (datetime.datetime.now().timestamp() - os.path.getmtime(HEART)) / 60.0

def edad_oferta_h():
    """Horas desde la ultima captura de oferta confirmada (None si no hay ninguna).
    Calculo en SQL para evitar lios de zona horaria (creado_en es UTC)."""
    try:
        import db
        con = db.conectar()
        row = con.execute(
            "SELECT (julianday('now') - julianday(MAX(creado_en)))*24 "
            "FROM capturas WHERE estado='confirmado' AND (clase='oferta' OR clase IS NULL)"
        ).fetchone()
        con.close()
    except Exception as e:
        print("edad_oferta_h error:", e)
        return None
    if not row or row[0] is None:
        return None
    return float(row[0])

def main():
    # --- Feed del canal (FIX #3, intacto) ---
    e = edad_min()
    stale = (e is None) or (e > UMBRAL_MIN)
    alerted = os.path.exists(FLAG)
    if stale and not alerted:
        edad = "nunca" if e is None else f"{e:.0f} min"
        enviar("⚠️ *Red de Acopio y Refugio*: el feed del canal NO se actualiza "
               f"(último fetch OK: {edad}; umbral {UMBRAL_MIN}m).\n"
               "Posible FloodWait largo / cuenta limitada / reader caído.\n"
               "Revisa: journalctl -u angeles.service --since '1 hour ago'")
        with open(FLAG, "w") as f: f.write(datetime.datetime.now().isoformat())
        print("ALERTA enviada")
    elif (not stale) and alerted:
        enviar(f"✅ *Red de Acopio y Refugio*: el feed del canal se recuperó (fetch OK hace {e:.0f} min).")
        os.remove(FLAG)
        print("recuperación notificada")
    else:
        print(f"feed OK (edad {'nunca' if e is None else f'{e:.0f}m'}), sin cambios")

    # --- Feed de OFERTAS (insumos) ---
    oh = edad_oferta_h()
    stale_of = (oh is None) or (oh > UMBRAL_OFERTA_H)
    alerted_of = os.path.exists(FLAG_OFERTA)
    if stale_of and not alerted_of:
        edad = "nunca" if oh is None else f"{oh:.0f} h"
        enviar("⚠️ *Red de Acopio y Refugio*: no entra inventario de oferta confirmado "
               f"desde hace {edad} (umbral {UMBRAL_OFERTA_H}h).\n"
               "El emparejador de insumos (necesidad→acopio) quedó a ciegas: "
               "las necesidades siguen en 'buscando' sin cruzar con ningún acopio.\n"
               "Acción: pedir a los centros que recarguen su inventario por el bot de apoyo.")
        with open(FLAG_OFERTA, "w") as f: f.write(datetime.datetime.now().isoformat())
        print("ALERTA oferta enviada")
    elif (not stale_of) and alerted_of:
        enviar(f"✅ *Red de Acopio y Refugio*: volvió a entrar inventario de oferta (hace {oh:.0f} h).")
        os.remove(FLAG_OFERTA)
        print("recuperación oferta notificada")
    else:
        _e = 'nunca' if oh is None else f'{oh:.0f}h'
        print(f"oferta FRÍA (edad {_e}) — ya avisado, sin re-alertar" if stale_of
              else f"oferta OK (edad {_e})")

if __name__ == "__main__":
    main()
