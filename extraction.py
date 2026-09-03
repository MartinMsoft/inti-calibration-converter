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


def render_pdf_page_cropped(pdf_bytes: bytes, page_num: int, dpi: int,
                             crop_box: tuple[float, float, float, float]):
    """Renderiza la pagina y la recorta a crop_box (left, top, right, bottom
    como fracciones 0-1 del ancho/alto). Sirve para aislar un solo bloque de
    columnas de una tabla multi-columna (ej: Winter Service) cuando el
    modelo confunde la lectura con el layout completo de fondo -- una
    imagen mas simple y sin el resto de la tabla al lado."""
    image = render_pdf_page(pdf_bytes, page_num, dpi)
    w, h = image.size
    left, top, right, bottom = crop_box
    box_px = (int(w * left), int(h * top), int(w * right), int(h * bottom))
    return image.crop(box_px)


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


# ── Modo fila-por-fila (una fila = una llamada, sin filas vecinas visibles) ──


def parse_row_response(raw: str) -> tuple[Optional[int], Optional[float]]:
    """Extrae (cm, lts) de la respuesta de una extraccion de fila individual."""
    raw = raw.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    idx = raw.find("{")
    if idx == -1:
        raise ValueError(f"No se encontro JSON en la respuesta: {raw[:200]!r}")
    data, _end = json.JSONDecoder().raw_decode(raw, idx)
    return data.get("cm"), data.get("lts")


def _call_claude_row(client: anthropic.Anthropic, image_b64: str, model: str,
                      prompt: str) -> tuple[Optional[int], Optional[float]]:
    response = client.messages.create(
        model=model,
        max_tokens=512,  # una sola fila, la respuesta es minuscula
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return parse_row_response(response.content[0].text)


def extract_row(client: anthropic.Anthropic, image, expected_pos: int,
                 model: str, prompt: str) -> Optional[float]:
    """Extrae el valor de UNA fila ya recortada. Reintenta ante error
    transitorio, si el "cm" que el modelo dice ver no coincide con la
    posicion esperada (señal de que el recorte no aislo la fila correcta),
    y tambien si el modelo responde null -- una sola respuesta "no lo veo
    claro" no es necesariamente definitiva, vale la pena volver a preguntar
    antes de darla por vacia."""
    image_b64 = image_to_base64(image)
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            seen_pos, value = _call_claude_row(client, image_b64, model, prompt)
            if seen_pos is not None and int(seen_pos) != expected_pos:
                logger.warning("Fila %d intento %d/%d: el modelo vio cm=%s en vez de %d, reintentando",
                                expected_pos, attempt, MAX_RETRIES, seen_pos, expected_pos)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                continue
            if value is None:
                if attempt < MAX_RETRIES:
                    logger.warning("Fila %d intento %d/%d: el modelo respondio null, reintentando",
                                    expected_pos, attempt, MAX_RETRIES)
                    time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    continue
                return None
            return float(value)
        except Exception as e:
            last_err = e
            logger.warning("Fila %d intento %d/%d fallo: %s", expected_pos, attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    if last_err:
        logger.error("Fila %d: se omite tras %d intentos (%s)", expected_pos, MAX_RETRIES, last_err)
    return None


def extract_row_consensus(client: anthropic.Anthropic, image, expected_pos: int,
                           model: str, prompt: str, max_reads: int = 3,
                           tolerance: float = 0.0005) -> Optional[float]:
    """Version de maxima confianza de extract_row: en vez de confiar en una
    sola lectura (que puede tener un digito mal transcripto aunque la
    imagen sea perfectamente clara -- un modelo de vision puede fallar en
    esto ocasionalmente), pide la MISMA fila varias veces de forma
    independiente y solo acepta un valor cuando dos lecturas distintas
    coinciden. Si nunca coinciden dos, se devuelve None -- es preferible
    dejar la fila sin confirmar (y que se marque como faltante) a aceptar
    un digito que no se pudo verificar dos veces."""
    reads: list[float] = []
    for _ in range(max_reads):
        v = extract_row(client, image, expected_pos, model, prompt)
        if v is None:
            continue
        for prior in reads:
            if abs(prior - v) <= tolerance:
                return v
        reads.append(v)
    return None


RowJob = tuple[int, object, int, str, str]  # (global_pos, cropped_image, expected_pos, prompt, model)


def run_row_extractions(client: anthropic.Anthropic, jobs: list[RowJob],
                         on_done: Optional[Callable[[int, Optional[float]], None]] = None,
                         confirm: bool = False
                         ) -> dict[int, Optional[float]]:
    """Version fila-por-fila de run_extractions: cada job ya trae su imagen
    recortada (renderizada una sola vez por pagina y reutilizada para las
    ~200 filas de esa pagina, no se vuelve a renderizar por fila).
    Con confirm=True usa extract_row_consensus (varias lecturas
    independientes que deben coincidir) en vez de una sola lectura -- mas
    lento y caro, pensado para la pasada de reintento sobre las pocas
    posiciones puntuales que quedaron marcadas dudosas."""
    extractor = extract_row_consensus if confirm else extract_row
    results: dict[int, Optional[float]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(extractor, client, image, expected_pos, model, prompt): global_pos
            for global_pos, image, expected_pos, prompt, model in jobs
        }
        for future in as_completed(futures):
            global_pos = futures[future]
            value = future.result()
            results[global_pos] = value
            if on_done:
                on_done(global_pos, value)
    return results
