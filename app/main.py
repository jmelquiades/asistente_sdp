# app/main.py
"""
API puente para integrar un bot conversacional con ServiceDesk Plus.
Incluye logging diario con retención de 60 días, trazabilidad, estado de conversación
(last_seen) y envío proactivo (webhooks de notificación).
Compatibilidad Single-Tenant / Multi-Tenant mediante MICROSOFT_APP_TENANT_ID.

Version: 1.8.1
"""

import os
import json
import base64
import logging
import time
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
app = FastAPI(title="Asistente SDP - API puente", version="1.8.1")

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
        MemoryStorage,
        ConversationState,
        UserState,
        AutoSaveStateMiddleware,
        StatePropertyAccessor,
        BotStateSet,  # <--- NUEVO
    )
    from botbuilder.schema import (
        Activity,
        ConversationReference,
        ChannelAccount,
        ConversationAccount,
    )
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
    ActivityHandler = object
    MessageFactory = None
    MemoryStorage = None
    ConversationState = None
    UserState = None
    AutoSaveStateMiddleware = None
    StatePropertyAccessor = None
    BotStateSet = None

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

# --- Estado (memoria del proceso; en prod cambia a Redis/BD) ---
conv_accessor: "StatePropertyAccessor" = None  # type: ignore
user_accessor: "StatePropertyAccessor" = None  # type: ignore
if _adapter and MemoryStorage and ConversationState and UserState and AutoSaveStateMiddleware:
    _storage = MemoryStorage()  # TODO: en prod, reemplazar por Redis/Cosmos/Blob
    conversation_state = ConversationState(_storage)
    user_state = UserState(_storage)

    # >>> FIX: AutoSaveStateMiddleware recibe BotStateSet en esta versión <<<
    bot_state_set = BotStateSet(conversation_state, user_state)
    _adapter.use(AutoSaveStateMiddleware(bot_state_set))

    conv_accessor = conversation_state.create_property("convdata")
    user_accessor = user_state.create_property("userdata")
    logger.info("[state] MemoryStorage habilitado (convdata/userdata)")

# --- Conversation reference + fallbacks ---
LAST_REF = {
    "ref": None,
    "service_url": None,
    "channel_id": None,
}

def _serialize_ref(activity: Activity):
    try:
        ref_obj = TurnContext.get_conversation_reference(activity)
        if not ref_obj:
            return
        ref_dict = ref_obj.as_dict() or {}

        su_in = getattr(activity, "service_url", None)
        ch_in = getattr(activity, "channel_id", None)

        su_ref = ref_dict.get("serviceUrl") or ref_dict.get("service_url")
        ch_ref = ref_dict.get("channelId") or ref_dict.get("channel_id")

        su_final = su_in or su_ref
        ch_final = ch_in or ch_ref

        if su_final:
            ref_dict["serviceUrl"] = su_final
            ref_dict["service_url"] = su_final
            LAST_REF["service_url"] = su_final
        if ch_final:
            ref_dict["channelId"] = ch_final
            ref_dict["channel_id"] = ch_final
            LAST_REF["channel_id"] = ch_final

        LAST_REF["ref"] = ref_dict
        logger.info(
            "ConversationReference almacenada (tipo=dict) conv.id=%s | su=%s | ch=%s",
            (getattr(getattr(activity, 'conversation', None), 'id', None)),
            su_final,
            ch_final,
        )
    except Exception as e:
        logger.warning("No se pudo guardar ConversationReference: %s", e)

def _deserialize_ref_any(ref_any) -> "ConversationReference":
    if isinstance(ref_any, str):
        try:
            ref_any = json.loads(ref_any)
        except Exception:
            raise HTTPException(status_code=409, detail="Referencia corrupta. Escribe 'hola' y reintenta.")
    if not isinstance(ref_any, dict):
        raise HTTPException(status_code=409, detail="Referencia inválida. Envía un mensaje al bot y reintenta.")
    return ConversationReference().deserialize(ref_any)

def _extract_from_ref_dict(ref_dict: dict, *keys):
    for k in keys:
        if k in ref_dict and ref_dict[k]:
            return ref_dict[k]
    return None

def _complete_ref_from_raw_and_fallbacks(ref: "ConversationReference", ref_raw: dict):
    su = getattr(ref, "service_url", None) or _extract_from_ref_dict(ref_raw, "serviceUrl", "service_url") or LAST_REF.get("service_url")
    ch = getattr(ref, "channel_id", None) or _extract_from_ref_dict(ref_raw, "channelId", "channel_id") or LAST_REF.get("channel_id")
    if not getattr(ref, "service_url", None) and su:
        setattr(ref, "service_url", su)
    if not getattr(ref, "channel_id", None) and ch:
        setattr(ref, "channel_id", ch)
    if not getattr(ref, "conversation", None) and isinstance(ref_raw.get("conversation"), dict):
        setattr(ref, "conversation", ConversationAccount(id=ref_raw["conversation"].get("id")))
    if not getattr(ref, "bot", None) and isinstance(ref_raw.get("bot"), dict):
        setattr(ref, "bot", ChannelAccount(id=ref_raw["bot"].get("id"), name=ref_raw["bot"].get("name")))
    if not getattr(ref, "user", None) and isinstance(ref_raw.get("user"), dict):
        setattr(ref, "user", ChannelAccount(id=ref_raw["user"]["id"], name=ref_raw["user"].get("name")))
    return getattr(ref, "service_url", None), getattr(ref, "channel_id", None)

# --- Wrapper de compatibilidad para proactive ---
async def _continue_conversation_compat(ref_obj: "ConversationReference", logic):
    try:
        logger.info("[compat] Probando firma A: continue_conversation(ref, logic, bot_id=APP_ID)")
        return await _adapter.continue_conversation(ref_obj, logic, bot_id=MICROSOFT_APP_ID)
    except TypeError as te_a:
        logger.info("[compat] Firma A no aplica (%s). Probando firma B...", te_a)
    try:
        logger.info("[compat] Probando firma B: continue_conversation(APP_ID, ref, logic)")
        return await _adapter.continue_conversation(MICROSOFT_APP_ID, ref_obj, logic)
    except TypeError as te_b:
        logger.info("[compat] Firma B tampoco aplica (%s). Probando firma C...", te_b)
    logger.info("[compat] Probando firma C: continue_conversation(ref, logic)")
    return await _adapter.continue_conversation(ref_obj, logic)

# --- Construcción explícita del Activity proactivo ---
def _build_proactive_activity(ref_obj: "ConversationReference", ref_raw: dict, text: str) -> Activity:
    a = Activity(type="message")
    a.service_url = getattr(ref_obj, "service_url", None) or _extract_from_ref_dict(ref_raw, "serviceUrl", "service_url") or LAST_REF.get("service_url")
    a.channel_id = getattr(ref_obj, "channel_id", None) or _extract_from_ref_dict(ref_raw, "channelId", "channel_id") or LAST_REF.get("channel_id")
    a.conversation = getattr(ref_obj, "conversation", None) or (
        ConversationAccount(id=ref_raw["conversation"]["id"]) if isinstance(ref_raw.get("conversation"), dict) else None
    )
    a.from_property = getattr(ref_obj, "bot", None) or (
        ChannelAccount(id=ref_raw["bot"]["id"], name=ref_raw["bot"].get("name")) if isinstance(ref_raw.get("bot"), dict) else None
    )
    a.recipient = getattr(ref_obj, "user", None) or (
        ChannelAccount(id=ref_raw["user"]["id"], name=ref_raw["user"].get("name")) if isinstance(ref_raw.get("user"), dict) else None
    )
    a.text = text
    return a

# --- Bot handler ---
class AdmInfraBot(ActivityHandler):
    async def on_message_activity(self, turn_context: "TurnContext"):
        text_raw = (turn_context.activity.text or "").strip()
        text_low = text_raw.lower()
        logger.info(
            "on_message_activity IN | ch=%s | serviceUrl=%s | text=%s",
            getattr(turn_context.activity, "channel_id", None),
            getattr(turn_context.activity, "service_url", None),
            text_low,
        )

        su = getattr(turn_context.activity, "service_url", None)
        if su:
            MicrosoftAppCredentials.trust_service_url(su)
            logger.info("trusted serviceUrl=%s", su)

        if conv_accessor:
            conv = await conv_accessor.get(turn_context, default_factory=dict)
            now = time.time()
            last_seen = conv.get("last_seen")
            conv["last_seen"] = now

            INACTIVITY_SEC = 8 * 60
            if last_seen and (now - last_seen) >= INACTIVITY_SEC:
                flow = conv.get("flow")
                step = conv.get("step")
                if flow:
                    msg = f"¡Volviste! Han pasado {int((now - last_seen)/60)} min. ¿Seguimos con **{flow}** (paso {step}) o prefieres empezar algo nuevo?"
                else:
                    msg = f"¡Volviste! Han pasado {int((now - last_seen)/60)} min. ¿Seguimos donde nos quedamos o empezamos algo nuevo?"
                await turn_context.send_activity(MessageFactory.text(msg))

        reply_text = "¡Hola! Soy AdmInfraBot. ¿En qué te ayudo?" if text_low in ("hi","hello","hola") else f"eco: {text_raw}"
        res = await turn_context.send_activity(MessageFactory.text(reply_text))
        logger.info("on_message_activity OUT | sent_id=%s", getattr(res, "id", None))

        try:
            log_exec(endpoint="/api/messages", action="bf_sent", params={"id": getattr(res, "id", None)}, ok=True)
        except Exception:
            pass

    async def on_members_added_activity(self, members_added, turn_context: "TurnContext"):
        su = getattr(turn_context.activity, "service_url", None)
        if su:
            MicrosoftAppCredentials.trust_service_url(su)
        for m in (members_added or []):
            if m.id != turn_context.activity.recipient.id:
                if conv_accessor:
                    conv = await conv_accessor.get(turn_context, default_factory=dict)
                    conv["last_seen"] = time.time()
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
    try:
        preview = json.dumps(ref, ensure_ascii=False) if isinstance(ref, dict) else str(ref)
        preview = (preview or "")[:500]
    except Exception as e:
        preview = f"<no-preview: {e}>"
    return {
        "kind": kind,
        "preview": preview,
        "fallbacks": {"service_url": LAST_REF.get("service_url"), "channel_id": LAST_REF.get("channel_id")},
    }

app.include_router(diag)

# ============================================================================
# Proactivo (solo si DEV_TRACE_ENABLED)
# ============================================================================
if DEV_TRACE_ENABLED:
    @app.post("/dev/ping")
    async def dev_ping():
        if not LAST_REF["ref"]:
            raise HTTPException(status_code=409, detail="Aún no se ha recibido ninguna conversación.")

        ref_any = LAST_REF["ref"]
        if isinstance(ref_any, str):
            try:
                ref_any = json.loads(ref_any)
            except Exception:
                raise HTTPException(status_code=409, detail="Referencia corrupta. Escribe 'hola' y reintenta.")
        if not isinstance(ref_any, dict):
            raise HTTPException(status_code=409, detail="Referencia inválida (esperado dict).")

        ref = ConversationReference().deserialize(ref_any)

        su, ch = _complete_ref_from_raw_and_fallbacks(ref, ref_any)
        logger.info("dev_ping usando ref conv.id=%s | su=%s | ch=%s",
                    getattr(getattr(ref, 'conversation', None), 'id', None), su, ch)

        if su:
            MicrosoftAppCredentials.trust_service_url(su)
        else:
            logger.error("[proactive] No hay service_url disponible (ref/any/fallback). Escribe al bot para refrescarla.")
            raise HTTPException(status_code=409, detail="Sin service_url para envío proactivo. Envía un mensaje al bot y reintenta.")

        async def _send(ctx: TurnContext):
            act = _build_proactive_activity(ref, ref_any, "pong ✅ (desde /dev/ping)")
            logger.info("[proactive] sending explicit activity: service_url=%s conv.id=%s",
                        getattr(act, "service_url", None),
                        getattr(getattr(act, "conversation", None), "id", None))
            await ctx.send_activity(act)

        await _continue_conversation_compat(ref, _send)
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
    su, ch = _complete_ref_from_raw_and_fallbacks(ref, ref_any)
    if su:
        MicrosoftAppCredentials.trust_service_url(su)
    else:
        raise HTTPException(status_code=409, detail="Sin service_url para envío proactivo. Envía un mensaje al bot y reintenta.")

    async def _send(ctx: TurnContext):
        msg = f"El ticket #{payload.get('ticketId')} pasó a {payload.get('status', 'Resuelto')} ✅"
        act = _build_proactive_activity(ref, ref_any, msg)
        logger.info("[proactive] sending explicit activity (notify): service_url=%s conv.id=%s",
                    getattr(act, "service_url", None),
                    getattr(getattr(act, "conversation", None), "id", None))
        await ctx.send_activity(act)

    await _continue_conversation_compat(ref, _send)
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
        logger.error(f"Error leyendo trazas: %s", e)
        raise HTTPException(status_code=500, detail="Trace read error")
