# app/main.py
"""
API puente para integrar un bot conversacional con ServiceDesk Plus.
- Bot Framework (WebChat/Teams) con estado mínimo (last_seen) sin AutoSaveStateMiddleware.
- Credenciales con tenant configurable y self-test MSAL.
- Logging rotativo diario (60 días).
- Endpoints de negocio (SDP) y diagnósticos.
"""

import os
import json
import time
import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi import APIRouter

# --- Cargar variables de entorno ---
load_dotenv()

# =========================
#  Módulos de negocio SDP
# =========================
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
from app.modules.sdp_auth import SDP_URL  # solo para debug

# =========================
#  Feature flags / config
# =========================
DEV_TRACE_ENABLED = os.getenv("DEV_TRACE_ENABLED", "false").lower() in ("1", "true", "yes")

# =========================
#  Logging
# =========================
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

# =========================
#  FastAPI app
# =========================
app = FastAPI(title="Asistente SDP - API puente", version="2.0.0")

# =========================================
#  Bot Framework SDK (import tolerante)
# =========================================
try:
    from botbuilder.core import (
        BotFrameworkAdapter,
        BotFrameworkAdapterSettings,
        TurnContext,
        ActivityHandler,
        MessageFactory,
        MemoryStorage,
        ConversationState,
        UserState,
    )
    from botbuilder.schema import Activity, ConversationReference
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
    # Fallbacks para levantar API aunque no esté el SDK
    BotFrameworkAdapter = None
    BotFrameworkAdapterSettings = None
    TurnContext = None
    Activity = None
    ActivityHandler = object
    MessageFactory = None
    MemoryStorage = None
    ConversationState = None
    UserState = None

    class ConversationReference: ...
    class AuthenticationError(Exception): ...
    class MicrosoftAppCredentials:
        @staticmethod
        def trust_service_url(url: str) -> None:
            pass

    logger.warning("botbuilder-core/schema no disponibles. Instala dependencias del Bot Framework.")

# =========================================
#  Credenciales y self-test MSAL
# =========================================
MICROSOFT_APP_ID = (os.getenv("MicrosoftAppId") or os.getenv("MICROSOFT_APP_ID") or "").strip()
MICROSOFT_APP_PASSWORD = (os.getenv("MicrosoftAppPassword") or os.getenv("MICROSOFT_APP_PASSWORD") or "").strip()
MICROSOFT_APP_TENANT = (os.getenv("MicrosoftAppTenantId") or os.getenv("MICROSOFT_APP_TENANT_ID") or "").strip()

logger.info(
    "[boot] BF creds -> app_tail=%s pwd_len=%s tenant=%s",
    (MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None),
    (len(MICROSOFT_APP_PASSWORD) if MICROSOFT_APP_PASSWORD else 0),
    MICROSOFT_APP_TENANT or "<default>",
)

def _msal_selftest() -> Dict[str, Any]:
    try:
        import msal  # type: ignore
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
    out = {"ok": ok, "expires_in": res.get("expires_in"), "error": res.get("error")}
    return out

_selftest = _msal_selftest()
logger.info(
    "[boot] creds_selftest=%s | app_tail=%s | tenant=%s | expires_in=%s | error=%s",
    "OK" if _selftest.get("ok") else "FAIL",
    MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
    MICROSOFT_APP_TENANT or "<default>",
    _selftest.get("expires_in"),
    _selftest.get("error"),
)

# =========================================
#  Adapter y estado
# =========================================
_adapter = None
conversation_state = None
user_state = None
conv_accessor = None

if BotFrameworkAdapterSettings and BotFrameworkAdapter and MICROSOFT_APP_ID and MICROSOFT_APP_PASSWORD:
    _settings = BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)

    # Tenant para validación/adquisición de tokens (si está definido)
    try:
        setattr(_settings, "channel_auth_tenant", MICROSOFT_APP_TENANT or "botframework.com")
    except Exception:
        pass

    # Scope correcto para canales públicos
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

    # Estado en memoria (simple y suficiente para "last_seen")
    if MemoryStorage and ConversationState and UserState:
        storage = MemoryStorage()
        conversation_state = ConversationState(storage)
        user_state = UserState(storage)
        conv_accessor = conversation_state.create_property("conv")

# Mantener última referencia de conversación para pruebas proactivas
LAST_REF: Dict[str, Optional[ConversationReference]] = {"ref": None}

# =========================================
#  Bot App
# =========================================
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
        try:
            su = getattr(turn_context.activity, "service_url", None)
            if su:
                MicrosoftAppCredentials.trust_service_url(su)
                logger.info("trusted serviceUrl=%s", su)

            # --- Estado mínimo (last_seen) ---
            if conv_accessor and conversation_state:
                conv = await conv_accessor.get(turn_context, {})  # default dict
                if not isinstance(conv, dict):
                    conv = {}
                last_seen = conv.get("last_seen")
                conv["last_seen"] = time.time()
                await conv_accessor.set(turn_context, conv)

            # --- Respuesta ---
            if text in ("hi", "hello", "hola"):
                reply_text = "¡Hola! Soy AdmInfraBot. ¿En qué te ayudo?"
            else:
                reply_text = f"Recibí: {text_raw}"

            res = await turn_context.send_activity(MessageFactory.text(reply_text))
            sent_id = getattr(res, "id", None)
            logger.info("on_message_activity OUT | sent_id=%s", sent_id)

            # Guardar estado (manual, sin AutoSaveStateMiddleware)
            if conversation_state:
                await conversation_state.save_changes(turn_context, force=False)
            if user_state:
                await user_state.save_changes(turn_context, force=False)

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
        # Guardar last_seen al unirse
        if conv_accessor and conversation_state:
            conv = await conv_accessor.get(turn_context, {})
            if not isinstance(conv, dict):
                conv = {}
            conv["last_seen"] = time.time()
            await conv_accessor.set(turn_context, conv)
            await conversation_state.save_changes(turn_context, force=False)

        for m in (members_added or []):
            if m.id != turn_context.activity.recipient.id:
                await turn_context.send_activity("Bienvenido/a a AdmInfraBot 👋")

_bot_instance = AdmInfraBot()

# =========================================
#  /api/messages (BF endpoint)
# =========================================
@app.post("/api/messages")
async def messages(request: Request):
    if _adapter is None or Activity is None:
        logger.error("Uso de /api/messages sin Bot Framework instalado.")
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

    # Log básico de actividad
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

    # Confiar serviceUrl pronto
    try:
        if getattr(activity, "service_url", None):
            MicrosoftAppCredentials.trust_service_url(activity.service_url)
    except Exception:
        pass

    # Guardar ConversationReference (objeto real)
    try:
        ref: ConversationReference = TurnContext.get_conversation_reference(activity)
        LAST_REF["ref"] = ref
        logger.info(
            "ConversationReference almacenada (tipo=%s) conv.id=%s | su=%s | ch=%s",
            type(ref).__name__,
            getattr(getattr(ref, "conversation", None), "id", None),
            getattr(ref, "service_url", None),
            getattr(ref, "channel_id", None),
        )
    except Exception as e:
        logger.warning("No se pudo almacenar ConversationReference: %s", e)

    auth_header = request.headers.get("Authorization", "")
    logger.info("BF auth header present=%s", bool(auth_header))

    # Procesar actividad
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

# =========================================
#  Diagnóstico & Dev
# =========================================
diag = APIRouter()

def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        # pad base64
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
        keys = ("appid", "aud", "iss", "exp")
        out = {k: payload.get(k) for k in keys if k in payload}
        if "exp" in out:
            out["exp_in"] = int(out["exp"]) - int(time.time())
        return out
    except Exception:
        return {}

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
    try:
        import msal  # type: ignore
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
    if "access_token" not in res:
        return {"ok": False, "error": res.get("error"), "error_description": res.get("error_description")}
    claims = _decode_jwt_payload(res["access_token"])
    return {
        "ok": True,
        "env_app_id_tail": MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
        "token_claims": claims,
        "tenant_effective": tenant,
    }

@diag.get("/dev/ref")
def dev_ref():
    ref = LAST_REF.get("ref")
    if ref is None:
        return {"kind": "NoneType", "preview": "None"}
    try:
        preview = {
            "conversation_id": getattr(getattr(ref, "conversation", None), "id", None),
            "service_url": getattr(ref, "service_url", None),
            "channel_id": getattr(ref, "channel_id", None),
            "user_id": getattr(getattr(ref, "user", None), "id", None),
            "bot_id": getattr(getattr(ref, "bot", None), "id", None),
        }
        return {"kind": type(ref).__name__, "preview": json.dumps(preview)}
    except Exception:
        return {"kind": type(ref).__name__, "preview": "<unserializable>"}

@diag.post("/dev/ping")
async def dev_ping():
    if _adapter is None:
        raise HTTPException(status_code=500, detail="Adapter no inicializado")
    ref = LAST_REF.get("ref")
    if not ref:
        raise HTTPException(status_code=409, detail="Aún no se ha recibido ninguna conversación.")
    logger.info(
        "dev_ping usando ref conv.id=%s | su=%s | ch=%s",
        getattr(getattr(ref, "conversation", None), "id", None),
        getattr(ref, "service_url", None),
        getattr(ref, "channel_id", None),
    )
    async def _send(ctx: "TurnContext"):
        su = getattr(ctx.activity, "service_url", None)
        if su:
            MicrosoftAppCredentials.trust_service_url(su)
        await ctx.send_activity("pong ✅ (desde /dev/ping)")
        # guardado manual por si acaso
        if conversation_state:
            await conversation_state.save_changes(ctx, force=False)
        if user_state:
            await user_state.save_changes(ctx, force=False)
    # Firma habitual (bot_id, reference, logic)
    await _adapter.continue_conversation(MICROSOFT_APP_ID, ref, _send)
    return {"ok": True}

app.include_router(diag)

# =========================================
#  Endpoints de salud
# =========================================
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

# =========================================
#  Endpoints SDP
# =========================================
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

# =========================
#  DEBUG SDP (opcional)
# =========================
logger.info("[SDP DEBUG] sdp_auth cargado desde: /app/modules/sdp_auth.py")
logger.info("[SDP DEBUG] SDP_URL=%s KEY_PRESENT=%s", SDP_URL, True)
