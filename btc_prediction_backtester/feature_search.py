"""Buscar mejores features, iterando SOLO contra validacion.

Hasta ahora se probo un unico conjunto de 15 features. Nunca se busco algo
mejor, por miedo a sobreajustar -- pero eso no se resuelve dejando de buscar,
se resuelve buscando donde corresponde. El 20% de validacion existe
exactamente para esto; el 20% de test no se toca hasta el final, una sola vez.

Se agregan ~20 features candidatas. Las que mas prometen, y por que:

  - MOVIMIENTOS NORMALIZADOS POR VOLATILIDAD (mv_5_z, mv_15_z, mv_60_z).
    El modelo actual tiene el movimiento en % y la volatilidad por separado,
    pero nunca su cociente. Moverse 0.1% en un mercado quieto y moverse 0.1%
    en uno violento son eventos totalmente distintos, y esa distincion no
    esta expresada en ninguna de las 15 features actuales.
  - EXPANSION DE VOLATILIDAD (vol_ratio, range_expansion). No importa solo
    cuanta volatilidad hay, sino si viene subiendo o bajando.
  - ACELERACION DEL FLUJO (flow_accel). El flujo de 5min contra el de 15min:
    si los compradores se estan retirando, el nivel no lo dice, el cambio si.
  - MECHAS (wick_up, wick_down). Rechazo de precio: una mecha larga arriba es
    oferta apareciendo, y eso no se ve en el cierre.
  - HORA DEL DIA (hour_sin, hour_cos). Las sesiones de Asia, Europa y EEUU no
    se comportan igual, y hasta ahora el modelo no sabia que hora era.

    python3 feature_search.py --data data/btcusdt_1m_long.csv
"""
import argparse
import math
import statistics

import predict_model as pm

L2_REF = 1000.0
N_REF = 252428
MARGIN = 0.05
ITERS = 5
ROUND_MS = 5 * 60 * 1000

BASE = [
    "eff_15", "eff_60", "flow15_x_volrank", "flow_15", "flow_5", "flow_60",
    "mv15_x_eff15", "mv5_x_volrank", "mv_15", "mv_5", "mv_60",
    "pos_240", "pos_60", "vol_15", "vol_rank",
]


def extra_features(by_time, start, base):
    """Las candidatas nuevas, todas de velas cerradas ANTES de `start`."""

    def px(ms):
        c = by_time.get(ms)
        return c["open"] if c else None

    def win(minutes):
        out = []
        for m in range(minutes, 0, -1):
            c = by_time.get(start - m * 60_000)
            if c:
                out.append(c)
        return out

    ahora = px(start)
    if ahora is None:
        return None

    f = {}

    # --- movimientos normalizados por la volatilidad del momento ---
    v15 = base.get("vol_15")
    if not v15:
        return None
    for h in (5, 15, 60):
        mv = base.get(f"mv_{h}")
        if mv is None:
            return None
        # vol_15 es por minuto; sobre h minutos escala con raiz de h
        f[f"mv_{h}_z"] = mv / (v15 * math.sqrt(h)) if v15 else 0.0

    # --- la volatilidad viene subiendo o bajando ---
    seg60 = win(60)
    if len(seg60) < 30:
        return None
    px60 = [c["open"] for c in seg60]
    rets60 = [(px60[i] - px60[i - 1]) / px60[i - 1] for i in range(1, len(px60)) if px60[i - 1]]
    v60 = statistics.pstdev(rets60) * 100 if len(rets60) > 1 else None
    if not v60:
        return None
    f["vol_ratio"] = v15 / v60

    seg15 = win(15)
    if len(seg15) < 10:
        return None
    r15 = (max(c["high"] for c in seg15) - min(c["low"] for c in seg15)) / ahora * 100
    r60 = (max(c["high"] for c in seg60) - min(c["low"] for c in seg60)) / ahora * 100
    f["range_expansion"] = r15 / (r60 / 4) if r60 else 1.0

    # --- el flujo se acelera o se apaga ---
    f5, f15, f60 = base.get("flow_5"), base.get("flow_15"), base.get("flow_60")
    if None in (f5, f15, f60):
        return None
    f["flow_accel"] = f5 - f15
    f["flow_drift"] = f15 - f60

    # --- mechas: rechazo de precio que el cierre no muestra ---
    cuerpo = sum(abs(c["close"] - c["open"]) for c in seg15)
    arriba = sum(c["high"] - max(c["open"], c["close"]) for c in seg15)
    abajo = sum(min(c["open"], c["close"]) - c["low"] for c in seg15)
    total = cuerpo + arriba + abajo
    f["wick_up"] = arriba / total if total else 0.0
    f["wick_down"] = abajo / total if total else 0.0

    # --- donde cerro dentro de su propio rango de 15min ---
    hi15 = max(c["high"] for c in seg15)
    lo15 = min(c["low"] for c in seg15)
    f["pos_15"] = (ahora - lo15) / (hi15 - lo15) if hi15 > lo15 else 0.5

    # --- volumen: mucho o poco para lo que suele haber ---
    v_15 = sum(c["volume"] for c in seg15)
    v_60 = sum(c["volume"] for c in seg60)
    f["vol_surge"] = v_15 / (v_60 / 4) if v_60 else 1.0

    # --- distancia al VWAP de una hora ---
    vv = sum(c["volume"] for c in seg60)
    if vv:
        vwap = sum(((c["high"] + c["low"] + c["close"]) / 3) * c["volume"] for c in seg60) / vv
        f["dist_vwap"] = (ahora - vwap) / ahora * 100
    else:
        f["dist_vwap"] = 0.0

    # --- persistencia: cuantos minutos seguidos en la misma direccion ---
    dirs = [1 if c["close"] > c["open"] else -1 for c in seg15]
    racha = 1
    for i in range(len(dirs) - 1, 0, -1):
        if dirs[i] == dirs[i - 1]:
            racha += 1
        else:
            break
    f["run_len"] = racha * dirs[-1] if dirs else 0

    # --- autocorrelacion de los retornos de 1 minuto (reversion vs momentum) ---
    r = rets60[-30:]
    if len(r) > 5:
        mr = statistics.fmean(r)
        num = sum((r[i] - mr) * (r[i - 1] - mr) for i in range(1, len(r)))
        den = sum((x - mr) ** 2 for x in r)
        f["autocorr"] = num / den if den else 0.0
    else:
        f["autocorr"] = 0.0

    # --- hora del dia: Asia, Europa y EEUU no se comportan igual ---
    hora = (start // 3600_000) % 24
    f["hour_sin"] = math.sin(2 * math.pi * hora / 24)
    f["hour_cos"] = math.cos(2 * math.pi * hora / 24)

    return f


def build(candles):
    by_time = {c["open_time_ms"]: c for c in candles}
    starts = sorted(t for t in by_time if t % ROUND_MS == 0)
    rows = []
    for s in starts:
        end, op = by_time.get(s + ROUND_MS), by_time.get(s)
        if end is None or op is None:
            continue
        base = pm.features_at(by_time, s)
        if base is None:
            continue
        ex = extra_features(by_time, s, base)
        if ex is None:
            continue
        base.update(ex)
        rows.append({"t": s, "x": base, "y": 1 if end["open"] > op["open"] else 0})
    return rows


# La busqueda hace ~10 ajustes con hasta 35 features. Sobre 252k filas eso son
# varios minutos cada uno en Python puro, asi que la BUSQUEDA usa una submuestra
# uniforme. Es para ordenar candidatas, no para el numero final -- ese sale del
# test, con todos los datos y una sola mirada.
SAMPLE = 100_000


def probar(tr_raw, va_raw, keys, etiqueta):
    if len(tr_raw) > SAMPLE:
        paso = len(tr_raw) / SAMPLE
        tr_raw = [tr_raw[int(i * paso)] for i in range(SAMPLE)]
    tr, stats = pm.standardize(tr_raw, keys)
    va, _ = pm.standardize(va_raw, keys, stats)
    l2 = L2_REF * len(tr_raw) / N_REF
    w, b = pm.train_logistic_newton(tr, keys, l2=l2, iters=ITERS)

    hc = nc = ht = 0
    for d in va:
        p = pm.predict(w, b, d["v"])
        if (p >= 0.5) == (d["y"] == 1):
            ht += 1
        if abs(p - 0.5) >= MARGIN:
            nc += 1
            if (p >= 0.5) == (d["y"] == 1):
                hc += 1
    pc, lc, _ = pm.wilson(hc, nc) if nc else (0, 0, 0)
    pt, _, _ = pm.wilson(ht, len(va))
    print(f"  {etiqueta:<34} conf {pc*100:>6.2f}% (n={nc:<6}) lim.inf {lc*100:>5.2f}%"
          f"   siempre {pt*100:>6.2f}%")
    return pc, lc, nc, pt, w, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    args = ap.parse_args()

    print("Cargando y construyendo features...")
    rows = build(pm.load_csv(args.data))
    rows.sort(key=lambda r: r["t"])
    todas = sorted(rows[0]["x"].keys())
    nuevas = [k for k in todas if k not in BASE]
    print(f"Rondas: {len(rows):,}   features base: {len(BASE)}   nuevas: {len(nuevas)}")
    print(f"Nuevas: {', '.join(nuevas)}\n")

    n = len(rows)
    a, bnd = int(n * 0.6), int(n * 0.8)
    tr_raw, va_raw = rows[:a], rows[a:bnd]
    print(f"Train {len(tr_raw):,} | validacion {len(va_raw):,}   "
          f"(el test NO se toca en este script)\n")

    print("Todo lo de abajo se mide contra VALIDACION:\n")
    base = probar(tr_raw, va_raw, BASE, "base (las 15 actuales)")
    todo = probar(tr_raw, va_raw, todas, f"base + las {len(nuevas)} nuevas")

    print("\n  Agregando de a un grupo sobre la base:")
    grupos = {
        "mv normalizados por vol": ["mv_5_z", "mv_15_z", "mv_60_z"],
        "expansion de volatilidad": ["vol_ratio", "range_expansion"],
        "aceleracion del flujo": ["flow_accel", "flow_drift"],
        "mechas": ["wick_up", "wick_down"],
        "posicion 15m": ["pos_15"],
        "volumen/vwap": ["vol_surge", "dist_vwap"],
        "persistencia/autocorr": ["run_len", "autocorr"],
        "hora del dia": ["hour_sin", "hour_cos"],
    }
    resultados = {"base": base, "todas": todo}
    for nombre, gs in grupos.items():
        gs = [g for g in gs if g in todas]
        if not gs:
            continue
        resultados[nombre] = probar(tr_raw, va_raw, BASE + gs, f"  + {nombre}")

    print("\n" + "=" * 78)
    print("LECTURA")
    print("=" * 78)
    pb = base[0]
    print(f"  Base en validacion: {pb*100:.2f}% (confianza)")
    mejoras = [(k, v) for k, v in resultados.items()
               if k not in ("base",) and v[0] > pb]
    if not mejoras:
        print("\n  Ninguna feature nueva mejora la base en validacion.")
        print("  Las 15 actuales ya capturan lo que hay en estos datos.")
    else:
        print("\n  Mejoran la base en validacion:")
        for k, v in sorted(mejoras, key=lambda kv: -kv[1][0]):
            print(f"    {k:<34} {v[0]*100:>6.2f}%  ({(v[0]-pb)*100:+.2f} pp)")
        print(f"\n  Se probaron {len(resultados)-1} variantes. Con ese numero de")
        print(f"  pruebas se espera ~{(len(resultados)-1)*0.05:.1f} falso(s) positivo(s) al 5%.")
        print("  Una mejora chica en validacion no es una mejora: hay que")
        print("  confirmarla en el test, UNA sola vez, con la ganadora ya elegida.")


if __name__ == "__main__":
    main()
