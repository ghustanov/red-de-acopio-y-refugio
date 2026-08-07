"""Base de datos SQLite: mensajes clasificados + centros de acopio."""
import sqlite3
import re
import difflib
import unicodedata
import wa_util
from config import DB_PATH


# Un PUNTO DE SALUD (hospital/clínica/CDI/ambulatorio/etc.) NO es un centro de acopio:
# se filtra de la pestaña 📦 para no mezclar las dos cosas. Los hospitales pertenecen a
# la red de colaboradores. Es un filtro por nombre (red de seguridad sobre el dato viejo);
# el classifier también dejó de mandarlos a `centros`.
_SALUD_RE = re.compile(
    r"(hospital|hospitalito|clinic|policlinic|\bcdi\b|ambulatorio|matern|"
    r"oncolog|hemocentro|periferic|morgue|medicina|seguro medic|"
    r"centro de salud|centro medic|centro clinic)"
)


def _es_salud(nombre):
    """True si el nombre del centro corresponde a un establecimiento de salud."""
    s = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode().lower()
    return bool(_SALUD_RE.search(s))


def conectar():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# Campos de la ficha informativa del punto (centro de acopio / hospital / refugio).
# El orden no importa aquí; el del bot lo define FICHA en bot_apoyo.py. El contacto
# se guarda aparte en la columna `telefono`.
FICHA_COLS = (
    "tipo_inmueble", "aforo", "banos", "duchas", "cocina", "agua_potable",
    "electricidad", "planta", "camas", "acceso_vehiculos", "accesibilidad",
    "almacenamiento", "refrigeracion", "ubicacion", "horario", "responsable",
    "rif", "cobertura",   # fundaciones: RIF (o "No posee") y zona de cobertura
    "areas",              # fundaciones: áreas de atención (animales, salud, niños…)
)

# Tipos de punto cuyo inventario es OFERTA (lo que TIENEN para dar). El resto
# (refugio/hospital/clínica/…) publica DEMANDA. Las fundaciones entran como
# oferta: cargan su inventario para servir a acopios y refugios.
TIPOS_OFERTA = ("acopio", "fundacion")
_SQL_OFERTA = "r.tipo_punto IN ('acopio', 'fundacion')"


def _asegurar_columna(con, columna, tipo, tabla="mensajes"):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({tabla})").fetchall()]
    if columna not in cols:
        con.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")


def init():
    con = conectar()
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mensajes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id           INTEGER UNIQUE,
            tg_fecha        TEXT,
            texto_original  TEXT,
            link            TEXT,
            relevante       INTEGER,
            categoria       TEXT,
            titulo          TEXT,
            ubicacion       TEXT,
            detalle         TEXT,
            necesidades     TEXT,
            contacto        TEXT,
            foto            TEXT,
            vigencia        TEXT,
            procesado_en    TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS centros (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            mensaje_tg_id  INTEGER,
            nombre         TEXT,
            ubicacion      TEXT,
            estado         TEXT,        -- disponible / lleno / desconocido
            necesidades    TEXT,
            tg_fecha       TEXT,
            link           TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS servicios (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            mensaje_tg_id  INTEGER,
            tipo           TEXT,        -- luz/agua/gasolina/internet/telefonia/gas/via/otro
            zona           TEXT,
            estado         TEXT,        -- ok / falla / parcial / desconocido
            detalle        TEXT,
            tg_fecha       TEXT,
            link           TEXT
        )
        """
    )
    _asegurar_columna(con, "foto", "TEXT")
    # Estado de personas desaparecidas: buscando / aparecio / fallecida
    _asegurar_columna(con, "estado_persona", "TEXT DEFAULT 'buscando'")
    # Autoregistro desde el canal: un punto DETECTADO en Emergencias Viales puede ser
    # reclamado por su responsable (web/bots) → queda vinculado al representante.
    _asegurar_columna(con, "reclamado_rep", "INTEGER", "centros")
    # Canal → pedidos: marca de revisión del pipeline canal_pedidos.py
    # (NULL = sin revisar · 0 = revisado sin convertir · >0 = llamado creado).
    _asegurar_columna(con, "pedido_lid", "INTEGER")
    # Contador de visitas (sin cookies, sin guardar IPs): vistas por día +
    # huellas saladas por día para contar únicos (irreversibles, se podan).
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS representantes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id  INTEGER,
            nombre       TEXT,
            tipo_punto   TEXT,     -- acopio / hospital / clinica / refugio / otro
            punto        TEXT,     -- nombre del punto
            telefono     TEXT,
            estado       TEXT DEFAULT 'pendiente',   -- pendiente / aprobado / rechazado
            creado_en    TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Ficha informativa del punto (para el directorio). Se agregan por migración
    # para que las DBs ya existentes en producción la adopten sin recrear la tabla.
    for _col in FICHA_COLS:
        _asegurar_columna(con, _col, "TEXT", "representantes")
    # Rol: 'representante' (dueño del punto, llena la ficha) o 'colaborador'
    # (se suma a un punto YA registrado para aportar requerimientos e inventario).
    _asegurar_columna(con, "rol", "TEXT DEFAULT 'representante'", "representantes")
    # Identidad ESTABLE del punto (separación a nivel de BD, no por el string del nombre):
    # el dueño es su propio punto (punto_id = su id); el colaborador apunta al del dueño.
    # Así dos puntos con el MISMO nombre quedan distintos aunque el texto coincida.
    _asegurar_columna(con, "punto_id", "INTEGER", "representantes")
    _backfill_punto_id(con)
    # Coordenadas GPS del punto (pin de Telegram). Sirven para conectar por cercanía:
    # un punto con demanda busca el centro de acopio más próximo que tenga lo que necesita.
    _asegurar_columna(con, "lat", "REAL", "representantes")
    _asegurar_columna(con, "lon", "REAL", "representantes")
    # Cédula de la persona que se afilia (verificación / anti-suplantación). PRIVADA:
    # nunca se publica en la web; solo queda en la DB, el panel master y el aviso al moderador.
    _asegurar_columna(con, "cedula", "TEXT", "representantes")
    # MANOS SOLIDARIAS: personas/emprendimientos que ofrecen un bien o servicio GRATIS
    # (comida, linternas, transporte…) sin ser un centro fijo. Tabla aparte y liviana
    # para no mezclar con la ficha de los puntos de la red.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS aliados (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id  INTEGER,
            nombre       TEXT,
            categoria    TEXT,     -- comida / herramientas / transporte / agua / carga / salud / hospedaje / otro
            descripcion  TEXT,
            zona         TEXT,
            contacto     TEXT,
            horario      TEXT,
            lat          REAL,
            lon          REAL,
            estado       TEXT DEFAULT 'pendiente',   -- pendiente / aprobado / rechazado
            creado_en    TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # VOLUNTARIADO: personas en terreno que levantan "llamados de apoyo" (sectores en
    # emergencia con poca cobertura). Tabla aparte y liviana, separada de aliados y
    # representantes. Su contacto es PRIVADO (nunca va a la web): solo se usa para que,
    # si el llamado pide insumos, un acopio que los tenga reciba el número por Telegram.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS voluntarios (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id  INTEGER,
            nombre       TEXT,
            zona         TEXT,
            contacto     TEXT,
            estado       TEXT DEFAULT 'pendiente',   -- pendiente / aprobado / rechazado
            creado_en    TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # LLAMADOS DE APOYO: solicitudes que publica un voluntario aprobado (foto + sector +
    # mensaje). Salen al instante en la web. La parte pública NUNCA incluye contacto:
    # el cruce con acopios y el aviso con teléfono privado viaja por Telegram.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS llamados (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            voluntario_id INTEGER,
            telegram_id  INTEGER,
            sector       TEXT,
            mensaje      TEXT,
            foto         TEXT,      -- ruta relativa en web/ (img/llamado_<id>.jpg) o NULL
            estado       TEXT DEFAULT 'activo',   -- voluntario: activo/oculto · pedido: pendiente/web/atencion/resuelto/descartado
            origen       TEXT,      -- 'voluntario' (llamado de apoyo) / 'pedido' (pedir ayuda)
            creado_en    TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Avisos de un llamado a un acopio / Manos Solidarias. Guarda el contexto (a quién, qué
    # calza, contacto privado del voluntario) para que el botón "Responder al voluntario"
    # arme los mensajes de WhatsApp aunque pase tiempo o el bot se reinicie.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS llamado_avisos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            llamado_id   INTEGER,
            destino_tgid INTEGER,   -- a quién se le envió el aviso (acopio / MS)
            tipo         TEXT,      -- 'acopio' / 'ms'
            quien        TEXT,      -- frase de quién responde (ej. "del centro de acopio X")
            items        TEXT,      -- lo que calza / lo pedido
            contacto     TEXT,      -- WhatsApp PRIVADO del voluntario
            sector       TEXT,
            creado_en    TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS capturas (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            representante_id  INTEGER,
            tipo_entrada     TEXT,     -- foto / audio / texto
            tipo_lectura     TEXT,     -- inventario / censo / personas / ...
            punto            TEXT,
            resumen          TEXT,
            estado           TEXT DEFAULT 'pendiente',   -- pendiente / confirmado
            creado_en        TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    con.execute("CREATE TABLE IF NOT EXISTS inventario (id INTEGER PRIMARY KEY AUTOINCREMENT, captura_id INTEGER, categoria TEXT, item TEXT, cantidad TEXT, detalle TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS censo_personas (id INTEGER PRIMARY KEY AUTOINCREMENT, captura_id INTEGER, nombre TEXT, cedula TEXT, edad TEXT, detalle TEXT)")
    # 'clase' marca explícito si la captura es OFERTA o DEMANDA (anula la deducción por tipo
    # de punto). NULL = se deduce del tipo (acopio→oferta, resto→demanda). La usa el Plan B de
    # /solicitar para publicar una NECESIDAD aunque el punto sea un acopio.
    _asegurar_columna(con, "clase", "TEXT", "capturas")
    # Fase 3: necesidades EN SEGUIMIENTO (se avisa al solicitante cuando aparezca cerca).
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS necesidades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id          INTEGER,
            punto_id        INTEGER,
            punto           TEXT,
            tipo_punto      TEXT,
            item            TEXT,
            item_norm       TEXT,
            estado          TEXT DEFAULT 'buscando',  -- buscando/disponible/coordinando/resuelta/cancelada
            match_punto_id  INTEGER,
            match_rep       INTEGER,
            match_punto     TEXT,
            creado_en       TEXT DEFAULT CURRENT_TIMESTAMP,
            actualizado_en  TEXT
        )
        """
    )
    # Fase 2: solicitudes de un punto con demanda a un centro de acopio (match por cercanía).
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS solicitudes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            de_rep       INTEGER,   -- representante que solicita (demanda)
            a_rep        INTEGER,   -- representante (dueño) del acopio
            a_punto_id   INTEGER,   -- punto_id del acopio solicitado
            item         TEXT,      -- insumo solicitado
            detalle      TEXT,      -- nota opcional
            estado       TEXT DEFAULT 'enviada',  -- enviada / aceptada / rechazada
            creado_en    TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    con.execute("CREATE TABLE IF NOT EXISTS visitas (dia TEXT PRIMARY KEY, vistas INTEGER NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE IF NOT EXISTS visita_fp (dia TEXT, fp TEXT, PRIMARY KEY(dia, fp))")
    # `compromisos` se creó por migración manual (no aquí): si existe, asegurar las
    # columnas de la reserva de stock (a qué renglón se le apartó y cuánto).
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='compromisos'").fetchone():
        _asegurar_columna(con, "stock_id", "INTEGER", "compromisos")
        _asegurar_columna(con, "reservada", "REAL", "compromisos")
    con.commit()
    con.close()


def ya_existe(tg_id):
    con = conectar()
    fila = con.execute("SELECT 1 FROM mensajes WHERE tg_id = ?", (tg_id,)).fetchone()
    con.close()
    return fila is not None


def guardar(registro):
    con = conectar()
    con.execute(
        """
        INSERT OR IGNORE INTO mensajes
        (tg_id, tg_fecha, texto_original, link, relevante, categoria, titulo,
         ubicacion, detalle, necesidades, contacto, foto, vigencia, procesado_en)
        VALUES
        (:tg_id, :tg_fecha, :texto_original, :link, :relevante, :categoria, :titulo,
         :ubicacion, :detalle, :necesidades, :contacto, :foto, :vigencia, :procesado_en)
        """,
        registro,
    )
    con.commit()
    con.close()


def guardar_centros(mensaje_tg_id, tg_fecha, link, centros):
    """Guarda los centros de acopio de un mensaje (uno por fila)."""
    con = conectar()
    con.execute("DELETE FROM centros WHERE mensaje_tg_id = ?", (mensaje_tg_id,))
    for c in centros:
        con.execute(
            """
            INSERT INTO centros (mensaje_tg_id, nombre, ubicacion, estado, necesidades, tg_fecha, link)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (mensaje_tg_id, c["nombre"], c.get("ubicacion"), c.get("estado"),
             c.get("necesidades"), tg_fecha, link),
        )
    con.commit()
    con.close()


def listar_relevantes():
    """Devuelve los mensajes útiles, más recientes primero."""
    con = conectar()
    filas = con.execute(
        """
        SELECT * FROM mensajes
        WHERE relevante = 1 AND categoria != 'descartar'
        ORDER BY tg_fecha DESC
        """
    ).fetchall()
    con.close()
    return [dict(f) for f in filas]


def resumen_centros(salud=False):
    """Un renglón por punto del canal. Por defecto (salud=False) devuelve solo CENTROS DE
    ACOPIO; con salud=True devuelve solo CENTROS DE ATENCIÓN (hospitales, clínicas, CDI,
    maternidades, etc.) — la misma tabla `centros`, partida por `_es_salud`.
    El estado lo decide el reporte CON ESTADO (disponible/lleno) MÁS RECIENTE de ese punto:
    la gente actualiza en vivo, así que el último aviso explícito manda (un 'ya no recibe /
    sin necesidades' lo marca 🔴 al instante). 'desconocido' solo si nadie aclaró nunca."""
    con = conectar()
    filas = con.execute(
        """
        SELECT nombre, ubicacion, estado, necesidades, tg_fecha, link
        FROM centros
        WHERE nombre IS NOT NULL AND TRIM(nombre) != ''
        ORDER BY tg_fecha DESC
        """
    ).fetchall()
    con.close()

    grupos = {}
    for f in filas:
        if _es_salud(f["nombre"]) != salud:
            continue  # acopio y atención van en pestañas separadas
        grupos.setdefault(f["nombre"].strip().lower(), []).append(dict(f))

    salida = []
    for ms in grupos.values():  # ms viene ordenado: más reciente primero
        reciente = ms[0]
        lleno = [m for m in ms if m["estado"] == "lleno"]
        # El reporte CON ESTADO más reciente manda (ms viene ordenado: más nuevo primero).
        # Los "desconocido" se ignoran para no borrar un estado claro anterior.
        con_estado = [m for m in ms if m["estado"] in ("disponible", "lleno")]
        estado = con_estado[0]["estado"] if con_estado else "desconocido"
        salida.append({
            "nombre": reciente["nombre"],
            "ubicacion": next((m["ubicacion"] for m in ms if m["ubicacion"]), None),
            "necesidades": next((m["necesidades"] for m in ms if m["necesidades"]), None),
            "estado": estado,
            "tg_fecha": reciente["tg_fecha"],
            "link": reciente["link"],
            "reportes_lleno": len(lleno),
        })

    salida.sort(key=lambda c: c["tg_fecha"] or "", reverse=True)  # más reciente primero
    return salida


def centros_sin_reclamar():
    """Puntos DETECTADOS por el canal (tabla `centros`) que aún NO tienen responsable
    en la red: sin reclamar (reclamado_rep IS NULL) y sin homónimo ya registrado.
    Para la web: sección «Detectados en el canal» con el botón de reclamo (autoregistro
    con los datos que el canal ya dio)."""
    con = conectar()
    filas = con.execute(
        """
        SELECT nombre, ubicacion, estado, tg_fecha
        FROM centros
        WHERE nombre IS NOT NULL AND TRIM(nombre) != '' AND reclamado_rep IS NULL
        ORDER BY tg_fecha DESC
        """).fetchall()
    registrados = {_norm(r["punto"]) for r in con.execute(
        "SELECT punto FROM representantes WHERE punto IS NOT NULL AND estado != 'rechazado'")}
    con.close()
    grupos = {}
    for f in filas:
        k = _norm(f["nombre"])
        if not k or k in registrados:
            continue
        g = grupos.setdefault(k, {"nombre": f["nombre"].strip(), "ubicacion": None,
                                  "estado": "desconocido", "tg_fecha": f["tg_fecha"],
                                  "reportes": 0, "salud": _es_salud(f["nombre"])})
        g["reportes"] += 1
        if not g["ubicacion"] and f["ubicacion"]:
            g["ubicacion"] = f["ubicacion"]
        if g["estado"] == "desconocido" and f["estado"] in ("disponible", "lleno"):
            g["estado"] = f["estado"]
    out = list(grupos.values())
    out.sort(key=lambda c: c["tg_fecha"] or "", reverse=True)
    return out


def reclamar_centros_canal(nombre, rid):
    """Vincula TODOS los reportes del canal de ese punto (por nombre normalizado)
    al representante que lo reclamó. Devuelve cuántos reportes quedaron vinculados."""
    con = conectar()
    filas = con.execute(
        "SELECT id, nombre FROM centros WHERE reclamado_rep IS NULL").fetchall()
    ids = [f["id"] for f in filas if _norm(f["nombre"] or "") == _norm(nombre or "")]
    n = 0
    if ids:
        marcas = ",".join("?" * len(ids))
        cur = con.execute(
            f"UPDATE centros SET reclamado_rep = ? WHERE id IN ({marcas})", [rid] + ids)
        n = cur.rowcount
        con.commit()
    con.close()
    return n


def guardar_servicios(mensaje_tg_id, tg_fecha, link, servicios):
    """Guarda el estado de servicios reportado por un mensaje (uno por fila)."""
    con = conectar()
    con.execute("DELETE FROM servicios WHERE mensaje_tg_id = ?", (mensaje_tg_id,))
    for s in servicios:
        con.execute(
            """
            INSERT INTO servicios (mensaje_tg_id, tipo, zona, estado, detalle, tg_fecha, link)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (mensaje_tg_id, s["tipo"], s.get("zona"), s.get("estado"),
             s.get("detalle"), tg_fecha, link),
        )
    con.commit()
    con.close()


def resumen_servicios():
    """Estado actual por (zona, tipo): manda el reporte CON ESTADO más reciente.
    Un 'desconocido' reciente no pisa a un dato conocido anterior."""
    con = conectar()
    filas = con.execute(
        """
        SELECT tipo, zona, estado, detalle, tg_fecha, link FROM servicios
        WHERE tipo IS NOT NULL AND TRIM(tipo) != '' ORDER BY tg_fecha DESC
        """
    ).fetchall()
    con.close()

    grupos = {}
    for f in filas:  # más reciente primero
        zona = (f["zona"] or "").strip() or "Sin zona indicada"
        key = (zona.lower(), f["tipo"])
        cur = grupos.get(key)
        if cur is None or (cur["estado"] == "desconocido" and f["estado"] != "desconocido"):
            grupos[key] = {"tipo": f["tipo"], "zona": zona, "estado": f["estado"],
                           "detalle": f["detalle"], "tg_fecha": f["tg_fecha"], "link": f["link"]}
    salida = list(grupos.values())
    salida.sort(key=lambda s: (s["zona"].lower(), s["tipo"]))
    return salida


def crear_representante(telegram_id, nombre, tipo_punto, punto, telefono, ficha=None, cedula=None):
    """Guarda una afiliación nueva (estado pendiente) con la ficha del punto y
    devuelve su id. `ficha` es un dict opcional con los campos de FICHA_COLS
    (cada campo puede traer 'No aplica' / 'No posee'). `cedula` = dato privado de la persona."""
    ficha = ficha or {}
    cols = ["telegram_id", "nombre", "cedula", "tipo_punto", "punto", "telefono"] + list(FICHA_COLS)
    vals = [telegram_id, nombre, cedula, tipo_punto, punto, telefono] + [ficha.get(c) for c in FICHA_COLS]
    marcadores = ", ".join("?" * len(cols))
    con = conectar()
    cur = con.execute(
        f"INSERT INTO representantes ({', '.join(cols)}) VALUES ({marcadores})", vals
    )
    rid = cur.lastrowid
    con.execute("UPDATE representantes SET punto_id = ? WHERE id = ?", (rid, rid))  # es su propio punto
    con.commit()
    con.close()
    return rid


def representante(rid):
    con = conectar()
    f = con.execute("SELECT * FROM representantes WHERE id = ?", (rid,)).fetchone()
    con.close()
    return dict(f) if f else None


def representante_por_tgid(telegram_id):
    """La afiliación más reciente de un telegram_id (o None)."""
    con = conectar()
    f = con.execute(
        "SELECT * FROM representantes WHERE telegram_id = ? ORDER BY id DESC LIMIT 1",
        (telegram_id,),
    ).fetchone()
    con.close()
    return dict(f) if f else None


def _norm(s):
    """Normaliza un nombre para comparar (sin acentos, minúsculas, sin espacios extra)."""
    s = unicodedata.normalize("NFKD", (s or "").strip()).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


# Sinónimos venezolanos/coloquiales para el matching de insumos: palabra (ya
# normalizada con _norm) → palabras extra que se le "pegan" al texto. Se aplica a
# AMBOS lados (lo que se pide y lo que hay), así cualquiera de las dos partes puede
# usar cualquiera de las dos formas. El match por prefijos ya cubre plural/singular
# y derivados (colchon↔colchoneta), aquí van solo los que NO comparten raíz.
SINONIMOS = {
    "perrarina": "comida perro alimento",
    "gatarina": "comida gato alimento",
    "mascotas": "perro gato animales", "mascota": "perro gato animales",
    "animales": "mascotas perro gato", "animal": "mascotas perro gato",
    "tetero": "biberon", "teteros": "biberones", "biberon": "tetero",
    "frazada": "cobija manta", "frazadas": "cobijas mantas",
    "cobija": "manta frazada", "cobijas": "mantas frazadas",
    "manta": "cobija frazada", "mantas": "cobijas frazadas",
    "inyectadora": "jeringa", "inyectadoras": "jeringas", "jeringa": "inyectadora",
    "medicina": "medicamento remedio", "medicinas": "medicamentos remedios",
    "medicamento": "medicina", "medicamentos": "medicinas", "remedio": "medicina",
    "curitas": "banditas adhesivas", "banditas": "curitas",
    "chupon": "chupete", "chupete": "chupon",
    "panitos": "toallitas humedas", "toallitas": "panitos humedos",
    "lentes": "gafas anteojos", "gafas": "lentes",
    "linterna": "lampara luz", "linternas": "lamparas",
    "fosforos": "cerillos", "cerillos": "fosforos",
    "aseo": "higiene", "higiene": "aseo",
    "cloro": "lejia desinfectante", "lejia": "cloro",
    "franela": "camiseta", "franelas": "camisetas", "camiseta": "franela",
}


def _con_sinonimos(texto_norm):
    """Devuelve el texto normalizado + los sinónimos de sus palabras (para matching)."""
    extra = [SINONIMOS[w] for w in texto_norm.split() if w in SINONIMOS]
    return texto_norm + (" " + " ".join(extra) if extra else "")


# Conectores sin significado para el match por palabras: exigirlos rompía consultas
# como «comida PARA perro» contra «Perrarina» (todas las palabras deben calzar).
_STOPWORDS = {"para", "con", "del", "los", "las", "por", "que", "una", "uno",
              "unas", "unos", "sin", "mas", "muy"}


def _backfill_punto_id(con):
    """Asigna punto_id a los registros que aún no lo tengan: el dueño es su propio punto;
    el colaborador hereda el del dueño homónimo (por nombre, como se vinculaban antes)."""
    filas = con.execute("SELECT id, punto, rol, punto_id FROM representantes").fetchall()
    if all(f["punto_id"] is not None for f in filas):
        return
    owners = {}
    for f in filas:
        if (f["rol"] or "representante") == "representante":
            owners.setdefault(_norm(f["punto"]), f["id"])
    for f in filas:
        if f["punto_id"] is not None:
            continue
        if (f["rol"] or "representante") == "representante":
            pid = f["id"]
        else:
            pid = owners.get(_norm(f["punto"]), f["id"])
        con.execute("UPDATE representantes SET punto_id = ? WHERE id = ?", (pid, f["id"]))


# Palabras genéricas: no distinguen un punto de otro (lo que distingue es el nombre
# propio). Se ignoran al comparar por tokens, para que "Pérez Carreño" y "Hospital
# Pérez Carreño" se reconozcan como el mismo punto.
_GENERICOS = {
    "hospital", "hospitalito", "clinica", "policlinico", "policlinica", "cdi",
    "ambulatorio", "maternidad", "centro", "punto", "casa", "iglesia", "colegio",
    "unidad", "escuela", "universidad", "plaza", "parque", "refugio", "albergue",
    "acopio", "de", "del", "la", "el", "los", "las", "y", "san", "santa", "santo",
    "dr", "dra", "doctor", "doctora",
}


def _tokens_sig(s):
    """Tokens significativos de un nombre (sin genéricos ni palabras de 1-2 letras)."""
    return {t for t in _norm(s).split() if len(t) >= 3 and t not in _GENERICOS}


def _similar(a, b):
    """Puntaje 0..1 de qué tan parecidos son dos nombres de punto (match aproximado).
    El match solo dispara una confirmación 'Sí/No' al usuario, así que conviene que
    sea generoso: si se equivoca, el colaborador responde que no."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    corto, largo = sorted((na, nb), key=len)
    if len(corto) >= 5 and corto in largo:      # uno contenido en el otro
        return 0.95
    ta, tb = _tokens_sig(a), _tokens_sig(b)
    if ta and tb:
        inter = ta & tb
        if inter and (ta <= tb or tb <= ta):    # mismos nombres propios (subconjunto)
            return 0.9
        if inter and len(inter) / len(ta | tb) >= 0.6:
            return 0.85
    return difflib.SequenceMatcher(None, na, nb).ratio()


def buscar_punto(nombre, umbral=0.85):
    """Busca un punto YA registrado por su nombre, con match APROXIMADO (insensible a
    mayúsculas/acentos, tolera prefijos genéricos y pequeñas diferencias). Devuelve el
    representante del punto más parecido por encima del umbral (prefiere al dueño), o None.
    Cuenta como censado desde que alguien introdujo los datos (pendiente o aprobado;
    no rechazado)."""
    if not _norm(nombre):
        return None
    con = conectar()
    filas = con.execute(
        "SELECT * FROM representantes WHERE estado != 'rechazado' "
        "AND punto IS NOT NULL AND TRIM(punto) != '' ORDER BY id DESC"
    ).fetchall()
    con.close()
    mejor, mejor_peso = None, 0.0
    for f in filas:
        s = _similar(nombre, f["punto"])
        if s < umbral:
            continue
        es_dueno = (dict(f).get("rol") or "representante") == "representante"
        peso = s + (0.01 if es_dueno else 0.0)   # a igualdad, gana el dueño
        if peso > mejor_peso:
            mejor, mejor_peso = dict(f), peso
    return mejor


def cedula_otro_nombre(cedula, nombre, excluir_tgid=None):
    """Anti-suplantación: si la cédula YA está registrada (no rechazada) bajo un nombre
    claramente DISTINTO al dado (comparación aproximada), devuelve ese otro nombre; si no,
    None. Excluye al propio telegram_id (la misma persona puede reaparecer)."""
    if not cedula:
        return None
    con = conectar()
    filas = con.execute(
        "SELECT telegram_id, nombre FROM representantes "
        "WHERE cedula = ? AND estado != 'rechazado' ORDER BY id",
        (cedula,),
    ).fetchall()
    con.close()
    for f in filas:
        if excluir_tgid is not None and str(f["telegram_id"]) == str(excluir_tgid):
            continue
        if _similar(f["nombre"], nombre) < 0.7:   # nombre claramente distinto → sospechoso
            return f["nombre"]
    return None


def existe_punto_exacto(nombre, excluir_id=None):
    """True si ya hay un punto registrado con EXACTAMENTE ese nombre (normalizado,
    sin rechazados). Sirve para desambiguar homónimos: si choca, se le agrega el sector."""
    objetivo = _norm(nombre)
    if not objetivo:
        return False
    con = conectar()
    filas = con.execute(
        "SELECT id, punto FROM representantes WHERE estado != 'rechazado' "
        "AND punto IS NOT NULL AND TRIM(punto) != ''"
    ).fetchall()
    con.close()
    return any(f["id"] != excluir_id and _norm(f["punto"]) == objetivo for f in filas)


def crear_colaborador(telegram_id, nombre, dueno, cedula=None):
    """Suma un colaborador a un punto ya registrado: hereda el nombre EXACTO, el tipo y el
    punto_id del dueño (las capturas agrupan por punto_id en la web), sin ficha (el punto ya
    tiene la suya). `cedula` = dato privado de la persona. Estado pendiente. Devuelve su id."""
    pid = dueno.get("punto_id") or dueno.get("id")
    con = conectar()
    cur = con.execute(
        "INSERT INTO representantes (telegram_id, nombre, cedula, tipo_punto, punto, rol, punto_id) "
        "VALUES (?, ?, ?, ?, ?, 'colaborador', ?)",
        (telegram_id, nombre, cedula, dueno.get("tipo_punto"), dueno.get("punto"), pid),
    )
    rid = cur.lastrowid
    if pid is None:   # salvaguarda: sin dueño claro, es su propio punto
        con.execute("UPDATE representantes SET punto_id = ? WHERE id = ?", (rid, rid))
    con.commit()
    con.close()
    return rid


def set_estado_representante(rid, estado):
    con = conectar()
    con.execute("UPDATE representantes SET estado = ? WHERE id = ?", (estado, rid))
    con.commit()
    con.close()


def guardar_gps(rid, lat, lon):
    """Guarda las coordenadas (pin de Telegram) del punto de un representante.
    Se replica al punto_id para que el dueño y sus colaboradores compartan ubicación."""
    if lat is None or lon is None:
        return
    con = conectar()
    con.execute("UPDATE representantes SET lat = ?, lon = ? WHERE id = ?", (lat, lon, rid))
    fila = con.execute("SELECT punto_id FROM representantes WHERE id = ?", (rid,)).fetchone()
    if fila and fila["punto_id"] is not None:
        con.execute("UPDATE representantes SET lat = ?, lon = ? WHERE punto_id = ?",
                    (lat, lon, fila["punto_id"]))
    con.commit()
    con.close()


def actualizar_ficha_representante(rid, ficha):
    """Actualiza solo la ficha de un representante ya existente (comando /ficha).
    No toca nombre/tipo/punto/estado. El contacto va a la columna `telefono`."""
    ficha = ficha or {}
    sets = ", ".join(f"{c} = ?" for c in FICHA_COLS)
    vals = [ficha.get(c) for c in FICHA_COLS]
    if ficha.get("contacto"):
        sets += ", telefono = ?"
        vals.append(ficha["contacto"])
    vals.append(rid)
    con = conectar()
    con.execute(f"UPDATE representantes SET {sets} WHERE id = ?", vals)
    con.commit()
    con.close()


def crear_captura(representante_id, tipo_entrada, tipo_lectura, punto, resumen, items, personas, clase=None):
    """Guarda una captura (estado pendiente) con sus ítems y personas. Devuelve su id.
    `clase` opcional ('oferta'/'demanda') fuerza el lado del match (si no, se deduce del tipo)."""
    con = conectar()
    cur = con.execute(
        "INSERT INTO capturas (representante_id, tipo_entrada, tipo_lectura, punto, resumen, clase) VALUES (?,?,?,?,?,?)",
        (representante_id, tipo_entrada, tipo_lectura, punto, resumen, clase),
    )
    cid = cur.lastrowid
    for it in items:
        con.execute("INSERT INTO inventario (captura_id, categoria, item, cantidad, detalle) VALUES (?,?,?,?,?)",
                    (cid, it.get("categoria"), it.get("item"), it.get("cantidad"), it.get("detalle")))
    for p in personas:
        con.execute("INSERT INTO censo_personas (captura_id, nombre, cedula, edad, detalle) VALUES (?,?,?,?,?)",
                    (cid, p.get("nombre"), p.get("cedula"), p.get("edad"), p.get("detalle")))
    con.commit()
    con.close()
    return cid


def captura(cid):
    con = conectar()
    f = con.execute("SELECT * FROM capturas WHERE id = ?", (cid,)).fetchone()
    con.close()
    return dict(f) if f else None


def confirmar_captura(cid):
    con = conectar()
    con.execute("UPDATE capturas SET estado = 'confirmado' WHERE id = ?", (cid,))
    con.commit()
    con.close()


def descartar_captura(cid):
    con = conectar()
    con.execute("DELETE FROM inventario WHERE captura_id = ?", (cid,))
    con.execute("DELETE FROM censo_personas WHERE captura_id = ?", (cid,))
    con.execute("DELETE FROM capturas WHERE id = ?", (cid,))
    con.commit()
    con.close()


# --- Edición puntual de una captura pendiente (corregir un dato antes de guardar) ---
_COLS_ITEM = {"categoria", "item", "cantidad", "detalle"}
_COLS_PERSONA = {"nombre", "cedula", "edad", "detalle"}


def captura_detalle(cid):
    """Captura + sus insumos y personas, cada renglón con su id, para editar dato por dato."""
    con = conectar()
    cap = con.execute("SELECT * FROM capturas WHERE id = ?", (cid,)).fetchone()
    if not cap:
        con.close()
        return None
    items = con.execute("SELECT * FROM inventario WHERE captura_id = ? ORDER BY id", (cid,)).fetchall()
    personas = con.execute("SELECT * FROM censo_personas WHERE captura_id = ? ORDER BY id", (cid,)).fetchall()
    con.close()
    return {"captura": dict(cap), "items": [dict(x) for x in items],
            "personas": [dict(x) for x in personas]}


def actualizar_item_captura(row_id, campo, valor):
    """Corrige UN campo de un renglón de inventario (columna en lista blanca)."""
    if campo not in _COLS_ITEM:
        return
    con = conectar()
    con.execute(f"UPDATE inventario SET {campo} = ? WHERE id = ?", (valor, row_id))
    con.commit()
    con.close()


def actualizar_persona_captura(row_id, campo, valor):
    """Corrige UN campo de un renglón de censo (columna en lista blanca)."""
    if campo not in _COLS_PERSONA:
        return
    con = conectar()
    con.execute(f"UPDATE censo_personas SET {campo} = ? WHERE id = ?", (valor, row_id))
    con.commit()
    con.close()


def borrar_item_captura(row_id):
    con = conectar()
    con.execute("DELETE FROM inventario WHERE id = ?", (row_id,))
    con.commit()
    con.close()


def borrar_persona_captura(row_id):
    con = conectar()
    con.execute("DELETE FROM censo_personas WHERE id = ?", (row_id,))
    con.commit()
    con.close()


def _inventario_por_punto(horas, acopio):
    """Núcleo de resumen_oferta / resumen_necesidades, de capturas CONFIRMADAS recientes.
    Agrupa por punto_id (identidad estable; cae al nombre si es un registro viejo sin id).
    Dedup por (punto, ítem), el más reciente gana. NUNCA nombres ni cédulas.
      · acopio=True  → SOLO centros de acopio: su inventario = lo que TIENEN (OFERTA),
                       sin censo de personas.
      · acopio=False → el resto de puntos (hospital/clínica/refugio/otro): lo que
                       NECESITAN (DEMANDA), con el conteo de personas del censo."""
    desde = "-{} hours".format(int(horas))
    # OFERTA vs DEMANDA: manda `c.clase` si está; si es NULL, se deduce del tipo de punto
    # (acopio/fundación→oferta, resto→demanda). Así un acopio puede publicar una NECESIDAD (clase=demanda).
    filtro = (f"(c.clase = 'oferta' OR (c.clase IS NULL AND {_SQL_OFERTA}))" if acopio
              else f"(c.clase = 'demanda' OR (c.clase IS NULL AND (r.tipo_punto IS NULL OR NOT {_SQL_OFERTA})))")
    con = conectar()
    rows = con.execute(
        f"""
        SELECT r.punto, r.punto_id, r.tipo_punto, i.categoria, i.item, i.cantidad, i.detalle, c.creado_en
        FROM inventario i JOIN capturas c ON c.id = i.captura_id
        JOIN representantes r ON r.id = c.representante_id
        WHERE c.estado = 'confirmado' AND r.punto IS NOT NULL AND TRIM(r.punto) != ''
          AND {filtro}
          AND c.creado_en >= datetime('now', ?)
        ORDER BY c.creado_en DESC
        """,
        (desde,),
    ).fetchall()
    # Conteo de personas = el CENSO MÁS RECIENTE por punto (no la suma de capturas).
    # Solo aplica al lado demanda (refugios/hospitales); un acopio no censa personas.
    cen = []
    if not acopio:
        cen = con.execute(
            f"""
            SELECT r.punto, r.punto_id, COUNT(*) AS n, c.creado_en
            FROM censo_personas p JOIN capturas c ON c.id = p.captura_id
            JOIN representantes r ON r.id = c.representante_id
            WHERE c.estado = 'confirmado' AND r.punto IS NOT NULL AND TRIM(r.punto) != ''
              AND {filtro}
              AND c.creado_en >= datetime('now', ?)
            GROUP BY c.id ORDER BY c.creado_en DESC
            """,
            (desde,),
        ).fetchall()
    con.close()

    def _iso(s):
        return (s or "").replace(" ", "T") + "Z"

    def _pk(row):  # clave de agrupación = punto_id (estable); cae al nombre si es viejo sin id
        return row["punto_id"] if row["punto_id"] is not None else "n:" + _norm(row["punto"])

    puntos, vistos = {}, set()
    for row in rows:
        pk = _pk(row)
        g = puntos.setdefault(pk, {"id": row["punto_id"], "punto": (row["punto"] or "").strip(),
                                   "tipo_punto": row["tipo_punto"],
                                   "items": [], "personas": 0, "actualizado": _iso(row["creado_en"])})
        clave = (pk, (row["item"] or "").strip().lower())
        if not clave[1] or clave in vistos:
            continue
        vistos.add(clave)
        g["items"].append({"categoria": row["categoria"], "item": row["item"],
                           "cantidad": row["cantidad"], "detalle": row["detalle"]})
    cen_listo = set()
    for row in cen:  # más reciente primero → el primero por punto es el censo vigente
        pk = _pk(row)
        if pk in cen_listo:
            continue
        cen_listo.add(pk)
        g = puntos.setdefault(pk, {"id": row["punto_id"], "punto": (row["punto"] or "").strip(),
                                   "tipo_punto": None,
                                   "items": [], "personas": 0, "actualizado": _iso(row["creado_en"])})
        g["personas"] = row["n"]
        if _iso(row["creado_en"]) > g["actualizado"]:
            g["actualizado"] = _iso(row["creado_en"])
    salida = list(puntos.values())
    salida.sort(key=lambda x: x["actualizado"] or "", reverse=True)  # más reciente primero
    return salida


def resumen_necesidades(horas=72):
    """Web PÚBLICA — lado DEMANDA: qué NECESITA cada punto que no es acopio
    (hospital/clínica/refugio/otro). Insumos + conteo de personas (sin datos personales)."""
    return _inventario_por_punto(horas, acopio=False)


def resumen_oferta(horas=72):
    """Web PÚBLICA — lado OFERTA: qué TIENE disponible cada CENTRO DE ACOPIO (su inventario).
    Solo insumos (sin censo). Es lo que un punto con demanda puede solicitar."""
    return _inventario_por_punto(horas, acopio=True)


# --- Fase 2: match oferta↔demanda por cercanía ---
def dueno_de_punto(punto_id):
    """Representante a quien notificar por un punto: el DUEÑO (rol representante) aprobado;
    si no hay, cualquier rep aprobado de ese punto_id. Trae lat/lon/teléfono/telegram_id."""
    if punto_id is None:
        return None
    con = conectar()
    f = con.execute(
        "SELECT * FROM representantes WHERE punto_id = ? AND estado = 'aprobado' "
        "ORDER BY (rol = 'representante') DESC, id DESC LIMIT 1",
        (punto_id,),
    ).fetchone()
    con.close()
    return dict(f) if f else None


def buscar_oferta(consulta, horas=72):
    """Centros de acopio APROBADOS con inventario CONFIRMADO reciente que coincide con
    `consulta` (match aproximado por substring normalizado). Agrupa por punto_id. Devuelve por
    punto: punto, punto_id, contacto (teléfono), lat, lon, dueno_tgid (a quién notificar),
    dueno_rep_id, e items que coinciden. NO calcula distancia (la pone el bot con las coords
    del solicitante)."""
    objetivo = _norm(consulta)
    if len(objetivo) < 2:
        return []
    desde = "-{} hours".format(int(horas))
    con = conectar()
    # Frescura por tipo: el stock de una fundación rota más lento que el de un
    # acopio → su ventana es de 7 días (si no, desaparecen del match al 3er día).
    rows = con.execute(
        f"""
        SELECT r.punto_id, i.item, i.categoria, i.cantidad, i.detalle, c.creado_en
        FROM inventario i JOIN capturas c ON c.id = i.captura_id
        JOIN representantes r ON r.id = c.representante_id
        WHERE c.estado = 'confirmado' AND r.estado = 'aprobado'
          AND (c.clase = 'oferta' OR (c.clase IS NULL AND {_SQL_OFERTA}))
          AND r.punto_id IS NOT NULL
          AND c.creado_en >= datetime('now', CASE WHEN r.tipo_punto = 'fundacion'
                                                  THEN '-168 hours' ELSE ? END)
        ORDER BY c.creado_en DESC
        """,
        (desde,),
    ).fetchall()
    con.close()

    # Match aproximado: por nombre del ítem, o por todas las palabras (≥3 letras) de la consulta
    # dentro de ítem+categoría+detalle → así "ropa de hombre" encuentra "Camisas (ropa, hombre)".
    # El match por palabra usa prefijos en ambos sentidos → tolera plural/singular (niños↔niño).
    # Sinónimos en ambos lados: «perrarina» pedida encuentra «comida para perro»
    # ofrecida, y al revés. La consulta se prueba en VARIANTES (la original y una por
    # cada sinónimo sustituido) porque el all() exige que calcen todas las palabras;
    # al inventario en cambio los sinónimos se le SUMAN (más palabras donde calzar).
    def _toks(s):
        return [t for t in s.split() if len(t) >= 3 and t not in _STOPWORDS]

    variantes = [_toks(objetivo)]
    for w in objetivo.split():
        if w in SINONIMOS:
            variantes.append(_toks(objetivo.replace(w, SINONIMOS[w])))
    grupos, vistos = {}, set()
    for row in rows:
        it = _norm(row["item"])
        hay = re.sub(r"[^a-z0-9 ]", " ", _con_sinonimos(_norm(
            (row["item"] or "") + " " + (row["categoria"] or "") + " " + (row["detalle"] or ""))))
        hwords = [w for w in hay.split() if len(w) >= 3]
        ok_tok = any(
            toks and all(any(w.startswith(t) or t.startswith(w) for w in hwords) for t in toks)
            for toks in variantes)
        ok = (it and (objetivo in it or it in objetivo)) or (objetivo in hay) or ok_tok
        if not it or not ok:
            continue
        pid = row["punto_id"]
        clave = (pid, it)
        if clave in vistos:
            continue
        vistos.add(clave)
        grupos.setdefault(pid, []).append(
            {"item": row["item"], "cantidad": row["cantidad"], "detalle": row["detalle"]})

    salida = []
    for pid, items in grupos.items():
        d = dueno_de_punto(pid)
        if not d:
            continue
        salida.append({
            "punto_id": pid, "punto": d.get("punto"), "contacto": d.get("telefono"),
            "lat": d.get("lat"), "lon": d.get("lon"), "dueno_tgid": d.get("telegram_id"),
            "dueno_rep_id": d.get("id"), "items": items,
        })
    return salida


def crear_solicitud(de_rep, a_rep, a_punto_id, item, detalle=None):
    con = conectar()
    cur = con.execute(
        "INSERT INTO solicitudes (de_rep, a_rep, a_punto_id, item, detalle) VALUES (?,?,?,?,?)",
        (de_rep, a_rep, a_punto_id, item, detalle),
    )
    sid = cur.lastrowid
    con.commit()
    con.close()
    return sid


def solicitud(sid):
    con = conectar()
    f = con.execute("SELECT * FROM solicitudes WHERE id = ?", (sid,)).fetchone()
    con.close()
    return dict(f) if f else None


def set_estado_solicitud(sid, estado):
    con = conectar()
    con.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (estado, sid))
    con.commit()
    con.close()


def publicar_necesidad(rep_id, punto, items):
    """Plan B: publica una NECESIDAD (clase='demanda', confirmada) del punto, para que aparezca
    en la pestaña web 'Requerimientos' aunque el punto sea un acopio. `items` = lista de nombres."""
    filas = [{"categoria": "solicitud", "item": it, "cantidad": None, "detalle": None}
             for it in items if (it or "").strip()]
    if not filas:
        return None
    cid = crear_captura(rep_id, "solicitud", "necesidad", punto,
                        "necesidad sin acopio cercano", filas, [], clase="demanda")
    confirmar_captura(cid)
    return cid


# --- Fase 3: seguimiento de necesidades (avisar cuando aparezca cerca) ---
_NEC_ABIERTAS = ("buscando", "disponible", "coordinando")


def abrir_necesidad(rep_id, punto_id, punto, tipo_punto, item,
                    meta=None, unidad=None, categoria=None, urgencia=None):
    """Registra/actualiza una necesidad EN SEGUIMIENTO para (punto, ítem). Devuelve id o None.
    Si ya hay una abierta, actualiza meta/unidad/categoria/urgencia cuando se pasan (no duplica)."""
    inorm = _norm(item)
    if not inorm:
        return None
    con = conectar()
    marc = ",".join("?" * len(_NEC_ABIERTAS))
    ya = con.execute(
        f"SELECT id FROM necesidades WHERE punto_id IS ? AND item_norm = ? AND estado IN ({marc})",
        (punto_id, inorm, *_NEC_ABIERTAS),
    ).fetchone()
    if ya:
        nid = ya["id"]
        sets, vals = [], []
        for col, v in (("meta", meta), ("unidad", unidad), ("categoria", categoria), ("urgencia", urgencia)):
            if v is not None:
                sets.append(f"{col}=?")
                vals.append(v)
        if sets:
            vals.append(nid)
            con.execute(f"UPDATE necesidades SET {', '.join(sets)}, actualizado_en=datetime('now') WHERE id=?", vals)
            con.commit()
        con.close()
        return nid
    cur = con.execute(
        "INSERT INTO necesidades (rep_id, punto_id, punto, tipo_punto, item, item_norm, "
        "meta, unidad, categoria, urgencia, actualizado_en) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))",
        (rep_id, punto_id, punto, tipo_punto, item, inorm, meta, unidad, categoria, urgencia or "media"),
    )
    nid = cur.lastrowid
    con.commit()
    con.close()
    return nid


def necesidades_abiertas():
    """Necesidades en estado 'buscando' (candidatas a rematch automático)."""
    con = conectar()
    filas = con.execute("SELECT * FROM necesidades WHERE estado = 'buscando' ORDER BY id").fetchall()
    con.close()
    return [dict(f) for f in filas]


def necesidad(nid):
    con = conectar()
    f = con.execute("SELECT * FROM necesidades WHERE id = ?", (nid,)).fetchone()
    con.close()
    return dict(f) if f else None


def set_estado_necesidad(nid, estado, match_punto_id=None, match_rep=None, match_punto=None):
    con = conectar()
    con.execute(
        "UPDATE necesidades SET estado=?, match_punto_id=COALESCE(?, match_punto_id), "
        "match_rep=COALESCE(?, match_rep), match_punto=COALESCE(?, match_punto), "
        "actualizado_en=datetime('now') WHERE id=?",
        (estado, match_punto_id, match_rep, match_punto, nid),
    )
    con.commit()
    con.close()


def necesidades_web():
    """Para la web PÚBLICA: necesidades EN SEGUIMIENTO (abiertas) agrupadas por punto, con estado
    (buscando / disponible / coordinando) para el badge. NUNCA datos personales."""
    con = conectar()
    filas = con.execute(
        "SELECT * FROM necesidades WHERE estado IN ('buscando','disponible','coordinando') "
        "ORDER BY actualizado_en DESC, id DESC"
    ).fetchall()
    con.close()
    puntos = {}
    for f in filas:
        pk = f["punto_id"] if f["punto_id"] is not None else "n:" + _norm(f["punto"])
        g = puntos.setdefault(pk, {"punto": (f["punto"] or "").strip(), "punto_id": f["punto_id"],
                                   "tipo_punto": f["tipo_punto"], "items": [],
                                   "actualizado": (f["actualizado_en"] or "").replace(" ", "T") + "Z"})
        g["items"].append({
            "id": f["id"], "item": f["item"], "estado": f["estado"], "match_punto": f["match_punto"],
            "categoria": f["categoria"], "urgencia": f["urgencia"] or "media",
            "meta": f["meta"], "unidad": f["unidad"],
            "recibido": f["recibido"] or 0, "en_camino": en_camino_de(f["id"]),
        })
    salida = list(puntos.values())
    salida.sort(key=lambda x: x["actualizado"] or "", reverse=True)
    return salida


def resumen_directorio():
    """Para la web PÚBLICA: ficha de cada punto a partir de los representantes
    APROBADOS. Solo datos del lugar (NUNCA datos personales sensibles). El
    'contacto' es el teléfono del punto, que el responsable aceptó publicar."""
    con = conectar()
    rows = con.execute(
        """
        SELECT id, punto_id, punto, tipo_punto, telefono AS contacto, responsable, creado_en,
               tipo_inmueble, aforo, banos, duchas, cocina, agua_potable,
               electricidad, planta, camas, acceso_vehiculos, accesibilidad,
               almacenamiento, refrigeracion, ubicacion, horario, cobertura, areas, lat, lon
        FROM representantes
        WHERE estado = 'aprobado' AND punto IS NOT NULL AND TRIM(punto) != ''
        ORDER BY id DESC
        """
    ).fetchall()
    con.close()
    # Solo aparecen en el directorio los puntos con ficha completada (evita mostrar
    # afiliaciones viejas/incompletas; se completan con el comando /ficha del bot).
    # Se agrupa por punto_id (identidad estable): dos puntos con el mismo nombre pero
    # distinta ubicación cuentan como puntos separados. Gana el registro con ficha.
    vistos, salida = set(), []
    for r in rows:
        clave = r["punto_id"] if r["punto_id"] is not None else "n:" + _norm(r["punto"])
        if clave in vistos:
            continue
        # r.keys(): el SELECT de arriba no trae TODAS las FICHA_COLS (el rif se queda
        # interno, para la aprobación) → contar solo las columnas presentes.
        llenos = sum(1 for c in FICHA_COLS if c in r.keys() and (r[c] or "").strip())
        if llenos < 3:            # ficha sin completar → no se publica todavía
            continue
        vistos.add(clave)
        salida.append(dict(r))
    salida.sort(key=lambda x: (x["punto"] or "").lower())
    return salida


def resumen_situacion():
    """Situacion consolidada de refugios: censo agregado por refugio y serie historica.
    NUNCA expone nombres, cedulas, telefonos ni datos personales."""
    import json
    import datetime
    from zoneinfo import ZoneInfo

    def _num(v):
        m = re.search(r"\d+", str(v or ""))
        return int(m.group(0)) if m else None

    def _ent(v):
        return int(v or 0)

    def _norm_local(v):
        return _norm(v)

    def _puntaje_ficha(r):
        return sum(1 for c in FICHA_COLS if c in r.keys() and (r[c] or "").strip())

    def _servicios(v):
        if not v:
            return None
        try:
            data = json.loads(v)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, list):
            return None
        salida = []
        for item in data:
            if not isinstance(item, list) or len(item) < 2:
                continue
            nombre = "" if item[0] is None else str(item[0])
            activo = bool(item[1])
            detalle = item[2] if len(item) > 2 else None
            salida.append([nombre, activo, detalle])
        return salida

    hoy = datetime.datetime.now(ZoneInfo("America/Caracas")).strftime("%Y-%m-%d")
    con = conectar()
    reps = con.execute(
        """
        SELECT id, punto_id, punto, tipo_punto, estado, aforo, camas, ubicacion, lat, lon,
               tipo_inmueble, banos, duchas, cocina, agua_potable, electricidad, planta,
               acceso_vehiculos, accesibilidad, almacenamiento, refrigeracion, horario,
               responsable, rif, cobertura, areas
        FROM representantes
        WHERE estado = 'aprobado'
          AND tipo_punto = 'refugio'
          AND punto IS NOT NULL
          AND TRIM(punto) != ''
        ORDER BY id DESC
        """
    ).fetchall()
    ultimos = con.execute(
        """
        SELECT rs.*
        FROM reportes_situacionales rs
        JOIN (
            SELECT punto_id, MAX(fecha) AS fecha
            FROM reportes_situacionales
            GROUP BY punto_id
        ) u ON u.punto_id = rs.punto_id AND u.fecha = rs.fecha
        """
    ).fetchall()
    serie_rows = con.execute(
        """
        SELECT fecha, SUM(total_personas) AS personas, SUM(familias) AS familias,
               COUNT(*) AS refugios
        FROM reportes_situacionales
        GROUP BY fecha
        ORDER BY fecha DESC
        LIMIT 60
        """
    ).fetchall()
    con.close()

    mejores = {}
    for r in reps:
        clave = r["punto_id"] if r["punto_id"] is not None else "n:" + _norm_local(r["punto"])
        actual = mejores.get(clave)
        if actual is None or _puntaje_ficha(r) > _puntaje_ficha(actual):
            mejores[clave] = r

    ult_por_punto = {r["punto_id"]: r for r in ultimos}
    refugios = []
    personas_global = 0
    familias_global = 0
    nna_global = 0
    lactantes_global = 0
    discapacidad_global = 0
    embarazadas_global = 0
    capacidad_global = 0
    tiene_capacidad = False
    reportando = 0
    reportando_hoy = 0

    for r in mejores.values():
        punto_id = r["punto_id"]
        snap = ult_por_punto.get(punto_id) if punto_id is not None else None
        cap_max_ficha = _num(r["aforo"])
        cap_inst_ficha = _num(r["camas"])
        if cap_inst_ficha is None:
            cap_inst_ficha = cap_max_ficha

        if snap:
            reportando += 1
            if snap["fecha"] == hoy:
                reportando_hoy += 1
            personas = _ent(snap["total_personas"])
            familias = _ent(snap["familias"])
            nna = (_ent(snap["adol_m"]) + _ent(snap["adol_f"]) +
                   _ent(snap["nino_m"]) + _ent(snap["nino_f"]) +
                   _ent(snap["lact_m"]) + _ent(snap["lact_f"]))
            lactantes = _ent(snap["lact_m"]) + _ent(snap["lact_f"])
            discapacidad = _ent(snap["discapacidad"])
            embarazadas = _ent(snap["embarazadas"])
            cap_max = snap["cap_max"] if snap["cap_max"] is not None else cap_max_ficha
            cap_inst = snap["cap_inst"] if snap["cap_inst"] is not None else cap_inst_ficha
            disponibles = cap_inst - personas if cap_inst is not None else None
            ocupacion_pct = round(100 * personas / cap_inst) if cap_inst else None
            demografia = {
                "am_m": _ent(snap["am_m"]), "am_f": _ent(snap["am_f"]),
                "ad_m": _ent(snap["ad_m"]), "ad_f": _ent(snap["ad_f"]),
                "adol_m": _ent(snap["adol_m"]), "adol_f": _ent(snap["adol_f"]),
                "nino_m": _ent(snap["nino_m"]), "nino_f": _ent(snap["nino_f"]),
                "lact_m": _ent(snap["lact_m"]), "lact_f": _ent(snap["lact_f"]),
            }
            estado_reporte = "hoy" if snap["fecha"] == hoy else "atrasado"
            ultima_fecha = snap["fecha"]
            ultima_hora = snap["hora"]
            servicios = _servicios(snap["servicios_json"])

            personas_global += personas
            familias_global += familias
            nna_global += nna
            lactantes_global += lactantes
            discapacidad_global += discapacidad
            embarazadas_global += embarazadas
        else:
            personas = familias = nna = discapacidad = embarazadas = None
            cap_max = cap_max_ficha
            cap_inst = cap_inst_ficha
            disponibles = ocupacion_pct = None
            demografia = None
            estado_reporte = "sin_censo"
            ultima_fecha = ultima_hora = None
            servicios = None

        if cap_inst is not None:
            capacidad_global += cap_inst
            tiene_capacidad = True

        refugios.append({
            "punto_id": punto_id,
            "nombre": r["punto"],
            "ubicacion": r["ubicacion"],
            "lat": r["lat"],
            "lon": r["lon"],
            "estado_reporte": estado_reporte,
            "ultima_fecha": ultima_fecha,
            "ultima_hora": ultima_hora,
            "personas": personas,
            "familias": familias,
            "nna": nna,
            "discapacidad": discapacidad,
            "embarazadas": embarazadas,
            "cap_max": cap_max,
            "cap_inst": cap_inst,
            "disponibles": disponibles,
            "ocupacion_pct": ocupacion_pct,
            "demografia": demografia,
            "servicios": servicios,
        })

    refugios.sort(key=lambda x: (-(x["personas"] or 0), (x["nombre"] or "").lower()))
    serie = [
        {
            "fecha": r["fecha"],
            "personas": _ent(r["personas"]),
            "familias": _ent(r["familias"]),
            "refugios": _ent(r["refugios"]),
        }
        for r in reversed(serie_rows)
    ]
    disponibles_global = capacidad_global - personas_global if tiene_capacidad else None
    return {
        "global": {
            "personas": personas_global,
            "familias": familias_global,
            "nna": nna_global,
            "lactantes": lactantes_global,
            "discapacidad": discapacidad_global,
            "embarazadas": embarazadas_global,
            "capacidad_instalada": capacidad_global if tiene_capacidad else None,
            "disponibles": disponibles_global,
            "refugios_total": len(refugios),
            "refugios_reportando": reportando,
            "refugios_hoy": reportando_hoy,
        },
        "refugios": refugios,
        "serie": serie,
    }


# ---------------------------------------------------------------------------
# MANOS SOLIDARIAS (aliados): ayuda gratuita de la comunidad.
# ---------------------------------------------------------------------------
ALIADO_CAMPOS = ("nombre", "categoria", "descripcion", "zona", "contacto", "horario", "lat", "lon")


def crear_aliado(telegram_id, datos):
    """Crea un aliado (Manos Solidarias) en estado 'pendiente'. `datos` es un dict con
    las claves de ALIADO_CAMPOS (lat/lon opcionales). Devuelve su id."""
    con = conectar()
    cols = ["telegram_id"] + list(ALIADO_CAMPOS)
    vals = [telegram_id] + [datos.get(c) for c in ALIADO_CAMPOS]
    ph = ",".join("?" for _ in cols)
    cur = con.execute(f"INSERT INTO aliados ({','.join(cols)}) VALUES ({ph})", vals)
    con.commit()
    rid = cur.lastrowid
    con.close()
    return rid


def aliado(aid):
    con = conectar()
    row = con.execute("SELECT * FROM aliados WHERE id = ?", (aid,)).fetchone()
    con.close()
    return dict(row) if row else None


def aliado_por_tgid(telegram_id):
    """El aliado más reciente de esa persona que NO esté rechazado (para no duplicar)."""
    con = conectar()
    row = con.execute(
        "SELECT * FROM aliados WHERE telegram_id = ? AND estado != 'rechazado' "
        "ORDER BY id DESC LIMIT 1", (telegram_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def aliado_por_contacto(contacto):
    """Busca un aliado por teléfono, para el dedup del formulario web /api/ofrecer.

    Normaliza con wa_util (móviles VE, el campo puede traer varios números) y compara
    contra `contacto` (texto libre, cualquier canal) y `wa_phone` (registro por
    WhatsApp, ya canónico). Si el contacto no trae un móvil VE normalizable, compara
    el texto exacto. Prefiere la ficha ACTIVA más reciente; si solo hay fichas
    rechazadas/retiradas devuelve la más reciente de esas — quien la reciba NO debe
    tocarle el estado (un quitado de la web no resucita por reenviar el formulario).
    Devuelve dict o None."""
    objetivo = wa_util.candidatos(contacto)
    texto = (contacto or "").strip().lower()
    if not objetivo and not texto:
        return None
    con = conectar()
    rows = con.execute("SELECT * FROM aliados ORDER BY id DESC").fetchall()
    con.close()
    oculto = None   # mejor match rechazado/retirado, por si no hay ninguno activo
    for r in rows:
        d = dict(r)
        if objetivo:
            guardados = wa_util.candidatos(d.get("contacto"))
            wa = wa_util.canonico(d.get("wa_phone"))
            if wa:
                guardados.add(wa)
            hit = bool(objetivo & guardados)
        else:
            hit = (d.get("contacto") or "").strip().lower() == texto
        if not hit:
            continue
        if (d.get("estado") or "") in ("rechazado", "retirado"):
            oculto = oculto or d
            continue
        return d
    return oculto


def actualizar_aliado(aid, datos):
    """Actualiza la ficha de un aliado (reenvío del formulario web) SIN tocar su
    estado, sus canales (telegram_id / wa_phone) ni el GPS. Solo pisa los campos
    editables presentes en `datos`."""
    editables = ("nombre", "categoria", "descripcion", "zona", "contacto", "horario")
    campos = [c for c in editables if c in datos]
    if not campos:
        return
    con = conectar()
    con.execute("UPDATE aliados SET " + ", ".join(f"{c} = ?" for c in campos) +
                " WHERE id = ?", [datos[c] for c in campos] + [aid])
    con.commit()
    con.close()


def set_estado_aliado(aid, estado):
    con = conectar()
    con.execute("UPDATE aliados SET estado = ? WHERE id = ?", (estado, aid))
    con.commit()
    con.close()


def guardar_gps_aliado(aid, lat, lon):
    if lat is None or lon is None:
        return
    con = conectar()
    con.execute("UPDATE aliados SET lat = ?, lon = ? WHERE id = ?", (lat, lon, aid))
    con.commit()
    con.close()


def resumen_aliados():
    """Para la web PÚBLICA: aliados solidarios APROBADOS. Solo info que el aliado aceptó
    publicar (qué ofrece + contacto). NUNCA datos personales sensibles."""
    con = conectar()
    rows = con.execute(
        """
        SELECT id, nombre, categoria, descripcion, zona, contacto, horario, lat, lon, creado_en
        FROM aliados
        WHERE estado = 'aprobado'
        ORDER BY categoria, nombre
        """
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def aliados_para_notificar(categorias):
    """Manos Solidarias APROBADOS cuya categoría calza con `categorias` (para avisarles por
    Telegram de un llamado que coincide con lo que ofrecen). Incluye telegram_id (privado,
    solo para el aviso interno; NUNCA va a la web)."""
    cats = [c for c in (categorias or []) if c]
    if not cats:
        return []
    con = conectar()
    ph = ",".join("?" for _ in cats)
    rows = con.execute(
        f"SELECT id, telegram_id, wa_phone, nombre, categoria, zona FROM aliados "
        f"WHERE estado = 'aprobado' AND categoria IN ({ph})", cats).fetchall()
    con.close()
    return [dict(r) for r in rows]


VOLUNTARIO_CAMPOS = ("nombre", "zona", "contacto")


def crear_voluntario(telegram_id, datos):
    """Crea un voluntario en estado 'pendiente'. `datos` es un dict con VOLUNTARIO_CAMPOS."""
    con = conectar()
    cols = ["telegram_id"] + list(VOLUNTARIO_CAMPOS)
    vals = [telegram_id] + [datos.get(c) for c in VOLUNTARIO_CAMPOS]
    ph = ",".join("?" for _ in cols)
    cur = con.execute(f"INSERT INTO voluntarios ({','.join(cols)}) VALUES ({ph})", vals)
    con.commit()
    vid = cur.lastrowid
    con.close()
    return vid


def voluntario(vid):
    con = conectar()
    row = con.execute("SELECT * FROM voluntarios WHERE id = ?", (vid,)).fetchone()
    con.close()
    return dict(row) if row else None


def voluntario_por_tgid(telegram_id):
    """El voluntario más reciente de esa persona que NO esté rechazado (para no duplicar)."""
    con = conectar()
    row = con.execute(
        "SELECT * FROM voluntarios WHERE telegram_id = ? AND estado != 'rechazado' "
        "ORDER BY id DESC LIMIT 1", (telegram_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def set_estado_voluntario(vid, estado):
    con = conectar()
    con.execute("UPDATE voluntarios SET estado = ? WHERE id = ?", (estado, vid))
    con.commit()
    con.close()


def crear_llamado(voluntario_id, telegram_id, sector, mensaje, foto=None, origen=None):
    """Crea una fila en `llamados`. La tabla guarda DOS conceptos distintos y la columna
    `origen` los separa de forma explícita (antes solo se infería de voluntario_id):
      - 'voluntario': llamado de apoyo de un voluntario aprobado → nace 'activo', sale al
        instante en la web.
      - 'pedido': pedir ayuda (web/Telegram/WhatsApp) → el llamador DEBE pasarlo justo
        después a 'web' con publicar_pedido (auto-publicación) o a 'pendiente' con
        set_estado_llamado (revisión previa: desaparecidos o kill-switch apagado)."""
    if origen is None:
        origen = "voluntario" if voluntario_id else "pedido"
    con = conectar()
    _asegurar_columna(con, "origen", "TEXT", "llamados")
    # triangulado=1 de fábrica (y para todas las filas históricas): solo los pedidos que
    # publica publicar_pedido quedan en 0 y el barrido de bot_apoyo los abanica a la red.
    _asegurar_columna(con, "triangulado", "INTEGER DEFAULT 1", "llamados")
    cur = con.execute(
        "INSERT INTO llamados (voluntario_id, telegram_id, sector, mensaje, foto, origen) "
        "VALUES (?,?,?,?,?,?)",
        (voluntario_id, telegram_id, sector, mensaje, foto, origen),
    )
    lid = cur.lastrowid
    con.commit()
    con.close()
    return lid


def set_foto_llamado(lid, foto):
    con = conectar()
    con.execute("UPDATE llamados SET foto = ? WHERE id = ?", (foto, lid))
    con.commit()
    con.close()


def guardar_contacto_pedido(llamado_id, nombre, contacto, categoria):
    """Contacto PRIVADO de un pedido de ayuda (web o bots). Vive en solicitudes_web
    (misma tabla que usa backend.py para el formulario público); la web NUNCA la lee."""
    con = conectar()
    con.execute(
        "CREATE TABLE IF NOT EXISTS solicitudes_web ("
        "llamado_id INTEGER PRIMARY KEY, nombre TEXT, contacto TEXT, "
        "categoria TEXT, creado_en TEXT DEFAULT CURRENT_TIMESTAMP)")
    con.execute(
        "INSERT OR REPLACE INTO solicitudes_web (llamado_id, nombre, contacto, categoria) "
        "VALUES (?,?,?,?)", (llamado_id, nombre or None, contacto, categoria))
    con.commit()
    con.close()


def _tabla_desaparecidos(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS desaparecidos_reportes ("
        "llamado_id INTEGER PRIMARY KEY, persona TEXT, "
        "ultimo_hash TEXT, avisado_en TEXT, "
        "creado_en TEXT DEFAULT CURRENT_TIMESTAMP)")


def guardar_persona_desaparecida(llamado_id, persona):
    """Nombre de la PERSONA BUSCADA en un pedido categoria 'desaparecido' (web o
    bots). Lo usa el buscador federado: sugerencia al reportar y cron depurador
    que cruza contra hospitales/rescatados (ver fuentes.py)."""
    persona = (persona or "").strip()[:80]
    if not persona:
        return
    con = conectar()
    _tabla_desaparecidos(con)
    con.execute(
        "INSERT OR REPLACE INTO desaparecidos_reportes (llamado_id, persona) "
        "VALUES (?,?)", (llamado_id, persona))
    con.commit()
    con.close()


def desaparecidos_activos():
    """Pedidos de desaparecido aún vivos (pendiente/web/atencion) con el nombre de
    la persona buscada — lo que el cron depurador cruza contra las fuentes."""
    con = conectar()
    _tabla_desaparecidos(con)
    filas = con.execute(
        "SELECT d.llamado_id, d.persona, d.ultimo_hash, l.estado, l.sector "
        "FROM desaparecidos_reportes d JOIN llamados l ON l.id = d.llamado_id "
        "WHERE l.estado IN ('pendiente','web','atencion') AND d.persona != ''"
    ).fetchall()
    con.close()
    return [dict(f) for f in filas]


def marcar_aviso_cruce(llamado_id, hash_refs):
    con = conectar()
    _tabla_desaparecidos(con)
    con.execute(
        "UPDATE desaparecidos_reportes SET ultimo_hash = ?, "
        "avisado_en = CURRENT_TIMESTAMP WHERE llamado_id = ?", (hash_refs, llamado_id))
    con.commit()
    con.close()


# Vocabularios de estado por origen (la web filtra por estos strings EXACTOS:
# resumen_llamados usa 'activo' y build_site._pedidos_web usa 'web'/'atencion').
ESTADOS_LLAMADO = {"activo", "oculto",                              # origen 'voluntario'
                   "pendiente", "web", "atencion", "descartado",    # origen 'pedido'
                   "resuelto", "vencido"}                           # cierre (ambos orígenes)


def set_estado_llamado(lid, estado):
    estado = (estado or "").strip()
    if estado not in ESTADOS_LLAMADO:
        raise ValueError("estado de llamado inválido: %r" % estado)
    con = conectar()
    con.execute("UPDATE llamados SET estado = ? WHERE id = ?", (estado, lid))
    con.commit()
    con.close()


def expirar_llamados(horas=48):
    """Marca 'vencido' los llamados/pedidos que llevan `horas` sin resolver (ambos
    orígenes) — mismo patrón que expirar_compromisos(). Sale de la web al instante
    (resumen_llamados / _pedidos_web ya excluyen cualquier estado que no sea el
    'vivo' de cada origen). Devuelve la lista de los recién vencidos para que quien
    llama pueda avisar a los moderadores (con opción de reactivar)."""
    con = conectar()
    filas = con.execute(
        "SELECT id, sector, mensaje, origen, voluntario_id, creado_en FROM llamados "
        "WHERE estado IN ('activo', 'web', 'atencion') "
        "AND creado_en <= datetime('now', ?)",
        ("-{} hours".format(int(horas)),),
    ).fetchall()
    if filas:
        ids = [f["id"] for f in filas]
        marc = ",".join("?" * len(ids))
        con.execute(f"UPDATE llamados SET estado = 'vencido' WHERE id IN ({marc})", ids)
        con.commit()
    con.close()
    return [dict(f) for f in filas]


def es_llamado_de_voluntario(fila):
    """True si el llamado es del origen 'voluntario' (para saber a qué estado
    'vivo' reactivarlo: 'activo' vs 'web')."""
    return fila.get("origen") == "voluntario" or (
        not fila.get("origen") and fila.get("voluntario_id") is not None)


# --- Auto-publicación de pedidos (2026-07-03): el pedido sale a la web al instante y el
# barrido de bot_apoyo.py le avisa a la red. `triangulado` marca si ya se abanicó. ---

def publicar_pedido(lid):
    """Publica un pedido en la web (estado 'web') y lo deja en cola para que el barrido
    de bot_apoyo.py le avise a la red (triangulado=0). Único camino de publicación:
    lo usan la auto-publicación de los 3 canales y el botón ✅ del moderador."""
    con = conectar()
    _asegurar_columna(con, "triangulado", "INTEGER DEFAULT 1", "llamados")
    con.execute("UPDATE llamados SET estado = 'web', triangulado = 0 WHERE id = ?", (lid,))
    con.commit()
    con.close()


def pedidos_por_triangular():
    """Pedidos publicados a los que aún no se les avisó a la red (para el barrido)."""
    con = conectar()
    _asegurar_columna(con, "triangulado", "INTEGER DEFAULT 1", "llamados")
    filas = con.execute(
        "SELECT * FROM llamados WHERE origen = 'pedido' AND estado = 'web' "
        "AND COALESCE(triangulado, 1) = 0 ORDER BY id").fetchall()
    con.close()
    return [dict(f) for f in filas]


def reclamar_triangulado(lid):
    """Marca un pedido como abanicado ANTES de enviar los avisos (lo 'reclama').
    Devuelve True solo para el primer proceso que lo logre → sin avisos duplicados."""
    con = conectar()
    cur = con.execute(
        "UPDATE llamados SET triangulado = 1 WHERE id = ? AND COALESCE(triangulado, 1) = 0",
        (lid,))
    con.commit()
    ok = cur.rowcount > 0
    con.close()
    return ok


def crear_aviso_llamado(llamado_id, destino_tgid, tipo, quien, items, contacto, sector,
                        destino_wa=None):
    """Registra el aviso de un llamado a un acopio/MS/voluntario (para el botón Responder).
    El aviso viaja por Telegram (destino_tgid) o por WhatsApp (destino_wa)."""
    con = conectar()
    _asegurar_columna(con, "destino_wa", "TEXT", "llamado_avisos")
    cur = con.execute(
        "INSERT INTO llamado_avisos (llamado_id, destino_tgid, tipo, quien, items, contacto, sector, destino_wa) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (llamado_id, destino_tgid, tipo, quien, items, contacto, sector, destino_wa),
    )
    aid = cur.lastrowid
    con.commit()
    con.close()
    return aid


def aviso_llamado(aid):
    con = conectar()
    row = con.execute("SELECT * FROM llamado_avisos WHERE id = ?", (aid,)).fetchone()
    con.close()
    return dict(row) if row else None


def wa_de_rep(rep_id):
    """wa_phone del representante dueño de un punto (para avisos por WhatsApp), o None."""
    if not rep_id:
        return None
    con = conectar()
    row = con.execute("SELECT wa_phone FROM representantes WHERE id = ?", (rep_id,)).fetchone()
    con.close()
    return (row["wa_phone"] or None) if row else None


def voluntarios_para_notificar(sector, limite=20):
    """Voluntarios APROBADOS cuya zona calza (por palabras) con el sector de un llamado.
    Devuelve telegram_id y wa_phone (privados, solo para el aviso; NUNCA van a la web)."""
    toks = [t for t in _norm(sector or "").split() if len(t) >= 4]
    if not toks:
        return []
    con = conectar()
    rows = con.execute(
        "SELECT id, telegram_id, wa_phone, nombre, zona FROM voluntarios "
        "WHERE estado = 'aprobado'").fetchall()
    con.close()
    out = []
    for r in rows:
        zona = _norm(r["zona"] or "")
        zwords = [w for w in zona.split() if len(w) >= 4]
        if any(t in zona or any(w.startswith(t) or t.startswith(w) for w in zwords)
               for t in toks):
            out.append(dict(r))
    return out[:limite]


def entregas_por_punto():
    """Para la web: entregas COMPLETADAS que salieron de cada punto (acopio/fundación),
    contadas desde los envíos con QR cerrados. {punto_id: {"n":…, "ultima":…}}."""
    con = conectar()
    try:
        rows = con.execute(
            "SELECT de_punto_id AS pid, COUNT(*) AS n, MAX(cerrado_en) AS ultima "
            "FROM envios WHERE estado IN ('recibido','incompleto') AND de_punto_id IS NOT NULL "
            "GROUP BY de_punto_id").fetchall()
    except sqlite3.OperationalError:   # DB sin tabla envios todavía
        rows = []
    con.close()
    return {r["pid"]: {"n": r["n"], "ultima": r["ultima"]} for r in rows}


def fundaciones_para_notificar(texto, limite=5):
    """Fundaciones APROBADAS cuyas ÁREAS de atención calzan (por palabras, con
    sinónimos en ambos lados) con el texto de un pedido/llamado. Complementa el
    match por inventario de buscar_oferta: una fundación médica recibe pedidos de
    salud aunque no tenga inventario cargado. Devuelve al dueño del punto con sus
    canales de aviso (dueno_tgid / wa_phone), NUNCA para publicar."""
    blob = _con_sinonimos(_norm(texto or ""))
    toks = [t for t in blob.split() if len(t) >= 4]
    if not toks:
        return []
    con = conectar()
    rows = con.execute(
        "SELECT id AS dueno_rep_id, punto_id, telegram_id AS dueno_tgid, wa_phone, "
        "punto, areas, cobertura FROM representantes "
        "WHERE estado = 'aprobado' AND tipo_punto = 'fundacion' "
        "AND rol = 'representante' AND areas IS NOT NULL AND TRIM(areas) != ''").fetchall()
    con.close()
    out = []
    for r in rows:
        areas = _con_sinonimos(_norm(r["areas"] or ""))
        awords = [w for w in areas.split() if len(w) >= 4]
        if any(t in areas or any(w.startswith(t) or t.startswith(w) for w in awords)
               for t in toks):
            out.append(dict(r))
    return out[:limite]


def cerrar_llamado_por_vinculo(llamado_id, quien, contacto, mensaje):
    """Registra que alguien de la red coordinó la ayuda (vínculo hecho) y cierra el
    llamado: pasa a 'resuelto' → deja de salir en la web en el próximo rebuild."""
    con = conectar()
    row = con.execute("SELECT * FROM llamados WHERE id = ?", (llamado_id,)).fetchone()
    if not row:
        con.close()
        return None
    con.execute(
        "INSERT INTO llamado_respuestas (llamado_id, nombre, contacto, mensaje, estado) "
        "VALUES (?,?,?,?,'vinculo')", (llamado_id, quien, contacto, mensaje))
    if row["estado"] in ("web", "atencion", "activo"):
        con.execute("UPDATE llamados SET estado = 'resuelto' WHERE id = ?", (llamado_id,))
    con.commit()
    con.close()
    return dict(row)


def resumen_llamados(horas=336):
    """Para la web PÚBLICA: llamados ACTIVOS recientes (por defecto, últimas 2 semanas).
    Solo sector + mensaje + foto + fecha. NUNCA contacto ni datos del voluntario."""
    desde = "-{} hours".format(int(horas))
    con = conectar()
    _asegurar_columna(con, "origen", "TEXT", "llamados")
    rows = con.execute(
        """
        SELECT id, sector, mensaje, foto, creado_en
        FROM llamados
        WHERE estado = 'activo' AND creado_en >= datetime('now', ?)
          AND (origen = 'voluntario' OR (origen IS NULL AND voluntario_id IS NOT NULL))
        ORDER BY creado_en DESC
        """,
        (desde,),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        ce = (d.get("creado_en") or "").strip()
        # SQLite guarda CURRENT_TIMESTAMP en UTC ("YYYY-MM-DD HH:MM:SS") → ISO con Z
        # para que el new Date() del navegador lo interprete bien.
        if ce and "T" not in ce:
            d["creado_en"] = ce.replace(" ", "T") + "Z"
        d["respuestas"] = respuestas_de_llamado(r["id"])
        out.append(d)
    return out


def llamado(lid):
    con = conectar()
    r = con.execute("SELECT * FROM llamados WHERE id = ?", (lid,)).fetchone()
    con.close()
    return dict(r) if r else None


def respuestas_de_llamado(lid):
    """Cuántas personas se han ofrecido a atender un llamado (respuestas activas)."""
    con = conectar()
    n = con.execute("SELECT COUNT(*) FROM llamado_respuestas WHERE llamado_id=? AND estado='activa'",
                    (lid,)).fetchone()[0]
    con.close()
    return int(n or 0)


def responder_llamado(lid, telegram_id, nombre, contacto, mensaje=None):
    """Alguien (registrado o no) se ofrece a atender un llamado. Devuelve el id de la respuesta."""
    con = conectar()
    cur = con.execute(
        "INSERT INTO llamado_respuestas (llamado_id, telegram_id, nombre, contacto, mensaje) "
        "VALUES (?,?,?,?,?)", (lid, telegram_id, nombre, contacto, mensaje))
    rid = cur.lastrowid
    con.commit()
    con.close()
    return rid


def inventario_de_punto(punto_id):
    """Todos los insumos CONFIRMADOS de un punto (por punto_id), con su clase
    (tiene/stock/necesita). Para el export por colaborador desde el bot."""
    con = conectar()
    rows = con.execute(
        """
        SELECT i.categoria, i.item, i.cantidad, i.detalle, c.clase, c.creado_en, r.tipo_punto
        FROM inventario i JOIN capturas c ON c.id = i.captura_id
        JOIN representantes r ON r.id = c.representante_id
        WHERE c.estado = 'confirmado' AND r.punto_id = ?
        ORDER BY c.creado_en DESC
        """,
        (punto_id,),
    ).fetchall()
    con.close()
    return [dict(x) for x in rows]


def _situacion_sql():
    """Etiqueta 'tiene' / 'stock (privado)' / 'necesita' según clase y tipo de punto."""
    return (f"CASE WHEN c.clase='demanda' OR (c.clase IS NULL AND (r.tipo_punto IS NULL OR NOT {_SQL_OFERTA})) "
            "THEN 'necesita' WHEN c.clase='stock' THEN 'stock (privado)' ELSE 'tiene' END")


def export_puntos():
    """Para el Excel maestro: ficha completa de cada punto aprobado (sin censo)."""
    con = conectar()
    rows = con.execute(
        """
        SELECT punto, tipo_punto, punto_id, rol, responsable, telefono, lat, lon,
               tipo_inmueble, aforo, banos, duchas, cocina, agua_potable, electricidad,
               planta, camas, acceso_vehiculos, accesibilidad, almacenamiento, refrigeracion,
               ubicacion, horario, rif, cobertura, creado_en
        FROM representantes WHERE estado = 'aprobado'
        ORDER BY punto, id
        """
    ).fetchall()
    con.close()
    return [dict(x) for x in rows]


def export_inventario():
    """Para el Excel maestro: todos los insumos confirmados, con situación (tiene/necesita/stock)."""
    con = conectar()
    rows = con.execute(
        f"""
        SELECT r.punto, r.tipo_punto, r.punto_id, {_situacion_sql()} AS situacion,
               i.categoria, i.item, i.cantidad, i.detalle, c.creado_en
        FROM inventario i JOIN capturas c ON c.id = i.captura_id
        JOIN representantes r ON r.id = c.representante_id
        WHERE c.estado = 'confirmado' AND r.estado = 'aprobado'
        ORDER BY r.punto, situacion, i.categoria
        """
    ).fetchall()
    con.close()
    return [dict(x) for x in rows]


def export_seguimiento():
    """Para el Excel maestro: necesidades en seguimiento con su estado."""
    con = conectar()
    rows = con.execute(
        "SELECT punto, tipo_punto, item, estado, match_punto, creado_en, actualizado_en "
        "FROM necesidades ORDER BY actualizado_en DESC"
    ).fetchall()
    con.close()
    return [dict(x) for x in rows]


def export_censo():
    """Para el Excel maestro COMPLETO (admin): censo de personas — CONTIENE DATOS PERSONALES."""
    con = conectar()
    rows = con.execute(
        """
        SELECT r.punto, r.tipo_punto, p.nombre, p.cedula, p.edad, p.detalle, c.creado_en
        FROM censo_personas p JOIN capturas c ON c.id = p.captura_id
        JOIN representantes r ON r.id = c.representante_id
        WHERE c.estado = 'confirmado'
        ORDER BY r.punto, p.nombre
        """
    ).fetchall()
    con.close()
    return [dict(x) for x in rows]


def listar_desaparecidos():
    """Lista de desaparecidos para el panel de moderación."""
    con = conectar()
    filas = con.execute(
        """
        SELECT tg_id, titulo, estado_persona, tg_fecha, link
        FROM mensajes
        WHERE categoria = 'desaparecidos' AND relevante = 1
        ORDER BY tg_fecha DESC
        """
    ).fetchall()
    con.close()
    return [dict(f) for f in filas]


def marcar_persona(tg_id, estado):
    """Marca a una persona como buscando / aparecio / fallecida."""
    con = conectar()
    con.execute("UPDATE mensajes SET estado_persona = ? WHERE tg_id = ?", (estado, tg_id))
    con.commit()
    con.close()


def registrar_visita(dia, fp, cutoff_dia):
    """Suma una vista al día; si la huella fp es nueva, cuenta un único.
    Poda huellas anteriores a cutoff_dia para no crecer sin límite."""
    con = conectar()
    con.execute("INSERT INTO visitas(dia, vistas) VALUES(?, 1) "
                "ON CONFLICT(dia) DO UPDATE SET vistas = vistas + 1", (dia,))
    if fp:
        con.execute("INSERT OR IGNORE INTO visita_fp(dia, fp) VALUES(?, ?)", (dia, fp))
        con.execute("DELETE FROM visita_fp WHERE dia < ?", (cutoff_dia,))
    con.commit()
    con.close()


def resumen_visitas(dia):
    """Total acumulado de vistas, vistas de hoy y visitantes únicos de hoy."""
    con = conectar()
    total = con.execute("SELECT COALESCE(SUM(vistas), 0) FROM visitas").fetchone()[0]
    fila = con.execute("SELECT vistas FROM visitas WHERE dia = ?", (dia,)).fetchone()
    hoy = fila[0] if fila else 0
    unicos = con.execute("SELECT COUNT(*) FROM visita_fp WHERE dia = ?", (dia,)).fetchone()[0]
    con.close()
    return {"total": total, "hoy": hoy, "unicos_hoy": unicos}


# ---------------------------------------------------------------------------
# INVENTARIO DE 3 ESTADOS + COMPROMISOS ("en camino")
# meta (objetivo) / recibido (lo confirma el centro) / en_camino (compromisos vigentes).
# faltan = max(0, meta - recibido - en_camino). Cubierto (resuelta) cuando faltan <= 0.
# Todo cambio queda en necesidad_bitacora (reversible/auditable).
# ---------------------------------------------------------------------------
HORAS_COMPROMISO = 12  # un "en camino" sin confirmar caduca a las 12 h y libera el cupo


def parse_cantidad(txt):
    """De '200 unidades' / '6 cajas' / '12' saca (meta:int|None, unidad:str|None)."""
    if txt is None:
        return None, None
    s = str(txt).strip()
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None, (s or None)
    num = m.group(0).replace(".", "").replace(",", "")
    try:
        meta = int(num)
    except ValueError:
        meta = None
    unidad = (s[m.end():].strip(" .-") or s[:m.start()].strip(" .-") or None)
    return meta, unidad


def _bitacora(con, nid, campo, antes, despues, quien):
    con.execute(
        "INSERT INTO necesidad_bitacora (necesidad_id, campo, valor_antes, valor_despues, quien) "
        "VALUES (?,?,?,?,?)", (nid, campo, str(antes), str(despues), str(quien)))


def en_camino_de(nid):
    """Suma de compromisos vigentes (activos y no vencidos) de una necesidad."""
    con = conectar()
    r = con.execute(
        "SELECT COALESCE(SUM(cantidad),0) AS s FROM compromisos "
        "WHERE necesidad_id=? AND estado='activo' AND (vence_en IS NULL OR vence_en > datetime('now'))",
        (nid,)).fetchone()
    con.close()
    return int(r["s"] or 0)


def _reservar_stock_compromiso(con, cmpid, nec, telegram_id, cant):
    """Si quien se compromete es el dueño/colaborador de un punto con stock del ítem
    (acopio o fundación), APARTA `cant` en su renglón (stock.reservado) para que la
    web no lo siga ofreciendo como disponible mientras está «en camino». Best-effort:
    un donante del público sin punto simplemente no reserva nada."""
    try:
        rep = con.execute("SELECT punto_id FROM representantes WHERE telegram_id = ? "
                          "AND estado = 'aprobado'", (telegram_id,)).fetchone()
        if not rep or rep["punto_id"] is None:
            return
        objetivo = _norm(nec.get("item_norm") or nec.get("item") or "")
        if not objetivo:
            return
        filas = con.execute("SELECT id, item_norm, cantidad, IFNULL(reservado,0) AS reservado "
                            "FROM stock WHERE punto_id = ? AND estado = 'disponible'",
                            (rep["punto_id"],)).fetchall()
        fila = next((f for f in filas if f["item_norm"] and
                     (objetivo in f["item_norm"] or f["item_norm"] in objetivo)), None)
        if not fila:
            return
        libre = (fila["cantidad"] - fila["reservado"]) if fila["cantidad"] is not None else cant
        apartar = max(0, min(float(cant), float(libre)))
        if apartar <= 0:
            return
        con.execute("UPDATE stock SET reservado = IFNULL(reservado,0) + ? WHERE id = ?",
                    (apartar, fila["id"]))
        con.execute("UPDATE compromisos SET stock_id = ?, reservada = ? WHERE id = ?",
                    (fila["id"], apartar, cmpid))
    except Exception as e:  # noqa: BLE001 — la reserva nunca debe tumbar el compromiso
        print("reserva compromiso error:", e)


def _liberar_reserva_compromisos(con, filtro_sql, params):
    """Devuelve al disponible la reserva de los compromisos que calcen con el filtro
    (se llama ANTES de cambiarles el estado)."""
    try:
        for c in con.execute(
                f"SELECT id, stock_id, reservada FROM compromisos "
                f"WHERE stock_id IS NOT NULL AND IFNULL(reservada,0) > 0 AND {filtro_sql}",
                params).fetchall():
            con.execute("UPDATE stock SET reservado = MAX(0, IFNULL(reservado,0) - ?) WHERE id = ?",
                        (c["reservada"], c["stock_id"]))
            con.execute("UPDATE compromisos SET reservada = 0 WHERE id = ?", (c["id"],))
    except Exception as e:  # noqa: BLE001
        print("liberar reserva error:", e)


def expirar_compromisos():
    """Marca 'expirado' los compromisos activos vencidos (liberando su reserva de
    stock). Devuelve cuántos (lo llama un barrido)."""
    con = conectar()
    _liberar_reserva_compromisos(
        con, "estado='activo' AND vence_en IS NOT NULL AND vence_en <= datetime('now')", ())
    cur = con.execute(
        "UPDATE compromisos SET estado='expirado' "
        "WHERE estado='activo' AND vence_en IS NOT NULL AND vence_en <= datetime('now')")
    n = cur.rowcount
    con.commit()
    con.close()
    return n


def crear_compromiso(nid, telegram_id, nombre, cantidad):
    """Un voluntario se compromete a llevar `cantidad`. Crea el compromiso (vence en N h),
    no deja comprometer más de lo que falta. Devuelve (compromiso_id, necesidad_actualizada)."""
    nec = necesidad(nid)
    if not nec or nec.get("estado") in ("resuelta", "cancelada"):
        return None, nec
    try:
        cant = max(1, int(cantidad))
    except (TypeError, ValueError):
        return None, nec
    meta = nec.get("meta")
    recibido = nec.get("recibido") or 0
    encam = en_camino_de(nid)
    if meta:
        falta = max(0, meta - recibido - encam)
        if falta <= 0:
            return None, nec
        cant = min(cant, falta)
    con = conectar()
    cur = con.execute(
        "INSERT INTO compromisos (necesidad_id, telegram_id, nombre, cantidad, estado, vence_en) "
        "VALUES (?,?,?,?, 'activo', datetime('now', ?))",
        (nid, telegram_id, nombre, cant, "+{} hours".format(HORAS_COMPROMISO)))
    cmpid = cur.lastrowid
    # Si el que se compromete tiene ese insumo en su stock (acopio/fundación), se
    # aparta la cantidad: la web deja de ofrecerla como disponible mientras viaja.
    _reservar_stock_compromiso(con, cmpid, nec, telegram_id, cant)
    _bitacora(con, nid, "en_camino", encam, encam + cant, "tg:{}".format(telegram_id))
    con.execute("UPDATE necesidades SET estado='coordinando', actualizado_en=datetime('now') "
                "WHERE id=? AND estado IN ('buscando','disponible')", (nid,))
    con.commit()
    con.close()
    return cmpid, necesidad(nid)


def cancelar_compromiso(cmpid):
    con = conectar()
    _liberar_reserva_compromisos(con, "id = ? AND estado='activo'", (cmpid,))
    con.execute("UPDATE compromisos SET estado='cancelado' WHERE id=? AND estado='activo'", (cmpid,))
    con.commit()
    con.close()


def registrar_recibido(nid, cantidad, quien="centro"):
    """El centro confirma que recibió `cantidad`: sube recibido, salda compromisos activos
    (más viejos primero) y marca 'resuelta' si se cubre la meta. Devuelve la necesidad."""
    nec = necesidad(nid)
    if not nec:
        return None
    try:
        n = max(0, int(cantidad))
    except (TypeError, ValueError):
        return nec
    con = conectar()
    antes = nec.get("recibido") or 0
    nuevo = antes + n
    con.execute("UPDATE necesidades SET recibido=?, actualizado_en=datetime('now') WHERE id=?", (nuevo, nid))
    _bitacora(con, nid, "recibido", antes, nuevo, quien)
    rest = n
    for c in con.execute("SELECT id, cantidad FROM compromisos WHERE necesidad_id=? AND estado='activo' "
                         "ORDER BY creado_en", (nid,)).fetchall():
        if rest <= 0:
            break
        # Al saldar se libera la reserva (el descuento real del stock lo hace el
        # despacho con QR, si lo usaron; si no, el punto actualiza su inventario).
        _liberar_reserva_compromisos(con, "id = ?", (c["id"],))
        con.execute("UPDATE compromisos SET estado='entregado' WHERE id=?", (c["id"],))
        rest -= (c["cantidad"] or 0)
    meta = nec.get("meta")
    if meta and nuevo >= meta:
        con.execute("UPDATE necesidades SET estado='resuelta', actualizado_en=datetime('now') WHERE id=?", (nid,))
        _bitacora(con, nid, "estado", nec.get("estado"), "resuelta", quien)
    con.commit()
    con.close()
    return necesidad(nid)


def necesidades_de_punto(rep_id=None, punto_id=None):
    """Necesidades abiertas de un punto (para que el centro elija al registrar recibido)."""
    con = conectar()
    if punto_id is not None:
        filas = con.execute(
            "SELECT * FROM necesidades WHERE punto_id=? AND estado IN ('buscando','disponible','coordinando') "
            "ORDER BY id DESC", (punto_id,)).fetchall()
    else:
        filas = con.execute(
            "SELECT * FROM necesidades WHERE rep_id=? AND estado IN ('buscando','disponible','coordinando') "
            "ORDER BY id DESC", (rep_id,)).fetchall()
    con.close()
    return [dict(f) for f in filas]
