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

MODEL_FAST   = "claude-haiku-4-5-20251001"   # 1ª pasada: rápido y barato
MODEL_PRECISE = "claude-sonnet-4-6"           # 2ª pasada: mayor precisión OCR

# ── Helpers de imagen ────────────────────────────────────────────────────────

def pdf_to_images(pdf_bytes: bytes):
    return convert_from_bytes(pdf_bytes, dpi=300)

def image_to_base64(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")

# ── Prompts ──────────────────────────────────────────────────────────────────

BASE_PROMPT = """Esta imagen contiene una tabla de calibración de tanque industrial certificada por INTI (Argentina).

La tabla tiene este formato:
- Primera columna: valor base en mm (0, 10, 20, 30, ...)
- Columnas 0 a 9: los 10 mm individuales de esa fila (base+0 a base+9)
- Valores en dm³

Extraé TODOS los datos en este JSON exacto:
{
  "rows": [
    {"base_mm": 0, "values": [v0, v1, v2, v3, v4, v5, v6, v7, v8, v9]},
    {"base_mm": 10, "values": [v0, v1, v2, v3, v4, v5, v6, v7, v8, v9]}
  ]
}

Reglas CRÍTICAS:

NÚMEROS:
- El punto (.) es separador de MILES, NO decimal.
    788.068 → entero 788068
    1.160.362 → entero 1160362
    27.344 → si hay UN solo punto y el número es pequeño (<10000), es decimal: 27.344
- Si hay DOS o más puntos en un número, siempre es entero: quitá los puntos.

IGNORAR completamente:
- Números de página ("Página 12", "12", "101", etc.)
- Encabezados, pies de página, firmas, sellos, logos
- Cualquier texto fuera de la tabla de datos

FORMATO ESTRICTO:
- Cada fila tiene EXACTAMENTE 10 valores. Nunca más (salvo null al final de la última fila).
- Si una celda está vacía, usá null.
- Si la página no tiene tabla (carátula, texto, firma), devolvé {"rows": []}.
- Respondé ÚNICAMENTE con el JSON, sin texto adicional ni bloques de código."""

def make_retry_prompt(expected_start: int, expected_end: int) -> str:
    return BASE_PROMPT + f"""

CONTEXTO IMPORTANTE PARA ESTA PÁGINA:
Esta página debería contener datos para el rango de mm aproximadamente {expected_start} a {expected_end}.
Si ves filas con esos valores de mm, extraélas TODAS con máxima precisión.
Revisá cada dígito con cuidado — es una tabla de calibración industrial crítica."""

# ── Extracción ───────────────────────────────────────────────────────────────

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
        data = json.loads(raw)
        return data.get("rows", [])
    except Exception as e:
        st.warning(f"Página {page_num}: error al procesar ({e}). Se omite.")
        return []

# ── Construcción del diccionario mm→dm³ ─────────────────────────────────────

def build_vols(page_results: list) -> tuple[dict[int, float], list[str]]:
    """
    Construye el diccionario mm→dm³ procesando página por página.
    Detecta y corrige offsets sistemáticos de base_mm causados por números de página.
    """
    jump_warnings = []
    vols = {}
    prev_base = None

    for page_num, _img, rows in page_results:
        if not rows:
            continue

        # Detectar offset sistemático: si las primeras filas de esta página
        # tienen todas el mismo desplazamiento vs el esperado, es error de página
        if prev_base is not None and len(rows) >= 2:
            expected_start = prev_base + 10
            actual_start   = int(rows[0]["base_mm"])
            offset         = actual_start - expected_start

            if offset != 0:
                # Verificar que el offset es consistente en todas las filas de la página
                offsets = [int(r["base_mm"]) - (expected_start + 10 * i)
                           for i, r in enumerate(rows)]
                if len(set(offsets)) == 1:
                    # Offset uniforme → corregir toda la página
                    jump_warnings.append(
                        f"Página {page_num}: offset sistemático de {offset} mm en base_mm "
                        f"(número de página contaminó la lectura). Corregido automáticamente."
                    )
                    rows = [{"base_mm": int(r["base_mm"]) - offset,
                             "values": r["values"]} for r in rows]
                else:
                    # Offset irregular → reportar sin corregir
                    jump_warnings.append(
                        f"Salto en base_mm página {page_num}: se esperaba {expected_start} "
                        f"pero el PDF entregó {actual_start} (diferencia: {offset}). "
                        f"Verificar manualmente."
                    )

        for row in rows:
            base = int(row["base_mm"])
            if prev_base is not None and base != prev_base + 10 and not jump_warnings:
                jump_warnings.append(
                    f"Salto en base_mm: se esperaba {prev_base + 10} pero se recibió {base}."
                )
            prev_base = base
            for i, v in enumerate(row["values"][:10]):
                if v is not None:
                    vols[base + i] = v

    return vols, jump_warnings


def normalize_scale(vols: dict) -> tuple[dict[int, float], list[str]]:
    """
    Detecta y corrige valores con escala incorrecta (×1000 o ÷1000)
    causados por lectura errónea del separador de miles.
    Compara cada valor contra sus vecinos inmediatos.
    """
    if len(vols) < 10:
        return vols, []

    mm_sorted = sorted(vols.keys())
    corrected = dict(vols)
    fixes = []
    WINDOW = 5  # vecinos a consultar en cada lado

    for idx, mm in enumerate(mm_sorted):
        v = vols[mm]
        neighbors = [
            vols[mm_sorted[j]]
            for j in range(max(0, idx - WINDOW), min(len(mm_sorted), idx + WINDOW + 1))
            if j != idx
        ]
        if not neighbors:
            continue
        neighbor_med = sorted(neighbors)[len(neighbors) // 2]
        if neighbor_med <= 0:
            continue

        ratio = v / neighbor_med if neighbor_med != 0 else 1

        if ratio < 0.005:
            # Valor ~1000x menor: le faltan los separadores de miles
            corrected[mm] = round(v * 1000)
            fixes.append(f"mm={mm}: {v} → {corrected[mm]} (×1000, separador de miles faltante)")
        elif ratio > 200:
            # Valor ~1000x mayor: tiene separadores de más
            corrected[mm] = round(v / 1000, 3)
            fixes.append(f"mm={mm}: {v} → {corrected[mm]} (÷1000, separador de miles extra)")

    return corrected, fixes

# ── Validación ───────────────────────────────────────────────────────────────

def validate_vols(vols: dict) -> dict:
    errors, warnings = [], []
    if not vols:
        return {"ok": False, "errors": ["No hay datos."], "warnings": [],
                "stats": {}, "bad_mm": set(), "missing": []}

    mm_sorted = sorted(vols.keys())
    min_mm, max_mm = mm_sorted[0], mm_sorted[-1]

    # 1. MM faltantes
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
            ranges_str += f" ... y {len(groups)-10} rangos más"
        errors.append(f"MM faltantes ({len(missing)} valores): {ranges_str}")

    # 2. Volumen siempre creciente
    non_mono = []
    prev_vol = None
    for mm in mm_sorted:
        vol = vols[mm]
        if prev_vol is not None and vol < prev_vol:
            non_mono.append((mm, prev_vol, vol))
        prev_vol = vol
    if non_mono:
        detail = "; ".join(f"mm={mm}: {pv:.3f}→{v:.3f}" for mm, pv, v in non_mono[:5])
        if len(non_mono) > 5: detail += f" ... y {len(non_mono)-5} más"
        errors.append(f"Volumen decrece en {len(non_mono)} punto(s): {detail}")

    # 3. Incrementos anómalos — comparación contra mediana local (ventana ±30 mm)
    inc_map: dict[int, float] = {}
    for i in range(1, len(mm_sorted)):
        if mm_sorted[i] == mm_sorted[i - 1] + 1:
            inc_map[mm_sorted[i]] = vols[mm_sorted[i]] - vols[mm_sorted[i - 1]]

    outliers = []
    if inc_map:
        inc_keys = sorted(inc_map.keys())
        WINDOW    = 30    # mm vecinos a considerar en cada lado
        THRESHOLD = 4.0   # ratio máximo tolerado vs mediana local

        def median(lst):
            s = sorted(lst)
            return s[len(s) // 2]

        for idx, mm in enumerate(inc_keys):
            actual = inc_map[mm]
            # Mediana local excluyendo el punto actual
            neighbors = [
                inc_map[inc_keys[j]]
                for j in range(max(0, idx - WINDOW), min(len(inc_keys), idx + WINDOW + 1))
                if j != idx and inc_map[inc_keys[j]] > 0
            ]
            if len(neighbors) < 5:
                continue
            local_med = median(neighbors)
            if local_med <= 0:
                continue
            ratio = actual / local_med
            if ratio > THRESHOLD or ratio < 1 / THRESHOLD:
                outliers.append((mm, actual, local_med, ratio))

        if outliers:
            detail = "; ".join(
                f"mm={mm}: Δ={act:.3f} (esperado≈{exp:.3f}, ratio={rat:.1f}x)"
                for mm, act, exp, rat in outliers[:8]
            )
            if len(outliers) > 8:
                detail += f" ... y {len(outliers) - 8} más"
            errors.append(
                f"Incremento proporcional anómalo en {len(outliers)} punto(s): {detail}"
            )

    bad_mm = set(missing)
    bad_mm.update(mm for mm, _, _ in non_mono)
    bad_mm.update(mm for mm, _, _, _ in outliers)

    stats = {
        "total_mm": len(vols),
        "rango": f"{min_mm} – {max_mm} mm",
        "vol_min": f"{min(vols.values()):.3f}",
        "vol_max": f"{max(vols.values()):.3f}",
        "faltantes": len(missing),
    }
    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings,
            "stats": stats, "bad_mm": bad_mm, "missing": missing}

# ── Lógica de retry ──────────────────────────────────────────────────────────

def find_pages_to_retry(page_results: list, validation: dict) -> set[int]:
    """Determina qué páginas necesitan re-procesarse con el modelo preciso."""
    bad_mm = validation["bad_mm"]
    missing = set(validation["missing"])
    retry = set()

    for page_num, _img, rows in page_results:
        # Páginas que devolvieron 0 filas
        if not rows:
            retry.add(page_num)
            continue
        # Páginas que contienen mm problemáticos
        for row in rows:
            base = int(row["base_mm"])
            if any((base + i) in bad_mm for i in range(10)):
                retry.add(page_num)
                break

    # Páginas adyacentes a zonas de mm faltantes
    # (la página anterior/siguiente al gap puede tener el borde corrupto)
    if missing:
        min_missing, max_missing = min(missing), max(missing)
        for page_num, _img, rows in page_results:
            if not rows:
                continue
            bases = [int(r["base_mm"]) for r in rows]
            page_max = max(bases) + 9
            page_min = min(bases)
            if page_max >= min_missing - 20 or page_min <= max_missing + 20:
                retry.add(page_num)

    return retry

def estimate_mm_range(page_num: int, page_results: list) -> tuple[int, int]:
    """Estima el rango de mm esperado para una página basándose en sus vecinas."""
    _pn, _img, rows = page_results[page_num - 1]
    if rows:
        bases = [int(r["base_mm"]) for r in rows]
        return min(bases), max(bases) + 9

    # Si la página está vacía, interpolar desde vecinos
    prev_max, next_min = None, None
    for i in range(page_num - 2, -1, -1):
        _, _, r = page_results[i]
        if r:
            prev_max = max(int(x["base_mm"]) for x in r) + 9
            break
    for i in range(page_num, len(page_results)):
        _, _, r = page_results[i]
        if r:
            next_min = min(int(x["base_mm"]) for x in r)
            break

    if prev_max is not None and next_min is not None:
        return prev_max + 1, next_min - 1
    if prev_max is not None:
        return prev_max + 1, prev_max + 200
    if next_min is not None:
        return next_min - 200, next_min - 1
    return 0, 9999

# ── Generación del Excel ─────────────────────────────────────────────────────

def has_decimals(vols: dict) -> bool:
    sample = list(vols.values())[:30]
    return any(isinstance(v, float) and v != int(v) for v in sample)

def generate_excel(vols: dict, tank_name: str, cert_number: str,
                   validation: dict, jump_warnings: list[str],
                   passes_info: str = "", scale_fixes: list[str] = None) -> bytes:
    wb = Workbook()

    # ── Hoja principal ───────────────────────────────────────────────────────
    ws = wb.active
    ws.title = f"TK-{tank_name}"

    TITLE_FILL  = PatternFill("solid", start_color="0D2A4A")
    HEADER_FILL = PatternFill("solid", start_color="1F4E79")
    ALT_FILL    = PatternFill("solid", start_color="D6E4F0")
    ERROR_FILL  = PatternFill("solid", start_color="FF4444")
    thin   = Side(style="thin", color="AAAAAA")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    data_font = Font(name="Arial", size=9)

    ws.merge_cells("A1:B1")
    ws["A1"] = f"TABLA DE LLENADO — TANQUE {tank_name}  |  TAGSA"
    ws["A1"].font = Font(name="Arial", bold=True, color="FFFFFF", size=12)
    ws["A1"].fill = TITLE_FILL
    ws["A1"].alignment = center
    ws.row_dimensions[1].height = 22

    estado = "✅ APROBADA" if validation["ok"] else "❌ CON ERRORES — ver hoja VALIDACIÓN"
    info = [
        ("Razón Social:", "Antivari S.A."),
        ("Tanque Nº:", tank_name),
        ("Certificado INTI Nº:", cert_number),
        ("Unidad:", "dm³"),
        ("Validación:", estado),
    ]
    for i, (lbl, val) in enumerate(info):
        r = i + 2
        ws.cell(r, 1, lbl).font = Font(name="Arial", bold=True, size=9)
        c2 = ws.cell(r, 2, val)
        c2.font = Font(name="Arial", size=9,
                       color="FF0000" if "ERRORES" in str(val) else "000000")
        ws.row_dimensions[r].height = 14

    header_row = len(info) + 2
    for c, h in [(1, "mm"), (2, "dm³")]:
        cell = ws.cell(header_row, c, h)
        cell.font      = Font(name="Arial", bold=True, color="FFFFFF", size=11)
        cell.fill      = HEADER_FILL
        cell.alignment = center
        cell.border    = border
    ws.row_dimensions[header_row].height = 18
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 16
    ws.freeze_panes = f"A{header_row + 1}"

    num_format = "#,##0.000" if has_decimals(vols) else "#,##0"
    bad_mm    = validation.get("bad_mm", set())
    missing_s = set(validation.get("missing", []))
    MISSING_FILL = PatternFill("solid", start_color="FF8C00")  # naranja

    row = header_row + 1
    for mm in range(0, max(vols.keys()) + 1):
        vol = vols.get(mm)

        if vol is None:
            # Fila faltante — siempre visible en el Excel
            c1 = ws.cell(row, 1, mm)
            c1.font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
            c1.alignment = center; c1.border = border; c1.fill = MISSING_FILL

            c2 = ws.cell(row, 2, "FALTANTE")
            c2.font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
            c2.alignment = center; c2.border = border; c2.fill = MISSING_FILL
            row += 1
            continue

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
        c2.alignment = center; c2.border = border
        c2.number_format = num_format
        if fill: c2.fill = fill

        row += 1

    # ── Hoja VALIDACIÓN ──────────────────────────────────────────────────────
    wv = wb.create_sheet("VALIDACIÓN")
    wv.column_dimensions["A"].width = 22
    wv.column_dimensions["B"].width = 90

    def vrow(r, lbl, val, bold=False, color="000000"):
        wv.cell(r, 1, lbl).font = Font(name="Arial", bold=bold, size=9)
        wv.cell(r, 2, val).font = Font(name="Arial", bold=bold, size=9, color=color)

    r = 1
    vrow(r, "REPORTE DE VALIDACIÓN", f"Tanque {tank_name}", bold=True); r += 1
    vrow(r, "Fecha",        datetime.now().strftime("%Y-%m-%d %H:%M")); r += 1
    vrow(r, "Certificado",  cert_number); r += 1
    vrow(r, "Procesamiento", passes_info); r += 1
    vrow(r, "Total mm",     str(validation["stats"].get("total_mm", "—"))); r += 1
    vrow(r, "Rango",        validation["stats"].get("rango", "—")); r += 1
    vrow(r, "Volumen mín",  validation["stats"].get("vol_min", "—") + " dm³"); r += 1
    vrow(r, "Volumen máx",  validation["stats"].get("vol_max", "—") + " dm³"); r += 1
    vrow(r, "MM faltantes", str(validation["stats"].get("faltantes", "—"))); r += 1
    r += 1

    result_txt = "APROBADA" if validation["ok"] else "FALLIDA — REVISAR FILAS EN ROJO"
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

    if jump_warnings:
        vrow(r, "SALTOS DETECTADOS", "", bold=True, color="0000CC"); r += 1
        for w in jump_warnings:
            vrow(r, "", w, color="0000CC"); r += 1
        r += 1

    if scale_fixes:
        vrow(r, "CORRECCIONES DE ESCALA", f"{len(scale_fixes)} valor(es)", bold=True, color="8B008B"); r += 1
        for f in scale_fixes[:20]:
            vrow(r, "", f, color="8B008B"); r += 1
        if len(scale_fixes) > 20:
            vrow(r, "", f"... y {len(scale_fixes)-20} más", color="8B008B"); r += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()

# ── UI ───────────────────────────────────────────────────────────────────────

def get_api_key() -> str:
    try:
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        return ""

def main():
    st.set_page_config(
        page_title="Conversor Tablas INTI — TAGSA",
        page_icon="📊",
        layout="centered",
    )
    st.title("📊 Conversor Tablas de Calibración INTI")
    st.caption("Antivari S.A.")
    st.divider()

    secret_key = get_api_key()
    api_key = secret_key or st.text_input(
        "API Key de Anthropic", type="password",
        help="Configurala como secret en Streamlit Cloud para no escribirla cada vez."
    )

    col1, col2 = st.columns(2)
    with col1:
        tank_name   = st.text_input("Nº de Tanque",         placeholder="ej: TK-81")
    with col2:
        cert_number = st.text_input("Nº Certificado INTI",  placeholder="ej: INTI 2623")

    uploaded = st.file_uploader("Subí el PDF del certificado INTI", type="pdf")
    ready = bool(api_key and tank_name and uploaded)

    if st.button("Convertir a Excel", type="primary", disabled=not ready):
        client = anthropic.Anthropic(api_key=api_key)

        # ── PASADA 1: Haiku (todas las páginas) ──────────────────────────────
        with st.status("Pasada 1 — lectura rápida (Haiku)…", expanded=True) as status1:
            st.write("Convirtiendo páginas a imagen…")
            images = pdf_to_images(uploaded.read())
            st.write(f"PDF tiene {len(images)} página(s).")

            page_results = []   # (page_num, image, rows)
            for i, img in enumerate(images, 1):
                st.write(f"Página {i}/{len(images)}…")
                rows = extract_page(client, img, i, model=MODEL_FAST)
                page_results.append((i, img, rows))
                st.write(f"  → {len(rows)} filas extraídas.")

            vols, jump_warns = build_vols(page_results)
            vols, scale_fixes = normalize_scale(vols)
            if scale_fixes:
                st.info(f"Escala corregida en {len(scale_fixes)} valores (separador de miles).")
            validation_1 = validate_vols(vols)
            p1_label = "Pasada 1 completa — sin errores" if validation_1["ok"] else f"Pasada 1 completa — {len(validation_1['errors'])} error(es)"
            status1.update(label=p1_label, state="complete")

        # ── PASADA 2: Sonnet (solo páginas con problemas) ────────────────────
        pages_to_retry = find_pages_to_retry(page_results, validation_1)
        passes_info = f"1 pasada (Haiku) — sin errores"

        if pages_to_retry and not validation_1["ok"]:
            passes_info = f"2 pasadas — Haiku + Sonnet en {len(pages_to_retry)} página(s)"
            with st.status(f"Pasada 2 — re-procesando {len(pages_to_retry)} página(s) con Sonnet…", expanded=True) as status2:
                for page_num in sorted(pages_to_retry):
                    exp_start, exp_end = estimate_mm_range(page_num, page_results)
                    st.write(f"Re-procesando página {page_num} (mm ~{exp_start}–{exp_end})…")
                    retry_prompt = make_retry_prompt(exp_start, exp_end)
                    _, img, _ = page_results[page_num - 1]
                    new_rows = extract_page(client, img, page_num,
                                            model=MODEL_PRECISE, prompt=retry_prompt)
                    # Reemplazar los rows de esa página
                    page_results[page_num - 1] = (page_num, img, new_rows)
                    st.write(f"  → {len(new_rows)} filas extraídas (antes: {len(page_results[page_num-1][2])}).")

                vols, jump_warns = build_vols(page_results)
                vols, scale_fixes = normalize_scale(vols)
                if scale_fixes:
                    st.info(f"Escala corregida en {len(scale_fixes)} valores (separador de miles).")
                validation_2 = validate_vols(vols)
                p2_label = "Pasada 2 completa — sin errores" if validation_2["ok"] else f"Pasada 2 completa — {len(validation_2['errors'])} error(es) restantes"
                status2.update(label=p2_label, state="complete")
            validation = validation_2
        else:
            validation = validation_1

        st.write("Generando Excel…")
        excel_bytes = generate_excel(vols, tank_name, cert_number or "—",
                                     validation, jump_warns, passes_info, scale_fixes)

        # ── Reporte de validación ─────────────────────────────────────────────
        st.subheader("Reporte de validación")
        s = validation["stats"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total mm",   s["total_mm"])
        c2.metric("Rango",      s["rango"])
        c3.metric("Vol. mín",   s["vol_min"] + " dm³")
        c4.metric("Vol. máx",   s["vol_max"] + " dm³")

        if validation["ok"] and not validation["warnings"]:
            st.success("✅ Validación APROBADA — todos los controles pasaron.")
        elif validation["ok"]:
            st.warning("⚠️ Aprobada con advertencias — revisá los detalles.")
        else:
            st.error("❌ Validación FALLIDA — el Excel tiene errores (filas en rojo). Revisá antes de usar.")

        if validation["errors"]:
            with st.expander("❌ Errores", expanded=True):
                for e in validation["errors"]:
                    st.error(e)
        if validation["warnings"]:
            with st.expander("⚠️ Advertencias"):
                for w in validation["warnings"]:
                    st.warning(w)
        if jump_warns:
            with st.expander("⚠️ Saltos de página detectados"):
                for w in jump_warns:
                    st.warning(w)

        st.divider()
        fname = f"TK-{tank_name}_Tabla_Llenado.xlsx"
        st.download_button(
            label="⬇️ Descargar Excel",
            data=excel_bytes,
            file_name=fname,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary" if validation["ok"] else "secondary",
        )
        if not validation["ok"]:
            st.caption("⚠️ El archivo tiene errores. Las filas problemáticas están marcadas en ROJO. Verificá contra el PDF original antes de subir al sistema.")

if __name__ == "__main__":
    main()
