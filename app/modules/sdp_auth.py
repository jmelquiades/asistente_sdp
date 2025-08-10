# asistente_sdp/app/modules/sdp_auth.py
import os
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv

# Carga variables de entorno (.env) antes de usarlas
load_dotenv()

def _conf():
    """
    Lee y valida la configuración de conexión a ServiceDesk Plus (SDP).
    Retorna (SDP_URL, SDP_API_KEY). Lanza error si falta alguno.
    """
    url = os.getenv("SDP_URL", "").rstrip("/")
    key = os.getenv("SDP_API_KEY")
    if not url or not key:
        raise RuntimeError(
            f"[SDP CONF] Faltan variables. SDP_URL='{url}' SDP_API_KEY presente={bool(key)}"
        )
    return url, key

def _base_headers():
    """
    Encabezados base para SDP v3 según documentación oficial.
    """
    _, key = _conf()
    return {
        "Accept": "application/vnd.manageengine.sdp.v3+json",
        "Authorization": f"authtoken: {key}",
        # Compatibilidad con variantes:
        "TECHNICIAN_KEY": key,
    }

def sdp_get(endpoint, params=None, timeout=20):
    """
    GET contra la API de SDP (retorna JSON o lanza error con cuerpo detallado).
    """
    url, _ = _conf()
    full = f"{url}{endpoint}"
    headers = _base_headers()
    r = requests.get(full, headers=headers, params=params, timeout=timeout)
    if not r.ok:
        # ❗ mostrar CUERPO del error (lo que necesitamos para destrabar)
        raise RuntimeError(f"[SDP HTTP GET] {r.status_code}: {r.text}")
    return r.json()

def sdp_post_json(endpoint, payload, timeout=20):
    url, _ = _conf()
    full = f"{url}{endpoint}"
    headers = _base_headers()
    headers["Content-Type"] = "application/json"
    r = requests.post(full, headers=headers, json=payload, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"[SDP HTTP] {r.status_code}: {r.text}")
    return r.json()

def sdp_post_form(endpoint, form_data: dict, timeout=20):
    url, _ = _conf()
    full = f"{url}{endpoint}"
    headers = _base_headers()
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    data = urlencode(form_data).encode()
    r = requests.post(full, headers=headers, data=data, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"[SDP HTTP] {r.status_code}: {r.text}")
    return r.json()

def sdp_post_multipart(endpoint, fields: dict, timeout=20):
    url, _ = _conf()
    full = f"{url}{endpoint}"
    headers = _base_headers()  # requests define boundary
    files = {k: (None, v) for k, v in fields.items()}
    r = requests.post(full, headers=headers, files=files, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"[SDP HTTP] {r.status_code}: {r.text}")
    return r.json()

# Diagnóstico al cargar el módulo
print(f"[SDP DEBUG] sdp_auth cargado desde: {__file__}")
print(f"[SDP DEBUG] SDP_URL={os.getenv('SDP_URL')} KEY_PRESENT={bool(os.getenv('SDP_API_KEY'))}")
