from typing import Optional

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
        mm = mm_sorted[i]
        mm_next = mm_sorted[i + 1]
        v = corrected[mm]
        v_next = corrected[mm_next]
        if v > v_next * 500:
            v_down = round(v / 1000, 3)
            if v_down <= v_next and v_down >= v_next / 2:
                corrected[mm] = v_down
                fixes.append(f"mm={mm}: {v} -> {v_down} (div1000)")

    for i in range(1, len(mm_sorted)):
        mm = mm_sorted[i]
        mm_prev = mm_sorted[i - 1]
        v = corrected[mm]
        v_prev = corrected[mm_prev]
        if v < v_prev:
            v_up = v * 1000
            if v_up >= v_prev and v_up <= v_prev * 2:
                corrected[mm] = v_up
                fixes.append(f"mm={mm}: {v} -> {v_up} (x1000)")

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
                f"mm={run_start}-{mm_prev}: corregido {'/1000' if factor == 1000 else 'x1000'} "
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
            f"mm={run_start}-{mm_sorted[-1]}: corregido {'/1000' if factor == 1000 else 'x1000'} "
            f"({mm_sorted[-1] - run_start + 1} valor(es), corrimiento de escala sostenido "
            f"hasta el final de los datos)"
        )

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
    bad_mm = validation["bad_mm"]
    missing_s = set(validation["missing"])
    retry = set()

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


def get_prev_context(vols: dict, expected_start: int) -> Optional[tuple[int, float]]:
    """Ultimo (mm, vol) conocido de una tabla de vols finalizada, anterior a
    expected_start. Se usa para darle a la Pasada 2 una pista de continuidad
    de escala sin depender del orden de finalizacion de la Pasada 1."""
    candidates = [mm for mm in vols if mm < expected_start]
    if not candidates:
        return None
    mm = max(candidates)
    return mm, vols[mm]
