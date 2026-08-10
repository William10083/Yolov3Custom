"""¿Existe aca el desfase de 0,3-0,5 s del oraculo? Medido en milisegundos.

El articulo del MIT describe esto: Polymarket resuelve contra un feed de
Chainlink; el feed publica con 0,3-0,5 s de retraso respecto del precio real;
en esa ventana uno ya sabe el resultado y el mercado todavia no. La pregunta
del usuario es si eso pasa tambien aca.

Se puede medir MUCHO mejor que muestreando Chainlink en vivo. Muestrear el
stream daria una lectura cada X ms con jitter de red encima. Pero
`market/detail` devuelve, para cada ronda pasada, el numero EXACTO con el que
el oraculo liquido el dinero:

    variantData.startPrice = 64817.245   <- oraculo en el instante T
    variantData.endPrice   = 64813.495   <- oraculo en el instante T+300

Esos dos numeros no son una muestra del oraculo: son el oraculo. Y del lado
de Binance hay `aggTrades`, que da cada operacion con marca de milisegundo.

Entonces el desfase se estima asi: para cada frontera de ronda T, se pregunta
a que instante del precio de Binance se parece mas el numero del oraculo.

    error(d) = precio_oraculo(T) - precio_binance(T - d)

Si el minimo cae en d=0, el oraculo publica el presente. Si cae en d=0,4 s,
el oraculo va cuatro decimas atras y ahi esta la ventana del articulo.

Dos cuidados que hacen la diferencia entre medir y creer que se mide:

  1. En un tramo plano TODOS los d dan el mismo precio. Esas fronteras no
     aportan nada y meterlas solo aplana la curva hasta que no se distingue
     nada. Se filtra por poder de discriminacion: solo entran las fronteras
     donde el precio de verdad se movio dentro de la ventana.
  2. Un minimo en la curva no prueba nada por si solo. Se hace ademas un test
     de signos pareado: frontera por frontera, ¿ajusta mejor d=0,4 que d=0?
     Con su p-valor. Una curva bonita con p=0,45 es ruido.

Modos:

    python3 chainlink_lag.py --validar
        Se inventa un oraculo con un retraso conocido a partir de ticks
        reales y comprueba que el estimador lo recupera. No necesita claves.

    source ~/.btc_env
    python3 chainlink_lag.py --rondas 200
        La medicion de verdad. Necesita las claves para `market/detail`.

Solo GET. No coloca ordenes ni mueve fondos.
"""
import argparse
import hashlib
import hmac
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

REST_BASE = "https://api.binance.com"
NS = "/sapi/v1/w3w/wallet/prediction"
AGG = "https://data-api.binance.vision/api/v3/aggTrades"
PREFIJO = "btc-updown-5m-"
ROUND = 300

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "oraculo_cache.json")
TICKS_CACHE = os.path.join(HERE, "data", "ticks_validacion.json")

VENTANA_MS = 4000          # cuanto tick se baja a cada lado de la frontera
REJILLA = [round(-1.0 + 0.05 * i, 2) for i in range(61)]   # -1,00 a +2,00 s
FEE = 0.02
SLIPPAGE = 0.0326          # medido en profundidad_libro.py


# --------------------------------------------------------------------------
# Precios oficiales del oraculo (necesita claves)
# --------------------------------------------------------------------------
def get(path, params=None):
    p = dict(params or {})
    p["timestamp"] = str(int(time.time() * 1000))
    p["recvWindow"] = "60000"
    qs = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{REST_BASE}{path}?{qs}&signature={sig}"
    try:
        r = requests.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
    except Exception:
        return None, None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def slug_ts(slug):
    if not isinstance(slug, str) or not slug.startswith(PREFIJO):
        return None
    try:
        return int(slug[len(PREFIJO):])
    except ValueError:
        return None


def extraer(d):
    ts = slug_ts(d.get("slug"))
    if ts is None:
        return None
    vd = d.get("variantData") or {}
    sp, ep = vd.get("startPrice"), vd.get("endPrice")
    if sp is None or ep is None:
        return None
    try:
        return ts, float(sp), float(ep)
    except (TypeError, ValueError):
        return None


def cargar_cache():
    if not os.path.exists(CACHE):
        return {}
    try:
        with open(CACHE) as f:
            return {int(k): tuple(v) for k, v in json.load(f).items()}
    except (ValueError, TypeError):
        return {}


def guardar_cache(d):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump({str(k): list(v) for k, v in d.items()}, f)


def recuperar_rondas(n_rondas):
    """Precios oficiales de las ultimas n_rondas. Cachea entre corridas: el
    paseo por ids es lo caro de todo esto y no cambia."""
    encontradas = cargar_cache()
    if encontradas:
        print(f"Cache: {len(encontradas)} rondas ya recuperadas antes.")

    code, body = get(f"{NS}/market/list", {"limit": 10})
    if code != 200 or not isinstance(body, dict):
        raise SystemExit(f"market/list fallo: {code} {body}")
    vivos = [t for t in (body.get("marketTopics") or [])
             if slug_ts(t.get("slug")) is not None]
    if not vivos:
        raise SystemExit("No hay un btc-updown-5m vivo en market/list")
    id0 = vivos[0]["marketTopicId"]
    ts0 = slug_ts(vivos[0]["slug"])
    print(f"Ronda viva: id={id0}  "
          f"{datetime.fromtimestamp(ts0, tz=timezone.utc):%d-%b %H:%M} UTC")

    # Que campos de tiempo trae la ronda viva. Decide si se puede apostar en
    # el ultimo segundo, que es lo unico que haria util un lag de decimas.
    viva = get(f"{NS}/market/detail", {"marketTopicId": id0})[1]
    if isinstance(viva, dict):
        campos = {}
        for k, v in list(viva.items()) + list((viva.get("variantData") or {}).items()):
            if isinstance(v, (int, float)) and 1_600_000_000 < float(v) < 4_000_000_000_000:
                campos[k] = v
        if campos:
            print("\n  Campos de tiempo de la ronda viva (inicio = "
                  f"{ts0}, fin = {ts0 + ROUND}):")
            for k, v in sorted(campos.items()):
                seg = v / 1000 if v > 4_000_000_000 else v
                rel = seg - ts0
                print(f"    {k:<24} {v}   ({rel:+.0f} s desde el inicio de la ronda)")
        print()

    dens = 6.72
    sonda = get(f"{NS}/market/detail", {"marketTopicId": id0 - 672})[1]
    if isinstance(sonda, dict):
        e = extraer(sonda)
        if e and e[0] != ts0:
            dens = 672 / ((ts0 - e[0]) / ROUND)
    print(f"Densidad estimada: {dens:.2f} ids por ronda")

    pedidas = 0
    for n in range(1, n_rondas + 1):
        objetivo = ts0 - n * ROUND
        if objetivo in encontradas:
            continue
        estimado = int(id0 - n * dens)
        for delta in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7):
            d = get(f"{NS}/market/detail", {"marketTopicId": estimado + delta})[1]
            pedidas += 1
            time.sleep(0.25)
            if not isinstance(d, dict):
                continue
            e = extraer(d)
            if not e:
                continue
            encontradas[e[0]] = (e[1], e[2])
            if e[0] == objetivo:
                dens = (id0 - (estimado + delta)) / n
                break
        if n % 20 == 0:
            sys.stdout.write(f"\r  {n}/{n_rondas} rondas · {len(encontradas)} en mano "
                             f"· {pedidas} peticiones nuevas")
            sys.stdout.flush()
    if pedidas:
        print()
    guardar_cache(encontradas)
    return encontradas


# --------------------------------------------------------------------------
# Ticks de Binance con marca de milisegundo
# --------------------------------------------------------------------------
def ticks(desde_ms, hasta_ms, reintentos=3):
    for i in range(reintentos):
        try:
            r = requests.get(AGG, params={"symbol": "BTCUSDT",
                                          "startTime": int(desde_ms),
                                          "endTime": int(hasta_ms),
                                          "limit": 1000}, timeout=20)
            if r.status_code == 200:
                return [(int(t["T"]), float(t["p"])) for t in r.json()]
        except Exception:
            pass
        time.sleep(1 + i)
    return []


def precio_en(serie, ms):
    """Ultimo trade en o antes de `ms`. None si no hay ninguno antes."""
    lo, hi = 0, len(serie)
    while lo < hi:
        m = (lo + hi) // 2
        if serie[m][0] <= ms:
            lo = m + 1
        else:
            hi = m
    return serie[lo - 1][1] if lo else None


# --------------------------------------------------------------------------
# Estimador
# --------------------------------------------------------------------------
def binomial_dos_colas(k, n):
    """P(|desvio| >= el observado) bajo moneda justa. Sin scipy."""
    if n == 0:
        return 1.0
    k = max(k, n - k)
    cola = sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)
    return min(1.0, 2 * cola)


def poder(serie, t_ms):
    """Cuanto se distinguen entre si los desfases que estan en discusion.

    No es el rango de toda la rejilla: un movimiento grande a -1 s no ayuda a
    separar 0 de 0,4. Lo que importa es cuanto cambia el precio entre el
    instante de la frontera y medio segundo antes, que es exactamente la
    comparacion que despues decide."""
    vals = []
    for d in REJILLA:
        v = precio_en(serie, t_ms - int(d * 1000))
        if v is None:
            return None, None
        vals.append(v)
    aqui = precio_en(serie, t_ms)
    antes = precio_en(serie, t_ms - 500)
    return abs(aqui - antes), vals


def estimar(observaciones, umbral, etiqueta):
    """observaciones: lista de (t_ms, precio_oraculo, serie_de_ticks)."""
    todas = []
    for t_ms, po, serie in observaciones:
        mov, vals = poder(serie, t_ms)
        if mov is None:
            continue
        todas.append((t_ms, po, vals, mov))

    print("\n" + "=" * 74)
    print("PODER DE LA MUESTRA")
    print("=" * 74)
    print("  Una frontera solo dice algo sobre latencia si el precio CAMBIO en")
    print("  el medio segundo previo. Si no cambio, adelantarse no da ninguna")
    print("  informacion distinta y la frontera no puede separar 0 de 0,4 s.\n")
    print(f"  {'movimiento en [T-0,5s, T]':<32}{'fronteras':>11}{'%':>8}")
    for u in (0.005, 0.01, 0.05, 0.10, 0.50, 1.00):
        c = sum(1 for *_, m in todas if m >= u)
        print(f"    >= ${u:<28.3f}{c:>11}{c/max(len(todas),1)*100:>7.1f}%")

    usables = [x for x in todas if x[3] >= umbral]
    print(f"\n  Se usan las {len(usables)} con movimiento >= ${umbral:.3f} "
          f"de {len(todas)} con datos.")
    if len(usables) < 30:
        print("\n  Con menos de 30 fronteras informativas no hay con que decidir.")
        print("  Esto no dice que no haya desfase: dice que esta muestra no")
        print("  puede verlo. Hace falta mas historial (--rondas mas alto, la")
        print("  cache acumula entre corridas) o un mercado mas movido.")
        return None

    # El estadistico es el error MEDIO, no el mediano. Va contra la intuicion
    # habitual -- la mediana suele ser la robusta -- y aca esta exactamente al
    # reves, por como es la muestra: en la mayoria de las fronteras el precio
    # no se movio y TODOS los desfases dan el mismo numero. Esas fronteras
    # empatan en cualquier hipotesis, dominan la mediana y la dejan plana en
    # el mismo valor para todos los desfases. La informacion vive en la
    # minoria que si se movio, y la media es la que la escucha. La primera
    # version de esto uso la mediana y el autotest lo pesco: con un retraso
    # inventado de 0,40 s devolvia 0,15 s.
    curva = []
    for j, d in enumerate(REJILLA):
        errs = [abs(po - vals[j]) for _, po, vals, _ in usables]
        curva.append((d, statistics.mean(errs), statistics.median(errs)))

    mejor = min(curva, key=lambda c: c[1])
    peor = max(c[1] for c in curva)
    print("\n" + "=" * 74)
    print(f"CURVA DE AJUSTE — {etiqueta}")
    print("=" * 74)
    print(f"  {'desfase':>9}  {'|error| medio':>14}  {'mediano':>9}")
    for d, mea, med in curva:
        if round(d * 100) % 20 and d != mejor[0]:
            continue                      # imprimir cada 0,20 s + el minimo
        marca = "  <-- minimo" if d == mejor[0] else ""
        ancho = int(40 * (mea - mejor[1]) / max(peor - mejor[1], 1e-12))
        print(f"  {d:>+8.2f}s  {mea:>14.4f}  {med:>9.4f}  {'#' * ancho}{marca}")

    print(f"\n  Desfase estimado: {mejor[0]:+.2f} s "
          f"(|error| medio ${mejor[1]:.4f})")

    # ¿Es un minimo de verdad o una meseta? Rango de desfases cuyo error medio
    # queda dentro del 10% del camino entre el mejor y el peor ajuste.
    tope = mejor[1] + 0.10 * (peor - mejor[1])
    meseta = [d for d, mea, _ in curva if mea <= tope]
    if meseta:
        print(f"  Indistinguibles del minimo: {min(meseta):+.2f}s a {max(meseta):+.2f}s")

    # El dibujo no decide: un minimo puede salir de la nada con pocas
    # fronteras. El test de signos pareado si -- frontera por frontera, ¿que
    # desfase ajusta mejor? -- y trae su p-valor.
    print("\n" + "=" * 74)
    print("TEST DE SIGNOS PAREADO (lo que decide, no el dibujo)")
    print("=" * 74)
    i0 = REJILLA.index(0.0)

    def duelo(cand):
        ic = REJILLA.index(cand)
        gana_c = gana_0 = 0
        for _, po, vals, _ in usables:
            e0, ec = abs(po - vals[i0]), abs(po - vals[ic])
            if ec < e0:
                gana_c += 1
            elif e0 < ec:
                gana_0 += 1
        n = gana_c + gana_0
        return gana_c, n, binomial_dos_colas(gana_c, n)

    candidatos = sorted({0.30, 0.40, 0.50, mejor[0]})
    for cand in candidatos:
        if cand not in REJILLA or cand == 0.0:
            continue
        gana_c, n, p = duelo(cand)
        # Tres desenlaces, no dos: el candidato gana, el candidato PIERDE
        # contra 0 -- que es evidencia positiva de que no hay retraso, no un
        # empate -- o no se distinguen.
        if p >= 0.05:
            veredicto = "empate, no se distinguen"
        elif gana_c * 2 > n:
            veredicto = "SI, ajusta mejor que 0"
        else:
            veredicto = "NO: d=0 ajusta mejor"
        extra = "  (el minimo de la curva)" if cand == mejor[0] else ""
        print(f"  d={cand:+.2f}s mejor que d=0 en {gana_c:>4} de {n:>4} fronteras "
              f"· p={p:.4f}  ->  {veredicto}{extra}")
    if not any(c in REJILLA and c != 0.0 for c in candidatos):
        print("  El minimo cae en 0 y no hay nada que contrastar: sin desfase.")

    return mejor[0], usables


# --------------------------------------------------------------------------
# Validacion sin claves
# --------------------------------------------------------------------------
def validar(lag_real, n_fronteras=120):
    print("=" * 74)
    print(f"VALIDACION — oraculo inventado con {lag_real:.2f} s de retraso")
    print("=" * 74)
    print("  Se toman ticks reales de Binance, se fabrica un 'oraculo' que es")
    print("  el precio de hace exactamente ese rato, y se le pide al estimador")
    print("  que lo descubra. Si no recupera el numero, el estimador no sirve")
    print("  y cualquier resultado que de sobre datos reales tampoco.\n")

    # Anclada a una hora redonda y con los ticks cacheados: asi se puede
    # reprobar con varios retrasos inventados sin volver a bajar nada, que es
    # lo que hace falta para saber si el estimador acierta en general y no
    # solo con un numero.
    t = (int(time.time()) - 3600) // ROUND * ROUND * 1000
    cache = {}
    if os.path.exists(TICKS_CACHE):
        try:
            with open(TICKS_CACHE) as f:
                cache = {int(k): [tuple(x) for x in v] for k, v in json.load(f).items()}
        except (ValueError, TypeError):
            cache = {}
    nuevas = 0

    obs = []
    for i in range(n_fronteras):
        t_ms = t - i * ROUND * 1000
        serie = cache.get(t_ms)
        if serie is None:
            serie = ticks(t_ms - VENTANA_MS - 2000, t_ms + VENTANA_MS)
            cache[t_ms] = serie
            nuevas += 1
            time.sleep(0.12)
        if len(serie) < 10:
            continue
        po = precio_en(serie, t_ms - int(lag_real * 1000))
        if po is None:
            continue
        # Chainlink publica un punto medio de libro, no el ultimo trade: eso
        # mete medio spread de ruido. Se imita para no validar en condiciones
        # mas limpias que las reales.
        po += (0.005 if i % 2 else -0.005)
        obs.append((t_ms, po, serie))
        if nuevas and (i + 1) % 20 == 0:
            sys.stdout.write(f"\r  {i+1}/{n_fronteras} ventanas ({nuevas} bajadas)")
            sys.stdout.flush()
    if nuevas:
        print()
        os.makedirs(os.path.dirname(TICKS_CACHE), exist_ok=True)
        with open(TICKS_CACHE, "w") as f:
            json.dump({str(k): v for k, v in cache.items()}, f)
    print(f"  {len(obs)} fronteras ({nuevas} ventanas nuevas, el resto de cache)\n")

    r = estimar(obs, umbral=0.01, etiqueta=f"validacion, retraso real {lag_real:.2f}s")
    if r:
        est = r[0]
        print("\n" + "=" * 74)
        err = abs(est - lag_real)
        if err <= 0.10:
            print(f"  RECUPERADO: {est:+.2f}s vs {lag_real:+.2f}s reales. El estimador sirve.")
        else:
            print(f"  FALLO: dio {est:+.2f}s y el real era {lag_real:+.2f}s.")
            print("  No hay que creerle a este estimador sobre datos reales.")


# --------------------------------------------------------------------------
# ¿Cuanto valdria el desfase si existiera?
# --------------------------------------------------------------------------
def valor_del_desfase(rondas, obs, desfase):
    print("\n" + "=" * 74)
    print("SI EXISTIERA, ¿CUANTO VALE?")
    print("=" * 74)
    if desfase is None or abs(desfase) < 0.05:
        print("  El desfase medido es 0. No hay ventana que explotar y esta")
        print("  cuenta no aplica; queda igual para saber que se perderia si")
        print("  el desfase apareciera mas adelante.")
        d = 0.40
        print(f"  Se calcula con {d:.2f}s, el numero del articulo.\n")
    else:
        d = abs(desfase)

    # Cuanto se mueve BTC en `d` segundos, medido en las mismas fronteras.
    movs = []
    for t_ms, _, serie in obs:
        a = precio_en(serie, t_ms - int(d * 1000))
        b = precio_en(serie, t_ms)
        if a is not None and b is not None:
            movs.append(abs(b - a))
    if not movs:
        print("  Sin datos de tick suficientes para esta parte.")
        return
    movs.sort()
    mov_med = movs[len(movs) // 2]
    print(f"  Movimiento de BTC en {d:.2f} s: mediana ${mov_med:.2f}   "
          f"p75 ${movs[int(len(movs)*0.75)]:.2f}   "
          f"p95 ${movs[int(len(movs)*0.95)]:.2f}")

    # El promedio esconde lo unico que importa. En los tramos quietos BTC no
    # se mueve un centavo en medio segundo y adelantarse no informa de nada;
    # toda la ventaja de latencia, si existe, vive en los tramos movidos. Se
    # separan por cuantos trades hubo en el segundo previo a la frontera.
    con_ritmo = []
    for t_ms, _, serie in obs:
        n_tk = sum(1 for ms, _ in serie if t_ms - 1000 <= ms <= t_ms)
        a = precio_en(serie, t_ms - int(d * 1000))
        b = precio_en(serie, t_ms)
        if a is not None and b is not None:
            con_ritmo.append((n_tk, abs(b - a)))
    if con_ritmo:
        con_ritmo.sort()
        corte = con_ritmo[int(len(con_ritmo) * 0.8)][0]
        quietos = [m for n_tk, m in con_ritmo if n_tk < corte]
        movidos = [m for n_tk, m in con_ritmo if n_tk >= corte]
        print(f"\n  Separado por actividad (corte: {corte} trades en el segundo previo):")
        for et, v in (("tramos quietos", quietos), ("20% mas movido", movidos)):
            if not v:
                continue
            v = sorted(v)
            print(f"    {et:<18} n={len(v):>4}  mediana ${v[len(v)//2]:>7.2f}   "
                  f"p90 ${v[int(len(v)*0.9)]:>8.2f}")
        print("    La ventana de latencia solo vale algo en la segunda fila.")

    # Solo cambia el resultado de la ronda si esa ronda en particular se
    # define por MENOS de lo que se movio el precio en esos d segundos. Si
    # cerro $30 arriba, saber $1 antes no cambia nada. Se compara ronda por
    # ronda y no mediana contra mediana: en un mercado quieto la mediana del
    # movimiento es $0,00 y la comparacion se vuelve trivialmente vacia.
    mov_por_frontera = {}
    for t_ms, _, serie in obs:
        a = precio_en(serie, t_ms - int(d * 1000))
        b = precio_en(serie, t_ms)
        if a is not None and b is not None:
            mov_por_frontera[t_ms] = abs(b - a)

    comparadas = decisivas = 0
    margenes = []
    for ts, (sp, ep) in rondas.items():
        m = mov_por_frontera.get((ts + ROUND) * 1000)
        if m is None:
            continue
        comparadas += 1
        margenes.append(abs(ep - sp))
        if abs(ep - sp) < m:
            decisivas += 1
    if not comparadas:
        return
    frac = decisivas / comparadas
    margenes.sort()
    print(f"\n  Rondas donde el margen de cierre fue menor que el movimiento de")
    print(f"  esos {d:.2f} s -- las unicas donde adelantarse puede cambiar la")
    print(f"  respuesta: {decisivas} de {comparadas} = {frac*100:.1f}%")
    print(f"  Margen mediano de cierre: ${margenes[len(margenes)//2]:.2f}")

    # En esas rondas: comprar el lado correcto a ~0.5 pagando el ask.
    costo = 1 + SLIPPAGE
    precio_pagado = 0.50 * costo
    retorno = (1 - FEE) / precio_pagado - 1
    print(f"\n  En una de esas rondas, sabiendo el resultado: se compra a ~0.50")
    print(f"  de mid, se paga {SLIPPAGE*100:.2f}% de ejecucion = {precio_pagado:.4f},")
    print(f"  cobra 1 menos {FEE*100:.0f}% de fee. Retorno {retorno*100:+.1f}%.")
    print(f"  Repartido sobre TODAS las rondas: {retorno*frac*100:+.2f}% por ronda.")
    print()
    print(f"  Y para cobrarlo hay que ver el tick, decidir y que la orden entre")
    print(f"  ENTERA dentro de {d*1000:.0f} ms. La medicion de este proyecto dio")
    print("  ida y vuelta de ~1-3 s contra esta API desde un telefono, y el")
    print("  libro se pudo leer 0,1 veces por segundo. El presupuesto de")
    print("  latencia es entre 3 y 30 veces menor que lo que tarda el sistema.")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rondas", type=int, default=200)
    ap.add_argument("--validar", type=float, nargs="?", const=0.40, default=None,
                    metavar="SEG",
                    help="autotest con un retraso inventado (por defecto 0.40)")
    ap.add_argument("--ventanas", type=int, default=250,
                    help="cuantas fronteras sinteticas usa --validar")
    ap.add_argument("--umbral", type=float, default=0.01,
                    help="movimiento minimo en [T-0,5s, T] para que la frontera cuente. "
                         "$0.01 es un tick de precio de BTC: por debajo de eso el "
                         "adelanto no cambia el numero que uno ve")
    args = ap.parse_args()

    if args.validar is not None:
        validar(args.validar, args.ventanas)
        return

    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "Faltan BINANCE_API_KEY / BINANCE_API_SECRET (source ~/.btc_env).\n"
            "Sin claves se puede correr igual el autotest:\n"
            "    python3 chainlink_lag.py --validar")

    rondas = recuperar_rondas(args.rondas)
    if len(rondas) < 20:
        raise SystemExit(f"Solo {len(rondas)} rondas recuperadas. Muy pocas.")

    # Cada frontera tiene hasta dos lecturas del oraculo: el cierre de una
    # ronda y la apertura de la siguiente, en el mismo instante. Si difieren,
    # el oraculo no se lee en el mismo momento para abrir que para cerrar --
    # cosa que vale la pena saber antes de seguir.
    por_frontera = {}
    for ts, (sp, ep) in rondas.items():
        por_frontera.setdefault(ts, []).append(("apertura", sp))
        por_frontera.setdefault(ts + ROUND, []).append(("cierre", ep))

    dobles = {t: v for t, v in por_frontera.items() if len(v) == 2}
    if dobles:
        difs = [abs(v[0][1] - v[1][1]) for v in dobles.values()]
        iguales = sum(1 for d in difs if d == 0)
        difs.sort()
        print("\n" + "=" * 74)
        print("CONTROL: cierre de una ronda vs apertura de la siguiente")
        print("=" * 74)
        print(f"  Mismo instante, dos lecturas del oraculo: {len(dobles)} fronteras")
        print(f"  Identicas al centavo: {iguales} ({iguales/len(dobles)*100:.1f}%)")
        print(f"  Diferencia: mediana ${difs[len(difs)//2]:.4f}  "
              f"p95 ${difs[int(len(difs)*0.95)]:.4f}  max ${difs[-1]:.2f}")
        if iguales / len(dobles) > 0.95:
            print("  -> El oraculo da un solo valor por instante. Consistente.")
        else:
            print("  -> Abrir y cerrar NO leen el mismo numero. El oraculo se")
            print("     muestrea en momentos distintos, y esa diferencia es")
            print("     por si sola una ventana.")

    print(f"\nBajando ticks de {len(por_frontera)} fronteras "
          f"(±{VENTANA_MS/1000:.0f} s cada una)...")
    obs = []
    for i, (t, lecturas) in enumerate(sorted(por_frontera.items())):
        t_ms = t * 1000
        serie = ticks(t_ms - VENTANA_MS - 2000, t_ms + VENTANA_MS)
        if len(serie) < 5:
            continue
        po = statistics.mean(v for _, v in lecturas)
        obs.append((t_ms, po, serie))
        if (i + 1) % 20 == 0:
            sys.stdout.write(f"\r  {i+1}/{len(por_frontera)}")
            sys.stdout.flush()
        time.sleep(0.10)
    print()

    r = estimar(obs, umbral=args.umbral,
                etiqueta="oraculo oficial vs ticks de Binance")

    print("\n" + "=" * 74)
    print("LECTURA")
    print("=" * 74)
    if r is None:
        print("  Sin poder estadistico. No dice ni que hay desfase ni que no.")
        # La cuenta de abajo se hace igual: no depende de haber medido el
        # desfase, y es la que dice si valdria la pena aunque existiera.
        valor_del_desfase(rondas, obs, None)
        return
    desfase, usables = r
    if abs(desfase) < 0.10:
        print(f"  El precio con el que se liquida el dinero es el de Binance del")
        print(f"  mismo instante ({desfase:+.2f} s). No hay ventana de oraculo:")
        print("  el mecanismo del articulo necesita que la fuente de resolucion")
        print("  vaya atrasada, y aca no lo esta.")
    elif desfase > 0:
        print(f"  El oraculo publica el precio de hace {desfase:.2f} s. La ventana")
        print("  del articulo EXISTE aca. Lo que falta comprobar es si se puede")
        print("  ejecutar dentro de ella, que es otra cosa.")
    else:
        print(f"  El minimo cae en {desfase:+.2f} s, o sea el oraculo se parece al")
        print("  precio del FUTURO. Eso no es fisico: es senal de que el ajuste")
        print("  esta dominado por ruido y no por latencia.")

    valor_del_desfase(rondas, obs, desfase)


if __name__ == "__main__":
    main()
