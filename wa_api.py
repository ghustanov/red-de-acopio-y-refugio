"""Cliente de WhatsApp Cloud API (Meta) — Red de Acopio y Refugio.

Capa de I/O SALIENTE hacia WhatsApp: texto, botones, listas, marcar leído y
descargar archivos recibidos (para pasarlos a captura.py). Solo stdlib (urllib),
igual que bot_apoyo.py hace con Telegram.

Credenciales desde el .env (config.py): WHATSAPP_TOKEN, WHATSAPP_PHONE_ID.
"""
import json
import urllib.request

from config import WHATSAPP_TOKEN, WHATSAPP_PHONE_ID

GRAPH = "https://graph.facebook.com/v21.0"


def _post(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{GRAPH}/{WHATSAPP_PHONE_ID}/messages", data=data,
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001 — best-effort, igual que Telegram
        print("wa error:", e)
        return {}


def enviar_texto(to, texto):
    return _post({"messaging_product": "whatsapp", "to": to, "type": "text",
                  "text": {"preview_url": False, "body": texto}})


def enviar_botones(to, texto, botones):
    """botones: lista de (id, titulo). Máx 3 (límite de WhatsApp)."""
    rows = [{"type": "reply", "reply": {"id": bid, "title": t[:20]}}
            for bid, t in botones[:3]]
    return _post({"messaging_product": "whatsapp", "to": to, "type": "interactive",
                  "interactive": {"type": "button", "body": {"text": texto},
                                  "action": {"buttons": rows}}})


def enviar_lista(to, texto, boton, opciones):
    """opciones: lista de (id, titulo[, descripcion]). Máx 10."""
    rows = []
    for op in opciones[:10]:
        row = {"id": op[0], "title": op[1][:24]}
        if len(op) > 2 and op[2]:
            row["description"] = op[2][:72]
        rows.append(row)
    return _post({"messaging_product": "whatsapp", "to": to, "type": "interactive",
                  "interactive": {"type": "list", "body": {"text": texto},
                                  "action": {"button": boton[:20],
                                             "sections": [{"rows": rows}]}}})


def enviar_imagen(to, url, caption=None):
    """Manda una imagen por URL pública (Meta la descarga él mismo — sin upload).
    OJO: si el origen está detrás de Cloudflare, el fetcher de Meta puede ser
    bloqueado y la imagen nunca llega (la API igual devuelve OK). Para imágenes
    propias usa enviar_imagen_bytes, que sube el binario directo."""
    img = {"link": url}
    if caption:
        img["caption"] = caption[:1024]
    return _post({"messaging_product": "whatsapp", "to": to, "type": "image", "image": img})


def subir_media(data, mime="image/png", filename="imagen.png"):
    """Sube un binario a WhatsApp (multipart) y devuelve su media_id, o None."""
    boundary = "----wamedia" + str(len(data))
    partes = []

    def campo(nombre, valor):
        partes.append((f"--{boundary}\r\n"
                       f'Content-Disposition: form-data; name="{nombre}"\r\n\r\n{valor}\r\n').encode())

    campo("messaging_product", "whatsapp")
    campo("type", mime)
    partes.append((f"--{boundary}\r\n"
                   f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                   f"Content-Type: {mime}\r\n\r\n").encode())
    body = b"".join(partes) + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{GRAPH}/{WHATSAPP_PHONE_ID}/media", data=body,
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("id")
    except Exception as e:  # noqa: BLE001
        print("wa subir_media error:", e)
        return None


def enviar_imagen_bytes(to, data, caption=None, mime="image/png"):
    """Sube la imagen y la manda por media_id (confiable: sin fetch externo de Meta)."""
    mid = subir_media(data, mime)
    if not mid:
        return {}
    img = {"id": mid}
    if caption:
        img["caption"] = caption[:1024]
    return _post({"messaging_product": "whatsapp", "to": to, "type": "image", "image": img})


def enviar_plantilla(to, nombre, variables=(), payloads=(), idioma="es"):
    """Mensaje de PLANTILLA aprobada (inicia conversación fuera de la ventana de 24 h).
    variables: textos para {{1}}, {{2}}… del cuerpo (sin saltos de línea, regla de Meta).
    payloads: payload de cada botón de respuesta rápida — vuelven al webhook como
    type='button'. Devuelve la respuesta cruda (trae 'messages' si Meta lo aceptó)."""
    componentes = []
    if variables:
        componentes.append({"type": "body", "parameters": [
            {"type": "text", "text": " ".join(str(v).split())[:200] or "-"}
            for v in variables]})
    for i, p in enumerate(payloads):
        componentes.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                            "parameters": [{"type": "payload", "payload": p}]})
    return _post({"messaging_product": "whatsapp", "to": to, "type": "template",
                  "template": {"name": nombre, "language": {"code": idioma},
                               "components": componentes}})


def marcar_leido(message_id):
    return _post({"messaging_product": "whatsapp", "status": "read",
                  "message_id": message_id})


def descargar_media(media_id):
    """(bytes, mime) de un archivo recibido, o (None, None). WhatsApp lo entrega
    en 2 pasos: pedir la URL temporal y luego bajarla con el token."""
    req = urllib.request.Request(
        f"{GRAPH}/{media_id}",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            meta = json.load(r)
    except Exception as e:  # noqa: BLE001
        print("wa media meta error:", e)
        return None, None
    url, mime = meta.get("url"), meta.get("mime_type")
    if not url:
        return None, None
    req2 = urllib.request.Request(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
    try:
        with urllib.request.urlopen(req2, timeout=60) as r:
            return r.read(), mime
    except Exception as e:  # noqa: BLE001
        print("wa media download error:", e)
        return None, None
