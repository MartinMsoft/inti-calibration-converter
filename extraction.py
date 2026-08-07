import base64
import io
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import anthropic
from pdf2image import convert_from_bytes, pdfinfo_from_bytes

logger = logging.getLogger("inti_converter")

MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_PRECISE = "claude-sonnet-4-6"

MAX_WORKERS = 5
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
DEFAULT_DPI = 150

# ── Helpers de PDF/imagen ─────────────────────────────────────────────────────


def get_pdf_page_count(pdf_bytes: bytes) -> int:
    info = pdfinfo_from_bytes(pdf_bytes)
    return int(info["Pages"])


def render_pdf_page(pdf_bytes: bytes, page_num: int, dpi: int = DEFAULT_DPI):
    """Renderiza UNA sola pagina del PDF por vez. Convertir el documento
    entero de una sola vez (30+ paginas) mantiene todas las imagenes
    decodificadas en memoria al mismo tiempo, lo cual hace crashear la app
    por falta de RAM en el plan free de Render. Renderizando bajo demanda,
    solo hay como maximo MAX_WORKERS imagenes en memoria en un momento dado."""
    pages = convert_from_bytes(pdf_bytes, dpi=dpi, first_page=page_num, last_page=page_num)
    return pages[0]


def image_to_base64(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


# ── Prompt ───────────────────────────────────────────────────────────────────

BASE_PROMPT = """Esta imagen es una pagina de una tabla de calibracion de tanque industrial certificada por INTI (Argentina).

La tabla tiene este formato:
- Primera columna: valor base en mm (0, 10, 20, 30, ...)
- Columnas 0 a 9: los 10 mm individuales de esa fila (base+0 a base+9)
- Valores en dm3

Extrae TODOS los datos en este JSON exacto:
{
  "rows": [
    {"base_mm": 0, "values": [v0, v1, v2, v3, v4, v5, v6, v7, v8, v9]},
    {"base_mm": 10, "values": [v0, v1, v2, v3, v4, v5, v6, v7, v8, v9]}
  ]
}

Reglas CRITICAS:

NUMEROS - el punto (.) es separador de MILES, NO decimal:
  788.068   -> entero 788068
  1.160.362 -> entero 1160362
  Si hay DOS o mas puntos, siempre es entero: quita los puntos.
  Si hay UN solo punto y el numero es menor a 10000, puede ser decimal: 27.344

FORMATO:
  Cada fila tiene EXACTAMENTE 10 valores (null si la celda esta vacia).
  Si la pagina no tiene tabla (caratula, texto, firma), devuelve {"rows": []}.
  IGNORAR numeros de pagina, encabezados, pies, firmas, sellos.
  Responde UNICAMENTE con el JSON, sin texto adicional ni bloques de codigo."""


def make_retry_prompt(expected_start: int, expected_end: int,
                       prev_mm: Optional[int] = None, prev_vol: Optional[float] = None) -> str:
    prompt = BASE_PROMPT + f"""

ATENCION ESPECIAL:
Esta pagina debe contener datos para mm aproximadamente {expected_start} a {expected_end}.
Revisa cada digito con maxima precision. Es una calibracion industrial critica."""
    if prev_mm is not None:
        prompt += f"""
El valor anterior confirmado fue mm={prev_mm}, volumen={prev_vol:.0f} dm3.
Los valores de esta pagina deben ser mayores en la misma escala."""
    return prompt


# ── Extraccion de una pagina (con reintento ante error transitorio) ──────────


def _call_claude(client: anthropic.Anthropic, image_b64: str, model: str, prompt: str) -> list[dict]:
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    if not raw.startswith("{"):
        idx = raw.find("{")
        if idx != -1:
            raw = raw[idx:]
    data = json.loads(raw)
    return data.get("rows", [])


def extract_page(client: anthropic.Anthropic, image, page_num: int,
                  model: str = MODEL_FAST, prompt: Optional[str] = None) -> list[dict]:
    """Extrae una pagina ya renderizada. Reintenta con backoff ante errores
    transitorios (red, rate-limit, JSON malformado); tras agotar los
    intentos devuelve lista vacia y deja constancia en el log."""
    prompt = prompt or BASE_PROMPT
    image_b64 = image_to_base64(image)
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return _call_claude(client, image_b64, model, prompt)
        except Exception as e:
            last_err = e
            logger.warning("Pagina %d intento %d/%d fallo: %s", page_num, attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    logger.error("Pagina %d: se omite tras %d intentos (%s)", page_num, MAX_RETRIES, last_err)
    return []


def extract_pdf_page(client: anthropic.Anthropic, pdf_bytes: bytes, page_num: int,
                      model: str = MODEL_FAST, prompt: Optional[str] = None,
                      dpi: int = DEFAULT_DPI) -> list[dict]:
    """Renderiza la pagina bajo demanda dentro del hilo de trabajo y la
    descarta apenas termina de usarla (no la retiene en memoria)."""
    image = render_pdf_page(pdf_bytes, page_num, dpi)
    try:
        return extract_page(client, image, page_num, model, prompt)
    finally:
        del image


# ── Ejecucion concurrente ─────────────────────────────────────────────────────

ExtractJob = tuple[int, bytes, str, str]  # (page_num, pdf_bytes, prompt, model)


def run_extractions(client: anthropic.Anthropic, jobs: list[ExtractJob],
                     on_done: Optional[Callable[[int, list[dict]], None]] = None) -> dict[int, list[dict]]:
    """Corre extract_pdf_page para cada job en paralelo (hasta MAX_WORKERS a
    la vez). Cada worker renderiza su propia pagina bajo demanda, asi que en
    ningun momento hay mas de MAX_WORKERS imagenes decodificadas a la vez,
    sin importar cuantas paginas tenga el PDF.
    on_done(page_num, rows) se llama desde el hilo principal a medida que
    cada pagina termina, en el orden en que van completando (no el orden de
    la lista)."""
    results: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(extract_pdf_page, client, pdf_bytes, page_num, model, prompt): page_num
            for page_num, pdf_bytes, prompt, model in jobs
        }
        for future in as_completed(futures):
            page_num = futures[future]
            rows = future.result()
            results[page_num] = rows
            if on_done:
                on_done(page_num, rows)
    return results
