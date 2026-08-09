"""¿El modelo en vivo calcula lo mismo que el backtest?

En vivo lleva 11/32 = 34%. Si el modelo funciona al 54.25%, ver eso tiene una
chance en 53. O es mala suerte, o hay una diferencia entre como se calcula la
prediccion en vivo y como se calculo en el backtest.

Esa diferencia YA PASO en este proyecto: una señal de micro-momentum usaba
cierres de ronda (pasos de 5 min) mientras el backtest creia estar usando
minutos, y el error fue invisible hasta medirlo. Por eso `features_at()` es
compartida hoy -- pero compartir la funcion no garantiza que reciba las mismas
ENTRADAS.

Las tres diferencias posibles, y las tres se prueban aca:

  1. LAS VELAS. En vivo se piden las ultimas 320 velas al vuelo; el backtest
     lee un CSV. Si el endpoint devuelve la vela en curso, o le falta la
     ultima cerrada, las features salen de datos distintos.
  2. EL PRECIO DE APERTURA. En vivo la vela del borde todavia no existe, asi
     que se usa el precio spot del momento. El backtest usa el "open" de esa
     vela. Si difieren, difieren todas las features que dependen de el.
  3. LA PREDICCION FINAL. Aunque 1 y 2 esten bien, conviene comparar el
     numero que sale por las dos vias sobre las mismas rondas.

    python3 verificar_vivo.py

Compara ronda por ronda sobre las ultimas horas y reporta cualquier
discrepancia. Si sale 0, el problema no es tecnico.
"""
import json
import math
import os
import statistics
import time
from datetime import datetime, timezone

import requests

import predict_model as pm
import predict_next as pn

ROUND_MS = 5 * 60 * 1000


def main():
    m = pn.load_model()
    print("Bajando las velas como las baja el modo VIVO (limit=320)...")
    vivo = pn.fetch_candles(limit=320)
    by_vivo = {c["open_time_ms"]: c for c in vivo}

    lo = min(by_vivo) - 60_000
    hi = max(by_vivo) + 60_000
    print("Bajando el MISMO rango por el camino del backtest (startTime/endTime)...")
    hist = []
    cursor = lo
    while cursor <= hi:
        r = requests.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m",
                    "startTime": cursor, "endTime": hi, "limit": 1000},
            timeout=30,
        )
        r.raise_for_status()
        ks = r.json()
        if not ks:
            break
        for k in ks:
            hist.append({
                "open_time_ms": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                "taker_buy_base": float(k[9]),
            })
        cursor = int(ks[-1][0]) + 60_000
    by_hist = {c["open_time_ms"]: c for c in hist}

    print(f"\nvivo: {len(by_vivo)} velas   backtest: {len(by_hist)} velas")

    print("\n" + "=" * 74)
    print("1. ¿SON LAS MISMAS VELAS?")
    print("=" * 74)
    comunes = set(by_vivo) & set(by_hist)
    solo_vivo = set(by_vivo) - set(by_hist)
    solo_hist = set(by_hist) - set(by_vivo)
    print(f"  en comun: {len(comunes)}   solo en vivo: {len(solo_vivo)}   "
          f"solo en backtest: {len(solo_hist)}")
    for t in sorted(solo_vivo)[:3]:
        d = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
        print(f"    solo vivo: {d:%H:%M} UTC  (¿vela en curso?)")
    # La vela del minuto en curso sigue cambiando entre una descarga y la
    # siguiente, asi que difiere siempre y no es un defecto. Lo que importa es
    # que difieran las velas YA CERRADAS.
    ahora_min = (int(time.time()) // 60) * 60_000
    difs = 0
    difs_cerradas = 0
    for t in comunes:
        a, b = by_vivo[t], by_hist[t]
        for campo in ("open", "high", "low", "close", "volume", "taker_buy_base"):
            if abs(a[campo] - b[campo]) > 1e-9:
                difs += 1
                if t < ahora_min:
                    difs_cerradas += 1
                    if difs_cerradas <= 3:
                        d = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
                        print(f"    DIFIERE (cerrada) {d:%H:%M} {campo}: "
                              f"vivo={a[campo]} backtest={b[campo]}")
    print(f"  campos distintos en total: {difs}")
    print(f"  de esos, en velas YA CERRADAS: {difs_cerradas}")
    if difs and not difs_cerradas:
        print("  (todas las diferencias son de la vela en curso: normal)")

    print("\n" + "=" * 74)
    print("2. ¿DA LA MISMA PREDICCION POR LAS DOS VIAS?")
    print("=" * 74)
    bordes = sorted(t for t in by_hist if t % ROUND_MS == 0)
    # Se saltea el ultimo borde: su ronda todavia no cerro.
    bordes = [t for t in bordes if t + ROUND_MS in by_hist][-40:]
    print(f"  Comparando {len(bordes)} rondas ya cerradas.\n")
    print(f"  {'ronda':<8}{'p_vivo':>10}{'p_backtest':>13}{'dif':>10}{'lado':>8}{'resultado':>12}")
    print("  " + "-" * 61)
    discrepancias = 0
    aciertos = total = 0
    for t in bordes[-12:]:
        # Via VIVO: usa el precio spot como apertura, igual que predict_next.
        precio_borde = by_hist[t]["open"]
        f_vivo = pm.features_at(by_vivo, t, price_now=precio_borde)
        f_hist = pm.features_at(by_hist, t)
        if f_vivo is None or f_hist is None:
            continue
        p_v = pn.apply_model(m, f_vivo)
        p_h = pn.apply_model(m, f_hist)
        dif = abs(p_v - p_h)
        if dif > 0.001:
            discrepancias += 1
        real = "Up" if by_hist[t + ROUND_MS]["open"] > precio_borde else "Down"
        lado = "Up" if p_h >= 0.5 else "Down"
        conf = max(p_h, 1 - p_h)
        marca = ""
        if conf >= 0.55:
            total += 1
            ok = lado == real
            aciertos += ok
            marca = "✓" if ok else "✗"
        d = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
        print(f"  {d:%H:%M}   {p_v*100:>8.2f}%{p_h*100:>12.2f}%{dif*100:>9.3f}"
              f"{lado:>8}{real:>9} {marca}")

    print(f"\n  Rondas con prediccion distinta entre vias: {discrepancias}")
    if total:
        print(f"  De las que superaron el umbral en esta ventana: {aciertos}/{total}")

    print("\n" + "=" * 74)
    print("LECTURA")
    print("=" * 74)
    if difs_cerradas or discrepancias:
        print("  HAY DIFERENCIA TECNICA entre vivo y backtest. Eso explicaria")
        print("  el bajo acierto en vivo sin necesidad de mala suerte, y es un")
        print("  bug que hay que arreglar antes de sacar cualquier conclusion")
        print("  sobre el modelo.")
    else:
        print("  Las dos vias calculan exactamente lo mismo. El bajo acierto en")
        print("  vivo NO se explica por un bug de calculo.")
        print()
        print("  Quedan dos opciones: mala suerte (p=0.019, poco probable) o que")
        print("  el modelo no funcione fuera del periodo en que se midio. La")
        print("  segunda solo se separa de la primera con mas rondas.")


if __name__ == "__main__":
    main()
