# app/main.py
"""
API puente para integrar un bot conversacional con ServiceDesk Plus.
Incluye logging diario con retención de 60 días, trazabilidad y envío proactivo.
Compatibilidad Single-Tenant / Multi-Tenant mediante MICROSOFT_APP_TENANT_ID.
"""

import os
import json
import base64
import logging
from datetime import datetime
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Body, APIRouter
from fastapi.responses import Response

# --- Cargar variables de entorno ---
load_dotenv()

# --- Módulos propios (SDP + Trazabilidad) ---
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
from app.modules.trazabilidad import log_exec, list_recent

# --- Feature flags ---
DEV_TRACE_ENABLED = os.getenv("DEV_TRACE_ENABLED", "false").lower() in ("1", "true", "yes")

# --- Logging (rotación diaria + stdout) ---
LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_filename = LOG_DIR / "app.log"
file_handler = TimedRotatingFileHandler(filename=log_filename, when="midnight", interval=1, backupCount=60, encoding="utf-8")
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s", "%Y-%m-%d %H:%M:%S")
file_handler.setFormatter(formatter)
logger = logging.getLogger("asistente_sdp")
logger.setLevel(logging.INFO)
if not any(isinstance(h, TimedRotatingFileHandler) for h in logger.handlers):
    logger.addHandler(file_handler)
sh = logging.StreamHandler()
sh.setFormatter(formatter)
logger.addHandler(sh)

# --- App FastAPI ---
app = FastAPI(title="Asistente SDP - API puente", version="1.7.2")

# ============================================================================
# Bot Framework (SDK v4)
# ============================================================================
try:
    from botbuilder.core import (
        BotFrameworkAdapter,
        BotFrameworkAdapterSettings,
        TurnContext,
        ActivityHandler,
        MessageFactory,
    )
    from botbuilder.schema import Activity, ConversationReference, ConversationAccount, ChannelAccount
    try:
        from botframework.connector.auth import AuthenticationError  # type: ignore
    except Exception:
        class AuthenticationError(Exception): ...
    try:
        from botframework.connector.auth import MicrosoftAppCredentials  # type: ignore
    except Exception:
        class MicrosoftAppCredentials:
            @staticmethod
            def trust_service_url(url: str) -> None: ...
except Exception:
    BotFrameworkAdapter = None
    BotFrameworkAdapterSettings = None
    TurnContext = None
    Activity = None
    ConversationReference = None
    ConversationAccount = None
    ChannelAccount = None
    ActivityHandler = object
    MessageFactory = None
    class AuthenticationError(Exception): ...
    class MicrosoftAppCredentials:
        @staticmethod
        def trust_service_url(url: str) -> None: ...
    logger.warning("botbuilder-core/schema no disponibles. Instala dependencias del Bot Framework.")

# --- Credenciales + Tenant ---
MICROSOFT_APP_ID = (os.getenv("MicrosoftAppId") or os.getenv("MICROSOFT_APP_ID") or "").strip()
MICROSOFT_APP_PASSWORD = (os.getenv("MicrosoftAppPassword") or os.getenv("MICROSOFT_APP_PASSWORD") or "").strip()
# Single-Tenant: GUID de tu directorio; Multi-Tenant: 'botframework.com'
MICROSOFT_APP_TENANT_ID = (
    os.getenv("MicrosoftAppTenantId")
    or os.getenv("MICROSOFT_APP_TENANT_ID")
    or "botframework.com"
).strip()

logger.info(
    "[boot] BF creds -> app_tail=%s pwd_len=%s tenant=%s",
    (MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None),
    (len(MICROSOFT_APP_PASSWORD) if MICROSOFT_APP_PASSWORD else 0),
    MICROSOFT_APP_TENANT_ID,
)

# --- Autotest de credenciales (MSAL) ---
def _creds_selftest() -> bool:
    try:
        import msal
        authority = f"https://login.microsoftonline.com/{MICROSOFT_APP_TENANT_ID}"
        scope = ["https://api.botframework.com/.default"]
        cca = msal.ConfidentialClientApplication(
            client_id=MICROSOFT_APP_ID, client_credential=MICROSOFT_APP_PASSWORD, authority=authority
        )
        res = cca.acquire_token_for_client(scopes=scope)
        ok = "access_token" in res
        logger.info(
            "[boot] creds_selftest=%s | app_tail=%s | tenant=%s | expires_in=%s | error=%s",
            "OK" if ok else "FAIL",
            MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
            MICROSOFT_APP_TENANT_ID,
            res.get("expires_in"),
            (res.get("error_description") or res.get("error"))[:180] if not ok else None,
        )
        return ok
    except Exception as e:
        logger.exception("[boot] creds_selftest EXCEPTION: %s", e)
        return False

CREDS_OK_AT_BOOT = _creds_selftest()

# --- Adapter ---
_adapter = None
if BotFrameworkAdapterSettings and BotFrameworkAdapter:
    _settings = BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD)
    try:
        setattr(_settings, "channel_auth_tenant", MICROSOFT_APP_TENANT_ID)  # GUID si Single-Tenant
    except Exception:
        pass
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

# --- Conversation reference ---
LAST_REF = {"ref": None}  # dict | None

def _serialize_ref(activity: Activity):
    """Guarda ConversationReference como dict (serializable) y loguea el tipo."""
    try:
        ref = TurnContext.get_conversation_reference(activity)
        if ref:
            LAST_REF["ref"] = ref.as_dict()
            logger.info("ConversationReference almacenada (tipo=%s) conv.id=%s",
                        type(LAST_REF["ref"]).__name__,
                        getattr(getattr(activity, 'conversation', None), 'id', None))
    except Exception as e:
        logger.warning("No se pudo guardar ConversationReference: %s", e)

def _deserialize_ref_any(ref_any) -> "ConversationReference":
    """Convierte dict/JSON string a ConversationReference."""
    if isinstance(ref_any, str):
        try:
            ref_any = json.loads(ref_any)
        except Exception:
            raise HTTPException(status_code=409, detail="Referencia corrupta. Escribe 'hola' y reintenta.")
    if not isinstance(ref_any, dict):
        raise HTTPException(status_code=409, detail="Referencia inválida. Envía un mensaje al bot y reintenta.")
    return ConversationReference().deserialize(ref_any)

# --- Bot handler ---
class AdmInfraBot(ActivityHandler):
    async def on_message_activity(self, turn_context: "TurnContext"):
        text = (turn_context.activity.text or "").strip()
        logger.info(
            "on_message_activity IN | ch=%s | serviceUrl=%s | text=%s",
            getattr(turn_context.activity, "channel_id", None),
            getattr(turn_context.activity, "service_url", None),
            text.lower(),
        )
        try:
            su = getattr(turn_context.activity, "service_url", None)
            if su:
                MicrosoftAppCredentials.trust_service_url(su)
                logger.info("trusted serviceUrl=%s", su)

            reply_text = "¡Hola! Soy AdmInfraBot. ¿En qué te ayudo?" if text.lower() in ("hi","hello","hola") else f"eco: {text}"
            res = await turn_context.send_activity(MessageFactory.text(reply_text))
            logger.info("on_message_activity OUT | sent_id=%s", getattr(res, "id", None))

            try:
                log_exec(endpoint="/api/messages", action="bf_sent", params={"id": getattr(res, "id", None)}, ok=True)
            except Exception:
                pass
        except Exception as e:
            logger.exception("send_activity failed: %s", e)
            if "Unauthorized" in str(e) or "access_token" in str(e):
                logger.error("[hint] Revisa AppId/Secret y TENANT. Si el Bot es Single-Tenant, fija MicrosoftAppTenantId=<TU_TENANT_GUID>.")
            try:
                log_exec(endpoint="/api/messages", action="bf_send_error", ok=False, message=str(e))
            except Exception:
                pass
            raise

    async def on_members_added_activity(self, members_added, turn_context: "TurnContext"):
        su = getattr(turn_context.activity, "service_url", None)
        if su:
            MicrosoftAppCredentials.trust_service_url(su)
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

    # Log básico
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

    # Guardar ConversationReference como dict
    _serialize_ref(activity)

    auth_header = request.headers.get("Authorization", "")
    logger.info("BF auth header present=%s", bool(auth_header))

    try:
        invoke_response = await _adapter.process_activity(activity, auth_header, lambda ctx: _bot_instance.on_turn(ctx))
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
            return Response(content=json.dumps(body_obj), media_type="application/json", status_code=status_code)
        if isinstance(body_obj, str):
            return Response(content=body_obj, media_type="text/plain", status_code=status_code)
        return Response(status_code=status_code)

    return Response(status_code=200)

# ============================================================================
# Diagnóstico / Developer
# ============================================================================
diag = APIRouter()

@diag.get("/health/botcreds")
def health_bot_creds():
    return {
        "has_app_id": bool(MICROSOFT_APP_ID),
        "app_id_tail": MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
        "has_password": bool(MICROSOFT_APP_PASSWORD),
        "pwd_len": len(MICROSOFT_APP_PASSWORD) if MICROSOFT_APP_PASSWORD else 0,
        "creds_selftest_ok": CREDS_OK_AT_BOOT,
    }

@diag.get("/dev/test_token")
def dev_test_token():
    try:
        import msal
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MSAL no disponible: {e}")
    authority = f"https://login.microsoftonline.com/{MICROSOFT_APP_TENANT_ID}"
    scope = ["https://api.botframework.com/.default"]
    cca = msal.ConfidentialClientApplication(
        client_id=MICROSOFT_APP_ID, client_credential=MICROSOFT_APP_PASSWORD, authority=authority
    )
    res = cca.acquire_token_for_client(scopes=scope)
    safe = {k: v for k, v in res.items() if k != "access_token"}
    safe["has_access_token"] = "access_token" in res
    return safe

@diag.get("/dev/whoami")
def dev_whoami():
    try:
        import msal
        authority = f"https://login.microsoftonline.com/{MICROSOFT_APP_TENANT_ID}"
        scope = ["https://api.botframework.com/.default"]
        cca = msal.ConfidentialClientApplication(
            client_id=MICROSOFT_APP_ID, client_credential=MICROSOFT_APP_PASSWORD, authority=authority
        )
        res = cca.acquire_token_for_client(scopes=scope)
        if "access_token" not in res:
            return {"ok": False, "error": res.get("error"), "error_description": res.get("error_description")}
        token = res["access_token"]
        def _b64d(part):
            rem = len(part) % 4
            if rem: part += "=" * (4 - rem)
            return json.loads(base64.urlsafe_b64decode(part.encode()).decode())
        header, payload = token.split(".")[0:2]
        claims = _b64d(payload)
        return {
            "ok": True,
            "env_app_id_tail": MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
            "token_claims": {"appid": claims.get("appid"), "aud": claims.get("aud"), "iss": claims.get("iss"), "exp_in": res.get("expires_in")},
            "tenant_effective": MICROSOFT_APP_TENANT_ID,
        }
    except Exception as e:
        logger.exception("/dev/whoami failed: %s", e)
        return {"ok": False, "error": str(e)}

@diag.get("/dev/config")
def dev_config():
    return {
        "app_id_tail": MICROSOFT_APP_ID[-6:] if MICROSOFT_APP_ID else None,
        "tenant_effective": MICROSOFT_APP_TENANT_ID,
        "dev_trace_enabled": DEV_TRACE_ENABLED,
    }

@diag.get("/dev/ref")
def dev_ref():
    ref = LAST_REF["ref"]
    kind = type(ref).__name__
    # serializa seguro para preview
    try:
        preview = json.dumps(ref, ensure_ascii=False) if isinstance(ref, dict) else str(ref)
        preview = (preview or "")[:500]
    except Exception as e:
        preview = f"<no-preview: {e}>"
    return {"kind": kind, "preview": preview}

app.include_router(diag)

# ============================================================================
# Proactivo (solo si DEV_TRACE_ENABLED)
# ============================================================================
if DEV_TRACE_ENABLED:
    @app.post("/dev/ping")
    async def dev_ping():
        if not LAST_REF["ref"]:
            raise HTTPException(status_code=409, detail="Aún no se ha recibido ninguna conversación.")

        # Siempre deserializa a ConversationReference
        ref_any = LAST_REF["ref"]
        if isinstance(ref_any, str):
            try:
                ref_any = json.loads(ref_any)
            except Exception:
                raise HTTPException(status_code=409, detail="Referencia corrupta. Escribe 'hola' y reintenta.")
        if not isinstance(ref_any, dict):
            raise HTTPException(status_code=409, detail="Referencia inválida (esperado dict).")

        ref = ConversationReference().deserialize(ref_any)
        logger.info("dev_ping usando ref tipo=%s conv.id=%s",
                    type(ref).__name__, getattr(getattr(ref, 'conversation', None), 'id', None))

        if getattr(ref, "service_url", None):
            MicrosoftAppCredentials.trust_service_url(ref.service_url)

        async def _send(ctx: TurnContext):
            res = await ctx.send_activity("pong ✅ (desde /dev/ping)")
            logger.info("dev_ping sent_id=%s", getattr(res, "id", None))

        await _adapter.continue_conversation(MICROSOFT_APP_ID, ref, _send)
        return {"ok": True}

# Webhook ejemplo para notificar "Resuelto"
@app.post("/notify")
async def notify(payload: dict = Body(...)):
    if not LAST_REF["ref"]:
        raise HTTPException(status_code=409, detail="Sin referencia de conversación almacenada.")

    ref_any = LAST_REF["ref"]
    if isinstance(ref_any, str):
        try:
            ref_any = json.loads(ref_any)
        except Exception:
            raise HTTPException(status_code=409, detail="Referencia corrupta. Escribe 'hola' y reintenta.")
    if not isinstance(ref_any, dict):
        raise HTTPException(status_code=409, detail="Referencia inválida (esperado dict).")

    ref = ConversationReference().deserialize(ref_any)
    if getattr(ref, "service_url", None):
        MicrosoftAppCredentials.trust_service_url(ref.service_url)

    async def _send(ctx: TurnContext):
        msg = f"El ticket #{payload.get('ticketId')} pasó a {payload.get('status', 'Resuelto')} ✅"
        await ctx.send_activity(msg)

    await _adapter.continue_conversation(MICROSOFT_APP_ID, ref, _send)
    return {"ok": True}

# ============================================================================
# Endpoints existentes (health + intents + meta)
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
        raise HTTPException(status_code=502, detail=f"SDP error: {e}")

@app.post("/intents/create")
def intent_create(subject: str, description: str, email: str):
    logger.info(f"Creando ticket | requester={email} | subject={subject}")
    try:
        res = create_ticket(email, subject, description)
        log_exec(endpoint="/intents/create", email=email, action="create", params={"subject": subject}, ok=True)
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
