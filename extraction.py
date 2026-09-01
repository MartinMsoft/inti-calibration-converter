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

from formats import INTI_PROMPT

logger = logging.getLogger("inti_converter")

MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_PRECISE = "claude-sonnet-4-6"

MAX_WORKERS = 5
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5
DEFAULT_DPI = 150
RETRY_DPI = 300  # mas resolucion en los reintentos: pocas paginas, ya usan el modelo caro
MAX_TOKENS = 8192

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


# ── Extraccion de una pagina (con reintento ante error transitorio) ──────────


def parse_json_response(raw: str) -> list[dict]:
    """Extrae la lista "rows" de la respuesta del modelo. El prompt le pide
    al modelo verificar los primeros valores en texto plano ANTES del JSON
    (reduce lecturas apresuradas), asi que raw_decode ubica el JSON dentro
    del texto e ignora tanto lo que venga antes como lo que quede despues,
    en vez de asumir que el JSON es exactamente todo el contenido."""
    raw = raw.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    idx = raw.find("{")
    if idx == -1:
        raise ValueError(f"No se encontro JSON en la respuesta: {raw[:200]!r}")
    data, _end = json.JSONDecoder().raw_decode(raw, idx)
    return data.get("rows", [])


def _call_claude(client: anthropic.Anthropic, image_b64: str, model: str, prompt: str) -> list[dict]:
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return parse_json_response(response.content[0].text)


def extract_page(client: anthropic.Anthropic, image, page_num: int,
                  model: str = MODEL_FAST, prompt: Optional[str] = None) -> list[dict]:
    """Extrae una pagina ya renderizada. Reintenta con backoff ante errores
    transitorios (red, rate-limit, JSON malformado); tras agotar los
    intentos devuelve lista vacia y deja constancia en el log."""
    prompt = prompt or INTI_PROMPT
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

ExtractJob = tuple[int, bytes, str, str, int]  # (page_num, pdf_bytes, prompt, model, dpi)


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
            pool.submit(extract_pdf_page, client, pdf_bytes, page_num, model, prompt, dpi): page_num
            for page_num, pdf_bytes, prompt, model, dpi in jobs
        }
        for future in as_completed(futures):
            page_num = futures[future]
            rows = future.result()
            results[page_num] = rows
            if on_done:
                on_done(page_num, rows)
    return results
