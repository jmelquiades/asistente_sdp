# asistente_sdp/app/modules/html_utils.py
from bs4 import BeautifulSoup

def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Opcional: eliminar scripts/estilos
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Normaliza espacios y saltos
    lines = [l.strip() for l in text.splitlines()]
    chunks = [l for l in lines if l]
    return "\n".join(chunks)
