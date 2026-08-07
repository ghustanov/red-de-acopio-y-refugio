"""Estado efímero COMPARTIDO entre procesos (SQLite, tablas en angeles.db).

Reemplaza los dicts en memoria que se rompen con gunicorn multi-worker (cada
worker tenía su propia copia) y se pierden en cada restart:
  - bot_wa.py    `estados`     → tabla wa_estados  (conversación por teléfono)
  - bot_wa.py    `_pedir_cool` → tabla rl_cooldowns (freno anti-spam del flujo pedir)
  - backend.py   `_*_ip` / `_*_global` (5 pares)   → rl_cooldowns + rl_eventos
  - bot_apoyo.py `estados`     → tabla tg_estados  (conversación por telegram_id)
  - bot_apoyo.py `_modo`/_pedir_cool → kv_efimero / rl_cooldowns

Solo stdlib + db.py (misma filosofía que wa_util.py / stock.py). Las escrituras
son atómicas con BEGIN IMMEDIATE (mismo patrón que envios.despachar), así dos
workers no pueden pasar el mismo cooldown a la vez.

Test: sudo -u angeles venv/bin/python3 test_efimero.py
"""
import json
import time

import db

ESTADO_TTL = 24 * 3600   # una conversación abandonada >24 h se considera muerta
EVENTOS_TTL = 3600       # los eventos de topes globales viejos se podan al pasar


def _init():
    con = db.conectar()
    con.execute(
        "CREATE TABLE IF NOT EXISTS wa_estados ("
        "telefono TEXT PRIMARY KEY, valor TEXT NOT NULL, ts REAL NOT NULL)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS rl_cooldowns ("
        "ambito TEXT NOT NULL, clave TEXT NOT NULL, ts REAL NOT NULL, "
        "PRIMARY KEY (ambito, clave))")
    con.execute(
        "CREATE TABLE IF NOT EXISTS rl_eventos ("
        "ambito TEXT NOT NULL, ts REAL NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_rl_eventos ON rl_eventos (ambito, ts)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS tg_estados ("
        "chat_id TEXT PRIMARY KEY, valor TEXT NOT NULL, ts REAL NOT NULL)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS kv_efimero ("
        "ambito TEXT NOT NULL, clave TEXT NOT NULL, valor TEXT NOT NULL, "
        "ts REAL NOT NULL, PRIMARY KEY (ambito, clave))")
    con.commit()
    con.close()


_init()


# ─────────────────── Estado de conversación (bot de WhatsApp) ─────────────────
def estado_get(telefono):
    """Estado vivo de la conversación, o None. Un estado más viejo que
    ESTADO_TTL se descarta (conversación abandonada; que no 'resucite' semanas
    después con la persistencia — antes el restart hacía esa limpieza solo)."""
    con = db.conectar()
    row = con.execute("SELECT valor, ts FROM wa_estados WHERE telefono = ?",
                      (telefono,)).fetchone()
    con.close()
    if not row:
        return None
    if time.time() - row["ts"] > ESTADO_TTL:
        estado_del(telefono)
        return None
    try:
        return json.loads(row["valor"])
    except (ValueError, TypeError):
        estado_del(telefono)
        return None


def estado_set(telefono, st):
    con = db.conectar()
    con.execute("INSERT OR REPLACE INTO wa_estados (telefono, valor, ts) VALUES (?,?,?)",
                (telefono, json.dumps(st, ensure_ascii=False), time.time()))
    con.commit()
    con.close()


def estado_del(telefono):
    con = db.conectar()
    con.execute("DELETE FROM wa_estados WHERE telefono = ?", (telefono,))
    con.commit()
    con.close()


# ──────────────── Estado de conversación (bot de Telegram) ───────────────────
def estado_tg_get(chat_id):
    """Igual que estado_get pero para el bot de Telegram (clave = telegram_id)."""
    con = db.conectar()
    row = con.execute("SELECT valor, ts FROM tg_estados WHERE chat_id = ?",
                      (chat_id,)).fetchone()
    con.close()
    if not row:
        return None
    if time.time() - row["ts"] > ESTADO_TTL:
        estado_tg_del(chat_id)
        return None
    try:
        return json.loads(row["valor"])
    except (ValueError, TypeError):
        estado_tg_del(chat_id)
        return None


def estado_tg_set(chat_id, st):
    con = db.conectar()
    con.execute("INSERT OR REPLACE INTO tg_estados (chat_id, valor, ts) VALUES (?,?,?)",
                (chat_id, json.dumps(st, ensure_ascii=False), time.time()))
    con.commit()
    con.close()


def estado_tg_del(chat_id):
    con = db.conectar()
    con.execute("DELETE FROM tg_estados WHERE chat_id = ?", (chat_id,))
    con.commit()
    con.close()


# ───────────────────── KV genérico con vencimiento opcional ──────────────────
def kv_set(ambito, clave, valor):
    """Guarda un valor JSON-serializable bajo (ambito, clave)."""
    con = db.conectar()
    con.execute("INSERT OR REPLACE INTO kv_efimero (ambito, clave, valor, ts) VALUES (?,?,?,?)",
                (ambito, clave, json.dumps(valor, ensure_ascii=False), time.time()))
    con.commit()
    con.close()


def kv_get(ambito, clave, ttl=None):
    """Devuelve el valor, o None si no existe o si es más viejo que `ttl` seg."""
    con = db.conectar()
    row = con.execute("SELECT valor, ts FROM kv_efimero WHERE ambito = ? AND clave = ?",
                      (ambito, clave)).fetchone()
    con.close()
    if not row:
        return None
    if ttl is not None and time.time() - row["ts"] > ttl:
        kv_del(ambito, clave)
        return None
    try:
        return json.loads(row["valor"])
    except (ValueError, TypeError):
        return None


def kv_del(ambito, clave):
    con = db.conectar()
    con.execute("DELETE FROM kv_efimero WHERE ambito = ? AND clave = ?", (ambito, clave))
    con.commit()
    con.close()


# ───────────────────────── Cooldowns (por clave) ──────────────────────────────
def cooldown_ts(ambito, clave):
    """Timestamp del último evento (0.0 si nunca). Solo consulta, no marca."""
    con = db.conectar()
    row = con.execute("SELECT ts FROM rl_cooldowns WHERE ambito = ? AND clave = ?",
                      (ambito, clave)).fetchone()
    con.close()
    return row["ts"] if row else 0.0


def cooldown_marcar(ambito, clave):
    """Registra 'ahora' como último evento de la clave."""
    con = db.conectar()
    con.execute("INSERT OR REPLACE INTO rl_cooldowns (ambito, clave, ts) VALUES (?,?,?)",
                (ambito, clave, time.time()))
    con.commit()
    con.close()


def cooldown_del(ambito, clave):
    """Borra un cooldown (lo usa la suite de pruebas para limpiar)."""
    con = db.conectar()
    con.execute("DELETE FROM rl_cooldowns WHERE ambito = ? AND clave = ?", (ambito, clave))
    con.commit()
    con.close()


def cooldown_ok(ambito, clave, gap):
    """True si pasó `gap` desde el último evento de la clave — y lo marca.
    Atómico entre workers (BEGIN IMMEDIATE): solo uno puede pasar a la vez."""
    ahora = time.time()
    con = db.conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT ts FROM rl_cooldowns WHERE ambito = ? AND clave = ?",
                          (ambito, clave)).fetchone()
        if row and ahora - row["ts"] < gap:
            con.rollback()
            return False
        con.execute("INSERT OR REPLACE INTO rl_cooldowns (ambito, clave, ts) VALUES (?,?,?)",
                    (ambito, clave, ahora))
        con.commit()
        return True
    finally:
        con.close()


# ─────────────────────── Topes globales (por ventana) ─────────────────────────
def tope_global_ok(ambito, tope, ventana):
    """True si el ámbito lleva menos de `tope` eventos en la última `ventana`
    (segundos) — y registra el nuevo evento. Atómico entre workers."""
    ahora = time.time()
    con = db.conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM rl_eventos WHERE ambito = ? AND ts < ?",
                    (ambito, ahora - max(ventana, EVENTOS_TTL)))
        n = con.execute("SELECT COUNT(*) FROM rl_eventos WHERE ambito = ? AND ts >= ?",
                        (ambito, ahora - ventana)).fetchone()[0]
        if n >= tope:
            con.rollback()
            return False
        con.execute("INSERT INTO rl_eventos (ambito, ts) VALUES (?,?)", (ambito, ahora))
        con.commit()
        return True
    finally:
        con.close()
