# asistente_sdp/app/modules/sdp_actions.py
import os
import json
from typing import Any, Dict, List
from .sdp_auth import sdp_get, sdp_post_form, sdp_post_multipart
from .html_utils import html_to_text

# --- Config desde .env ---
SDP_DEFAULT_SITE_ID = os.getenv("SDP_DEFAULT_SITE_ID")
SDP_DEFAULT_SITE_NAME = os.getenv("SDP_DEFAULT_SITE_NAME")
SDP_TEMPLATE_ID = os.getenv("SDP_TEMPLATE_ID")
SDP_TEMPLATE_NAME = os.getenv("SDP_TEMPLATE_NAME")

ALLOW_TEMPLATE_FALLBACK = os.getenv("ALLOW_TEMPLATE_FALLBACK", "true").lower() in ("1", "true", "yes")


# ---------------------- Utilitarios ----------------------
def _extract_list_items(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extrae la lista de tickets u objetos desde una respuesta heterogénea de SDP.

    Parámetros:
        resp (dict): respuesta JSON recibida desde la API de SDP.

    Retorna:
        list: lista de elementos encontrada en la respuesta.
    """
    if not isinstance(resp, dict):
        return []
    for k in ("requests", "list", "data", "response"):
        v = resp.get(k)
        if isinstance(v, list):
            return v
    for v in resp.values():
        if isinstance(v, list):
            return v
    return []


def _apply_site_and_template(request_obj: Dict[str, Any], include_template: bool = True) -> None:
    """
    Inserta datos de site y/o template en un objeto request según configuración .env.

    Parámetros:
        request_obj (dict): objeto de ticket a modificar.
        include_template (bool): si True, incluye datos de template.

    Notas:
        - Usa IDs si están configurados; de lo contrario, usa nombres.
    """
    if SDP_DEFAULT_SITE_ID:
        try:
            request_obj["site"] = {"id": int(SDP_DEFAULT_SITE_ID)}
        except Exception:
            request_obj["site"] = {"id": SDP_DEFAULT_SITE_ID}
    elif SDP_DEFAULT_SITE_NAME:
        request_obj["site"] = {"name": SDP_DEFAULT_SITE_NAME}

    if include_template:
        if SDP_TEMPLATE_ID:
            try:
                request_obj["template"] = {"id": int(SDP_TEMPLATE_ID)}
            except Exception:
                request_obj["template"] = {"id": SDP_TEMPLATE_ID}
        elif SDP_TEMPLATE_NAME:
            request_obj["template"] = {"name": SDP_TEMPLATE_NAME}


# ---------------------- Anuncios ----------------------
def get_announcements() -> Dict[str, Any]:
    """
    Obtiene anuncios activos desde SDP y limpia el HTML a texto plano.

    Retorna:
        dict: estructura con lista de anuncios y campos normalizados.
    """
    raw = sdp_get("/api/v3/announcements")
    items = _extract_list_items(raw) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    anns: List[Dict[str, Any]] = []
    for it in items:
        title = it.get("title") or it.get("name") or "Anuncio"
        desc_html = it.get("description") or it.get("content") or ""
        anns.append({
            "title": title,
            "description_text": html_to_text(desc_html),
            "description_html_present": bool(desc_html),
            "status": it.get("status"),
            "start_time": it.get("start_time") or it.get("start_time_ms"),
            "end_time": it.get("end_time") or it.get("end_time_ms"),
            "id": it.get("id") or it.get("announcement_id"),
        })
    return {"announcements": anns}


# ---------------------- Tickets: crear / listar ----------------------
def create_ticket(requester_email: str, subject: str, description: str) -> Dict[str, Any]:
    """
    Crea un ticket en SDP usando x-www-form-urlencoded (form) como formato de envío.

    Parámetros:
        requester_email (str): correo del solicitante registrado en SDP.
        subject (str): asunto del ticket.
        description (str): descripción detallada del ticket.

    Retorna:
        dict: respuesta completa de la API SDP al crear el ticket.

    Notas:
        - Intenta crear con template si está configurado.
        - Si falla por template y ALLOW_TEMPLATE_FALLBACK=True, reintenta sin template.
    """
    def _do_create(include_template: bool) -> Dict[str, Any]:
        req: Dict[str, Any] = {
            "requester": {"email_id": requester_email},
            "subject": subject,
            "description": description
        }
        _apply_site_and_template(req, include_template=include_template)
        form = {"input_data": json.dumps({"request": req})}
        return sdp_post_form("/api/v3/requests", form)

    try:
        return _do_create(include_template=True)
    except Exception as e:
        if ALLOW_TEMPLATE_FALLBACK and "template" in str(e).lower():
            return _do_create(include_template=False)
        raise

def list_my_tickets(requester_email: str, page: int = 1, page_size: int = 25) -> Dict[str, Any]:
    """
    Lista tickets del solicitante usando una cascada robusta y compatible con On-Prem:
      V0) GET con input_data (patrón oficial del fabricante).
      V1) POST minimal + search_fields por email.
      V2) POST minimal + search_criteria (criteria + value) por email.
      V3) SIN filtros server-side: traer bloque y filtrar/ordenar en Python.

    Optimizaciones:
      - raw_row_count = 200 (mejor ventana para filtrar en cliente).
      - Orden local por created_time (desc) ANTES de paginar.

    Notas:
      - Puedes activar una vista del servidor con:
        "filter_by": {"id": "<ID_VISTA>"}
        dentro de list_info (V0/V1/V2).
    """
    start_index = 1 + (page - 1) * page_size

    # ---------- V0: GET con input_data (formato oficial) ----------
    input_v0 = {
        "list_info": {
            "row_count": page_size,
            "start_index": start_index,
            "sort_field": "created_time",
            "sort_order": "desc",
            "get_total_count": True,
            "search_fields": {"requester.email_id": requester_email}
            # "filter_by": {"id": "<ID_VISTA>"}  # opcional
        }
    }
    try:
        return sdp_get("/api/v3/requests", params={"input_data": json.dumps(input_v0)})
    except Exception:
        pass

    # ---------- V1: POST minimal con search_fields ----------
    form_v1 = {
        "input_data": json.dumps({
            "list_info": {
                "search_fields": {"requester.email_id": requester_email},
                "row_count": page_size,
                "start_index": start_index
                # "filter_by": {"id": "<ID_VISTA>"}  # opcional
            }
        })
    }
    try:
        return sdp_post_form("/api/v3/requests", form_v1)
    except Exception:
        pass

    # ---------- V2: POST minimal con search_criteria (criteria + value) ----------
    form_v2 = {
        "input_data": json.dumps({
            "list_info": {
                "search_criteria": {
                    "criteria": [
                        {"field": "requester.email_id", "condition": "is", "value": requester_email}
                    ],
                    "operator": "and"
                },
                "row_count": page_size,
                "start_index": start_index
                # "filter_by": {"id": "<ID_VISTA>"}  # opcional
            }
        })
    }
    try:
        return sdp_post_form("/api/v3/requests", form_v2)
    except Exception:
        pass

    # ---------- V3: Sin filtros server-side → filtrado + orden en cliente ----------
    raw_row_count = max(page_size * 5, 200)
    form_v3_pull = {"input_data": json.dumps({"list_info": {"row_count": raw_row_count, "start_index": 1}})}
    try:
        resp = sdp_post_form("/api/v3/requests", form_v3_pull)
    except Exception:
        input_get_min = {"list_info": {"row_count": raw_row_count, "start_index": 1}}
        resp = sdp_get("/api/v3/requests", params={"input_data": json.dumps(input_get_min)})

    items = _extract_list_items(resp) or []

    # Filtrar por email (case-insensitive)
    email_norm = requester_email.strip().lower()
    filtered = []
    for it in items:
        req = it if isinstance(it, dict) else {}
        requester = req.get("requester") or {}
        if (requester.get("email_id") or "").strip().lower() == email_norm:
            filtered.append(it)

    # Orden local por created_time (desc)
    def _created_value(o: Dict[str, Any]) -> int:
        ct = (o.get("created_time") or {})
        # SDP suele enviar "value" como epoch ms. Si no está, usamos 0.
        try:
            return int(ct.get("value") or 0)
        except Exception:
            return 0

    filtered.sort(key=_created_value, reverse=True)

    # Paginación en cliente
    total = len(filtered)
    s = (page - 1) * page_size
    e = s + page_size
    page_items = filtered[s:e]

    return {
        "list_info": {
            "row_count": len(page_items),
            "start_index": s + 1 if total else 1,
            "get_total_count": True,
            "total_count": total,
            "has_more_rows": e < total
        },
        "requests": page_items
    }



# ---------------------- Estado por display_id ----------------------
def _compact_from_request_obj(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normaliza un objeto de ticket de SDP a un formato compacto para el bot.

    Parámetros:
        obj (dict): objeto de ticket tal como lo retorna SDP.

    Retorna:
        dict: datos esenciales (display_id, subject, status, fechas, requester, técnico, site).
    """
    status = obj.get("status") or {}
    created = obj.get("created_time") or {}
    requester = obj.get("requester") or {}
    tech = obj.get("technician") or {}
    site = obj.get("site") or {}
    return {
        "display_id": str(obj.get("id") or obj.get("display_id")),
        "subject": obj.get("subject") or obj.get("short_description"),
        "status": status.get("name"),
        "status_id": status.get("id"),
        "created_time": created.get("display_value"),
        "requester_email": requester.get("email_id"),
        "technician": tech.get("name"),
        "site": site.get("name"),
    }


def get_ticket_status_by_display(display_id: str) -> Dict[str, Any]:
    """
    Devuelve el estado compacto de un ticket según su display_id.

    Parámetros:
        display_id (str): identificador visible para el usuario en SDP.

    Retorna:
        dict: objeto con ticket normalizado y datos raw.

    Notas:
        - Si el display_id es numérico, intenta GET directo a /requests/{id}.
        - Fallback: búsqueda vía GET con search_fields por display_id.
    """
    disp = str(display_id).strip()

    # 1) Intento directo por ID
    if disp.isdigit():
        try:
            r = sdp_get(f"/api/v3/requests/{disp}")
            req = r.get("request") if isinstance(r, dict) else None
            if isinstance(req, dict):
                return {"ticket": _compact_from_request_obj(req), "raw": r}
        except Exception:
            pass

    # 2) Fallback: búsqueda por display_id
    input_data = {
        "list_info": {
            "row_count": 1,
            "start_index": 1,
            "search_fields": {"display_id": disp}
        }
    }
    resp = sdp_get("/api/v3/requests", params={"input_data": json.dumps(input_data)})
    items = _extract_list_items(resp)
    if not items:
        raise RuntimeError(f"No se encontró ticket con display_id={display_id}")
    return {"ticket": _compact_from_request_obj(items[0]), "raw": {"list_response": resp}}


# ---------------------- Notas ----------------------
def add_note(ticket_id: int, requester_email: str, note_text: str) -> Dict[str, Any]:
    """
    Agrega una nota a un ticket en SDP, probando múltiples formatos de envío.

    Parámetros:
        ticket_id (int): ID interno del ticket en SDP.
        requester_email (str): correo del solicitante.
        note_text (str): contenido de la nota.

    Retorna:
        dict: respuesta de SDP tras agregar la nota.

    Notas:
        - Fallbacks automáticos:
            1) multipart + request_note.description
            2) multipart + request_note.content
            3) multipart + note.description
            4) x-www-form-urlencoded + request_note.description
    """
    try:
        note_obj = {
            "request_note": {
                "description": note_text,
                "show_to_requester": True,
                "add_to_linked_requests": False
            }
        }
        return sdp_post_multipart(f"/api/v3/requests/{ticket_id}/notes",
                                  {"input_data": json.dumps(note_obj)})
    except Exception:
        pass

    try:
        note_obj = {
            "request_note": {
                "content": note_text,
                "show_to_requester": True,
                "add_to_linked_requests": False
            }
        }
        return sdp_post_multipart(f"/api/v3/requests/{ticket_id}/notes",
                                  {"input_data": json.dumps(note_obj)})
    except Exception:
        pass

    try:
        note_obj = {
            "note": {
                "description": note_text,
                "show_to_requester": True
            }
        }
        return sdp_post_multipart(f"/api/v3/requests/{ticket_id}/notes",
                                  {"input_data": json.dumps(note_obj)})
    except Exception:
        pass

    note_obj = {
        "request_note": {
            "description": note_text,
            "show_to_requester": True,
            "add_to_linked_requests": False
        }
    }
    return sdp_post_form(f"/api/v3/requests/{ticket_id}/notes",
                         {"input_data": json.dumps(note_obj)})


def add_note_by_display_id(display_id: str, requester_email: str, note_text: str) -> Dict[str, Any]:
    """
    Agrega una nota a un ticket usando su display_id.

    Parámetros:
        display_id (str): identificador visible en SDP.
        requester_email (str): correo del solicitante.
        note_text (str): contenido de la nota.

    Retorna:
        dict: respuesta de SDP tras agregar la nota.

    Notas:
        - Si display_id es numérico, se usa directo como id interno.
        - Si no, se resuelve buscando el id por display_id.
    """
    disp = str(display_id).strip()
    if disp.isdigit():
        rid = int(disp)
    else:
        input_data = {
            "list_info": {
                "row_count": 1,
                "start_index": 1,
                "search_fields": {"display_id": disp}
            }
        }
        resp = sdp_get("/api/v3/requests", params={"input_data": json.dumps(input_data)})
        items = _extract_list_items(resp)
        if not items:
            raise RuntimeError(f"No se encontró ticket con display_id={display_id}")
        rid = int(items[0].get("id"))
    return add_note(rid, requester_email, note_text)


# ---------------------- Meta ----------------------
def list_sites() -> Dict[str, Any]:
    """
    Lista los sitios configurados en SDP.

    Retorna:
        dict: respuesta de SDP con todos los sitios.
    """
    return sdp_get("/api/v3/sites")


def list_request_templates() -> Dict[str, Any]:
    """
    Lista las plantillas de solicitud configuradas en SDP.

    Retorna:
        dict: respuesta de SDP con todas las plantillas.
    """
    return sdp_get("/api/v3/request_templates")

def get_requester_id(email: str) -> int:
    """
    Obtiene el ID interno del requester a partir del email (si tu build expone /api/v3/requesters).
    """
    input_data = {"list_info": {"row_count": 1, "start_index": 1, "search_fields": {"email_id": email}}}
    resp = sdp_get("/api/v3/requesters", params={"input_data": json.dumps(input_data)})
    items = _extract_list_items(resp)
    if not items or items[0].get("id") is None:
        raise RuntimeError(f"Requester no encontrado o sin ID para email={email}")
    return int(items[0]["id"])
