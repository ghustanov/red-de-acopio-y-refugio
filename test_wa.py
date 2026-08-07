"""Pruebas de bot_wa.py que NO requieren Meta ni escriben en la DB.

Corre:  sudo -u angeles venv/bin/python3 test_wa.py
"""
import wa_api

# --- Silenciar la salida hacia Meta: registrar en vez de enviar ---
SENT = []
wa_api.enviar_texto = lambda to, t: SENT.append(("texto", to, t))
wa_api.enviar_botones = lambda to, t, b: SENT.append(("botones", to, t, b))
wa_api.enviar_lista = lambda to, t, btn, ops: SENT.append(("lista", to, t, btn, ops))
wa_api.marcar_leido = lambda mid: None

import bot_wa  # noqa: E402  (después del monkeypatch)

fallos = 0


def check(cond, msg):
    global fallos
    print(("[ok] " if cond else "[FALLA] ") + msg)
    if not cond:
        fallos += 1


# 1) Resolver de identidad (Opción B) contra la DB real — solo lectura.
print("── Identidad (Opción B) ──")
for wa_phone, rol_esperado in [("584240000011", "centro"),      # representante id=2
                               ("584240000044", "aliado"),      # aliado id=1
                               ("584129320053", "voluntario")]:  # voluntario id=2
    r = bot_wa.resolver_identidad(wa_phone)
    check(r["tipo"] == "match", f"{wa_phone} → reconocido como registro existente ({r['tipo']})")

r_nuevo = bot_wa.resolver_identidad("580000000001")
check(r_nuevo["tipo"] == "nuevo", "número inexistente → 'nuevo'")

# 2) Webhook de verificación (GET) — lo que Meta llama al conectar.
print("\n── Webhook verificación ──")
bot_wa.WHATSAPP_VERIFY_TOKEN = "TESTTOK"
cli = bot_wa.app.test_client()
ok = cli.get("/wa/webhook?hub.mode=subscribe&hub.verify_token=TESTTOK&hub.challenge=42")
check(ok.status_code == 200 and ok.data == b"42", "token correcto → 200 + challenge")
bad = cli.get("/wa/webhook?hub.mode=subscribe&hub.verify_token=MAL&hub.challenge=42")
check(bad.status_code == 403, "token incorrecto → 403")

# 3) Ruteo del registro (in-memory, sin escribir en la DB).
print("\n── Ruteo de registro ──")
NUEVO = "580000000001"  # canónico None → siempre 'nuevo'
bot_wa.estados.pop(NUEVO, None)
SENT.clear()

bot_wa.manejar_texto(NUEVO, "hola")
check(any(e[0] == "lista" for e in SENT), "primer contacto → menú de opciones (lista)")
check(any("rol|pedir" in str(e) for e in SENT), "el menú incluye Necesito ayuda")

SENT.clear()
bot_wa.manejar_interactivo(NUEVO, "rol|voluntario")
check(bot_wa.estados.get(NUEVO, {}).get("flujo") == "voluntario", "elige Voluntariado → flujo abierto")
check(any("nombre" in str(e).lower() for e in SENT), "pregunta el nombre")

SENT.clear()
bot_wa.manejar_texto(NUEVO, "Juan Pérez")
check(bot_wa.estados[NUEVO]["datos"].get("nombre") == "Juan Pérez", "guarda el nombre")
check(bot_wa.estados[NUEVO]["paso"] == "zona", "avanza a preguntar la zona")
check(any("zona" in str(e).lower() for e in SENT), "pregunta la zona")

# NO respondemos la zona → NO se crea registro. Limpiamos el estado de prueba.
bot_wa.estados.pop(NUEVO, None)

# 3b) Ruteo de PEDIR AYUDA (in-memory; se corta antes de finalizar → no escribe en la DB).
print("\n── Ruteo de pedir ayuda ──")
SENT.clear()
bot_wa.manejar_texto(NUEVO, "ayuda")
check(bot_wa.estados.get(NUEVO, {}).get("flujo") == "pedir", "palabra clave 'ayuda' → flujo pedir")
check(any(e[0] == "lista" for e in SENT), "pregunta la categoría (lista)")

SENT.clear()
bot_wa.manejar_interactivo(NUEVO, "pcat|agua")
check(bot_wa.estados[NUEVO]["datos"].get("cat") == "agua", "guarda la categoría")
check(bot_wa.estados[NUEVO]["paso"] == "descripcion", "avanza a la descripción")

SENT.clear()
bot_wa.manejar_texto(NUEVO, "agua potable para 3 familias")
check(bot_wa.estados[NUEVO]["paso"] == "zona", "descripción guardada → pregunta la zona")

SENT.clear()
bot_wa.manejar_texto(NUEVO, "La Guaira, sector El Cardonal")
check(bot_wa.estados[NUEVO]["paso"] == "nombre", "zona guardada → pregunta el nombre")

# NO respondemos el nombre → NO se crea el llamado. Limpiamos el estado de prueba.
bot_wa.estados.pop(NUEVO, None)
bot_wa.efimero.cooldown_del("pedir_wa", NUEVO)  # limpia el freno (ahora vive en la DB)

# 3b-bis) Foto en 'pedir ayuda': _pedir_siguiente_paso salta los campos que la IA
# ya prellenó (sin llamar a captura.leer_flyer real — eso es I/O, no lógica).
print("\n── Pedir ayuda con foto (prellenado por IA) ──")
st = {"flujo": "pedir", "paso": "categoria", "datos": {}}
SENT.clear()
bot_wa._pedir_siguiente_paso(NUEVO, st)
check(st["paso"] == "categoria" and any(e[0] == "lista" for e in SENT),
      "sin nada prellenado → pregunta la categoría")

st["datos"]["cat"] = "desaparecido"
SENT.clear()
bot_wa._pedir_siguiente_paso(NUEVO, st)
check(st["paso"] == "descripcion", "con categoría ya puesta (por IA) → salta a descripción")

st["datos"]["descripcion"] = "Hombre visto por última vez cerca del puente"
st["datos"]["zona"] = "Catia, sector La Silsa"
SENT.clear()
bot_wa._pedir_siguiente_paso(NUEVO, st)
check(st["paso"] == "nombre", "con descripción y zona ya puestas → salta directo a nombre")

st["datos"]["nombre"] = ""  # la IA no encontró nombre, pero el paso YA se preguntó
FINALIZADO = []
_orig_finalizar_pedido = bot_wa._finalizar_pedido
bot_wa._finalizar_pedido = lambda to, s: FINALIZADO.append((to, s))
bot_wa._pedir_siguiente_paso(NUEVO, st)
check(len(FINALIZADO) == 1, "con los 4 campos completos → finaliza el pedido (no vuelve a preguntar)")
bot_wa._finalizar_pedido = _orig_finalizar_pedido

# 3c) Ruteo de LLAMADOS del Voluntariado (in-memory; se corta antes de crear).
print("\n── Ruteo de llamado (voluntariado) ──")
SENT.clear()
bot_wa._iniciar_llamado(NUEVO, {"id": 999, "estado": "pendiente"})
check(NUEVO not in bot_wa.estados, "voluntario no aprobado → no abre el flujo")
check(any("revisión" in str(e) for e in SENT), "explica que está en revisión")

SENT.clear()
bot_wa._iniciar_llamado(NUEVO, {"id": 999, "estado": "aprobado"})
check(bot_wa.estados.get(NUEVO, {}).get("flujo") == "llamado", "voluntario aprobado → flujo llamado")
check(any("sector" in str(e).lower() for e in SENT), "pregunta el sector")

SENT.clear()
bot_wa.manejar_texto(NUEVO, "El Valle, bloque 5")
check(bot_wa.estados[NUEVO]["datos"].get("sector") == "El Valle, bloque 5", "guarda el sector")
check(bot_wa.estados[NUEVO]["paso"] == "contenido", "avanza a pedir el contenido")
check(any("nota de voz" in str(e).lower() for e in SENT), "ofrece texto/foto/voz")

SENT.clear()
bot_wa.manejar_texto(NUEVO, "cancelar")
check(NUEVO not in bot_wa.estados, "'cancelar' cierra cualquier flujo")
check(any("cancel" in str(e).lower() for e in SENT), "confirma la cancelación")

SENT.clear()
bot_wa.manejar_interactivo(NUEVO, "vlla|new")
check(any("regístrate" in str(e).lower() for e in SENT),
      "botón Levantar llamado sin registro → invita a registrarse")

# Saludo por rol (identidad simulada; no toca la DB).
_orig_ident = bot_wa.resolver_identidad
bot_wa.resolver_identidad = lambda p: {"tipo": "vinculado", "rol": "voluntario",
                                       "tabla": "voluntarios",
                                       "registro": {"id": 999, "estado": "aprobado"}}
SENT.clear()
bot_wa.manejar_texto(NUEVO, "hola")
check(any(e[0] == "botones" and "vlla|new" in str(e) for e in SENT),
      "saludo a voluntario → botón 📣 Levantar llamado")
SENT.clear()
bot_wa.manejar_texto(NUEVO, "llamado")
check(bot_wa.estados.get(NUEVO, {}).get("flujo") == "llamado", "palabra 'llamado' abre el flujo")
bot_wa.estados.pop(NUEVO, None)

bot_wa.resolver_identidad = lambda p: {"tipo": "vinculado", "rol": "aliado",
                                       "tabla": "aliados",
                                       "registro": {"id": 999, "estado": "aprobado"}}
SENT.clear()
bot_wa.manejar_texto(NUEVO, "hola")
check(any(e[0] == "botones" and "rol|pedir" in str(e) for e in SENT),
      "saludo a Manos Solidarias → botón 🆘 Necesito ayuda")
bot_wa.resolver_identidad = _orig_ident
bot_wa.estados.pop(NUEVO, None)

# 4) Ruteo de la FICHA del centro (in-memory, sin llegar a crear el registro).
print("\n── Ruteo de ficha del centro ──")
bot_wa.estados.pop(NUEVO, None)
SENT.clear()
bot_wa.manejar_interactivo(NUEVO, "rol|centro")
bot_wa.manejar_texto(NUEVO, "Juan Pérez")          # nombre
bot_wa.manejar_interactivo(NUEVO, "tipo|acopio")   # tipo
SENT.clear()
bot_wa.manejar_texto(NUEVO, "Refugio San José")    # punto → arranca la ficha
st = bot_wa.estados[NUEVO]
check(st["paso"] == "ficha", "tras el nombre del punto → entra a la ficha")
check(st["datos"]["fi"] == 0, "ficha arranca en el campo 0")
check(any("inmueble" in str(e).lower() for e in SENT), "primera pregunta de ficha = tipo de inmueble")

SENT.clear()
bot_wa.manejar_texto(NUEVO, "Iglesia")             # responde tipo_inmueble (texto)
check(st["datos"]["ficha"].get("tipo_inmueble") == "Iglesia", "guarda tipo_inmueble")
check(st["datos"]["fi"] == 1, "avanza al campo 1")

# Responder un campo sí/no por BOTÓN (mapea 'si' -> 'Sí')
bot_wa.estados[NUEVO]["datos"]["fi"] = 4            # saltar a 'cocina' (modo sino)
SENT.clear()
bot_wa.manejar_interactivo(NUEVO, "fi|si")
check(st["datos"]["ficha"].get("cocina") == "Sí", "botón 'Sí' en cocina → guarda 'Sí'")
check(st["datos"]["fi"] == 5, "avanza al campo 5 tras el botón")

# NO completamos los 16 → NO se crea registro. Limpiamos.
bot_wa.estados.pop(NUEVO, None)
check(len(bot_wa.FICHA) == 16, "la ficha tiene 16 campos (= FICHA_COLS)")

# 5) Las LISTAS deben mandar ids CON PREFIJO (bug real: iban pelados y el handler no matcheaba).
print("\n── Prefijos en listas interactivas ──")
bot_wa.estados.pop(NUEVO, None)
SENT.clear()
bot_wa.manejar_interactivo(NUEVO, "rol|centro")
bot_wa.manejar_texto(NUEVO, "Ana")  # → dispara la lista de tipos
listas = [e for e in SENT if e[0] == "lista"]
check(bool(listas) and all(op[0].startswith("tipo|") for op in listas[-1][4]),
      "lista de tipos: todos los ids con prefijo 'tipo|'")

bot_wa.estados.pop(NUEVO, None)
SENT.clear()
bot_wa.manejar_interactivo(NUEVO, "rol|aliado")  # → dispara la lista de categorías
listas = [e for e in SENT if e[0] == "lista"]
check(bool(listas) and all(op[0].startswith("cat|") for op in listas[-1][4]),
      "lista de Manos Solidarias: todos los ids con prefijo 'cat|'")

# Y que al seleccionar de la lista, el flujo AVANZA (no se queda pegado).
bot_wa.estados.pop(NUEVO, None)
bot_wa.manejar_interactivo(NUEVO, "rol|centro")
bot_wa.manejar_texto(NUEVO, "Ana")
bot_wa.manejar_interactivo(NUEVO, "tipo|acopio")  # selección de la lista de tipos
check(bot_wa.estados[NUEVO]["paso"] == "punto", "seleccionar tipo de la lista → avanza a 'punto'")
bot_wa.estados.pop(NUEVO, None)

# 6) Ingestión de inventario: gate + botón confirmar/descartar (sin media real ni tocar la DB).
print("\n── Ingestión (gate + confirmar) ──")
SENT.clear()
# Un número NO registrado manda una imagen → debe pedir registro, NO procesar.
bot_wa._despachar({"from": "580000000009", "id": "m1", "type": "image", "image": {"id": "fake"}})
check(any(("registrad" in str(e).lower()) or ("hola" in str(e).lower()) for e in SENT),
      "media de no-registrado → pide registrarse (no procesa)")

# Botón "Descartar" de una captura inexistente → no crashea y responde (UPDATE 0 filas).
SENT.clear()
bot_wa.manejar_interactivo("580000000009", "cap|99999999|no")
check(any("descart" in str(e).lower() for e in SENT), "botón Descartar responde")

# 7) Stock numérico UNIFICADO: aplicar_captura (compartido WhatsApp/Telegram).
#    Crea filas reales (rep + capturas) y las limpia al final.
print("\n── Stock numérico (aplicar_captura) ──")
import db as _db
import stock as _st

check(_st.parse_cantidad("30 cápsulas") == (30.0, "cápsulas"), "parse '30 cápsulas'")
check(_st.parse_cantidad("2 cajas") == (2.0, "cajas"), "parse '2 cajas'")
check(_st.parse_cantidad("algunos") == (None, "algunos"), "parse 'algunos' → sin número")

rid = _db.crear_representante(None, "TEST STOCK", "acopio", "TEST STOCK", "58400000000")
cid = _db.crear_captura(rid, "texto", "inventario", "TEST", "x", [
    {"categoria": "bebés", "item": "Pañales", "cantidad": "200 unidades"},
    {"categoria": "salud", "item": "Gasas", "cantidad": ""}], [])
pid = _st.aplicar_captura(cid, reemplazar=False)
inv = _st.de_punto(pid)
check(len(inv) == 2, "aplicar_captura → 2 insumos en stock")
pan = [r for r in inv if r["item_norm"] == "panales"]
check(bool(pan) and pan[0]["cantidad"] == 200.0 and pan[0]["unidad"] == "unidades", "Pañales 200 'unidades'")
check(_st.faltan_cantidad(pid) == ["Gasas"], "Gasas → falta cantidad")

_st.aplicar_captura(cid, reemplazar=False)  # misma captura otra vez
check(len(_st.de_punto(pid)) == 2, "re-aplicar misma captura → idempotente (sigue 2)")

cid2 = _db.crear_captura(rid, "texto", "inventario", "TEST", "x",
                         [{"categoria": "bebés", "item": "pañales", "cantidad": "50 unidades"}], [])
_st.aplicar_captura(cid2, reemplazar=False)
pan = [r for r in _st.de_punto(pid) if r["item_norm"] == "panales"]
check(bool(pan) and pan[0]["cantidad"] == 250.0, "sumar otra captura → Pañales 200+50 = 250")

cid3 = _db.crear_captura(rid, "texto", "inventario", "TEST", "x",
                         [{"categoria": "agua", "item": "Agua", "cantidad": "10 litros"}], [])
_st.aplicar_captura(cid3, reemplazar=True)
inv = _st.de_punto(pid)
check(len(inv) == 1 and inv[0]["item_norm"] == "agua", "reemplazar → queda solo lo nuevo")

# Una captura de DEMANDA (/necesito) NO debe entrar al stock (va a necesidades).
cid4 = _db.crear_captura(rid, "texto", "necesidad", "TEST", "x",
                         [{"categoria": "agua", "item": "Cloro", "cantidad": "5 litros"}], [], clase="demanda")
_st.aplicar_captura(cid4, reemplazar=False)
check(not any(r["item_norm"] == "cloro" for r in _st.de_punto(pid)), "captura 'demanda' NO entra al stock")

# 8) Auditoría anti-duplicado (parece_snapshot) — lección Ecocamping.
print("\n── Auditoría anti-duplicado ──")
cid5 = _db.crear_captura(rid, "texto", "inventario", "TEST", "x", [
    {"categoria": "agua", "item": "Agua", "cantidad": "5 litros"},
    {"categoria": "alimento", "item": "Harina", "cantidad": "3 kg"},
    {"categoria": "alimento", "item": "Arroz", "cantidad": "2 kg"}], [])
c, t = _st.parece_snapshot(cid5)
check((c, t) == (1, 3), f"lista mayormente nueva → no sospechosa ({c}/{t})")
_st.aplicar_captura(cid5, reemplazar=False)
cid6 = _db.crear_captura(rid, "texto", "inventario", "TEST", "x", [
    {"categoria": "agua", "item": "agua", "cantidad": "5 litros"},
    {"categoria": "alimento", "item": "HARINA", "cantidad": "3 kg"},
    {"categoria": "alimento", "item": "Arroz", "cantidad": "2 kg"}], [])
c, t = _st.parece_snapshot(cid6)
check((c, t) == (3, 3), f"misma lista re-enviada → snapshot detectado ({c}/{t})")

# 9) Completar cantidades faltantes ('gasas 12').
print("\n── Completar cantidades ──")
cid7 = _db.crear_captura(rid, "texto", "inventario", "TEST", "x",
                         [{"categoria": "salud", "item": "Gasas", "cantidad": ""}], [])
_st.aplicar_captura(cid7, reemplazar=False)
check("Gasas" in _st.faltan_cantidad(pid), "Gasas quedó sin cantidad")
r = _st.completar_cantidad(pid, "gasas 12")
check(r == ("Gasas", 12.0), f"completar 'gasas 12' → {r}")
check("Gasas" not in _st.faltan_cantidad(pid), "Gasas ya no falta")
check(_st.completar_cantidad(pid, "hola como estas") is None, "texto sin número → None")

# 10) Nudge: inv|later crea recordatorio; capa bot con centro vinculado.
print("\n── Nudge + ver inventario (capa bot) ──")
SENT.clear()
bot_wa.manejar_interactivo("580000000009", f"inv|later|{rid}")
row = _db.conectar().execute("SELECT canal, activo FROM recordatorios_inv WHERE rep_id = ?", (rid,)).fetchone()
check(row is not None and row[1] == 1, "inv|later → recordatorio activo creado")
check(any("record" in str(e).lower() for e in SENT), "responde confirmando el recordatorio")

_c = _db.conectar()
_c.execute("UPDATE representantes SET wa_phone = '4000000009' WHERE id = ?", (rid,))
_c.commit()
_c.close()
SENT.clear()
bot_wa.manejar_texto("584000000009", "inventario")
check(any("inventario publicado" in str(e).lower() for e in SENT), "'inventario' → muestra el stock")

_c = _db.conectar()
_c.execute("DELETE FROM stock WHERE punto_id = ?", (pid,))
_c.execute("DELETE FROM inventario WHERE captura_id IN (SELECT id FROM capturas WHERE representante_id = ?)", (rid,))
_c.execute("DELETE FROM capturas WHERE representante_id = ?", (rid,))
_c.execute("DELETE FROM recordatorios_inv WHERE rep_id = ?", (rid,))
_c.execute("DELETE FROM representantes WHERE id = ?", (rid,))
_c.commit()
_c.close()

# 11) Motor de envíos (Fase B): despachar reserva, recibir descuenta, cancelar libera.
print("\n── Envíos (despacho → recepción) ──")
import envios as _env
_env.crear_tablas()
rid2 = _db.crear_representante(None, "TEST ACOPIO", "acopio", "TEST ACOPIO", "58400000001")
rid3 = _db.crear_representante(None, "TEST REFUGIO", "refugio", "TEST REFUGIO", "58400000002")
cidz = _db.crear_captura(rid2, "texto", "inventario", "T", "x", [
    {"categoria": "bebés", "item": "Pañales", "cantidad": "100 unidades"},
    {"categoria": "agua", "item": "Agua", "cantidad": "20 litros"}], [])
pid2 = _st.aplicar_captura(cidz, reemplazar=True)
inv = {r["item"]: r for r in _st.de_punto(pid2)}
e = _env.despachar(pid2, rid2, rid3, rid3, [(inv["Pañales"]["id"], 40), (inv["Agua"]["id"], 5)])
check(bool(e.get("token")), "despachar → token generado")
inv2 = {r["item"]: r for r in _st.de_punto(pid2)}
check(inv2["Pañales"]["reservado"] == 40, "despacho reserva 40 pañales")
try:
    _env.despachar(pid2, rid2, rid3, rid3, [(inv["Pañales"]["id"], 70)])
    check(False, "sobre-despacho debía fallar")
except ValueError:
    check(True, "sobre-despacho rechazado (solo quedan 60 disponibles)")
d = _env.por_token(e["token"])
check(d is not None and len(d["items"]) == 2 and d["estado"] == "despachado", "por_token → manifiesto completo")
recep = {d["items"][0]["id"]: (30, "menos"), d["items"][1]["id"]: (5, None)}
d2 = _env.recibir(e["token"], recep)
check(d2["estado"] == "incompleto", "faltaron 10 pañales → estado 'incompleto'")
check(d2["items"][0]["motivo"] == "menos", "motivo del faltante registrado")
inv3 = {r["item"]: r for r in _st.de_punto(pid2)}
check(inv3["Pañales"]["cantidad"] == 60.0 and inv3["Pañales"]["reservado"] == 0,
      "stock: 100−40 enviados = 60, reserva liberada")
check(inv3["Agua"]["cantidad"] == 15.0, "agua 20−5 = 15")
check(_env.recibir(e["token"], {}) is None, "token ya cerrado → None (no doble descuento)")
e2 = _env.despachar(pid2, rid2, rid3, rid3, [(inv["Agua"]["id"], 3)])
check(_env.cancelar(e2["id"]), "cancelar envío despachado")
inv4 = {r["item"]: r for r in _st.de_punto(pid2)}
check(inv4["Agua"]["reservado"] == 0 and inv4["Agua"]["cantidad"] == 15.0,
      "cancelación libera reserva sin descontar")

# 12) Despacho por texto (parse_lineas) + página pública del QR (backend, E2E sin red).
print("\n── Despacho por texto + página del QR ──")
items12, errs12 = _env.parse_lineas(pid2, "pañales 10\nagua 2")
check(len(items12) == 2 and not errs12, "parse_lineas: 2 líneas entendidas")
_i, _e2 = _env.parse_lineas(pid2, "chocolate 5")
check(not _i and _e2, "parse_lineas: ítem inexistente → error claro")
_i, _e2 = _env.parse_lineas(pid2, "pañales 999")
check(not _i and _e2, "parse_lineas: pedir más de lo disponible → error claro")

import backend as _bk
e3 = _env.despachar(pid2, rid2, rid3, rid3, items12)
cliB = _bk.app.test_client()
pg = cliB.get(f"/api/entrega/{e3['token']}")
check(pg.status_code == 200 and "Pañales".encode() in pg.data, "página del envío muestra el manifiesto")
png = cliB.get(f"/api/entrega/{e3['token']}.png")
check(png.status_code == 200 and png.data[:4] == b"\x89PNG", "el QR PNG se genera")
check(cliB.get("/api/entrega/token-falso").status_code == 404, "token falso → 404")
rc = cliB.post(f"/api/entrega/{e3['token']}/confirmar", json={"completo": True})
check(rc.status_code == 200 and rc.get_json().get("estado") == "recibido", "confirmar completo → 'recibido'")
inv5 = {r["item"]: r for r in _st.de_punto(pid2)}
check(inv5["Pañales"]["cantidad"] == 50.0, "stock descontado tras confirmar (60−10=50)")
check(cliB.post(f"/api/entrega/{e3['token']}/confirmar", json={"completo": True}).status_code == 409,
      "segunda confirmación → 409 (sin doble descuento)")

_c = _db.conectar()
_c.execute("DELETE FROM envio_items WHERE envio_id IN (SELECT id FROM envios WHERE de_rep = ?)", (rid2,))
_c.execute("DELETE FROM envios WHERE de_rep = ?", (rid2,))
_c.execute("DELETE FROM stock WHERE punto_id = ?", (pid2,))
_c.execute("DELETE FROM inventario WHERE captura_id = ?", (cidz,))
_c.execute("DELETE FROM capturas WHERE representante_id IN (?,?)", (rid2, rid3))
_c.execute("DELETE FROM representantes WHERE id IN (?,?)", (rid2, rid3))
_c.commit()
_c.close()

# Retiro voluntario (in-memory: número desconocido y prompts; no escribe en la DB).
print("\n── Retiro voluntario ──")
SENT.clear()
bot_wa.manejar_interactivo(NUEVO, "ret|si")
check(any("No encontré un registro" in str(e) for e in SENT),
      "ret|si de un número sin registro → no toca nada")

SENT.clear()
_id_activo = {"tipo": "vinculado", "rol": "aliado", "tabla": "aliados",
              "registro": {"id": 999999, "nombre": "Prueba", "estado": "aprobado"}}
bot_wa._iniciar_retiro(NUEVO, _id_activo)
check(any(e[0] == "botones" and "ret|si" in str(e) and "ret|no" in str(e) for e in SENT),
      "pedir retiro → confirmación con botones Sí / Seguir")

SENT.clear()
_id_retirado = {"tipo": "vinculado", "rol": "aliado", "tabla": "aliados",
                "registro": {"id": 999999, "nombre": "Prueba", "estado": "retirado"}}
bot_wa._iniciar_retiro(NUEVO, _id_retirado)
check(any("ret|volver" in str(e) for e in SENT), "ya retirado → ofrece volver a la red")

# Botones «Puedo ayudar · WhatsApp» de la web (códigos LL-/ND- prellenados).
# Solo el arranque del flujo: se corta antes de finalizar, no escribe en la DB.
print("\n── Códigos web LL-/ND- ──")
_c = _db.conectar()
_lla = _c.execute("SELECT id, sector FROM llamados WHERE estado = 'activo' "
                  "ORDER BY id DESC LIMIT 1").fetchone()
_nec = _c.execute("SELECT id FROM necesidades WHERE estado NOT IN ('resuelta','cancelada') "
                  "ORDER BY id DESC LIMIT 1").fetchone()
_c.close()

if _lla:
    bot_wa.estados.pop(NUEVO, None)
    SENT.clear()
    bot_wa.manejar_texto(NUEVO, f"Hola 🙋 quiero ayudar con el llamado LL-{_lla['id']} — {_lla['sector'] or ''}")
    check(bot_wa.estados.get(NUEVO, {}).get("flujo") == "resp_llamado",
          "texto con LL-<id> activo → abre flujo resp_llamado")
    check(any("Cómo puedes ayudar" in str(e) for e in SENT), "pregunta cómo puede ayudar")
else:
    print("[--] sin llamados activos en la DB local; se prueba solo el cerrado")

bot_wa.estados.pop(NUEVO, None)
SENT.clear()
bot_wa.manejar_texto(NUEVO, "Hola 🙋 quiero ayudar con el llamado LL-999999")
check(NUEVO not in bot_wa.estados and any("ya se cerró" in str(e) for e in SENT),
      "LL-<id> inexistente → avisa cerrado y no deja flujo abierto")

if _nec:
    bot_wa.estados.pop(NUEVO, None)
    SENT.clear()
    bot_wa.manejar_texto(NUEVO, f"Hola 🤝 puedo llevar esto — necesidad ND-{_nec['id']}")
    _fl = bot_wa.estados.get(NUEVO, {}).get("flujo")
    check(_fl == "compromiso" or any("cubierta" in str(e) for e in SENT),
          "texto con ND-<id> → abre flujo compromiso (o avisa cubierta)")
else:
    print("[--] sin necesidades abiertas en la DB local; se prueba solo la inexistente")

bot_wa.estados.pop(NUEVO, None)
SENT.clear()
bot_wa.manejar_texto(NUEVO, "Hola 🤝 puedo llevar — necesidad ND-999999")
check(NUEVO not in bot_wa.estados and any("cubierta o cerrada" in str(e) for e in SENT),
      "ND-<id> inexistente → avisa cerrada y no deja flujo abierto")

# El código interrumpe un flujo abierto (como un deep link) y el texto normal no dispara.
bot_wa.estados[NUEVO] = {"flujo": "pedir", "paso": "zona", "datos": {}}
SENT.clear()
bot_wa.manejar_texto(NUEVO, "Quiero ayudar con el llamado LL-999999")
check(bot_wa.estados.get(NUEVO, {}).get("flujo") != "pedir",
      "un código LL- en medio de otro flujo lo interrumpe (semántica deep link)")
bot_wa.estados.pop(NUEVO, None)

print(f"\n{'TODO OK' if not fallos else str(fallos) + ' FALLAS'}")
raise SystemExit(1 if fallos else 0)
