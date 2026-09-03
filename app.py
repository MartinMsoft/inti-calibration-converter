import logging
import os

import anthropic
import streamlit as st

from excel_export import generate_excel, sanitize_filename_part
from extraction import (
    DEFAULT_DPI,
    MODEL_FAST,
    MODEL_PRECISE,
    RETRY_DPI,
    ExtractJob,
    RowJob,
    extract_page,
    get_pdf_page_count,
    render_pdf_page,
    render_pdf_page_cropped,
    run_extractions,
    run_row_extractions,
)
from formats import FORMATS, compute_row_crop_box, make_retry_prompt, make_row_prompt
from validation import (
    build_vols,
    find_pages_to_retry,
    fix_row_scale_consistency,
    fix_scale_errors,
    fix_scale_shift_runs,
    get_forward_anchor,
    get_prev_context,
    validate_vols,
)

logger = logging.getLogger("inti_converter")
logger.setLevel(logging.INFO)

# Cuantas filas se recortan y se mandan a la API por tanda dentro de una
# misma pagina en el modo fila-por-fila. Armar las ~200 filas de la pagina
# de una sola vez (y recien despues pasarlas al pool de hilos) mantiene
# esas ~200 imagenes recortadas en memoria a la vez -- exactamente el mismo
# problema de RAM que ya resolvimos para paginas enteras, pero reaparecido
# a nivel de recortes. Procesando de a tandas chicas, como mucho hay
# ROW_CHUNK_SIZE recortes vivos por vez.
ROW_CHUNK_SIZE = 20

FUTURISTIC_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@600;800&family=Rajdhani:wght@400;500;600;700&display=swap');

html, body, [class*="css"] { font-family: 'Rajdhani', sans-serif; }

.stApp {
    background:
        radial-gradient(circle at 15% 0%, rgba(0,229,255,0.08) 0%, rgba(0,0,0,0) 40%),
        radial-gradient(circle at 85% 100%, rgba(123,92,255,0.10) 0%, rgba(0,0,0,0) 45%),
        linear-gradient(180deg, #05070d 0%, #060a14 100%);
}

h1, h1 span {
    font-family: 'Orbitron', sans-serif !important;
    font-weight: 800 !important;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    background: linear-gradient(90deg, #00e5ff 0%, #7b5cff 55%, #ff2fd1 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    text-shadow: none;
}

[data-testid="stCaptionContainer"] {
    font-family: 'Orbitron', sans-serif;
    letter-spacing: 3px;
    text-transform: uppercase;
    color: #7fd8ff !important;
    opacity: 0.85;
}

hr { border-color: rgba(0,229,255,0.25) !important; }

div[data-testid="stTextInput"] input,
div[data-testid="stFileUploaderDropzone"] {
    background-color: rgba(13,21,38,0.75) !important;
    border: 1px solid rgba(0,229,255,0.35) !important;
    border-radius: 6px !important;
    color: #e6f6ff !important;
}
div[data-testid="stTextInput"] input:focus {
    border-color: #00e5ff !important;
    box-shadow: 0 0 8px rgba(0,229,255,0.55) !important;
}

div[data-testid="stButton"] button[kind="primary"] {
    background: linear-gradient(90deg, #00b8d4, #7b5cff) !important;
    border: none !important;
    font-family: 'Orbitron', sans-serif !important;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    box-shadow: 0 0 16px rgba(0,229,255,0.45);
    transition: box-shadow 0.2s ease, transform 0.2s ease;
}
div[data-testid="stButton"] button[kind="primary"]:hover {
    box-shadow: 0 0 26px rgba(0,229,255,0.85);
    transform: translateY(-1px);
}
div[data-testid="stButton"] button[kind="primary"]:disabled {
    background: rgba(90,100,120,0.35) !important;
    box-shadow: none;
}

div[data-testid="stDownloadButton"] button {
    font-family: 'Orbitron', sans-serif !important;
    letter-spacing: 1px;
    border: 1px solid rgba(0,229,255,0.5) !important;
    box-shadow: 0 0 12px rgba(0,229,255,0.25);
}

div[data-testid="stMetric"] {
    background: rgba(13,21,38,0.6);
    border: 1px solid rgba(123,92,255,0.35);
    border-radius: 8px;
    padding: 10px 12px;
    box-shadow: 0 0 14px rgba(0,229,255,0.08);
    overflow: visible;
}
div[data-testid="stMetricValue"] {
    color: #00e5ff !important;
    font-size: 1.35rem !important;
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
    overflow-wrap: anywhere;
    line-height: 1.25;
}
div[data-testid="stMetricLabel"] { font-size: 0.85rem !important; }

div[data-testid="stExpander"], div[data-testid="stStatusWidget"] {
    border: 1px solid rgba(0,229,255,0.25) !important;
    border-radius: 8px !important;
    background: rgba(9,14,26,0.55) !important;
}

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: #05070d; }
::-webkit-scrollbar-thumb { background: linear-gradient(180deg, #00b8d4, #7b5cff); border-radius: 6px; }
</style>
"""


class ListLogHandler(logging.Handler):
    """Junta los mensajes de log de una corrida para incluirlos en el Excel."""

    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def apply_row_consistency(page_results: list) -> tuple[list, list[str]]:
    """Corrige la consistencia interna de cada fila (ver fix_row_scale_consistency)
    para todas las paginas, y devuelve el page_results corregido junto con el
    log de correcciones aplicadas."""
    fixed = []
    fixes: list[str] = []
    for page_num, rows in page_results:
        fixed_rows, row_fixes = fix_row_scale_consistency(rows)
        fixes.extend(row_fixes)
        fixed.append((page_num, fixed_rows))
    return fixed, fixes


def run_page_based_pipeline(client, pdf_bytes, fmt, log_handler):
    """Extraccion por pagina completa (o por bloques de 10 filas): rapida y
    barata, pero un modelo de lenguaje procesando muchas filas similares
    seguidas puede "completar el patron" en vez de leer cada digito -- ver
    run_row_by_row_pipeline para el modo de maxima fidelidad."""
    pdf_bytes = pdf_bytes  # (solo por claridad de firma)

    # ── PASADA 1: Haiku, todas las paginas en paralelo ────────────────
    with st.status("Pasada 1 - lectura rapida (Haiku)...", expanded=True) as status1:
        page_count = get_pdf_page_count(pdf_bytes)
        st.write(f"PDF tiene {page_count} pagina(s).")

        progress = st.progress(0.0)
        done_count = 0

        def on_page_done(page_num: int, rows: list[dict]) -> None:
            nonlocal done_count
            done_count += 1
            progress.progress(done_count / page_count)
            st.write(f"Pagina {page_num}: {len(rows)} filas extraidas.")

        jobs: list[ExtractJob] = [
            (i, pdf_bytes, fmt.base_prompt, MODEL_FAST, DEFAULT_DPI) for i in range(1, page_count + 1)
        ]
        results1 = run_extractions(client, jobs, on_done=on_page_done)
        page_results = [(i, results1[i]) for i in range(1, page_count + 1)]
        page_results, row_fixes1 = apply_row_consistency(page_results)
        if row_fixes1: st.info(f"Consistencia interna de fila corregida en {len(row_fixes1)} valor(es).")

        vols = build_vols(page_results)
        if not vols:
            st.error("No se extrajeron datos en la pasada 1. Revisa el PDF.")
            st.stop()
        vols, sf1a = fix_scale_errors(vols)
        vols, sf1b = fix_scale_shift_runs(vols)
        sf1 = row_fixes1 + sf1a + sf1b
        if sf1a or sf1b: st.info(f"Escala corregida en {len(sf1a) + len(sf1b)} tramo(s)/valor(es).")
        validation_1 = validate_vols(vols, unit_label=fmt.height_unit)
        p1_lbl = "Pasada 1 completa - sin errores" if validation_1["ok"] else f"Pasada 1: {len(validation_1['errors'])} error(es)"
        status1.update(label=p1_lbl, state="complete")

    # ── PASADA 2: Sonnet, en paralelo, solo paginas con problemas ────
    pages_to_retry = find_pages_to_retry(page_results, validation_1)
    passes_info = "1 pasada (Haiku)"

    if pages_to_retry and not validation_1["ok"]:
        passes_info = f"2 pasadas - Haiku + Sonnet en {len(pages_to_retry)} pagina(s)"
        with st.status(f"Pasada 2 - re-procesando {len(pages_to_retry)} pagina(s) con Sonnet...",
                        expanded=True) as status2:
            retry_jobs: list[ExtractJob] = []
            for page_num in sorted(pages_to_retry):
                _pn, old_rows = page_results[page_num - 1]
                bases = [int(r["pos"]) for r in old_rows] if old_rows else []
                naive_start = min(bases) if bases else 0
                row_span = len(old_rows) * fmt.values_per_row if old_rows else 100

                # Ojo: no confiar en "bases" para el rango esperado, porque
                # si la Pasada 1 le puso una etiqueta de fila equivocada a
                # TODA la pagina (lo vimos pasar: +100 de corrimiento), usar
                # esas bases como "rango esperado" solo refuerza el mismo
                # error en el reintento. Mejor anclar el inicio en la
                # continuidad real (donde termino la pagina anterior).
                ctx = get_prev_context(vols, naive_start)
                prev_m, prev_v = ctx if ctx else (None, None)
                if prev_m is not None:
                    step = fmt.values_per_row
                    exp_start = ((prev_m + 1 + step - 1) // step) * step
                else:
                    exp_start = naive_start
                exp_end = exp_start + row_span - 1 + 100  # margen generoso: la pagina
                # puede tener mas filas de las que la Pasada 1 llego a detectar

                # Sin pagina anterior (ej: pagina 1), buscamos un ancla hacia
                # ADELANTE: un punto ya validado como bueno mas alla de esta
                # pagina (en ella misma o en la siguiente). Como el volumen
                # siempre crece, ese punto futuro confirmado acota "para
                # arriba" cuanto pueden valer las filas de esta pagina.
                next_m, next_v = None, None
                if prev_m is None:
                    fwd = get_forward_anchor(vols, validation_1["bad_mm"], naive_start)
                    next_m, next_v = fwd if fwd else (None, None)

                retry_prompt = make_retry_prompt(fmt, exp_start, exp_end, prev_m, prev_v, next_m, next_v)
                retry_jobs.append((page_num, pdf_bytes, retry_prompt, MODEL_PRECISE, RETRY_DPI))

            def on_retry_done(page_num: int, new_rows: list[dict]) -> None:
                old_rows = page_results[page_num - 1][1]
                st.write(f"Pagina {page_num}: {len(new_rows)} filas Sonnet (Haiku tenia: {len(old_rows)}).")

            results2 = run_extractions(client, retry_jobs, on_done=on_retry_done)
            for page_num, new_rows in results2.items():
                _pn, old_rows = page_results[page_num - 1]
                keep_rows = new_rows if new_rows else old_rows
                page_results[page_num - 1] = (page_num, keep_rows)

            page_results, row_fixes2 = apply_row_consistency(page_results)
            if row_fixes2: st.info(f"Consistencia interna de fila corregida en {len(row_fixes2)} valor(es).")

            vols = build_vols(page_results)
            if not vols:
                st.error("No se pudieron extraer datos. Revisa el PDF.")
                st.stop()
            vols, sf2a = fix_scale_errors(vols)
            vols, sf2b = fix_scale_shift_runs(vols)
            sf2 = row_fixes2 + sf2a + sf2b
            if sf2a or sf2b: st.info(f"Escala corregida en {len(sf2a) + len(sf2b)} tramo(s)/valor(es).")
            validation_2 = validate_vols(vols, unit_label=fmt.height_unit)
            p2_lbl = "Pasada 2 completa - sin errores" if validation_2["ok"] else f"Pasada 2: {len(validation_2['errors'])} error(es) restantes"
            status2.update(label=p2_lbl, state="complete")
        validation = validation_2
        scale_fixes = sf2
    else:
        validation = validation_1
        scale_fixes = sf1

    # ── PASADA 3: recorte del primer bloque, solo para la pagina 1 ───
    # (la unica sin pagina anterior de la cual anclarse) si despues de
    # Sonnet los primeros valores siguen sin validar. Le mandamos una
    # imagen mas simple -- una sola columna, sin el resto de la tabla
    # ni el encabezado pesado al lado -- por si el problema es el
    # layout completo, no la nitidez de los digitos.
    still_bad = find_pages_to_retry(page_results, validation) if not validation["ok"] else set()
    if 1 in still_bad and fmt.first_block_crop and fmt.crop_prompt:
        with st.status("Pasada 3 - recorte enfocado en el primer bloque...",
                        expanded=True) as status3:
            cropped_img = render_pdf_page_cropped(pdf_bytes, 1, RETRY_DPI, fmt.first_block_crop)
            new_rows = extract_page(client, cropped_img, 1, MODEL_PRECISE, fmt.crop_prompt)
            if new_rows:
                _pn, old_rows = page_results[0]
                merged = {r["pos"]: r for r in old_rows}
                for r in new_rows:
                    merged[r["pos"]] = r
                page_results[0] = (1, [merged[k] for k in sorted(merged)])
                page_results, row_fixes3 = apply_row_consistency(page_results)
                scale_fixes = scale_fixes + row_fixes3

                vols = build_vols(page_results)
                vols, sf3a = fix_scale_errors(vols)
                vols, sf3b = fix_scale_shift_runs(vols)
                scale_fixes = scale_fixes + sf3a + sf3b
                validation = validate_vols(vols, unit_label=fmt.height_unit)
                passes_info += " + recorte pagina 1"
                p3_lbl = ("Pasada 3 completa - sin errores" if validation["ok"]
                          else f"Pasada 3: {len(validation['errors'])} error(es) restantes")
                status3.update(label=p3_lbl, state="complete")
            else:
                status3.update(label="Pasada 3: sin filas nuevas, se mantiene el resultado anterior",
                               state="complete")

    return vols, validation, passes_info, scale_fixes


def run_row_by_row_pipeline(client, pdf_bytes, fmt, log_handler):
    """Extraccion de maxima fidelidad: cada fila se recorta y se manda a la
    IA de a UNA, sin ninguna otra fila visible. Mas lenta y mas cara (una
    llamada por posicion en vez de una por pagina/bloque de 10), pero
    elimina la posibilidad de que el modelo "complete un patron" viendo
    varias filas similares juntas -- justamente la causa raiz que
    identificamos en los modos anteriores."""
    with st.status("Pasada 1 - lectura fila por fila con doble verificacion (Haiku)...",
                    expanded=True) as status1:
        page_count = get_pdf_page_count(pdf_bytes)
        total_positions = page_count * fmt.positions_per_page
        st.write(f"PDF tiene {page_count} pagina(s), hasta {total_positions} filas a leer una por una.")

        progress = st.progress(0.0)
        done_count = 0

        def on_row_done(global_pos: int, value) -> None:
            nonlocal done_count
            done_count += 1
            if done_count % 25 == 0 or done_count == total_positions:
                progress.progress(done_count / total_positions)
                st.write(f"{done_count}/{total_positions} filas procesadas...")

        # Procesamos PAGINA POR PAGINA (no todas de una): cada pagina se
        # renderiza entera una sola vez, se recortan sus ~200 filas (los
        # recortes son chicos, pero renderizar las 30 paginas de golpe y
        # guardar sus imagenes completas mientras se arman todos los
        # recortes seria el mismo problema de memoria que ya resolvimos
        # antes (502 por falta de RAM en paginas grandes).
        vols: dict[int, float] = {}
        for page_num in range(1, page_count + 1):
            page_image = render_pdf_page(pdf_bytes, page_num, RETRY_DPI)
            w, h = page_image.size
            local_positions = list(range(fmt.positions_per_page))
            for chunk_start in range(0, len(local_positions), ROW_CHUNK_SIZE):
                chunk = local_positions[chunk_start:chunk_start + ROW_CHUNK_SIZE]
                page_jobs: list[RowJob] = []
                for local_pos in chunk:
                    global_pos = (page_num - 1) * fmt.positions_per_page + local_pos
                    box = compute_row_crop_box(fmt, local_pos)
                    box_px = (int(w * box[0]), int(h * box[1]), int(w * box[2]), int(h * box[3]))
                    cropped = page_image.crop(box_px)
                    page_jobs.append((global_pos, cropped, global_pos, make_row_prompt(global_pos), MODEL_FAST))

                page_results1 = run_row_extractions(client, page_jobs, on_done=on_row_done, confirm=True)
                for pos, v in page_results1.items():
                    if v is not None:
                        vols[pos] = v
            del page_image
        if not vols:
            st.error("No se extrajeron datos en la pasada 1. Revisa el PDF.")
            st.stop()
        vols, sf1 = fix_scale_errors(vols)
        validation_1 = validate_vols(vols, unit_label=fmt.height_unit)
        p1_lbl = "Pasada 1 completa - sin errores" if validation_1["ok"] else f"Pasada 1: {len(validation_1['errors'])} error(es)"
        status1.update(label=p1_lbl, state="complete")

    # ── PASADA 2: Sonnet, solo en las posiciones puntuales con problemas ──
    bad_positions = validation_1["bad_mm"] | (set(range(min(vols), max(vols) + 1)) - set(vols.keys()))
    passes_info = "1 pasada fila-por-fila con doble verificacion (Haiku)"

    if bad_positions and not validation_1["ok"]:
        passes_info = f"2 pasadas fila-por-fila - Haiku + Sonnet con doble verificacion en {len(bad_positions)} fila(s)"
        with st.status(f"Pasada 2 - re-leyendo {len(bad_positions)} fila(s) puntuales con Sonnet "
                        f"(2 lecturas independientes por fila, solo se acepta si coinciden)...",
                        expanded=True) as status2:
            def on_retry_done(global_pos: int, value) -> None:
                st.write(f"Fila {global_pos}: {'sin valor' if value is None else value}")

            # Agrupamos por pagina (misma razon que en la Pasada 1: no
            # queremos varias paginas completas en memoria a la vez).
            by_page: dict[int, list[int]] = {}
            for global_pos in sorted(bad_positions):
                page_num = global_pos // fmt.positions_per_page + 1
                by_page.setdefault(page_num, []).append(global_pos)

            for page_num, positions in by_page.items():
                page_image = render_pdf_page(pdf_bytes, page_num, RETRY_DPI)
                w, h = page_image.size
                for chunk_start in range(0, len(positions), ROW_CHUNK_SIZE):
                    chunk = positions[chunk_start:chunk_start + ROW_CHUNK_SIZE]
                    retry_jobs: list[RowJob] = []
                    for global_pos in chunk:
                        local_pos = global_pos % fmt.positions_per_page
                        box = compute_row_crop_box(fmt, local_pos)
                        box_px = (int(w * box[0]), int(h * box[1]), int(w * box[2]), int(h * box[3]))
                        cropped = page_image.crop(box_px)
                        retry_jobs.append((global_pos, cropped, global_pos, make_row_prompt(global_pos), MODEL_PRECISE))

                    results2 = run_row_extractions(client, retry_jobs, on_done=on_retry_done, confirm=True)
                    for global_pos, value in results2.items():
                        if value is not None:
                            vols[global_pos] = value
                del page_image

            vols, sf2 = fix_scale_errors(vols)
            validation_2 = validate_vols(vols, unit_label=fmt.height_unit)
            p2_lbl = "Pasada 2 completa - sin errores" if validation_2["ok"] else f"Pasada 2: {len(validation_2['errors'])} error(es) restantes"
            status2.update(label=p2_lbl, state="complete")
        validation = validation_2
        scale_fixes = sf1 + sf2
    else:
        validation = validation_1
        scale_fixes = sf1

    return vols, validation, passes_info, scale_fixes


def get_api_key() -> str:
    try:
        key = st.secrets["ANTHROPIC_API_KEY"]
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "")


def main():
    st.set_page_config(page_title="Conversor Tablas de Calibracion - Antivari",
                        page_icon="🛰️", layout="centered")
    st.markdown(FUTURISTIC_CSS, unsafe_allow_html=True)
    st.title("Conversor Tablas de Calibración")
    st.caption("Antivari S.A.")
    st.divider()

    secret_key = get_api_key()
    api_key = secret_key or st.text_input(
        "API Key de Anthropic", type="password",
        help="Configurala como secret (Streamlit) o variable de entorno ANTHROPIC_API_KEY (Render).")

    format_key = st.selectbox(
        "Formato de tabla", options=list(FORMATS.keys()),
        format_func=lambda k: FORMATS[k].label)
    fmt = FORMATS[format_key]

    col1, col2 = st.columns(2)
    with col1: tank_name = st.text_input("N de Tanque", placeholder="ej: TK-81")
    with col2: cert_number = st.text_input("N Certificado", placeholder="ej: INTI 2623")

    uploaded = st.file_uploader("Subi el PDF del certificado", type="pdf")
    ready = bool(api_key and tank_name and uploaded)

    if st.button("Convertir a Excel", type="primary", disabled=not ready):
        client = anthropic.Anthropic(api_key=api_key)

        log_handler = ListLogHandler()
        log_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(log_handler)

        try:
            pdf_bytes = uploaded.read()

            if fmt.row_by_row:
                vols, validation, passes_info, scale_fixes = run_row_by_row_pipeline(
                    client, pdf_bytes, fmt, log_handler)
            else:
                vols, validation, passes_info, scale_fixes = run_page_based_pipeline(
                    client, pdf_bytes, fmt, log_handler)

            st.write("Generando Excel...")
            excel_bytes = generate_excel(vols, tank_name, cert_number or "-",
                                          validation, passes_info, scale_fixes,
                                          log_lines=log_handler.records,
                                          height_unit=fmt.height_unit,
                                          volume_unit=fmt.volume_unit)
        finally:
            logger.removeHandler(log_handler)

        st.subheader("Reporte de validacion")
        s = validation["stats"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(f"Total {fmt.height_unit}", s["total_mm"])
        c2.metric("Rango", s["rango"])
        c3.metric("Vol. min", s["vol_min"] + f" {fmt.volume_unit}")
        c4.metric("Vol. max", s["vol_max"] + f" {fmt.volume_unit}")

        if not validation["ok"]:
            st.error("Validacion FALLIDA - el Excel tiene errores (filas en rojo). Revisa antes de usar.")
        elif validation["warnings"]:
            st.warning("Validacion APROBADA CON ADVERTENCIAS - revisa los puntos senializados antes de usar.")
        else:
            st.success("Validacion APROBADA - todos los controles pasaron.")

        if validation["errors"]:
            with st.expander("Errores", expanded=True):
                for e in validation["errors"]: st.error(e)
        if validation["warnings"]:
            with st.expander("Advertencias", expanded=True):
                for w in validation["warnings"]: st.warning(w)

        st.divider()
        fname = f"TK-{sanitize_filename_part(tank_name)}_Tabla_Llenado.xlsx"
        st.download_button(
            label="Descargar Excel",
            data=excel_bytes, file_name=fname,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary" if validation["ok"] else "secondary",
        )
        if not validation["ok"]:
            st.caption("El archivo tiene errores. Las filas problematicas estan en ROJO. Verifica contra el PDF original antes de usar.")


if __name__ == "__main__":
    main()
