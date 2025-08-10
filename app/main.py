# app/main.py
"""
API puente para integrar un bot conversacional con ServiceDesk Plus.
Incluye logging diario con retención de 60 días y trazabilidad en base de datos.
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
from fastapi import APIRouter

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

# 🔐 Bot Framework auth helper
from app.modules.bf_auth import trust_service_url as bf_trust, diagnose_activity as bf_diag, acquire_bf_token

# --- Config de feature flags ---
DEV_TRACE_ENABLED = os.getenv("DEV_TRACE_ENABLED", "false").lower() in ("1", "true", "yes")

# --- Configuración de logs (rotación diaria, 60 días) ---
LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_filename = LOG_DIR / f"app_{datetime.now().strftime('%Y-%m-%d')}.log"

handler = TimedRotatingFileHandler(
    filename=log_filename, when="midnight", interval=1, backupCount=60, encoding="utf-8"
)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
handler.setFormatter(formatter)

logger = logging.getLogger("asistente_sdp")
logger.setLevel(logging.INFO)
if not any(isinstance(h, TimedRotatingFileHandler) for h in logger.handlers):
    logger.addHandler(handler)

# --- Crear aplicación FastAPI ---
app = FastAPI(title="Asistente SDP - API puente", version="1.6.0")

# ============================================================================
# Bot Framework: Adapter y endpoint /api/messages
# ============================================================================
try:
    from botbuilder.core import (
        BotFrameworkAdapter,
        BotFrameworkAdapterSettings,
        TurnContext,
        ActivityHandler,
        MessageFactory,
    )
    from botbuilder.schema import Activity
    try:
        from botframework.connector.auth import AuthenticationError  # type: ignore
    except Exception:
        class AuthenticationError(Exception):
            ...
except Exception:
    BotFrameworkAdapter = None
    BotFrameworkAdapterSettings = None
    TurnContext = None
    Activity = None
    ActivityHandler = object
    MessageFactory = None
    class AuthenticationError(Exception):
        ...
    logger.warning("botbuilder-core/schema no disponibles. Instala dependencias del Bot Framework.")

# --- Credenciales robustas ---
MICROSOFT_APP_ID = (os.getenv("MicrosoftAppId") or os.getenv("MICROSOFT_APP_ID") or "").strip()
MICROSOFT_APP_PASSWORD = (os.getenv("MicrosoftAppPassword") or os.getenv("MICROSOFT_APP_PASSWORD") or "").strip()

logger.info(
    "[boot] BF creds -> app_tail=%s pwd_len=%s",
    (MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None),
    (len(MICROSOFT_APP_PASSWORD) if MICROSOFT_APP_PASSWORD else 0),
)

_adapter = None
if BotFrameworkAdapterSettings and BotFrameworkAdapter:
    _settings = BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
    _adapter = BotFrameworkAdapter(_settings)
    logger.info(
        "[bf] adapter listo | app_tail=%s | secret=%s",
        MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else "------",
        "ok" if bool(MICROSOFT_APP_PASSWORD) else "missing",
    )

    async def _on_error(turn_context: "TurnContext", error: Exception):
        logger.exception("[bf.on_error] %s", error)
        try:
            await turn_context.send_activity("Ups, ocurrió un problema procesando tu mensaje. Intentemos nuevamente.")
        except Exception:
            pass
    _adapter.on_turn_error = _on_error

LAST_REF = {"ref": None}

class AdmInfraBot(ActivityHandler):
    async def on_message_activity(self, turn_context: "TurnContext"):
        text = (turn_context.activity.text or "").strip().lower()
        logger.info(
            "on_message_activity IN | ch=%s | serviceUrl=%s | text=%s",
            getattr(turn_context.activity, "channel_id", None),
            getattr(turn_context.activity, "service_url", None),
            text,
        )
        try:
            # Confiar la serviceUrl del canal que nos habló (Web Chat / Teams)
            bf_trust(getattr(turn_context.activity, "service_url", None))

            reply_text = (
                "¡Hola! Soy AdmInfraBot. ¿En qué te ayudo?"
                if text in ("hi", "hello", "hola")
                else f"Recibí: {turn_context.activity.text}"
            )
            res = await turn_context.send_activity(MessageFactory.text(reply_text))
            sent_id = getattr(res, "id", None)
            logger.info("on_message_activity OUT | sent_id=%s", sent_id)
            try:
                log_exec(endpoint="/api/messages", action="bf_sent", params={"id": sent_id}, ok=True)
            except Exception:
                pass
        except Exception as e:
            logger.exception("send_activity failed: %s", e)
            try:
                log_exec(endpoint="/api/messages", action="bf_send_error", ok=False, message=str(e))
            except Exception:
                pass
            raise

    async def on_members_added_activity(self, members_added, turn_context: "TurnContext"):
        for m in (members_added or []):
            if m.id != turn_context.activity.recipient.id:
                await turn_context.send_activity("Bienvenido/a a AdmInfraBot 👋")

_bot_instance = AdmInfraBot()

@app.post("/api/messages")
async def messages(request: Request):
    if _adapter is None or Activity is None:
        logger.error("Intento de uso de /api/messages sin botbuilder-core instalado.")
        raise HTTPException(status_code=500, detail="Bot Framework no disponible. Instala botbuilder-core/schema.")

    try:
        log_exec(endpoint="/api/messages", action="bf_receive", ok=True)
    except Exception:
        pass

    try:
        body = await request.json()
    except Exception as e:
        logger.exception("JSON inválido en /api/messages: %s", e)
        raise HTTPException(status_code=400, detail="Invalid activity payload")

    activity = Activity().deserialize(body)

    # Log diagnóstico extendido (incluye recipientId)
    try:
        ainfo = {
            "type": activity.type,
            "channelId": getattr(activity, "channel_id", None),
            "serviceUrl": getattr(activity, "service_url", None),
            "conversationId": getattr(getattr(activity, "conversation", None), "id", None),
            "fromId": getattr(getattr(activity, "from_property", None), "id", None),
            "recipientId": getattr(getattr(activity, "recipient", None), "id", None),
            "text": (getattr(activity, "text", None) or "")[:200],
        }
        logger.info("BF activity: %s", json.dumps(ainfo, ensure_ascii=False))
        log_exec(endpoint="/api/messages", action="bf_activity", params=ainfo, ok=True)
    except Exception as e:
        logger.warning("No se pudo loguear ainfo: %s", e)

    # Confiar serviceUrl y dejar diagnóstico de autenticación (solo si está habilitado)
    try:
        bf_trust(getattr(activity, "service_url", None))
        if DEV_TRACE_ENABLED:
            diag = bf_diag(activity, MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
            log_exec(endpoint="/api/messages", action="bf_auth_diag", params=diag, ok=True)
    except Exception as e:
        logger.warning("diag auth error: %s", e)

    try:
        LAST_REF["ref"] = TurnContext.get_conversation_reference(activity)
    except Exception:
        pass

    auth_header = request.headers.get("Authorization", "")
    logger.info("BF auth header present=%s", bool(auth_header))

    try:
        invoke_response = await _adapter.process_activity(
            activity, auth_header, lambda ctx: _bot_instance.on_turn(ctx)
        )
    except AuthenticationError as e:
        logger.warning("Auth BotFramework (401): %s", e)
        return Response(status_code=401, content="Unauthorized")
    except Exception as e:
        logger.exception("Error procesando actividad BF: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Adapter error")

    if invoke_response is not None:
        status_code = getattr(invoke_response, "status", None) or 200
        body_obj = getattr(invoke_response, "body", None)
        if isinstance(body_obj, (dict, list)):
            content = json.dumps(body_obj); media = "application/json"
        elif isinstance(body_obj, str):
            content = body_obj; media = "text/plain"
        else:
            content = ""; media = "text/plain"
        return Response(content=content, media_type=media, status_code=status_code)

    return Response(status_code=200)

# ============================================================================
# Endpoints de diagnóstico (credenciales y token AAD)
# ============================================================================
diag = APIRouter()

@diag.get("/health/botcreds")
def health_bot_creds():
    return {
        "has_app_id": bool(MICROSOFT_APP_ID),
        "app_id_tail": MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
        "has_password": bool(MICROSOFT_APP_PASSWORD),
        "pwd_len": len(MICROSOFT_APP_PASSWORD) if MICROSOFT_APP_PASSWORD else 0,
    }

@diag.get("/dev/test_token")
def dev_test_token():
    return acquire_bf_token(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)

app.include_router(diag)

# ============================================================================
# Endpoint de ping (solo si está habilitado DEV_TRACE_ENABLED)
# ============================================================================
if DEV_TRACE_ENABLED:
    @app.post("/dev/ping")
    async def dev_ping():
        if not LAST_REF["ref"]:
            raise HTTPException(status_code=409, detail="Aún no se ha recibido ninguna conversación.")
        async def _send(ctx: "TurnContext"):
            bf_trust(getattr(ctx.activity, "service_url", None))
            res = await ctx.send_activity("pong ✅ (desde /dev/ping)")
            logger.info("dev_ping sent_id=%s", getattr(res, "id", None))
        await _adapter.continue_conversation(MICROSOFT_APP_ID, LAST_REF["ref"], _send)
        return {"ok": True}

# ============================================================================
# Endpoints existentes
# ============================================================================
@app.get("/health")
def health():
    logger.info("Health check solicitado.")
    try:
        log_exec(endpoint="/health", action="health", ok=True)
    except Exception:
        pass
    return {"status": "ok"}

@app.get("/__ready")
def __ready():
    return {"ok": True, "source": "app/main.py"}

@app.get("/announcements/active")
def announcements_active():
    logger.info("Solicitud de anuncios activos.")
    try:
        res = get_announcements()
        log_exec(endpoint="/announcements/active", action="announcements", ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/announcements/active", action="announcements", ok=False, code=502, message=str(e))
        logger.error(f"Error obteniendo anuncios: {e}")
        raise HTTPException(status_code=502, detail="SDP error: {e}")

@app.post("/intents/create")
def intent_create(subject: str, description: str, email: str):
    logger.info(f"Creando ticket | requester={email} | subject={subject}")
    try:
        res = create_ticket(email, subject, description)
        log_exec(endpoint="/intents/create", email=email, action="create", params={"subject": subject}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/create", email=email, action="create", params={"subject": subject}, ok=False, code=502, message=str(e))
        logger.error(f"Error creando ticket: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")

@app.get("/intents/status")
def intent_status(email: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200)):
    logger.info(f"Listando tickets | requester={email} | page={page} | size={page_size}")
    try:
        res = list_my_tickets(email, page, page_size)
        log_exec(endpoint="/intents/status", email=email, action="list_mine", params={"page": page, "page_size": page_size}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/status", email=email, action="list_mine", params={"page": page, "page_size": page_size}, ok=False, code=502, message:str(e))
        logger.error(f"Error listando tickets: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")

@app.get("/intents/status_by_display")
def intent_status_by_display(display_id: str):
    logger.info(f"Consultando estado | display_id={display_id}")
    try:
        res = get_ticket_status_by_display(display_id)
        log_exec(endpoint="/intents/status_by_display", action="status_by_display", params={"display_id": display_id}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/status_by_display", action="status_by_display", params={"display_id": display_id}, ok=False, code=502, message:str(e))
        logger.error(f"Error consultando ticket {display_id}: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")

@app.post("/intents/note")
def intent_note(ticket_id: int, email: str, note: str):
    logger.info(f"Agregando nota | ticket_id={ticket_id} | requester={email}")
    try:
        res = add_note(ticket_id, email, note)
        log_exec(endpoint="/intents/note", email=email, action="note", params={"ticket_id": ticket_id}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/note", email=email, action="note", params={"ticket_id": ticket_id}, ok=False, code=502, message:str(e))
        logger.error(f"Error agregando nota a ticket {ticket_id}: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")

@app.post("/intents/note_by_display")
def intent_note_by_display(display_id: str, email: str, note: str):
    logger.info(f"Agregando nota por display | display_id={display_id} | requester={email}")
    try:
        res = add_note_by_display_id(display_id, email, note)
        log_exec(endpoint="/intents/note_by_display", email=email, action="note_by_display", params={"display_id": display_id}, ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/intents/note_by_display", email=email, action="note_by_display", params={"display_id": display_id}, ok=False, code=502, message:str(e))
        logger.error(f"Error agregando nota a ticket {display_id}: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")

@app.get("/meta/sites")
def meta_sites():
    logger.info("Listando sites de SDP.")
    try:
        res = list_sites()
        log_exec(endpoint="/meta/sites", action="meta_sites", ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/meta/sites", action="meta_sites", ok=False, code=502, message:str(e))
        logger.error(f"Error listando sites: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")

@app.get("/meta/request_templates")
def meta_templates():
    logger.info("Listando plantillas de solicitud.")
    try:
        res = list_request_templates()
        log_exec(endpoint="/meta/request_templates", action="meta_templates", ok=True)
        return res
    except Exception as e:
        log_exec(endpoint="/meta/request_templates", action="meta_templates", ok=False, code=502, message:str(e))
        logger.error(f"Error listando plantillas: {e}")
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")

@app.get("/meta/trace/recent")
def trace_recent(limit: int = Query(50, ge=1, le=500)):
    if not DEV_TRACE_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        raw = list_recent(limit)
        items = []
        for r in raw:
            fecha_hora = r["ts"].replace("T", " ").replace("Z", "")
            items.append({
                "fecha_hora": fecha_hora,
                "id": r["id"],
                "endpoint": r["endpoint"],
                "email": r["email"],
                "action": r["action"],
                "params": r.get("params", {}),
                "ok": r["ok"],
                "code": r["code"],
                "message": r["message"],
            })
        body = json.dumps(items, ensure_ascii=False, indent=2)
        return Response(content=body, media_type="application/json")
    except Exception as e:
        logger.error(f"Error leyendo trazas: {e}")
        raise HTTPException(status_code=500, detail="Trace read error")

