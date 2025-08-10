# asistente_sdp/app/main.py
"""
API puente para integrar un bot conversacional con ServiceDesk Plus.
Incluye logging diario con retención de 60 días y trazabilidad en base de datos.

Entradas: Peticiones HTTP desde el bot u otros sistemas.
Salidas: Respuestas JSON con datos de SDP o confirmación de operaciones.
"""

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

# --- Cargar variables de entorno ---
load_dotenv()

from app.modules.sdp_actions import (
    get_announcements,
    create_ticket,
    list_my_tickets,
    add_note,
    add_note_by_display_id,
    get_ticket_status_by_display,
    list_sites,
    list_request_templates,
)
from app.modules.trazabilidad import log_exec, list_recent  # trazabilidad

# --- Config de feature flags ---
DEV_TRACE_ENABLED = os.getenv("DEV_TRACE_ENABLED", "false").lower() in ("1", "true", "yes")

# --- Configuración de logs (rotación diaria, 60 días) ---
LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_filename = LOG_DIR / f"app_{datetime.now().strftime('%Y-%m-%d')}.log"

handler = TimedRotatingFileHandler(
    filename=log_filename,
    when="midnight",
    interval=1,
    backupCount=60,  # Mantener 60 días de logs
    encoding="utf-8",
)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
handler.setFormatter(formatter)

logger = logging.getLogger("asistente_sdp")
logger.setLevel(logging.INFO)
# Evita duplicar handlers en reloads
if not any(isinstance(h, TimedRotatingFileHandler) for h in logger.handlers):
    logger.addHandler(handler)

# --- Crear aplicación FastAPI ---
app = FastAPI(title="Asistente SDP - API puente", version="1.5.1")


# ============================================================================
# Bot Framework: Adapter y endpoint /api/messages
# ============================================================================

# Entrada:
#   - CREDENCIALES de Azure AD (MicrosoftAppId / MicrosoftAppPassword) vía env.
# Qué hace:
#   - Configura el adapter de Bot Framework y publica POST /api/messages para Teams.
#   - Implementa un bot mínimo: welcome + respuesta a "hi/hello/hola".
# Salida esperada:
#   - HTTP 200 en /api/messages si procesa la actividad.

try:
    from botbuilder.core import (
        BotFrameworkAdapter,
        BotFrameworkAdapterSettings,
        TurnContext,
        ActivityHandler,
    )
    from botbuilder.schema import Activity
    try:
        # Si el token/JWT del canal no valida, capturamos 401 de forma explícita
        from botframework.connector.auth import AuthenticationError  # type: ignore
    except Exception:
        class AuthenticationError(Exception):
            ...
except Exception:
    BotFrameworkAdapter = None
    BotFrameworkAdapterSettings = None
    TurnContext = None
    Activity = None
    ActivityHandler = object  # fallback inocuo
    class AuthenticationError(Exception):
        ...
    logger.warning("botbuilder-core/schema no disponibles. Instala dependencias del Bot Framework.")

# Soportamos nombres de env en PascalCase y UPPER_CASE
MICROSOFT_APP_ID = (
    os.getenv("MicrosoftAppId")
    or os.getenv("MICROSOFT_APP_ID")
    or ""
)
MICROSOFT_APP_PASSWORD = (
    os.getenv("MicrosoftAppPassword")
    or os.getenv("MICROSOFT_APP_PASSWORD")
    or ""
)

_adapter = None
if BotFrameworkAdapterSettings and BotFrameworkAdapter:
    _settings = BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
    _adapter = BotFrameworkAdapter(_settings)
    logger.info(
        "BF adapter listo | app_id=...%s | secret=%s",
        MICROSOFT_APP_ID[-4:] if MICROSOFT_APP_ID else "----",
        "ok" if bool(MICROSOFT_APP_PASSWORD) else "missing",
    )

class AdmInfraBot(ActivityHandler):
    """Bot mínimo para validación de canal (welcome + saludo básico)."""

    async def on_message_activity(self, turn_context: "TurnContext"):
        """
        Entrada:
            - turn_context.activity.text: texto del usuario.
        Qué hace:
            - Responde saludo y eco simple (para validación).
        Salida esperada:
            - Mensaje de texto.
        """
        text = (turn_context.activity.text or "").strip().lower()
        if text in ("hi", "hello", "hola"):
            await turn_context.send_activity("¡Hola! Soy AdmInfraBot. ¿En qué te ayudo?")
        else:
            await turn_context.send_activity(f"Recibí: {turn_context.activity.text}")

    async def on_members_added_activity(self, members_added, turn_context: "TurnContext"):
        """
        Entrada:
            - members_added: usuarios añadidos a la conversación.
        Qué hace:
            - Envía mensaje de bienvenida cuando agregan el bot.
        Salida esperada:
            - Mensaje de bienvenida.
        """
        for m in (members_added or []):
            if m.id != turn_context.activity.recipient.id:
                await turn_context.send_activity("Bienvenido/a a AdmInfraBot 👋")

_bot_instance = AdmInfraBot()


@app.post("/api/messages")
async def messages(request: Request):
    """
    Entrada:
        - Body: Activity del Bot Framework (Teams) en JSON.
        - Header: Authorization (portador / firma del canal).
    Qué hace:
        - Deserializa la actividad y la procesa con el adapter de Bot Framework.
        - Registra trazabilidad / logs de diagnóstico (sin romper la conversación).
    Salida esperada:
        - 200 si procesa correctamente.
        - 401 si la firma/token no valida.
        - 400 si el body no es JSON válido.
        - 500 ante error inesperado del adapter.
    """
    if _adapter is None or Activity is None:
        logger.error("Intento de uso de /api/messages sin botbuilder-core instalado.")
        raise HTTPException(status_code=500, detail="Bot Framework no disponible. Instala botbuilder-core/schema.")

    # Trazabilidad ligera (no crítica)
    try:
        log_exec(endpoint="/api/messages", action="bf_receive", ok=True)
    except Exception:
        pass

    try:
        body = await request.json()
    except Exception as e:
        logger.exception(f"JSON inválido en /api/messages: {e}")
        raise HTTPException(status_code=400, detail="Invalid activity payload")

    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    try:
        # ActivityHandler despacha automáticamente a on_message_activity / on_members_added_activity
        await _adapter.process_activity(activity, auth_header, lambda ctx: _bot_instance.on_turn(ctx))
        return Response(status_code=200)
    except AuthenticationError as e:
        logger.warning(f"Auth BotFramework (401): {e}")
        return Response(status_code=401, content="Unauthorized")
    except Exception as e:
        logger.exception(f"Error procesando actividad BF: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Adapter error")


# ============================================================================
# Endpoints existentes (se mantienen igual)
# ============================================================================

@app.get("/health")
def health():
    """
    Entrada:
        - Ninguna.
    Qué hace:
        - Verifica que el API está activo.
        - Registra log y traza de disponibilidad.
    Salida esperada:
        - JSON con {"status": "ok"}.
    """
    logger.info("Health check solicitado.")
    try:
        log_exec(endpoint="/health", action="health", ok=True)
    except Exception:
        # Si la trazabilidad falla, no rompemos el health.
        pass
    return {"status": "ok"}


@app.get("/announcements/active")
def announcements_active():
    """
    Entrada:
        - Ninguna.
    Qué hace:
        - Lista anuncios activos en SDP.
        - Registra trazabilidad (éxito/error) con acción 'announcements'.
    Salida esperada:
        - dict con clave "announcements": lista de anuncios normalizados.
    """
    logger.info("Solicitud de anuncios activos.")
    try:
        res = get_announcements()
        log_exec(endpoint="/announcements/active", action="announcements", ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/announcements/active", action="announcements", ok=False, code=502, message=str(e))
        logger.error(f"Error obteniendo anuncios: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")


@app.post("/intents/create")
def intent_create(subject: str, description: str, email: str):
    """
    Entrada:
        - subject (str): asunto del ticket.
        - description (str): descripción del ticket.
        - email (str): correo del solicitante.
    Qué hace:
        - Crea un ticket en SDP.
        - Registra trazabilidad con acción 'create' (params: subject).
    Salida esperada:
        - dict con datos del ticket creado.
    """
    logger.info(f"Creando ticket | requester={email} | subject={subject}")
    try:
        res = create_ticket(email, subject, description)
        log_exec(endpoint="/intents/create", email=email, action="create",
                 params={"subject": subject}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/create", email=email, action="create",
                 params={"subject": subject}, ok=False, code=502, message=str(e))
        logger.error(f"Error creando ticket: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")


@app.get("/intents/status")
def intent_status(email: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200)):
    """
    Entrada:
        - email (str): correo del solicitante.
        - page (int): número de página (>=1).
        - page_size (int): tamaño de página (1..200).
    Qué hace:
        - Lista tickets del solicitante (cascada V0→V3).
        - Registra trazabilidad con acción 'list_mine' y paginación.
    Salida esperada:
        - dict con 'list_info' y 'requests'.
    """
    logger.info(f"Listando tickets | requester={email} | page={page} | size={page_size}")
    try:
        res = list_my_tickets(email, page, page_size)
        log_exec(endpoint="/intents/status", email=email, action="list_mine",
                 params={"page": page, "page_size": page_size}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/status", email=email, action="list_mine",
                 params={"page": page, "page_size": page_size}, ok=False, code=502, message=str(e))
        logger.error(f"Error listando tickets: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")


@app.get("/intents/status_by_display")
def intent_status_by_display(display_id: str):
    """
    Entrada:
        - display_id (str): ID visible del ticket.
    Qué hace:
        - Devuelve estado compacto del ticket (GET directo o búsqueda por display_id).
        - Registra trazabilidad con acción 'status_by_display'.
    Salida esperada:
        - dict con 'ticket' (compacto) y 'raw' (datos soporte).
    """
    logger.info(f"Consultando estado | display_id={display_id}")
    try:
        res = get_ticket_status_by_display(display_id)
        log_exec(endpoint="/intents/status_by_display", action="status_by_display",
                 params={"display_id": display_id}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/status_by_display", action="status_by_display",
                 params={"display_id": display_id}, ok=False, code=502, message=str(e))
        logger.error(f"Error consultando ticket {display_id}: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")


@app.post("/intents/note")
def intent_note(ticket_id: int, email: str, note: str):
    """
    Entrada:
        - ticket_id (int): ID interno en SDP.
        - email (str): correo del autor de la nota.
        - note (str): texto de la nota.
    Qué hace:
        - Agrega una nota al ticket.
        - Registra trazabilidad con acción 'note'.
    Salida esperada:
        - dict con confirmación/resultado de SDP.
    """
    logger.info(f"Agregando nota | ticket_id={ticket_id} | requester={email}")
    try:
        res = add_note(ticket_id, email, note)
        log_exec(endpoint="/intents/note", email=email, action="note",
                 params={"ticket_id": ticket_id}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/note", email=email, action="note",
                 params={"ticket_id": ticket_id}, ok=False, code=502, message=str(e))
        logger.error(f"Error agregando nota a ticket {ticket_id}: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")


@app.post("/intents/note_by_display")
def intent_note_by_display(display_id: str, email: str, note: str):
    """
    Entrada:
        - display_id (str): ID visible del ticket.
        - email (str): correo del autor de la nota.
        - note (str): texto de la nota.
    Qué hace:
        - Agrega una nota al ticket por display_id (resuelve ID si aplica).
        - Registra trazabilidad con acción 'note_by_display'.
    Salida esperada:
        - dict con confirmación/resultado de SDP.
    """
    logger.info(f"Agregando nota por display | display_id={display_id} | requester={email}")
    try:
        res = add_note_by_display_id(display_id, email, note)
        log_exec(endpoint="/intents/note_by_display", email=email, action="note_by_display",
                 params={"display_id": display_id}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/note_by_display", email=email, action="note_by_display",
                 params={"display_id": display_id}, ok=False, code=502, message=str(e))
        logger.error(f"Error agregando nota a ticket {display_id}: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")


# --- Meta utilitarios ---
@app.get("/meta/sites")
def meta_sites():
    """
    Entrada:
        - Ninguna.
    Qué hace:
        - Lista todos los sites configurados en SDP.
        - Registra trazabilidad con acción 'meta_sites'.
    Salida esperada:
        - dict con sitios de SDP.
    """
    logger.info("Listando sites de SDP.")
    try:
        res = list_sites()
        log_exec(endpoint="/meta/sites", action="meta_sites", ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/meta/sites", action="meta_sites", ok=False, code=502, message=str(e))
        logger.error(f"Error listando sites: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")


@app.get("/meta/request_templates")
def meta_templates():
    """
    Entrada:
        - Ninguna.
    Qué hace:
        - Lista todas las plantillas de solicitud en SDP.
        - Registra trazabilidad con acción 'meta_templates'.
    Salida esperada:
        - dict con plantillas de solicitud de SDP.
    """
    logger.info("Listando plantillas de solicitud.")
    try:
        res = list_request_templates()
        log_exec(endpoint="/meta/request_templates", action="meta_templates", ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/meta/request_templates", action="meta_templates", ok=False, code=502, message=str(e))
        logger.error(f"Error listando plantillas: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")


@app.get("/meta/trace/recent")
def trace_recent(limit: int = Query(50, ge=1, le=500)):
    """
    Entrada:
        - limit (int): cantidad máxima de registros a devolver (1..500).
    Qué hace:
        - Devuelve las últimas ejecuciones registradas (vía list_recent) para diagnóstico.
        - Re-formatea a salida legible: 'fecha_hora' primero y JSON indentado.
        - Solo visible si DEV_TRACE_ENABLED=true.
    Salida esperada:
        - JSON (indentado) como lista de objetos:
          {fecha_hora, id, endpoint, email, action, params, ok, code, message}.
    """
    if not DEV_TRACE_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")

    try:
        raw = list_recent(limit)

        # Reordenar campos y formatear fecha para lectura web
        items = []
        for r in raw:
            fecha_hora = r["ts"].replace("T", " ").replace("Z", "")  # ej: 2025-08-09 18:15:14
            items.append({
                "fecha_hora": fecha_hora,  # primero para lectura
                "id": r["id"],
                "endpoint": r["endpoint"],
                "email": r["email"],
                "action": r["action"],
                "params": r.get("params", {}),
                "ok": r["ok"],
                "code": r["code"],
                "message": r["message"],
            })

        # Responder bonito para browser (JSON indentado)
        body = json.dumps(items, ensure_ascii=False, indent=2)
        return Response(content=body, media_type="application/json")
    except Exception as e:
        logger.error(f"Error leyendo trazas: {e}")
        raise HTTPException(status_code=500, detail="Trace read error")
