import logging
import os

import anthropic
import streamlit as st

from excel_export import generate_excel, sanitize_filename_part
from extraction import (
    BASE_PROMPT,
    MODEL_FAST,
    MODEL_PRECISE,
    ExtractJob,
    get_pdf_page_count,
    make_retry_prompt,
    run_extractions,
)
from validation import (
    build_vols,
    find_pages_to_retry,
    fix_row_scale_consistency,
    fix_scale_errors,
    fix_scale_shift_runs,
    get_prev_context,
    validate_vols,
)

logger = logging.getLogger("inti_converter")
logger.setLevel(logging.INFO)

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


def get_api_key() -> str:
    try:
        key = st.secrets["ANTHROPIC_API_KEY"]
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "")


def main():
    st.set_page_config(page_title="Conversor Tablas INTI - Antivari",
                        page_icon="🛰️", layout="centered")
    st.markdown(FUTURISTIC_CSS, unsafe_allow_html=True)
    st.title("Conversor Tablas de Calibración INTI")
    st.caption("Antivari S.A.")
    st.divider()

    secret_key = get_api_key()
    api_key = secret_key or st.text_input(
        "API Key de Anthropic", type="password",
        help="Configurala como secret (Streamlit) o variable de entorno ANTHROPIC_API_KEY (Render).")

    col1, col2 = st.columns(2)
    with col1: tank_name = st.text_input("N de Tanque", placeholder="ej: TK-81")
    with col2: cert_number = st.text_input("N Certificado INTI", placeholder="ej: INTI 2623")

    uploaded = st.file_uploader("Subi el PDF del certificado INTI", type="pdf")
    ready = bool(api_key and tank_name and uploaded)

    if st.button("Convertir a Excel", type="primary", disabled=not ready):
        client = anthropic.Anthropic(api_key=api_key)

        log_handler = ListLogHandler()
        log_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(log_handler)

        try:
            pdf_bytes = uploaded.read()

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
                    (i, pdf_bytes, BASE_PROMPT, MODEL_FAST) for i in range(1, page_count + 1)
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
                validation_1 = validate_vols(vols)
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
                        bases = [int(r["base_mm"]) for r in old_rows] if old_rows else []
                        exp_start = min(bases) if bases else 0
                        exp_end = max(bases) + 9 if bases else 9999
                        ctx = get_prev_context(vols, exp_start)
                        prev_m, prev_v = ctx if ctx else (None, None)
                        retry_prompt = make_retry_prompt(exp_start, exp_end, prev_m, prev_v)
                        retry_jobs.append((page_num, pdf_bytes, retry_prompt, MODEL_PRECISE))

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
                    validation_2 = validate_vols(vols)
                    p2_lbl = "Pasada 2 completa - sin errores" if validation_2["ok"] else f"Pasada 2: {len(validation_2['errors'])} error(es) restantes"
                    status2.update(label=p2_lbl, state="complete")
                validation = validation_2
                scale_fixes = sf2
            else:
                validation = validation_1
                scale_fixes = sf1

            st.write("Generando Excel...")
            excel_bytes = generate_excel(vols, tank_name, cert_number or "-",
                                          validation, passes_info, scale_fixes,
                                          log_lines=log_handler.records)
        finally:
            logger.removeHandler(log_handler)

        st.subheader("Reporte de validacion")
        s = validation["stats"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total mm", s["total_mm"])
        c2.metric("Rango", s["rango"])
        c3.metric("Vol. min", s["vol_min"] + " dm3")
        c4.metric("Vol. max", s["vol_max"] + " dm3")

        if validation["ok"]:
            st.success("Validacion APROBADA - todos los controles pasaron.")
        else:
            st.error("Validacion FALLIDA - el Excel tiene errores (filas en rojo). Revisa antes de usar.")

        if validation["errors"]:
            with st.expander("Errores", expanded=True):
                for e in validation["errors"]: st.error(e)
        if validation["warnings"]:
            with st.expander("Advertencias"):
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
