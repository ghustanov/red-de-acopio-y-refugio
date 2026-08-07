"""Observabilidad central de Red de Acopio y Refugio.

Tres cosas, todas apagables/opcionales:
  1. LOGGING con niveles y formato → stdout (bajo systemd va a journald, que ya rota y
     centraliza; se consulta con `journalctl -u <servicio>`). Nunca sale del VPS.
  2. ALERTAS de error por Telegram: un DM al operador cuando ocurre un ERROR+ (mismo patrón
     que health.sh). Anti-spam (cooldown por firma + tope/hora). No bloquea (thread daemon).
     Va solo al DM del operador; manda un resumen, no el request. Se apaga si no hay token/ids.
  3. SENTRY opcional y APAGADO por defecto (solo con SENTRY_DSN + sentry-sdk). Blindado.

Uso en cada entrypoint:
    import obs
    obs.setup_logging("angeles-web")
    obs.init_sentry("angeles-web")          # no-op sin DSN
    obs.init_error_alerts("angeles-web")    # no-op sin token/ids
    log = obs.get_logger("angeles-web")
"""
import logging
import os
import sys
import threading
import time as _time
import urllib.parse as _uparse
import urllib.request as _ureq

try:
    from config import SENTRY_DSN, ALERTAS_TG_IDS, ALERTAS_BOT_TOKEN
except Exception:
    SENTRY_DSN = os.getenv("SENTRY_DSN", "")
    ALERTAS_TG_IDS = os.getenv("ALERTAS_TG_IDS", "")
    ALERTAS_BOT_TOKEN = os.getenv("ALERTAS_BOT_TOKEN", "")

_LOG_CONFIGURED = False
_SENTRY_ON = False
_ALERTS_ON = False

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(service="angeles", level=None):
    """Configura el root logger UNA sola vez (idempotente entre workers de gunicorn).
    Escribe a stdout → journald. Nivel por defecto INFO; override con LOG_LEVEL."""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return logging.getLogger(service)
    lvl = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    root.setLevel(lvl)
    for h in list(root.handlers):          # evita handlers duplicados
        root.removeHandler(h)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    root.addHandler(h)
    logging.captureWarnings(True)
    _LOG_CONFIGURED = True
    logging.getLogger(service).info("logging iniciado (nivel=%s)", lvl)
    return logging.getLogger(service)


# ---------------------------------------------------------------------------
# 2) Alertas de error por Telegram
# ---------------------------------------------------------------------------
class _AlertFormatter(logging.Formatter):
    """Resumen conciso para el DM. El traceback completo queda en journald."""
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        txt = (f"🔴 Red de Acopio · {record.levelname}\n"
               f"svc: {self.service}\n"
               f"{record.name}: {record.getMessage()}")
        if record.exc_info and record.exc_info[0]:
            etype = record.exc_info[0].__name__
            emsg = str(record.exc_info[1])[:200] if record.exc_info[1] else ""
            txt += f"\n{etype}: {emsg}"
        txt += f"\n@ {record.filename}:{record.lineno}\n(detalle: journalctl en el VPS)"
        return txt[:900]


class _TelegramErrorHandler(logging.Handler):
    """Manda logs ERROR+ como DM de Telegram. Anti-spam por firma + tope/hora.
    No bloquea (envía en thread daemon). Nunca rompe el logging si el envío falla."""
    def __init__(self, token, chat_ids, service, cooldown_s=1800, max_per_hour=8):
        super().__init__(level=logging.ERROR)
        self.token = token
        self.chat_ids = chat_ids
        self.service = service
        self.cooldown_s = cooldown_s
        self.max_per_hour = max_per_hour
        self._last = {}      # firma -> ts del último envío
        self._sent = []      # timestamps de la última hora
        self._lock = threading.Lock()

    def _allow(self, sig):
        now = _time.time()
        with self._lock:
            self._sent = [t for t in self._sent if now - t < 3600]
            if len(self._sent) >= self.max_per_hour:
                return False
            if now - self._last.get(sig, 0) < self.cooldown_s:
                return False
            self._last[sig] = now
            self._sent.append(now)
            return True

    def _send(self, text):
        for cid in self.chat_ids:
            try:
                data = _uparse.urlencode({"chat_id": cid, "text": text,
                                          "disable_web_page_preview": "true"}).encode()
                _ureq.urlopen(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              data=data, timeout=10)
            except Exception:
                pass  # best-effort; el error igual quedó en journald

    def emit(self, record):
        try:
            sig = f"{record.name}:{record.pathname}:{record.lineno}"
            if not self._allow(sig):
                return
            msg = self.format(record)
            threading.Thread(target=self._send, args=(msg,), daemon=True).start()
        except Exception:
            pass  # una alerta jamás debe romper el logging


def init_error_alerts(service="angeles"):
    """Engancha el handler de alertas a Telegram. No-op si falta token o destinos.
    Idempotente. Devuelve True si quedó activo."""
    global _ALERTS_ON
    if _ALERTS_ON:
        return True
    token = (ALERTAS_BOT_TOKEN or "").strip()
    ids = [x.strip() for x in (ALERTAS_TG_IDS or "").split(",") if x.strip()]
    if not token or not ids:
        return False
    h = _TelegramErrorHandler(token, ids, service)
    h.setFormatter(_AlertFormatter(service))
    logging.getLogger().addHandler(h)
    _ALERTS_ON = True
    logging.getLogger(service).info("alertas de error a Telegram activas (%d destino/s)", len(ids))
    return True


# ---------------------------------------------------------------------------
# 3) Sentry (opcional, apagado por defecto)
# ---------------------------------------------------------------------------
def _scrub(event, hint):
    """Defensa en profundidad: quita datos personales antes de que salgan a Sentry."""
    req = event.get("request")
    if isinstance(req, dict):
        for k in ("data", "cookies", "query_string"):
            req.pop(k, None)
        hdrs = req.get("headers")
        if isinstance(hdrs, dict):
            for k in ("Authorization", "Cookie", "X-Api-Key", "X-API-Key"):
                hdrs.pop(k, None)
    return event


def init_sentry(service="angeles"):
    """Activa Sentry SOLO si hay SENTRY_DSN y sentry-sdk instalado. Idempotente.
    Blindado para privacidad. Devuelve True si quedó activo."""
    global _SENTRY_ON
    if _SENTRY_ON:
        return True
    dsn = (SENTRY_DSN or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger(service).warning(
            "SENTRY_DSN configurado pero falta sentry-sdk "
            "(pip install sentry-sdk) → Sentry desactivado.")
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENV", "prod"),
            server_name=service,
            traces_sample_rate=0.0,
            send_default_pii=False,
            max_request_body_size="never",
            before_send=_scrub,
        )
        _SENTRY_ON = True
        logging.getLogger(service).info("Sentry activo (%s)", service)
        return True
    except Exception:
        logging.getLogger(service).exception("no se pudo iniciar Sentry")
        return False


def capture_exception(exc=None):
    """Manda una excepción a Sentry si está activo; si no, no-op. Nunca lanza."""
    if not _SENTRY_ON:
        return
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def install_excepthook(service="angeles"):
    """Para scripts (run_once, monitor_feed): loguea + manda a Sentry cualquier
    excepción NO capturada antes de que el proceso muera. Preserva el exit."""
    log = logging.getLogger(service)

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("crash no capturado", exc_info=(exc_type, exc, tb))
        capture_exception(exc)
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook


def get_logger(name="angeles"):
    return logging.getLogger(name)
