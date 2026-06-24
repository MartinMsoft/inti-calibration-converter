import streamlit as st
import anthropic
import json
import io
import base64
import re
from pdf2image import convert_from_bytes
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# ── Helpers de imagen ────────────────────────────────────────────────────────

def pdf_to_images(pdf_bytes: bytes):
    return convert_from_bytes(pdf_bytes, dpi=200)

def image_to_base64(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")

# ── Extracción con Claude Vision ─────────────────────────────────────────────

EXTRACT_PROMPT = """Esta imagen contiene una tabla de calibración de tanque industrial certificada por INTI (Argentina).

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

Reglas CRÍTICAS — leé con atención:

NÚMEROS:
- El punto (.) es separador de MILES, NO decimal. Ejemplos:
    788.068 → devolvé el entero 788068
    1.160.362 → devolvé el entero 1160362
    27.344 → si solo hay UN punto y el número es pequeño, es decimal: devolvé 27.344
- Si hay DOS o más puntos en un número (ej: 1.160.362), siempre es entero: quitá los puntos y devolvé el número entero.

NÚMEROS DE PÁGINA y ENCABEZADOS:
- La imagen puede tener números de página (ej: "Página 12", "12", "101") impresos fuera de la tabla.
- IGNORÁ completamente cualquier número de página, encabezado, pie de página, firma o texto que NO sea parte de la tabla de datos.
- Los números de página NO son datos de la tabla.

FORMATO:
- Cada fila tiene EXACTAMENTE 10 valores (uno por cada columna 0-9). Nunca más, nunca menos (salvo null al final de la última fila incompleta).
- Si una celda está vacía (última fila incompleta), usá null.
- Si la página no contiene tabla de datos (solo carátula o texto), devolvé {"rows": []}.
- Respondé ÚNICAMENTE con el JSON, sin texto adicional ni bloques de código."""


def extract_page(client: anthropic.Anthropic, image, page_num: int) -> list[dict]:
    image_b64 = image_to_base64(image)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                },
                {"type": "text", "text": EXTRACT_PROMPT},
            ],
        }],
    )

    raw = response.content[0].text.strip()

    # Limpiar posibles bloques markdown que Claude agrega a veces
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        data = json.loads(raw)
        return data.get("rows", [])
    except json.JSONDecodeError:
        st.warning(f"Página {page_num}: no se pudo parsear la respuesta. Se omite.")
        return []


# ── Construcción del diccionario mm→dm³ ─────────────────────────────────────

def build_vols(all_rows: list[dict]) -> tuple[dict[int, float], list[str]]:
    """
    Devuelve (vols, jump_warnings).
    Detecta saltos imposibles de base_mm y los reporta SIN corregir,
    para no colocar volúmenes en posiciones incorrectas.
    """
    jump_warnings = []
    vols = {}
    prev_base = None

    for row in all_rows:
        base = int(row["base_mm"])

        if prev_base is not None and base != prev_base + 10:
            expected = prev_base + 10
            jump_warnings.append(
                f"Salto en base_mm: se esperaba {expected} pero el PDF entregó {base} "
                f"(diferencia: {base - expected}). Verificar manualmente esa zona del PDF."
            )

        prev_base = base

        for i, v in enumerate(row["values"][:10]):
            if v is not None:
                vols[base + i] = v

    return vols, jump_warnings


def has_decimals(vols: dict) -> bool:
    sample = list(vols.values())[:30]
    return any(isinstance(v, float) and v != int(v) for v in sample)


# ── Validación automática ────────────────────────────────────────────────────

def validate_vols(vols: dict) -> dict:
    """
    Valida la integridad del diccionario mm→dm³.
    Retorna {"ok": bool, "errors": [...], "warnings": [...], "stats": {...}}
    """
    errors   = []
    warnings = []

    if not vols:
        return {"ok": False, "errors": ["No hay datos."], "warnings": [], "stats": {}}

    mm_sorted = sorted(vols.keys())
    min_mm, max_mm = mm_sorted[0], mm_sorted[-1]
    total_expected = max_mm - min_mm + 1

    # 1. MM correlativos: ningún valor faltante
    missing = [mm for mm in range(min_mm, max_mm + 1) if mm not in vols]
    if missing:
        # Agrupar rangos contiguos para no listar miles de números
        groups = []
        start = missing[0]
        prev  = missing[0]
        for m in missing[1:]:
            if m == prev + 1:
                prev = m
            else:
                groups.append((start, prev))
                start = prev = m
        groups.append((start, prev))
        ranges_str = ", ".join(
            str(a) if a == b else f"{a}-{b}" for a, b in groups[:10]
        )
        if len(groups) > 10:
            ranges_str += f" ... y {len(groups) - 10} rangos más"
        errors.append(f"MM faltantes ({len(missing)} valores): {ranges_str}")

    # 2. Volumen siempre creciente (no puede disminuir)
    non_mono = []
    prev_vol = None
    for mm in mm_sorted:
        vol = vols[mm]
        if prev_vol is not None and vol < prev_vol:
            non_mono.append((mm, prev_vol, vol))
        prev_vol = vol
    if non_mono:
        sample = non_mono[:5]
        detail = "; ".join(f"mm={mm}: {pv:.3f}→{v:.3f}" for mm, pv, v in sample)
        if len(non_mono) > 5:
            detail += f" ... y {len(non_mono) - 5} más"
        errors.append(f"Volumen decrece en {len(non_mono)} punto(s): {detail}")

    # 3. Saltos bruscos de volumen entre mm consecutivos
    increments = []
    for i in range(1, len(mm_sorted)):
        mm_curr = mm_sorted[i]
        mm_prev = mm_sorted[i - 1]
        if mm_curr == mm_prev + 1:  # solo comparar mm consecutivos
            increments.append(vols[mm_curr] - vols[mm_prev])

    if increments:
        avg_inc = sum(increments) / len(increments)
        # Alertar si algún incremento es >20x el promedio o negativo
        outliers = [
            (mm_sorted[i + 1], increments[i])
            for i in range(len(increments))
            if increments[i] > avg_inc * 20 or increments[i] < 0
        ]
        if outliers:
            sample = outliers[:5]
            detail = "; ".join(f"mm={mm}: Δ={d:.3f}" for mm, d in sample)
            warnings.append(f"Incrementos anómalos en {len(outliers)} punto(s): {detail}")

    stats = {
        "total_mm": len(vols),
        "rango": f"{min_mm} – {max_mm} mm",
        "vol_min": f"{min(vols.values()):.3f}",
        "vol_max": f"{max(vols.values()):.3f}",
        "faltantes": len(missing),
    }

    # Conjunto de mm problemáticos para marcar en rojo en el Excel
    bad_mm: set[int] = set(missing)
    bad_mm.update(mm for mm, _, _ in non_mono)
    bad_mm.update(mm for mm, _ in outliers) if increments else None

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
        "bad_mm": bad_mm,
    }


# ── Generación del Excel ─────────────────────────────────────────────────────

def generate_excel(vols: dict, tank_name: str, cert_number: str,
                   validation: dict, jump_warnings: list[str]) -> bytes:
    from datetime import datetime
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
        c1 = ws.cell(r, 1, lbl)
        c1.font = Font(name="Arial", bold=True, size=9)
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
    bad_mm = validation.get("bad_mm", set())

    row = header_row + 1
    for mm in range(0, max(vols.keys()) + 1):
        vol = vols.get(mm)
        if vol is None:
            continue
        is_bad = mm in bad_mm
        is_alt = (mm // 10) % 2 == 1

        fill = ERROR_FILL if is_bad else (ALT_FILL if is_alt else None)
        font_color = "FFFFFF" if is_bad else "000000"

        c1 = ws.cell(row, 1, mm)
        c1.font = Font(name="Arial", size=9, bold=is_bad, color=font_color)
        c1.alignment = center
        c1.border = border
        if fill: c1.fill = fill

        c2 = ws.cell(row, 2, vol)
        c2.font = Font(name="Arial", size=9, bold=is_bad, color=font_color)
        c2.alignment = center
        c2.border = border
        c2.number_format = num_format
        if fill: c2.fill = fill

        row += 1

    # ── Hoja VALIDACIÓN ──────────────────────────────────────────────────────
    wv = wb.create_sheet("VALIDACIÓN")
    wv.column_dimensions["A"].width = 20
    wv.column_dimensions["B"].width = 80

    def val_row(r, label, value, bold=False, color="000000"):
        c1 = wv.cell(r, 1, label)
        c1.font = Font(name="Arial", bold=bold, size=9)
        c2 = wv.cell(r, 2, value)
        c2.font = Font(name="Arial", bold=bold, size=9, color=color)

    r = 1
    val_row(r, "REPORTE DE VALIDACIÓN", f"Tanque {tank_name}", bold=True); r += 1
    val_row(r, "Fecha generación", datetime.now().strftime("%Y-%m-%d %H:%M")); r += 1
    val_row(r, "Certificado INTI", cert_number); r += 1
    val_row(r, "Total mm procesados", str(validation["stats"]["total_mm"])); r += 1
    val_row(r, "Rango", validation["stats"]["rango"]); r += 1
    val_row(r, "Volumen mín (dm³)", validation["stats"]["vol_min"]); r += 1
    val_row(r, "Volumen máx (dm³)", validation["stats"]["vol_max"]); r += 1
    val_row(r, "MM faltantes", str(validation["stats"]["faltantes"])); r += 1
    r += 1

    result_txt = "APROBADA" if validation["ok"] else "FALLIDA — NO USAR"
    result_col = "008000" if validation["ok"] else "FF0000"
    val_row(r, "RESULTADO", result_txt, bold=True, color=result_col); r += 2

    if validation["errors"]:
        val_row(r, "ERRORES", "", bold=True, color="FF0000"); r += 1
        for e in validation["errors"]:
            val_row(r, "", e, color="FF0000"); r += 1
        r += 1

    if validation["warnings"]:
        val_row(r, "ADVERTENCIAS", "", bold=True, color="CC6600"); r += 1
        for w in validation["warnings"]:
            val_row(r, "", w, color="CC6600"); r += 1
        r += 1

    if jump_warnings:
        val_row(r, "SALTOS DE PÁGINA", "", bold=True, color="0000CC"); r += 1
        for w in jump_warnings:
            val_row(r, "", w, color="0000CC"); r += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


# ── UI ───────────────────────────────────────────────────────────────────────

def get_api_key() -> str:
    """Lee la API key desde secrets de Streamlit o pide al usuario."""
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

    # API key
    secret_key = get_api_key()
    if secret_key:
        api_key = secret_key
    else:
        api_key = st.text_input(
            "API Key de Anthropic",
            type="password",
            help="Ingresá tu clave de API. Para no escribirla cada vez, configurala como secret en Streamlit Cloud.",
        )

    col1, col2 = st.columns(2)
    with col1:
        tank_name = st.text_input("Nº de Tanque", placeholder="ej: TK-81")
    with col2:
        cert_number = st.text_input("Nº Certificado INTI", placeholder="ej: INTI 2623")

    uploaded = st.file_uploader("Subí el PDF del certificado INTI", type="pdf")

    ready = bool(api_key and tank_name and uploaded)

    if st.button("Convertir a Excel", type="primary", disabled=not ready):
        client = anthropic.Anthropic(api_key=api_key)

        with st.status("Procesando PDF...", expanded=True) as status:
            st.write("Convirtiendo páginas a imagen…")
            images = pdf_to_images(uploaded.read())
            st.write(f"PDF tiene {len(images)} página(s).")

            all_rows: list[dict] = []
            for i, img in enumerate(images, 1):
                st.write(f"Leyendo página {i}/{len(images)}…")
                rows = extract_page(client, img, i)
                all_rows.extend(rows)
                st.write(f"  → {len(rows)} filas extraídas.")

            if not all_rows:
                status.update(label="Error", state="error")
                st.error("No se encontraron datos en el PDF. Verificá que sea una tabla de calibración INTI.")
                st.stop()

            st.write("Generando Excel…")
            vols, jump_warns = build_vols(all_rows)

            st.write("Validando integridad de los datos…")
            validation = validate_vols(vols)
            excel_bytes = generate_excel(vols, tank_name, cert_number or "—",
                                         validation, jump_warns)

            status.update(label="¡Listo!", state="complete")

        # ── Reporte de validación ──────────────────────────────────────────
        st.subheader("Reporte de validación")

        s = validation["stats"]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total mm procesados", s["total_mm"])
        col2.metric("Rango", s["rango"])
        col3.metric("Volumen mín", s["vol_min"] + " dm³")
        col4.metric("Volumen máx", s["vol_max"] + " dm³")

        if validation["ok"] and not validation["warnings"]:
            st.success("✅ Validación APROBADA — todos los controles pasaron correctamente.")
        elif validation["ok"] and validation["warnings"]:
            st.warning("⚠️ Validación con ADVERTENCIAS — revisá los detalles antes de usar el archivo.")
        else:
            st.error("❌ Validación FALLIDA — el archivo tiene errores. NO usar hasta corregir.")

        if validation["errors"]:
            with st.expander("❌ Errores encontrados", expanded=True):
                for e in validation["errors"]:
                    st.error(e)

        if validation["warnings"]:
            with st.expander("⚠️ Advertencias", expanded=True):
                for w in validation["warnings"]:
                    st.warning(w)

        if jump_warns:
            with st.expander("⚠️ Saltos de página detectados (requieren verificación manual)", expanded=False):
                for w in jump_warns:
                    st.warning(w)

        st.divider()

        fname = f"TK-{tank_name}_Tabla_Llenado.xlsx"
        if validation["ok"]:
            st.download_button(
                label="⬇️ Descargar Excel",
                data=excel_bytes,
                file_name=fname,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            st.download_button(
                label="⬇️ Descargar Excel igualmente (con errores)",
                data=excel_bytes,
                file_name=fname,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="secondary",
            )
            st.caption("⚠️ Se recomienda NO usar este archivo hasta resolver los errores indicados.")

if __name__ == "__main__":
    main()
