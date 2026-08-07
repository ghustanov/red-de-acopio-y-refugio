"""
Fase 3: seguimiento de necesidades. Revisa las necesidades en estado 'buscando' contra el
inventario ACTUAL (que los colaboradores mantienen vivo) y, si un centro de acopio CERCANO ya
tiene el insumo, le avisa al solicitante por el bot de apoyo con 2 botones (coordinar entrega /
ya no la necesito). Lo dispara run_once cada 5 minutos. Cero llamadas si no hay necesidades.
"""
import html
import json
import math
import socket
import urllib.parse
import urllib.request

import db
from config import APOYO_BOT_TOKEN

# Forzar IPv4: desde este VPS algunas conexiones IPv6 a Telegram se congelan ~70s.
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4(host, *args, **kwargs):
    res = _orig_getaddrinfo(host, *args, **kwargs)
    solo_v4 = [r for r in res if r[0] == socket.AF_INET]
    return solo_v4 or res
socket.getaddrinfo = _getaddrinfo_ipv4

API = f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}"
RADIO_CERCANO = 25.0   # km; igual que en el bot


def _dist_km(a_lat, a_lon, b_lat, b_lon):
    if None in (a_lat, a_lon, b_lat, b_lon):
        return None
    R = 6371.0
    dlat = math.radians(b_lat - a_lat)
    dlon = math.radians(b_lon - a_lon)
    x = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def _tg(metodo, **params):
    payload = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
               for k, v in params.items() if v is not None}
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(f"{API}/{metodo}", data=data, timeout=30) as r:
            return json.load(r)
    except Exception as e:
        print("seguimiento tg error:", metodo, e)
        return {}


def revisar(limite=50):
    """Necesidades 'buscando' → si aparece un acopio cercano con el insumo, avisa al solicitante
    y la pasa a 'disponible'. Devuelve cuántas avisó."""
    if not APOYO_BOT_TOKEN:
        return 0
    avisadas = 0
    for nec in db.necesidades_abiertas()[:limite]:
        rep = db.representante(nec["rep_id"]) if nec.get("rep_id") else None
        mylat = rep.get("lat") if rep else None
        mylon = rep.get("lon") if rep else None
        cands = []
        for r in db.buscar_oferta(nec["item"]):
            if nec.get("punto_id") is not None and r.get("punto_id") == nec["punto_id"]:
                continue  # no ofrecerle su propio punto
            d = _dist_km(mylat, mylon, r.get("lat"), r.get("lon"))
            if d is None or d <= RADIO_CERCANO:   # sin coords no se mide → se acepta
                cands.append((d if d is not None else 9e9, r))
        if not cands:
            continue
        cands.sort(key=lambda x: x[0])
        d, r = cands[0]
        db.set_estado_necesidad(nec["id"], "disponible", match_punto_id=r.get("punto_id"),
                                match_rep=r.get("dueno_rep_id"), match_punto=r.get("punto"))
        if rep and rep.get("telegram_id"):
            dist_txt = f" ({d:.1f} km)" if d < 9e9 else ""
            kb = {"inline_keyboard": [[
                {"text": "🤝 Coordinar entrega", "callback_data": f"seg|{nec['id']}|coord"},
                {"text": "✅ Ya no la necesito", "callback_data": f"seg|{nec['id']}|no"}]]}
            _tg("sendMessage", chat_id=rep["telegram_id"], parse_mode="HTML",
                text=(f"🔔 <b>¡Apareció lo que necesitabas!</b>\n\n"
                      f"<b>{html.escape(nec['item'] or '')}</b> está disponible en "
                      f"<b>{html.escape(r.get('punto') or 'un acopio')}</b>{dist_txt}.\n\n"
                      "¿Qué quieres hacer?"),
                reply_markup=kb)
            avisadas += 1
    return avisadas


if __name__ == "__main__":
    print("avisadas:", revisar())
