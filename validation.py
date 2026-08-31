from typing import Optional

# ── Construccion del diccionario mm->dm3 ─────────────────────────────────────


def build_vols(page_results: list) -> dict[int, float]:
    """Convierte la lista de (page_num, rows) en el diccionario pos->vol.
    No asume una cantidad fija de valores por fila: sirve tanto para
    formatos de 10 valores por fila (INTI) como de 1 (Winter Service)."""
    vols = {}
    for _pn, rows in page_results:
        for row in rows:
            base = int(row["pos"])
            for i, v in enumerate(row["values"]):
                if v is not None:
                    vols[base + i] = v
    return vols


# ── Consistencia interna de fila ──────────────────────────────────────────────


def fix_row_scale_consistency(rows: list[dict], tolerance: float = 4.0) -> tuple[list[dict], list[str]]:
    """
    Los 10 valores de una misma fila (mismo base_mm) salen de la MISMA
    lectura de la IA y deberian ser monotonos crecientes con incrementos
    parecidos entre si. Si la IA interpreto un par "punto=decimal vs
    punto=miles" de forma inconsistente DENTRO de la fila (ej: v0 leido como
    decimal y v1..v9 leidos como enteros x1000), fix_scale_errors y
    fix_scale_shift_runs no lo detectan porque comparan contra la tendencia
    de OTRAS paginas, que puede estar igual de "contaminada". Esta funcion
    usa solo el contexto de la propia fila (misma llamada, maxima confianza)
    para detectar y corregir esos saltos.
    """
    fixed_rows = []
    fixes: list[str] = []

    for row in rows:
        base = row["pos"]
        values = list(row["values"])

        incs = [values[i] - values[i - 1] for i in range(1, len(values))
                if values[i] is not None and values[i - 1] is not None
                and values[i] - values[i - 1] > 0]
        if len(incs) < 3:
            fixed_rows.append({"pos": base, "values": values})
            continue
        incs_sorted = sorted(incs)
        med = incs_sorted[len(incs_sorted) // 2]
        if med <= 0:
            fixed_rows.append({"pos": base, "values": values})
            continue

        def close_enough(inc: float) -> bool:
            return abs(inc) <= med * tolerance

        for i in range(1, len(values)):
            prev_v, cur_v = values[i - 1], values[i]
            if prev_v is None or cur_v is None:
                continue
            if close_enough(cur_v - prev_v):
                continue
            for factor, label in ((1 / 1000.0, "/1000"), (1000.0, "x1000")):
                candidate = cur_v * factor
                if close_enough(candidate - prev_v):
                    fixes.append(
                        f"pos={base + i}: {cur_v} -> {candidate} ({label}, consistencia de fila)"
                    )
                    values[i] = candidate
                    break

        fixed_rows.append({"pos": base, "values": values})

    return fixed_rows, fixes


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
        mm = mm_sorted[i]
        mm_next = mm_sorted[i + 1]
        v = corrected[mm]
        v_next = corrected[mm_next]
        if v > v_next * 500:
            v_down = round(v / 1000, 3)
            if v_down <= v_next and v_down >= v_next / 2:
                corrected[mm] = v_down
                fixes.append(f"pos={mm}: {v} -> {v_down} (div1000)")

    for i in range(1, len(mm_sorted)):
        mm = mm_sorted[i]
        mm_prev = mm_sorted[i - 1]
        v = corrected[mm]
        v_prev = corrected[mm_prev]
        if v < v_prev:
            v_up = v * 1000
            if v_up >= v_prev and v_up <= v_prev * 2:
                corrected[mm] = v_up
                fixes.append(f"pos={mm}: {v} -> {v_up} (x1000)")

    return corrected, fixes


# ── Correccion de corrimientos de escala sostenidos ──────────────────────────


def fix_scale_shift_runs(vols: dict, window: int = 20,
                          accept_tolerance: float = 4.0) -> tuple[dict[int, float], list[str]]:
    """
    fix_scale_errors detecta errores de escala AISLADOS (un solo mm fuera de
    lugar entre vecinos correctos). Pero si una pagina entera se lee mal, el
    salto de escala solo se nota en el primer punto: todos los valores
    siguientes quedan igual de mal, pero *consistentes entre si*, y ese tipo
    de corrimiento sostenido pasa desapercibido para las comparaciones
    puntuales.

    Esta funcion mantiene una ventana movil de incrementos "confirmados
    buenos" y, cuando un incremento se dispara ~1000x (o ~1/1000x) respecto
    de esa tendencia, prueba si corrigiendo /1000 (o x1000) el incremento
    vuelve a un rango normal. Si es asi, sigue aplicando la misma correccion
    a los mm siguientes mientras se mantengan consistentes, hasta que la
    racha termina (el valor corregido deja de tener sentido) o se acaban los
    datos.
    """
    mm_sorted = sorted(vols.keys())
    corrected = dict(vols)
    fixes: list[str] = []
    good_increments: list[float] = []
    factor: Optional[float] = None
    run_start: Optional[int] = None

    def median(values: list[float]) -> float:
        s = sorted(values)
        return s[len(s) // 2]

    def close_to_trend(inc: float, med: float) -> bool:
        return med > 0 and inc > 0 and (1 / accept_tolerance) <= (inc / med) <= accept_tolerance

    def close_window(values: list[float]) -> list[float]:
        return values[-window:]

    for i in range(1, len(mm_sorted)):
        mm, mm_prev = mm_sorted[i], mm_sorted[i - 1]
        if mm != mm_prev + 1:
            factor = None  # hueco en la serie: no se puede comparar, cortar racha
            continue

        if factor is not None:
            candidate = corrected[mm] / factor
            med = median(good_increments) if good_increments else 0.0
            if close_to_trend(candidate - corrected[mm_prev], med):
                corrected[mm] = candidate
                good_increments = close_window(good_increments + [candidate - corrected[mm_prev]])
                continue
            fixes.append(
                f"pos={run_start}-{mm_prev}: corregido {'/1000' if factor == 1000 else 'x1000'} "
                f"({mm_prev - run_start + 1} valor(es), corrimiento de escala sostenido)"
            )
            factor = None

        raw_inc = corrected[mm] - corrected[mm_prev]
        med = median(good_increments) if len(good_increments) >= 5 else 0.0

        if med > 0 and not close_to_trend(raw_inc, med):
            # El punto no encaja con la tendencia reciente (puede ser un
            # incremento gigante -> /1000, o incluso negativo si el valor
            # corrupto quedo muy por debajo del anterior -> x1000).
            candidate_div = corrected[mm] / 1000.0
            candidate_mul = corrected[mm] * 1000.0
            if close_to_trend(candidate_div - corrected[mm_prev], med):
                corrected[mm] = candidate_div
                factor = 1000.0
                run_start = mm
                good_increments = close_window(good_increments + [candidate_div - corrected[mm_prev]])
                continue
            if close_to_trend(candidate_mul - corrected[mm_prev], med):
                corrected[mm] = candidate_mul
                factor = 1 / 1000.0
                run_start = mm
                good_increments = close_window(good_increments + [candidate_mul - corrected[mm_prev]])
                continue
            # Anomalia real pero no un simple factor de 1000: no la tocamos
            # (queda para que validate_vols la reporte) y no contaminamos la
            # ventana de tendencia con este punto.
            continue

        if raw_inc > 0:
            good_increments = close_window(good_increments + [raw_inc])

    if factor is not None:
        fixes.append(
            f"pos={run_start}-{mm_sorted[-1]}: corregido {'/1000' if factor == 1000 else 'x1000'} "
            f"({mm_sorted[-1] - run_start + 1} valor(es), corrimiento de escala sostenido "
            f"hasta el final de los datos)"
        )

    return corrected, fixes


# ── Validacion ────────────────────────────────────────────────────────────────


def validate_vols(vols: dict, unit_label: str = "mm") -> dict:
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
        errors.append(f"{unit_label.upper()} faltantes ({len(missing)} valores): {ranges_str}")

    non_mono = []
    prev_vol = None
    for mm in mm_sorted:
        vol = vols[mm]
        if prev_vol is not None and vol < prev_vol:
            non_mono.append((mm, prev_vol, vol))
        prev_vol = vol
    if non_mono:
        detail = "; ".join(f"{unit_label}={mm}: {pv:.3f}->{v:.3f}" for mm, pv, v in non_mono[:5])
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
            detail = "; ".join(f"{unit_label}={mm}: D={act:.3f} (esp~{exp:.3f}, ratio={rat:.1f}x)"
                               for mm, act, exp, rat in outliers[:8])
            if len(outliers) > 8: detail += f" ... y {len(outliers)-8} mas"
            errors.append(f"Incremento anomalo en {len(outliers)} punto(s): {detail}")

    bad_mm = set(missing)
    bad_mm.update(mm for mm, _, _ in non_mono)
    bad_mm.update(mm for mm, _, _, _ in outliers)

    stats = {
        "total_mm": len(vols),
        "rango": f"{min_mm} - {max_mm} {unit_label}",
        "vol_min": f"{min(vols.values()):.3f}",
        "vol_max": f"{max(vols.values()):.3f}",
        "faltantes": len(missing),
    }
    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings,
            "stats": stats, "bad_mm": bad_mm, "missing": missing}


# ── Logica de retry ───────────────────────────────────────────────────────────


def find_pages_to_retry(page_results: list, validation: dict) -> set[int]:
    """No asume una cantidad fija de valores por fila: usa len(row["values"])
    para saber cuantas posiciones cubre cada fila, asi sirve tanto para
    formatos de 10 valores por fila (INTI) como de 1 (Winter Service)."""
    bad_mm = validation["bad_mm"]
    missing_s = set(validation["missing"])
    retry = set()

    for page_num, rows in page_results:
        if not rows:
            retry.add(page_num); continue
        for row in rows:
            base = int(row["pos"])
            if any((base + i) in bad_mm for i in range(len(row["values"]))):
                retry.add(page_num); break

    if missing_s:
        min_m, max_m = min(missing_s), max(missing_s)
        for page_num, rows in page_results:
            if not rows: continue
            spans = [(int(r["pos"]), int(r["pos"]) + len(r["values"]) - 1) for r in rows]
            page_min = min(s[0] for s in spans)
            page_max = max(s[1] for s in spans)
            if page_max >= min_m - 20 and page_min <= max_m + 20:
                retry.add(page_num)

    return retry


def get_prev_context(vols: dict, expected_start: int) -> Optional[tuple[int, float]]:
    """Ultimo (mm, vol) conocido de una tabla de vols finalizada, anterior a
    expected_start. Se usa para darle a la Pasada 2 una pista de continuidad
    de escala sin depender del orden de finalizacion de la Pasada 1."""
    candidates = [mm for mm in vols if mm < expected_start]
    if not candidates:
        return None
    mm = max(candidates)
    return mm, vols[mm]
