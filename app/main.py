# app/main.py
"""
API puente para integrar un bot conversacional con ServiceDesk Plus.
Versión estable/minimal: responde en Webchat/Teams, guarda ConversationReference,
y expone endpoints de diagnóstico sin middleware de estado conflictivo.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi import APIRouter

load_dotenv()

# ==============================
# Módulos propios (SDP + trazas)
# ==============================
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

# ==============================
# Feature flags
# ==============================
DEV_TRACE_ENABLED = os.getenv("DEV_TRACE_ENABLED", "false").lower() in ("1", "true", "yes")

# ==============================
# Logging con rotación diaria
# ==============================
LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_filename = LOG_DIR / f"app_{datetime.now().strftime('%Y-%m-%d')}.log"

handler = TimedRotatingFileHandler(
    filename=log_filename,
    when="midnight",
    interval=1,
    backupCount=60,
    encoding="utf-8",
)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
handler.setFormatter(formatter)

logger = logging.getLogger("asistente_sdp")
logger.setLevel(logging.INFO)
if not any(isinstance(h, TimedRotatingFileHandler) for h in logger.handlers):
    logger.addHandler(handler)

# ==============================
# FastAPI app
# ==============================
app = FastAPI(title="Asistente SDP - API puente", version="1.7.0")

# =============================================================================
# Bot Framework SDK (con tolerancia a ausencia de dependencias)
# =============================================================================
try:
    from botbuilder.core import (
        BotFrameworkAdapter,
        BotFrameworkAdapterSettings,
        TurnContext,
        ActivityHandler,
        MessageFactory,
    )
    from botbuilder.core import ConversationState, UserState, MemoryStorage
    from botbuilder.schema import Activity
    try:
        from botframework.connector.auth import AuthenticationError  # type: ignore
    except Exception:
        class AuthenticationError(Exception):
            ...
    try:
        from botframework.connector.auth import MicrosoftAppCredentials  # type: ignore
    except Exception:
        class MicrosoftAppCredentials:
            @staticmethod
            def trust_service_url(url: str) -> None:
                pass
except Exception:
    BotFrameworkAdapter = None
    BotFrameworkAdapterSettings = None
    TurnContext = None
    Activity = None
    ActivityHandler = object
    MessageFactory = None

    class AuthenticationError(Exception):
        ...
    class MicrosoftAppCredentials:  # fallback
        @staticmethod
        def trust_service_url(url: str) -> None:
            pass

# =============================================================================
# Credenciales y configuración AAD
# =============================================================================
MICROSOFT_APP_ID = (os.getenv("MicrosoftAppId") or os.getenv("MICROSOFT_APP_ID") or "").strip()
MICROSOFT_APP_PASSWORD = (os.getenv("MicrosoftAppPassword") or os.getenv("MICROSOFT_APP_PASSWORD") or "").strip()
MICROSOFT_APP_TENANT = (os.getenv("MicrosoftAppTenantId") or os.getenv("MICROSOFT_APP_TENANT_ID") or "").strip()

logger.info(
    "[boot] BF creds -> app_tail=%s pwd_len=%s tenant=%s",
    (MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None),
    (len(MICROSOFT_APP_PASSWORD) if MICROSOFT_APP_PASSWORD else 0),
    MICROSOFT_APP_TENANT or "(none)"
)

# Diagnóstico rápido de token de app (con MSAL)
def _selftest_token() -> dict:
    try:
        import msal
    except Exception as e:
        return {"ok": False, "error": f"MSAL no disponible: {e}"}

    tenant = MICROSOFT_APP_TENANT or "botframework.com"
    authority = f"https://login.microsoftonline.com/{tenant}"
    scope = ["https://api.botframework.com/.default"]
    cca = msal.ConfidentialClientApplication(
        client_id=MICROSOFT_APP_ID,
        client_credential=MICROSOFT_APP_PASSWORD,
        authority=authority,
    )
    res = cca.acquire_token_for_client(scopes=scope)
    ok = "access_token" in res
    return {
        "ok": ok,
        "app_tail": MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
        "tenant": tenant,
        "expires_in": res.get("expires_in"),
        "error": None if ok else res.get("error_description") or res.get("error"),
    }

_selftest = _selftest_token()
if _selftest.get("ok"):
    logger.info("[boot] creds_selftest=OK | app_tail=%s | tenant=%s | expires_in=%s | error=%s",
                _selftest.get("app_tail"), _selftest.get("tenant"),
                _selftest.get("expires_in"), _selftest.get("error"))
else:
    logger.warning("[boot] creds_selftest=FAIL | detail=%s", _selftest)

# =============================================================================
# Adapter + almacenamiento simple en memoria para estado de conversación
# =============================================================================
_adapter = None
conversation_state = None
user_state = None
conv_accessor = None
user_accessor = None

if BotFrameworkAdapterSettings and BotFrameworkAdapter:
    _settings = BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
    # Importante: el canal público de Bot Framework
    try:
        setattr(_settings, "channel_auth_tenant", "botframework.com")
    except Exception:
        pass
    # Scope correcto para emitir mensajes a canales públicos
    try:
        setattr(_settings, "oauth_scope", "https://api.botframework.com/.default")
    except Exception:
        try:
            setattr(_settings, "oAuthScope", "https://api.botframework.com/.default")
        except Exception:
            pass

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

    # Estado: en memoria. Mantiene last_seen y lo que necesites sin middleware complejo
    storage = MemoryStorage()
    conversation_state = ConversationState(storage)
    user_state = UserState(storage)
    conv_accessor = conversation_state.create_property("conversationData")
    user_accessor = user_state.create_property("userData")

# Guardamos un ConversationReference REAL (no dict) para /dev/ping
LAST_REF = {"ref": None}

# Umbral de “inactividad” para agregar un “seguimos aquí…”
INACTIVITY_DELTA = timedelta(minutes=5)

# =============================================================================
# Bot
# =============================================================================
class AdmInfraBot(ActivityHandler):
    async def on_message_activity(self, turn_context: "TurnContext"):
        text_raw = (turn_context.activity.text or "").strip()
        text = text_raw.lower()

        logger.info(
            "on_message_activity IN | ch=%s | serviceUrl=%s | text=%s",
            getattr(turn_context.activity, "channel_id", None),
            getattr(turn_context.activity, "service_url", None),
            text_raw,
        )

        # confía en el serviceUrl antes de intentar responder
        try:
            su = getattr(turn_context.activity, "service_url", None)
            if su:
                MicrosoftAppCredentials.trust_service_url(su)
                logger.info("trusted serviceUrl=%s", su)
        except Exception as e:
            logger.warning("trust_service_url error: %s", e)

        # Estado de conversación simple
        conv = await conv_accessor.get(turn_context, {})
        if conv is None or not isinstance(conv, dict):
            conv = {}

        now = datetime.utcnow()
        prefix = ""
        try:
            last_seen_iso = conv.get("last_seen")
            if last_seen_iso:
                last_seen = datetime.fromisoformat(last_seen_iso)
                if now - last_seen > INACTIVITY_DELTA:
                    prefix = "Seguimos aquí contigo 🙂. "
        except Exception:
            pass

        if text in ("hi", "hello", "hola"):
            reply_text = f"{prefix}¡Hola! Soy AdmInfraBot. ¿En qué te ayudo?"
        else:
            reply_text = f"{prefix}Recibí: {text_raw}"

        res = await turn_context.send_activity(MessageFactory.text(reply_text))
        sent_id = getattr(res, "id", None)
        logger.info("on_message_activity OUT | sent_id=%s", sent_id)

        # Actualiza last_seen y guarda
        try:
            conv["last_seen"] = now.isoformat()
            await conv_accessor.set(turn_context, conv)
            await conversation_state.save_changes(turn_context)
            await user_state.save_changes(turn_context)
        except Exception as e:
            logger.warning("No se pudo guardar estado conv/user: %s", e)

        # Guarda ConversationReference real para /dev/ping
        try:
            LAST_REF["ref"] = TurnContext.get_conversation_reference(turn_context.activity)
        except Exception:
            pass

        try:
            log_exec(endpoint="/api/messages", action="bf_sent", params={"id": sent_id}, ok=True)
        except Exception:
            pass

    async def on_members_added_activity(self, members_added, turn_context: "TurnContext"):
        # Guarda referencia al entrar a la conversación
        try:
            LAST_REF["ref"] = TurnContext.get_conversation_reference(turn_context.activity)
        except Exception:
            pass

        for m in (members_added or []):
            if m.id != turn_context.activity.recipient.id:
                await turn_context.send_activity("Bienvenido/a a AdmInfraBot 👋")

_bot_instance = AdmInfraBot()

# =============================================================================
# Endpoint principal del Bot: /api/messages
# =============================================================================
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

    # Diagnóstico de la actividad recibida
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

    # Confía en el serviceUrl cuanto antes
    try:
        if getattr(activity, "service_url", None):
            MicrosoftAppCredentials.trust_service_url(activity.service_url)
            logger.info("trusted (early) serviceUrl=%s", activity.service_url)
    except Exception as e:
        logger.warning("No se pudo confiar serviceUrl temprano: %s", e)

    # Guarda ref real
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
            content = json.dumps(body_obj)
            media = "application/json"
        elif isinstance(body_obj, str):
            content = body_obj
            media = "text/plain"
        else:
            content = ""
            media = "text/plain"
        return Response(content=content, media_type=media, status_code=status_code)

    return Response(status_code=200)

# =============================================================================
# Endpoints de diagnóstico
# =============================================================================
diag = APIRouter()

@diag.get("/health/botcreds")
def health_bot_creds():
    return {
        "has_app_id": bool(MICROSOFT_APP_ID),
        "app_id_tail": MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
        "has_password": bool(MICROSOFT_APP_PASSWORD),
        "pwd_len": len(MICROSOFT_APP_PASSWORD) if MICROSOFT_APP_PASSWORD else 0,
        "creds_selftest_ok": bool(_selftest.get("ok")),
    }

@diag.get("/dev/config")
def dev_config():
    return {
        "app_id_tail": MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
        "tenant_effective": MICROSOFT_APP_TENANT or "botframework.com",
    }

@diag.get("/dev/whoami")
def dev_whoami():
    res = _selftest_token()
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    # Decodifica el JWT para ver claims básicas
    try:
        import base64, json as _json
        token = res.get("access_token") or ""
        # Nota: no devolvemos el token, solo claims, por seguridad
        parts = (token or "").split(".")
        payload = {}
        if len(parts) >= 2:
            seg = parts[1] + "==="
            seg = seg.replace("-", "+").replace("_", "/")
            payload = _json.loads(base64.b64decode(seg))
        claims = {
            "appid": payload.get("appid") or payload.get("azp"),
            "aud": payload.get("aud"),
            "iss": payload.get("iss"),
            "exp_in": res.get("expires_in"),
        }
        return {"ok": True, "env_app_id_tail": MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
                "token_claims": claims, "tenant_effective": MICROSOFT_APP_TENANT or "botframework.com"}
    except Exception as e:
        return {"ok": True, "note": f"no claims decoded: {e}"}

def _ref_preview(ref_obj):
    try:
        # Construimos una vista sencilla sin convertir a dict global
        return {
            "activity_id": getattr(ref_obj, "activity_id", None),
            "user": {"id": getattr(getattr(ref_obj, "user", None), "id", None),
                     "name": getattr(getattr(ref_obj, "user", None), "name", None)},
            "bot": {"id": getattr(getattr(ref_obj, "bot", None), "id", None),
                    "name": getattr(getattr(ref_obj, "bot", None), "name", None)},
            "conversation": {"id": getattr(getattr(ref_obj, "conversation", None), "id", None)},
            "channel_id": getattr(ref_obj, "channel_id", None),
            "locale": getattr(ref_obj, "locale", None),
            "service_url": getattr(ref_obj, "service_url", None),
        }
    except Exception:
        return None

@diag.get("/dev/ref")
def dev_ref():
    ref = LAST_REF.get("ref")
    if ref is None:
        return {"kind": "NoneType", "preview": "None"}
    return {"kind": type(ref).__name__, "preview": json.dumps(_ref_preview(ref))}

@app.post("/dev/ping")
async def dev_ping():
    if _adapter is None:
        raise HTTPException(status_code=500, detail="Adapter BF no disponible")
    ref = LAST_REF.get("ref")
    if ref is None:
        raise HTTPException(status_code=409, detail="Aún no se ha recibido ninguna conversación.")
    logger.info("dev_ping usando ref conv.id=%s | su=%s | ch=%s",
                getattr(getattr(ref, "conversation", None), "id", None),
                getattr(ref, "service_url", None),
                getattr(ref, "channel_id", None))
    async def _send(ctx: "TurnContext"):
        try:
            if getattr(ctx.activity, "service_url", None):
                MicrosoftAppCredentials.trust_service_url(ctx.activity.service_url)
        except Exception:
            pass
        await ctx.send_activity("pong ✅ (desde /dev/ping)")
    # Bot ID variante (3 args) garantiza tokens válidos en SDK viejo
    await _adapter.continue_conversation(MICROSOFT_APP_ID, ref, _send)
    return {"ok": True}

app.include_router(diag)

# =============================================================================
# Endpoints ya existentes (SDP)
# =============================================================================
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
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")

@app.post("/intents/create")
def intent_create(subject: str, description: str, email: str):
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

@app.get("/meta/sites")
def meta_sites():
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
