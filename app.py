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
- Valores en dm³ (pueden ser enteros o con hasta 3 decimales)

Extraé TODOS los datos en este JSON exacto:
{
  "rows": [
    {"base_mm": 0, "values": [v0, v1, v2, v3, v4, v5, v6, v7, v8, v9]},
    {"base_mm": 10, "values": [v0, v1, v2, v3, v4, v5, v6, v7, v8, v9]}
  ]
}

Reglas estrictas:
- Copiá exactamente los números que ves, sin redondear ni interpolar
- Si una celda está vacía (última fila incompleta), usá null
- Si la página no contiene tabla de datos (solo carátula o texto), devolvé {"rows": []}
- Respondé ÚNICAMENTE con el JSON, sin texto adicional ni bloques de código"""


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

def build_vols(all_rows: list[dict]) -> dict[int, float]:
    vols = {}
    for row in all_rows:
        base = int(row["base_mm"])
        for i, v in enumerate(row["values"]):
            if v is not None:
                vols[base + i] = v
    return vols


def has_decimals(vols: dict) -> bool:
    sample = list(vols.values())[:30]
    return any(isinstance(v, float) and v != int(v) for v in sample)


# ── Generación del Excel ─────────────────────────────────────────────────────

def generate_excel(vols: dict, tank_name: str, cert_number: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = f"TK-{tank_name}"

    TITLE_FILL  = PatternFill("solid", start_color="0D2A4A")
    HEADER_FILL = PatternFill("solid", start_color="1F4E79")
    ALT_FILL    = PatternFill("solid", start_color="D6E4F0")
    thin   = Side(style="thin", color="AAAAAA")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    data_font = Font(name="Arial", size=9)

    # Título
    ws.merge_cells("A1:B1")
    ws["A1"] = f"TABLA DE LLENADO — TANQUE {tank_name}  |  TAGSA"
    ws["A1"].font = Font(name="Arial", bold=True, color="FFFFFF", size=12)
    ws["A1"].fill = TITLE_FILL
    ws["A1"].alignment = center
    ws.row_dimensions[1].height = 22

    # Info certificado
    info = [
        ("Razón Social:", "ODFJELL TERMINALS TAGSA S.A."),
        ("Tanque Nº:", tank_name),
        ("Certificado INTI Nº:", cert_number),
        ("Unidad:", "dm³"),
    ]
    for i, (lbl, val) in enumerate(info):
        r = i + 2
        c1 = ws.cell(r, 1, lbl)
        c1.font = Font(name="Arial", bold=True, size=9)
        c2 = ws.cell(r, 2, val)
        c2.font = Font(name="Arial", size=9)
        ws.row_dimensions[r].height = 14

    # Encabezados
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

    # Datos
    row = header_row + 1
    for mm in range(0, max(vols.keys()) + 1):
        vol = vols.get(mm)
        if vol is None:
            continue
        is_alt = (mm // 10) % 2 == 1

        c1 = ws.cell(row, 1, mm)
        c1.font = data_font
        c1.alignment = center
        c1.border = border
        if is_alt:
            c1.fill = ALT_FILL

        c2 = ws.cell(row, 2, vol)
        c2.font = data_font
        c2.alignment = center
        c2.border = border
        c2.number_format = num_format
        if is_alt:
            c2.fill = ALT_FILL

        row += 1

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
    st.caption("ODFJELL TERMINALS TAGSA S.A.")
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
            vols = build_vols(all_rows)
            excel_bytes = generate_excel(vols, tank_name, cert_number or "—")

            status.update(label="¡Listo!", state="complete")

        st.success(f"Se procesaron **{len(vols)}** puntos de medición (mm 0 a {max(vols.keys())}).")
        fname = f"TK-{tank_name}_Tabla_Llenado.xlsx"
        st.download_button(
            label="⬇️ Descargar Excel",
            data=excel_bytes,
            file_name=fname,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

if __name__ == "__main__":
    main()
