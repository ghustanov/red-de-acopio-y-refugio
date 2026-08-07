"""Lee mensajes del canal de Telegram usando tu cuenta (Telethon). Baja fotos."""
import json
import os

from telethon.sync import TelegramClient
from telethon.tl.types import PeerChannel, InputPeerChannel
from config import API_ID, API_HASH, CANAL, SESSION_PATH, WEB_DIR, BASE

IMG_DIR = WEB_DIR / "img"
# Caché del canal (id + access_hash) para NO re-resolverlo en cada corrida.
# Resolverlo cada vez disparaba GetContactsRequest → FloodWait de Telegram.
CANAL_CACHE = BASE / "canal_cache.json"


def _link(username, cid, msg_id):
    """Arma el link al mensaje original para poder verificarlo.
    Con username público queda t.me/<usuario>/<id> (abre cualquiera); si no,
    t.me/c/<id>/<id> (solo miembros)."""
    if username:
        return f"https://t.me/{username}/{msg_id}"
    return f"https://t.me/c/{cid}/{msg_id}"


def _resolver_canal(client, canal):
    """Devuelve (peer, username) SIN re-resolver el canal en cada corrida:
    1) si hay caché en disco, arma el InputPeerChannel directo (cero llamadas);
    2) si no, resuelve la entidad completa (1 llamada barata por access_hash de
       la sesión) para obtener también el username, y guarda la caché;
    3) último recurso (solo 1ª vez): buscarlo entre los diálogos.
    Antes hacía get_entity(ID crudo) que SIEMPRE fallaba y caía a iter_dialogs()
    cada corrida → eso disparaba GetContactsRequest → FloodWait."""
    # 1) Caché en disco (camino normal: ninguna llamada de resolución)
    try:
        d = json.loads(CANAL_CACHE.read_text())
        return InputPeerChannel(d["id"], d["access_hash"]), d.get("username")
    except (FileNotFoundError, KeyError, ValueError):
        pass

    # 2) Resolver la entidad completa (usa el access_hash cacheado en la sesión)
    s = str(canal).strip()
    entity = None
    try:
        cid = int(s[4:]) if s.startswith("-100") else int(s.lstrip("-"))
        entity = client.get_entity(PeerChannel(cid))
    except (ValueError, TypeError):
        entity = None

    # 3) Último recurso: recorrer diálogos (caro; solo si la sesión no lo tenía)
    if entity is None:
        for dlg in client.iter_dialogs():
            if str(dlg.id) == s:
                entity = dlg.entity
                break
    if entity is None:
        raise ValueError(
            f"No encontré el canal '{canal}'. Verifica el ID en .env "
            f"(o vuelve a correr login.py para ver la lista de canales)."
        )

    username = getattr(entity, "username", None)
    # Guardar caché para próximas corridas
    try:
        CANAL_CACHE.write_text(json.dumps(
            {"id": entity.id, "access_hash": entity.access_hash, "username": username}))
    except (OSError, AttributeError):
        pass
    return entity, username


def obtener_mensajes(limite=80):
    """Devuelve los últimos `limite` mensajes (con texto y/o foto) del canal."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    with TelegramClient(SESSION_PATH, API_ID, API_HASH) as client:
        peer, username = _resolver_canal(client, CANAL)
        cid = getattr(peer, "id", None) or getattr(peer, "channel_id", None)
        salida = []
        for m in client.iter_messages(peer, limit=limite):
            tiene_foto = m.photo is not None
            if not m.text and not tiene_foto:
                continue

            foto_rel, foto_abs = None, None
            if tiene_foto:
                destino = client.download_media(m, file=str(IMG_DIR / str(m.id)))
                if destino:
                    foto_abs = destino
                    foto_rel = "img/" + os.path.basename(destino)

            salida.append(
                {
                    "tg_id": m.id,
                    "tg_fecha": m.date.isoformat(),
                    "texto": m.text or "",
                    "link": _link(username, cid, m.id),
                    "foto_rel": foto_rel,   # ruta relativa para la web
                    "foto_abs": foto_abs,   # ruta absoluta para que la IA la lea
                }
            )
        return salida
