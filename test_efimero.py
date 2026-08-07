#!/usr/bin/env python3
"""Pruebas de efimero.py — SIN tocar la DB real (usa una DB temporal).

Cubre lo que el patch necesita garantizar:
  1. estado get/set/del básico (round-trip JSON con unicode)
  2. TTL: un estado viejo se descarta
  3. dos "workers" (conexiones/procesos separados) ven el MISMO estado
  4. cooldown_ok atómico: N hilos compitiendo → pasa exactamente 1
  5. tope_global_ok respeta el tope bajo concurrencia
  6. cooldown_ts / cooldown_marcar (patrón del flujo 'pedir' de bot_wa)

Corre local:  python3 test_efimero.py
Corre en VPS: sudo -u angeles venv/bin/python3 test_efimero.py
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix="efimero_test_")
DB_TEST = os.path.join(TMP, "test.db")

# Apuntar db.DB_PATH a la DB temporal ANTES de importar efimero (su _init crea
# las tablas al importar). Así el test jamás toca angeles.db.
import db  # noqa: E402
db.DB_PATH = DB_TEST
import efimero  # noqa: E402

OK = 0


def check(nombre, cond):
    global OK
    if not cond:
        sys.exit(f"FALLO: {nombre}")
    OK += 1
    print(f"  ok {OK:2d} — {nombre}")


# 1) round-trip básico
st = {"flujo": "centro", "paso": "ficha",
      "datos": {"nombre": "María Pérez", "fi": 11, "ficha": {"baños": "2", "año": "ñ✓"}}}
efimero.estado_set("58412000001", st)
check("estado_set/get round-trip", efimero.estado_get("58412000001") == st)
check("estado_get de tel desconocido → None", efimero.estado_get("58999999999") is None)
efimero.estado_del("58412000001")
check("estado_del borra", efimero.estado_get("58412000001") is None)
efimero.estado_del("58412000001")  # idempotente
check("estado_del idempotente", True)

# 2) TTL: estado viejo se descarta (y se limpia de la tabla)
efimero.estado_set("58412000002", {"flujo": "pedir", "paso": "zona", "datos": {}})
con = sqlite3.connect(DB_TEST)
con.execute("UPDATE wa_estados SET ts = ? WHERE telefono = ?",
            (time.time() - efimero.ESTADO_TTL - 60, "58412000002"))
con.commit(); con.close()
check("estado más viejo que TTL → None", efimero.estado_get("58412000002") is None)
con = sqlite3.connect(DB_TEST)
n = con.execute("SELECT COUNT(*) FROM wa_estados WHERE telefono='58412000002'").fetchone()[0]
con.close()
check("estado vencido se limpió de la tabla", n == 0)

# 3) dos "workers": el estado escrito por un proceso lo ve otro proceso
efimero.estado_set("58412000003", {"flujo": "aliado", "paso": "zona", "datos": {"x": 1}})
otro_worker = subprocess.run(
    [sys.executable, "-c",
     f"import db; db.DB_PATH = {DB_TEST!r}; import efimero, json; "
     "st = efimero.estado_get('58412000003'); "
     "print(json.dumps(st)); "
     "st['paso'] = 'nombre'; efimero.estado_set('58412000003', st)"],
    capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
check("worker B lee el estado de worker A", '"flujo": "aliado"' in otro_worker.stdout)
st3 = efimero.estado_get("58412000003")
check("worker A ve la mutación de worker B", st3 and st3["paso"] == "nombre")

# 4) cooldown_ok atómico: 8 hilos a la vez → pasa exactamente 1
pasan = []
barrera = threading.Barrier(8)
def compite():
    barrera.wait()
    if efimero.cooldown_ok("test_cd", "misma_ip", 30):
        pasan.append(1)
hilos = [threading.Thread(target=compite) for _ in range(8)]
[h.start() for h in hilos]; [h.join() for h in hilos]
check("cooldown_ok bajo carrera: pasa exactamente 1 de 8", len(pasan) == 1)
check("cooldown_ok dentro de la ventana → False",
      efimero.cooldown_ok("test_cd", "misma_ip", 30) is False)
check("otra clave no se ve afectada",
      efimero.cooldown_ok("test_cd", "otra_ip", 30) is True)

# 5) tope_global_ok respeta el tope bajo concurrencia (tope 5, 12 intentos)
pasan2 = []
barrera2 = threading.Barrier(12)
def compite_tope():
    barrera2.wait()
    if efimero.tope_global_ok("test_tope", 5, 600):
        pasan2.append(1)
hilos = [threading.Thread(target=compite_tope) for _ in range(12)]
[h.start() for h in hilos]; [h.join() for h in hilos]
check("tope_global_ok bajo carrera: pasan exactamente 5 de 12", len(pasan2) == 5)
check("tope lleno → False", efimero.tope_global_ok("test_tope", 5, 600) is False)

# 6) patrón del flujo 'pedir': consultar sin marcar, luego marcar
check("cooldown_ts sin eventos → 0.0", efimero.cooldown_ts("pedir_wa", "58412000004") == 0.0)
efimero.cooldown_marcar("pedir_wa", "58412000004")
check("cooldown_ts tras marcar ≈ ahora",
      abs(efimero.cooldown_ts("pedir_wa", "58412000004") - time.time()) < 2)
check("cooldown_ts no marca (solo consulta)",
      efimero.cooldown_ok("pedir_wa2", "x", 30) is True)

# 7) estado del bot de Telegram (tabla tg_estados, mismo contrato que WA)
st_tg = {"paso": "pd_zona", "pedido": {"cat": "agua", "desc": "agua para 2 familias"}}
efimero.estado_tg_set("712000001", st_tg)
check("estado_tg round-trip", efimero.estado_tg_get("712000001") == st_tg)
check("estado_tg de chat desconocido → None", efimero.estado_tg_get("712999999") is None)
efimero.estado_tg_del("712000001")
check("estado_tg_del borra", efimero.estado_tg_get("712000001") is None)
con = sqlite3.connect(DB_TEST)
con.execute("INSERT INTO tg_estados (chat_id, valor, ts) VALUES (?,?,?)",
            ("712000002", '{"paso":"x"}', time.time() - efimero.ESTADO_TTL - 60))
con.commit(); con.close()
check("estado_tg más viejo que TTL → None", efimero.estado_tg_get("712000002") is None)

# 8) kv genérico con TTL (lo usa _modo del bot de Telegram)
efimero.kv_set("modo_tg", "712000003", "oferta")
check("kv round-trip", efimero.kv_get("modo_tg", "712000003") == "oferta")
check("kv respeta TTL amplio", efimero.kv_get("modo_tg", "712000003", ttl=900) == "oferta")
con = sqlite3.connect(DB_TEST)
con.execute("UPDATE kv_efimero SET ts = ? WHERE clave = '712000003'", (time.time() - 901,))
con.commit(); con.close()
check("kv vencido (TTL 900) → None", efimero.kv_get("modo_tg", "712000003", ttl=900) is None)
check("kv de clave desconocida → None", efimero.kv_get("modo_tg", "nope") is None)
efimero.kv_set("modo_tg", "712000004", "demanda")
efimero.kv_del("modo_tg", "712000004")
check("kv_del borra", efimero.kv_get("modo_tg", "712000004") is None)

print(f"\nTODO OK — {OK}/{OK} pruebas pasaron (DB temporal: {DB_TEST})")
