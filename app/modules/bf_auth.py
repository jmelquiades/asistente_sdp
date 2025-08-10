# app/modules/bf_auth.py
import base64
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("asistente_sdp.bf_auth")

# MSAL para obtener token
try:
    import msal  # type: ignore
except Exception:  # pragma: no cover
    msal = None  # evitamos romper import en entornos sin msal

# Confiar serviceUrl (evita 401 al enviar)
try:
    from botframework.connector.auth import MicrosoftAppCredentials  # type: ignore
except Exception:  # pragma: no cover
    class MicrosoftAppCredentials:  # fallback inocuo
        @staticmethod
        def trust_service_url(url: str) -> None:
            pass

AUTHORITY = "https://login.microsoftonline.com/botframework.com"
SCOPE = ["https://api.botframework.com/.default"]


def _b64url_to_json(segment: str) -> Dict[str, Any]:
    """Decodifica la parte payload de un JWT (sin validar firma)."""
    seg = segment + "==="  # padding seguro
    seg = seg.replace("-", "+").replace("_", "/")
    raw = base64.b64decode(seg)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def decode_jwt_noverify(token: str) -> Dict[str, Any]:
    """Devuelve el payload del JWT sin verificar firma (solo diagnóstico)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        return _b64url_to_json(parts[1])
    except Exception:
        return {}


def acquire_bf_token(app_id: str, app_secret: str) -> Dict[str, Any]:
    """Obtiene un token para Bot Framework con MSAL (client credentials)."""
    if not msal:
        return {"has_access_token": False, "error": "msal_not_available"}

    cca = msal.ConfidentialClientApplication(
        client_id=app_id,
        client_credential=app_secret,
        authority=AUTHORITY,
    )
    res = cca.acquire_token_for_client(scopes=SCOPE)
    out: Dict[str, Any] = {k: v for k, v in res.items() if k != "access_token"}
    token = res.get("access_token")
    out["has_access_token"] = bool(token)
    if token:
        claims = decode_jwt_noverify(token)
        out["token_appid"] = claims.get("appid") or claims.get("azp")
        out["token_appid_tail"] = (out["token_appid"][-6:] if out.get("token_appid") else None)
    return out


def trust_service_url(url: Optional[str]) -> None:
    """Marca la serviceUrl como confiable para envíos salientes."""
    if url:
        try:
            MicrosoftAppCredentials.trust_service_url(url)
            logger.info("bf_auth: trusted serviceUrl=%s", url)
        except Exception as e:  # pragma: no cover
            logger.warning("bf_auth: trust_service_url error: %s", e)


def diagnose_activity(activity: Any, app_id: str, app_secret: str) -> Dict[str, Any]:
    """
    Diagnóstico integral por actividad:
      - channelId, serviceUrl, recipientId
      - token AAD (has_access_token + appid del token)
      - alineación por tails (visual)
    """
    ch = getattr(activity, "channel_id", None)
    su = getattr(activity, "service_url", None)
    recipient = getattr(getattr(activity, "recipient", None), "id", None)

    trust_service_url(su)  # confía cuanto antes

    token_info = acquire_bf_token(app_id, app_secret)
    env_tail = (app_id[-6:] if app_id else None)

    diag = {
        "channelId": ch,
        "serviceUrl": su,
        "recipientId": recipient,
        "env_app_id_tail": env_tail,
        "token_has_access": token_info.get("has_access_token"),
        "token_app_id_tail": token_info.get("token_appid_tail"),
    }
    # Señal visual rápida
    diag["aligned_by_token_tail"] = (
        token_info.get("token_appid_tail") == env_tail and env_tail is not None
    )
    return diag
