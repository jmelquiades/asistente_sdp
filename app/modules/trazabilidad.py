# asistente_sdp/app/modules/trazabilidad.py
"""
Módulo de trazabilidad de ejecuciones del Asistente SDP.
Permite registrar y consultar la actividad de los endpoints.
"""

import os
import sqlite3
import json
import datetime
from threading import Lock

# Ruta del archivo SQLite (puedes moverlo vía .env con TRACE_DB_PATH)
_DB_PATH = os.getenv("TRACE_DB_PATH", "trace.db")

# Lock para evitar condiciones de carrera en escrituras concurrentes
_lock = Lock()


def _conn():
    """
    Entrada:
        - Ninguna.
    Qué hace:
        - Crea y devuelve una conexión SQLite hacia la base de datos definida en _DB_PATH.
        - check_same_thread=False permite usar la misma conexión desde distintos hilos controlados por _lock.
    Salida esperada:
        - Objeto de conexión sqlite3 listo para ejecutar queries.
    """
    return sqlite3.connect(_DB_PATH, check_same_thread=False)


def _init():
    """
    Entrada:
        - Ninguna.
    Qué hace:
        - Inicializa la base de datos (tabla + índices) si no existen.
        - Aplica PRAGMA básicos para mejor estabilidad en escritura/lectura ligera.
    Salida esperada:
        - Base de datos lista para registrar trazas sin errores de estructura.
    """
    with _conn() as c:
        # PRAGMA básicos (no críticos, pero ayudan)
        try:
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass

        c.execute("""
        CREATE TABLE IF NOT EXISTS ejecuciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,           -- ISO8601 UTC con sufijo 'Z'
            endpoint TEXT NOT NULL,     -- ruta (ej: /intents/create)
            email TEXT,                 -- correo del solicitante si aplica
            action TEXT,                -- acción lógica (ej: create, list_mine)
            params_json TEXT,           -- parámetros serializados a JSON
            ok INTEGER NOT NULL,        -- 1=éxito, 0=error
            code INTEGER,               -- código adicional (p.ej. HTTP interno)
            message TEXT                -- detalle de error o nota
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ejec_ts ON ejecuciones(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ejec_email ON ejecuciones(email)")


_init()


def log_exec(*, endpoint: str, email: str | None = None, action: str | None = None,
             params: dict | None = None, ok: bool = False, code: int | None = None,
             message: str | None = None) -> int:
    """
    Entrada:
      - endpoint (str): Ruta del endpoint que se ejecutó.
      - email (str|None): Correo asociado al solicitante, si aplica.
      - action (str|None): Nombre lógico de la acción ejecutada (ej. 'create', 'list_mine').
      - params (dict|None): Diccionario con parámetros relevantes de la ejecución.
      - ok (bool): True si la ejecución fue exitosa, False si falló.
      - code (int|None): Código de error o estado opcional (p.ej., 502).
      - message (str|None): Mensaje breve asociado al resultado.
    Qué hace:
      - Registra en la tabla 'ejecuciones' una nueva fila con todos los datos de la ejecución.
      - Guarda la fecha/hora UTC en formato ISO8601 con sufijo 'Z'.
      - Convierte params a JSON para almacenarlo en la base.
    Salida esperada:
      - Devuelve el ID (int) del registro insertado para trazabilidad interna.
    """
    ts = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    pj = json.dumps(params or {}, ensure_ascii=False)
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO ejecuciones (ts,endpoint,email,action,params_json,ok,code,message) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, endpoint, email, action, pj, 1 if ok else 0, code, (message or "")[:1000])
        )
        return cur.lastrowid


def list_recent(limit: int = 50) -> list[dict]:
    """
    Entrada:
      - limit (int): Cantidad máxima de registros a devolver (default: 50).
    Qué hace:
      - Consulta la tabla 'ejecuciones' y devuelve las últimas ejecuciones ordenadas de más reciente a más antigua.
      - Convierte el JSON de parámetros a diccionario Python.
    Salida esperada:
      - Lista de diccionarios con claves: id, ts, endpoint, email, action, params, ok, code, message.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT id,ts,endpoint,email,action,params_json,ok,code,message "
            "FROM ejecuciones ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()

    out: list[dict] = []
    for r in rows:
        out.append({
            "id": r[0],
            "ts": r[1],
            "endpoint": r[2],
            "email": r[3],
            "action": r[4],
            "params": json.loads(r[5] or "{}"),
            "ok": bool(r[6]),
            "code": r[7],
            "message": r[8],
        })
    return out


def prune_old_records(retention_days: int | None = None) -> int:
    """
    Entrada:
        - retention_days (int|None): Días a conservar. Si no se indica, usa TRACE_RETENTION_DAYS o 180.
    Qué hace:
        - Elimina de la tabla 'ejecuciones' todos los registros con ts anterior al umbral (UTC).
    Salida esperada:
        - int: cantidad de filas eliminadas.
    """
    if retention_days is None:
        try:
            retention_days = int(os.getenv("TRACE_RETENTION_DAYS", "180"))
        except Exception:
            retention_days = 180

    # Umbral: ahora - retention_days (UTC)
    now = datetime.datetime.utcnow()
    cutoff = (now - datetime.timedelta(days=retention_days)).isoformat(timespec="seconds") + "Z"

    with _conn() as c:
        cur = c.execute("DELETE FROM ejecuciones WHERE ts < ?", (cutoff,))
        return cur.rowcount
