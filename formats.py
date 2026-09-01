from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TableFormat:
    """Describe un diseño de tabla de calibración soportado.

    Agregar un formato nuevo es sumar una entrada a FORMATS con su propio
    prompt -- el motor de extraccion/validacion (extraction.py, validation.py)
    no conoce unidades ni layouts especificos, solo trabaja con posiciones
    enteras y valores flotantes."""

    key: str
    label: str            # texto para el selector en la UI
    height_unit: str      # "mm", "cm", etc.
    volume_unit: str      # "dm3", "litros", etc.
    values_per_row: int   # cuantos valores trae cada fila del JSON (10 en INTI, 1 en Winter Service)
    base_prompt: str


INTI_PROMPT = """Esta imagen es una pagina de una tabla de calibracion de tanque industrial certificada por INTI (Argentina).

La tabla tiene este formato:
- Primera columna: valor base en mm (0, 10, 20, 30, ...)
- Columnas 0 a 9: los 10 mm individuales de esa fila (base+0 a base+9)
- Valores en dm3

Extrae TODOS los datos en este JSON exacto:
{
  "rows": [
    {"pos": 0, "values": [v0, v1, v2, v3, v4, v5, v6, v7, v8, v9]},
    {"pos": 10, "values": [v0, v1, v2, v3, v4, v5, v6, v7, v8, v9]}
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
  Copia cada digito de la imagen tal cual esta impreso -- NUNCA generes una
  secuencia matematica o un patron por tu cuenta, aunque los valores
  cercanos parezcan seguir una progresion regular.
  Responde UNICAMENTE con el JSON, sin texto adicional ni bloques de codigo."""


WINTER_SERVICE_PROMPT = """Esta imagen es una pagina de una tabla de calibracion de tanque industrial
(formato "Winter Service S.A." / T.A.G.S.A.).

La tabla tiene 4 bloques de columnas lado a lado, cada uno con una columna
"cm." y una columna "lts." Los 4 bloques son la CONTINUACION de la misma
secuencia (no son 4 tanques distintos): el bloque 1 cubre los primeros ~50
valores, el bloque 2 continua donde termino el bloque 1, y asi con el 3 y el 4.
Por ejemplo, si el bloque 1 va de cm 0 a 49, el bloque 2 va de cm 50 a 99, el
bloque 3 de 100 a 149, y el bloque 4 de 150 a 199.

Para reducir errores, agrupa los datos de a 10 filas consecutivas de la
columna "cm." (aunque en la imagen sean 10 lineas impresas por separado).
Extrae TODOS los datos, en orden (bloque 1 de arriba a abajo agrupando de a
10, despues bloque 2, despues bloque 3, despues bloque 4), en este JSON
exacto:
{
  "rows": [
    {"pos": 0, "values": [412.050, 412.988, 413.926, 414.864, 415.802, 416.740, 417.678, 418.616, 419.554, 420.492]},
    {"pos": 10, "values": [421.430, 422.368, 423.306, 424.244, 425.182, 426.120, 427.058, 427.996, 428.934, 429.872]}
  ]
}
(Los numeros de este ejemplo son inventados solo para mostrar el formato del
JSON -- NO tienen relacion con la imagen real que vas a leer. Ignoralos por
completo al extraer los datos reales.)

Reglas CRITICAS:

NUMEROS: en este formato el punto (.) es SIEMPRE separador DECIMAL (no de
miles). Los valores de "lts." tienen siempre 3 decimales, ej: 74.407 son
setenta y cuatro litros con cuatrocientos siete mililitros, NO 74407.

FORMATO:
  "pos" es el "cm." de la PRIMERA fila del grupo de 10.
  Cada fila del JSON tiene EXACTAMENTE 10 valores en orden creciente de cm
  (null si alguna celda esta vacia).
  Copia cada digito de la imagen uno por uno, tal cual esta impreso --
  NUNCA generes una secuencia matematica o un patron por tu cuenta (ej NO
  hagas 102, 204, 306...), aunque los valores parezcan seguir una
  progresion regular: cada numero debe salir de lo que ves impreso, no de
  un calculo.
  Si la pagina no tiene tabla (caratula, texto, firma), devuelve {"rows": []}.
  IGNORAR encabezados (nombre de empresa, tanque, direccion), pie de pagina
  (FECHA, HOJA, "Punto de referencia"), firmas y sellos.
  Responde UNICAMENTE con el JSON, sin texto adicional ni bloques de codigo."""


FORMATS: dict[str, TableFormat] = {
    "inti": TableFormat(
        key="inti",
        label="INTI (mm, filas de 10 valores)",
        height_unit="mm",
        volume_unit="dm3",
        values_per_row=10,
        base_prompt=INTI_PROMPT,
    ),
    "winter_service": TableFormat(
        key="winter_service",
        label="Winter Service (cm, 4 columnas)",
        height_unit="cm",
        volume_unit="litros",
        values_per_row=10,
        base_prompt=WINTER_SERVICE_PROMPT,
    ),
}


def make_retry_prompt(fmt: TableFormat, expected_start: int, expected_end: int,
                       prev_pos: Optional[int] = None, prev_vol: Optional[float] = None) -> str:
    u, v = fmt.height_unit, fmt.volume_unit
    prompt = fmt.base_prompt + f"""

ATENCION ESPECIAL - esta pagina se esta re-procesando porque la lectura anterior
tuvo un error de validacion (faltan valores, o la secuencia no es consistente):
Se espera que esta pagina cubra APROXIMADAMENTE {u} {expected_start} a {expected_end},
pero ese rango es una ESTIMACION basada en la pagina anterior, no una certeza.

NO ASUMAS que la lectura anterior conto bien las filas ni les puso la etiqueta
"{u}" correcta en la primera columna. Volve a contar CADA fila de la imagen desde
cero, columna por columna, y anota el numero exacto que esta impreso en la
columna "{u}" de cada fila (no lo deduzcas por continuidad). Es comun que una
pagina tenga MAS filas de las detectadas antes, o que la primera columna se
haya leido con un digito equivocado. Revisa cada digito con maxima precision:
es una calibracion industrial critica.

IMPORTANTE: copia cada numero de la imagen, digito por digito. NUNCA generes
una secuencia matematica ni completes un patron por tu cuenta (ej: no
inventes 102, 204, 306... solo porque "parece" una progresion): si no podes
leer un valor con claridad, es preferible dejarlo como null a inventarlo."""
    if prev_pos is not None:
        prompt += f"""

El ultimo valor CONFIRMADO de la pagina anterior fue {u}={prev_pos}, volumen={prev_vol:.3f} {v}.
La primera fila de esta pagina deberia continuar inmediatamente despues de ese {u},
y los volumenes deben ser mayores y en la misma escala."""
    return prompt
