import streamlit as st
import anthropic
import json
import io
import base64
import re
from datetime import datetime
from pdf2image import convert_from_bytes
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

MODEL_FAST    = "claude-haiku-4-5-20251001"
MODEL_PRECISE = "claude-sonnet-4-6"

# ── Helpers de imagen ────────────────────────────────────────────────────────

def pdf_to_images(pdf_bytes: bytes):
    return convert_from_bytes(pdf_bytes, dpi=150)

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


def make_context_prompt(prev_last_mm: int, prev_last_vol: float) -> str:
    """Agrega contexto del ultimo valor conocido para consistencia de escala."""
    return BASE_PROMPT + f"""

CONTEXTO CRITICO DE CONTINUIDAD:
La pagina anterior termino con mm={prev_last_mm}, volumen={prev_last_vol:.0f} dm3.
Los valores de ESTA pagina deben ser MAYORES que {prev_last_vol:.0f} y en la MISMA escala.
Ejemplo correcto: si el anterior fue {prev_last_vol:.0f}, el siguiente debe ser ~{prev_last_vol + 100:.0f}.
Ejemplo INCORRECTO: {prev_last_vol/1000:.3f} (ese es 1000 veces menor, no es valido)."""


def make_retry_prompt(expected_start: int, expected_end: int,
                      prev_last_mm: int = None, prev_last_vol: float = None) -> str:
    prompt = BASE_PROMPT + f"""

ATENCION ESPECIAL:
Esta pagina debe contener datos para mm aproximadamente {expected_start} a {expected_end}.
Revisa cada digito con maxima precision. Es una calibracion industrial critica."""
    if prev_last_mm is not None:
        prompt += f"""
El valor anterior confirmado fue mm={prev_last_mm}, volumen={prev_last_vol:.0f} dm3.
Los valores de esta pagina deben ser mayores en la misma escala."""
    return prompt

# ── Extraccion ───────────────────────────────────────────────────────────────

def extract_page(client: anthropic.Anthropic, image, page_num: int,
                 model: str = MODEL_FAST, prompt: str = BASE_PROMPT) -> list[dict]:
    image_b64 = image_to_base64(image)
    try:
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
    except Exception as e:
        st.warning(f"Pagina {page_num}: error ({e}). Se omite.")
        return []


def last_known_vol(rows: list[dict]) -> tuple[int, float] | None:
    """Retorna (mm, vol) del ultimo valor no-null de la lista de rows."""
    for row in reversed(rows):
        base = int(row["base_mm"])
        for j in range(9, -1, -1):
            if row["values"][j] is not None:
                return base + j, float(row["values"][j])
    return None

# ── Construccion del diccionario mm->dm3 ─────────────────────────────────────

def build_vols(page_results: list) -> dict[int, float]:
    """Convierte la lista de (page_num, img, rows) en el diccionario mm->vol."""
    vols = {}
    for _pn, _img, rows in page_results:
        for row in rows:
            base = int(row["base_mm"])
            for i, v in enumerate(row["values"][:10]):
                if v is not None:
                    vols[base + i] = v
    return vols

# ── Correccion de escala ──────────────────────────────────────────────────────

def fix_scale_errors(vols: dict) -> tuple[dict[int, float], list[str]]:
    """
    Corrige x1000 / div1000 usando restauracion de monotonia.
    """
    mm_sorted = sorted(vols.keys())
    if len(mm_sorted) < 2:
        return vols, []

    corrected = dict(vols)
    fixes = []

    for i in range(len(mm_sorted) - 2, -1, -1):
        mm      = mm_sorted[i]
        mm_next = mm_sorted[i + 1]
        v       = corrected[mm]
        v_next  = corrected[mm_next]
        if v > v_next * 500:
            v_down = round(v / 1000, 3)
            if v_down <= v_next and v_down >= v_next / 2:
                corrected[mm] = v_down
                fixes.append(f"mm={mm}: {v} -> {v_down} (div1000)")

    for i in range(1, len(mm_sorted)):
        mm      = mm_sorted[i]
        mm_prev = mm_sorted[i - 1]
        v       = corrected[mm]
        v_prev  = corrected[mm_prev]
        if v < v_prev:
            v_up = v * 1000
            if v_up >= v_prev and v_up <= v_prev * 2:
                corrected[mm] = v_up
                fixes.append(f"mm={mm}: {v} -> {v_up} (x1000)")

    return corrected, fixes

# ── Validacion ────────────────────────────────────────────────────────────────

def validate_vols(vols: dict) -> dict:
    errors, warnings = [], []
    if not vols:
        return {"ok": False, "errors": ["No hay datos."], "warnings": [],
                "stats": {}, "bad_mm": set(), "missing": []}

    mm_sorted = sorted(vols.keys())
    min_mm, max_mm = mm_sorted[0], mm_sorted[-1]

    missing = [mm for mm in range(min_mm, max_mm + 1) if mm not in vols]
    if missing:
        groups, start, prev = [], missing[0], missing[0]
        for m in missing[1:]:
            if m == prev + 1:
                prev = m
            else:
                groups.append((start, prev)); start = prev = m
        groups.append((start, prev))
        ranges_str = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in groups[:10])
        if len(groups) > 10:
            ranges_str += f" ... y {len(groups)-10} rangos mas"
        errors.append(f"MM faltantes ({len(missing)} valores): {ranges_str}")

    non_mono = []
    prev_vol = None
    for mm in mm_sorted:
        vol = vols[mm]
        if prev_vol is not None and vol < prev_vol:
            non_mono.append((mm, prev_vol, vol))
        prev_vol = vol
    if non_mono:
        detail = "; ".join(f"mm={mm}: {pv:.3f}->{v:.3f}" for mm, pv, v in non_mono[:5])
        if len(non_mono) > 5: detail += f" ... y {len(non_mono)-5} mas"
        errors.append(f"Volumen decrece en {len(non_mono)} punto(s): {detail}")

    inc_map: dict[int, float] = {}
    for i in range(1, len(mm_sorted)):
        if mm_sorted[i] == mm_sorted[i - 1] + 1:
            inc_map[mm_sorted[i]] = vols[mm_sorted[i]] - vols[mm_sorted[i - 1]]

    outliers = []
    if inc_map:
        inc_keys = sorted(inc_map.keys())
        WINDOW = 30; THRESHOLD = 4.0
        def median(lst): s = sorted(lst); return s[len(s) // 2]
        for idx, mm in enumerate(inc_keys):
            actual = inc_map[mm]
            neighbors = [inc_map[inc_keys[j]]
                         for j in range(max(0, idx-WINDOW), min(len(inc_keys), idx+WINDOW+1))
                         if j != idx and inc_map[inc_keys[j]] > 0]
            if len(neighbors) < 5: continue
            local_med = median(neighbors)
            if local_med <= 0: continue
            ratio = actual / local_med
            if ratio > THRESHOLD or ratio < 1 / THRESHOLD:
                outliers.append((mm, actual, local_med, ratio))
        if outliers:
            detail = "; ".join(f"mm={mm}: D={act:.3f} (esp~{exp:.3f}, ratio={rat:.1f}x)"
                               for mm, act, exp, rat in outliers[:8])
            if len(outliers) > 8: detail += f" ... y {len(outliers)-8} mas"
            errors.append(f"Incremento anomalo en {len(outliers)} punto(s): {detail}")

    bad_mm = set(missing)
    bad_mm.update(mm for mm, _, _ in non_mono)
    bad_mm.update(mm for mm, _, _, _ in outliers)

    stats = {
        "total_mm": len(vols),
        "rango": f"{min_mm} - {max_mm} mm",
        "vol_min": f"{min(vols.values()):.3f}",
        "vol_max": f"{max(vols.values()):.3f}",
        "faltantes": len(missing),
    }
    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings,
            "stats": stats, "bad_mm": bad_mm, "missing": missing}

# ── Logica de retry ───────────────────────────────────────────────────────────

def find_pages_to_retry(page_results: list, validation: dict) -> set[int]:
    bad_mm    = validation["bad_mm"]
    missing_s = set(validation["missing"])
    retry     = set()

    for page_num, _img, rows in page_results:
        if not rows:
            retry.add(page_num); continue
        for row in rows:
            base = int(row["base_mm"])
            if any((base + i) in bad_mm for i in range(10)):
                retry.add(page_num); break

    if missing_s:
        min_m, max_m = min(missing_s), max(missing_s)
        for page_num, _img, rows in page_results:
            if not rows: continue
            bases = [int(r["base_mm"]) for r in rows]
            if max(bases) + 9 >= min_m - 20 and min(bases) <= max_m + 20:
                retry.add(page_num)

    return retry


def get_prev_vol_for_page(page_num: int, page_results: list) -> tuple[int, float] | None:
    """Busca el ultimo valor conocido de las paginas anteriores."""
    for pn, _img, rows in reversed(page_results[:page_num - 1]):
        lv = last_known_vol(rows)
        if lv is not None:
            return lv
    return None

# ── Generacion del Excel ──────────────────────────────────────────────────────

def has_decimals(vols: dict) -> bool:
    sample = list(vols.values())[:30]
    return any(isinstance(v, float) and v != int(v) for v in sample)

def generate_excel(vols: dict, tank_name: str, cert_number: str,
                   validation: dict, passes_info: str = "",
                   scale_fixes: list[str] = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = f"TK-{tank_name}"

    TITLE_FILL  = PatternFill("solid", start_color="0D2A4A")
    HEADER_FILL = PatternFill("solid", start_color="1F4E79")
    ALT_FILL    = PatternFill("solid", start_color="D6E4F0")
    ERROR_FILL  = PatternFill("solid", start_color="FF4444")
    thin   = Side(style="thin", color="AAAAAA")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")

    ws.merge_cells("A1:B1")
    ws["A1"] = f"TABLA DE LLENADO - TANQUE {tank_name}  |  TAGSA"
    ws["A1"].font = Font(name="Arial", bold=True, color="FFFFFF", size=12)
    ws["A1"].fill = TITLE_FILL
    ws["A1"].alignment = center
    ws.row_dimensions[1].height = 22

    estado = "APROBADA" if validation["ok"] else "CON ERRORES - ver hoja VALIDACION"
    info = [
        ("Razon Social:", "Antivari S.A."),
        ("Tanque N:", tank_name),
        ("Certificado INTI N:", cert_number),
        ("Unidad:", "dm3"),
        ("Validacion:", estado),
    ]
    for i, (lbl, val) in enumerate(info):
        r = i + 2
        ws.cell(r, 1, lbl).font = Font(name="Arial", bold=True, size=9)
        c2 = ws.cell(r, 2, val)
        c2.font = Font(name="Arial", size=9,
                       color="FF0000" if "ERRORES" in str(val) else "000000")
        ws.row_dimensions[r].height = 14

    header_row = len(info) + 2
    for c, h in [(1, "mm"), (2, "dm3")]:
        cell = ws.cell(header_row, c, h)
        cell.font = Font(name="Arial", bold=True, color="FFFFFF", size=11)
        cell.fill = HEADER_FILL; cell.alignment = center; cell.border = border
    ws.row_dimensions[header_row].height = 18
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 16
    ws.freeze_panes = f"A{header_row + 1}"

    num_format   = "#,##0.000" if has_decimals(vols) else "#,##0"
    bad_mm       = validation.get("bad_mm", set())
    MISSING_FILL = PatternFill("solid", start_color="FF8C00")

    row = header_row + 1
    for mm in range(0, max(vols.keys()) + 1):
        vol = vols.get(mm)
        if vol is None:
            c1 = ws.cell(row, 1, mm)
            c1.font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
            c1.alignment = center; c1.border = border; c1.fill = MISSING_FILL
            c2 = ws.cell(row, 2, "FALTANTE")
            c2.font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
            c2.alignment = center; c2.border = border; c2.fill = MISSING_FILL
            row += 1; continue

        is_bad = mm in bad_mm
        is_alt = (mm // 10) % 2 == 1
        fill = ERROR_FILL if is_bad else (ALT_FILL if is_alt else None)
        font_color = "FFFFFF" if is_bad else "000000"

        c1 = ws.cell(row, 1, mm)
        c1.font = Font(name="Arial", size=9, bold=is_bad, color=font_color)
        c1.alignment = center; c1.border = border
        if fill: c1.fill = fill

        c2 = ws.cell(row, 2, vol)
        c2.font = Font(name="Arial", size=9, bold=is_bad, color=font_color)
        c2.alignment = center; c2.border = border; c2.number_format = num_format
        if fill: c2.fill = fill

        row += 1

    wv = wb.create_sheet("VALIDACION")
    wv.column_dimensions["A"].width = 22
    wv.column_dimensions["B"].width = 90

    def vrow(r, lbl, val, bold=False, color="000000"):
        wv.cell(r, 1, lbl).font = Font(name="Arial", bold=bold, size=9)
        wv.cell(r, 2, val).font = Font(name="Arial", bold=bold, size=9, color=color)

    r = 1
    vrow(r, "REPORTE DE VALIDACION", f"Tanque {tank_name}", bold=True); r += 1
    vrow(r, "Fecha",        datetime.now().strftime("%Y-%m-%d %H:%M")); r += 1
    vrow(r, "Certificado",  cert_number); r += 1
    vrow(r, "Procesamiento", passes_info); r += 1
    vrow(r, "Total mm",     str(validation["stats"].get("total_mm", "-"))); r += 1
    vrow(r, "Rango",        validation["stats"].get("rango", "-")); r += 1
    vrow(r, "Volumen min",  validation["stats"].get("vol_min", "-") + " dm3"); r += 1
    vrow(r, "Volumen max",  validation["stats"].get("vol_max", "-") + " dm3"); r += 1
    vrow(r, "MM faltantes", str(validation["stats"].get("faltantes", "-"))); r += 1
    r += 1
    result_txt = "APROBADA" if validation["ok"] else "FALLIDA - REVISAR FILAS EN ROJO"
    result_col = "008000" if validation["ok"] else "FF0000"
    vrow(r, "RESULTADO", result_txt, bold=True, color=result_col); r += 2

    if validation["errors"]:
        vrow(r, "ERRORES", "", bold=True, color="FF0000"); r += 1
        for e in validation["errors"]:
            vrow(r, "", e, color="FF0000"); r += 1
        r += 1
    if validation["warnings"]:
        vrow(r, "ADVERTENCIAS", "", bold=True, color="CC6600"); r += 1
        for w in validation["warnings"]:
            vrow(r, "", w, color="CC6600"); r += 1
        r += 1
    if scale_fixes:
        vrow(r, "CORRECCIONES ESCALA", f"{len(scale_fixes)} valor(es)", bold=True, color="8B008B"); r += 1
        for f_txt in scale_fixes[:20]:
            vrow(r, "", f_txt, color="8B008B"); r += 1
        if len(scale_fixes) > 20:
            vrow(r, "", f"... y {len(scale_fixes)-20} mas", color="8B008B"); r += 1

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return buf.getvalue()

# ── UI ────────────────────────────────────────────────────────────────────────

def get_api_key() -> str:
    try:
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        return ""

def main():
    st.set_page_config(page_title="Conversor Tablas INTI - TAGSA",
                       page_icon="=", layout="centered")
    st.title("Conversor Tablas de Calibracion INTI")
    st.caption("Antivari S.A.")
    st.divider()

    secret_key = get_api_key()
    api_key = secret_key or st.text_input(
        "API Key de Anthropic", type="password",
        help="Configurala como secret en Streamlit Cloud.")

    col1, col2 = st.columns(2)
    with col1: tank_name   = st.text_input("N de Tanque",        placeholder="ej: TK-81")
    with col2: cert_number = st.text_input("N Certificado INTI", placeholder="ej: INTI 2623")

    uploaded = st.file_uploader("Subi el PDF del certificado INTI", type="pdf")
    ready = bool(api_key and tank_name and uploaded)

    if st.button("Convertir a Excel", type="primary", disabled=not ready):
        client = anthropic.Anthropic(api_key=api_key)

        # ── PASADA 1: Haiku pagina por pagina con contexto de continuidad ────
        with st.status("Pasada 1 - lectura rapida (Haiku)...", expanded=True) as status1:
            st.write("Convirtiendo PDF a imagenes...")
            images = pdf_to_images(uploaded.read())
            st.write(f"PDF tiene {len(images)} pagina(s).")

            page_results = []
            prev_mm, prev_vol = None, None

            for i, img in enumerate(images, 1):
                st.write(f"Pagina {i}/{len(images)}...")
                if prev_mm is not None:
                    prompt = make_context_prompt(prev_mm, prev_vol)
                else:
                    prompt = BASE_PROMPT
                rows = extract_page(client, img, i, model=MODEL_FAST, prompt=prompt)
                page_results.append((i, img, rows))
                st.write(f"  -> {len(rows)} filas extraidas.")
                # Actualizar contexto para la siguiente pagina
                lv = last_known_vol(rows)
                if lv is not None:
                    prev_mm, prev_vol = lv

            vols = build_vols(page_results)
            if not vols:
                st.error("No se extrajeron datos en la pasada 1. Revisa el PDF.")
                st.stop()
            vols, sf1    = fix_scale_errors(vols)
            if sf1: st.info(f"Escala corregida en {len(sf1)} valores.")
            validation_1 = validate_vols(vols)
            p1_lbl = "Pasada 1 completa - sin errores" if validation_1["ok"] else f"Pasada 1: {len(validation_1['errors'])} error(es)"
            status1.update(label=p1_lbl, state="complete")

        # ── PASADA 2: Sonnet en paginas con problemas ────────────────────────
        pages_to_retry = find_pages_to_retry(page_results, validation_1)
        passes_info    = "1 pasada (Haiku)"

        if pages_to_retry and not validation_1["ok"]:
            passes_info = f"2 pasadas - Haiku + Sonnet en {len(pages_to_retry)} pagina(s)"
            with st.status(f"Pasada 2 - re-procesando {len(pages_to_retry)} pagina(s) con Sonnet...",
                           expanded=True) as status2:
                for page_num in sorted(pages_to_retry):
                    _pn, img, old_rows = page_results[page_num - 1]
                    bases = [int(r["base_mm"]) for r in old_rows] if old_rows else []
                    exp_start = min(bases) if bases else 0
                    exp_end   = max(bases) + 9 if bases else 9999
                    lv = get_prev_vol_for_page(page_num, page_results)
                    prev_m = lv[0] if lv else None
                    prev_v = lv[1] if lv else None
                    st.write(f"Re-procesando pagina {page_num} (mm ~{exp_start}-{exp_end})...")
                    retry_prompt = make_retry_prompt(exp_start, exp_end, prev_m, prev_v)
                    new_rows = extract_page(client, img, page_num,
                                            model=MODEL_PRECISE, prompt=retry_prompt)
                    keep_rows = new_rows if new_rows else old_rows
                    page_results[page_num - 1] = (page_num, img, keep_rows)
                    st.write(f"  -> {len(new_rows)} filas Sonnet (Haiku tenia: {len(old_rows)}). Usando: {len(keep_rows)}.")

                vols = build_vols(page_results)
                if not vols:
                    st.error("No se pudieron extraer datos. Revisa el PDF.")
                    st.stop()
                vols, sf2    = fix_scale_errors(vols)
                if sf2: st.info(f"Escala corregida en {len(sf2)} valores.")
                validation_2 = validate_vols(vols)
                p2_lbl = "Pasada 2 completa - sin errores" if validation_2["ok"] else f"Pasada 2: {len(validation_2['errors'])} error(es) restantes"
                status2.update(label=p2_lbl, state="complete")
            validation  = validation_2
            scale_fixes = sf2
        else:
            validation  = validation_1
            scale_fixes = sf1

        st.write("Generando Excel...")
        excel_bytes = generate_excel(vols, tank_name, cert_number or "-",
                                     validation, passes_info, scale_fixes)

        st.subheader("Reporte de validacion")
        s = validation["stats"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total mm",  s["total_mm"])
        c2.metric("Rango",     s["rango"])
        c3.metric("Vol. min",  s["vol_min"] + " dm3")
        c4.metric("Vol. max",  s["vol_max"] + " dm3")

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
        fname = f"TK-{tank_name}_Tabla_Llenado.xlsx"
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
