"""¿Se puede ejecutar de verdad, o solo en la planilla?

Todo lo medido hasta ahora uso el punto medio del libro como si uno pudiera
comprar ahi. No se puede. El mid es un promedio entre la mejor oferta de
compra y la de venta; lo que uno paga de verdad es el ASK, y solo hasta donde
alcance el tamaño publicado.

Dos limites distintos, y ninguno tiene que ver con la velocidad:

  1. ¿HAY LADO? Para comprar tiene que haber alguien con una orden de venta ya
     puesta. Si el lado de asks esta vacio, no hay nada que comprar por rapido
     que uno sea. Dejar una orden limite y esperar arruina la idea entera: la
     ventaja de latencia exige entrar DENTRO de la ventana de segundos.
  2. ¿HAY TAMAÑO? Aunque haya oferta, puede ser de 9 acciones cuando hacen
     falta 20 para poner $10. Ahi uno consume esa oferta y sigue subiendo por
     el libro, pagando cada vez peor.

Este script recorre el libro guardado en cada foto de live_odds_log.csv y
calcula lo que REALMENTE costaria poner $10, comparado con el mid que venimos
usando en todos los calculos.

    python3 profundidad_libro.py

Solo lectura. No apuesta nada.
"""
import csv
import json
import os
import statistics
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ODDS = os.path.join(HERE, "data", "live_odds_log.csv")
APUESTA = 10.0
FEE = 0.02


def cargar():
    if not os.path.exists(ODDS):
        raise SystemExit(f"No encuentro {ODDS}")
    with open(ODDS, newline="") as f:
        return list(csv.reader(f))


def libro_de(fila):
    """El JSON del libro puede estar en la ultima columna o, con cabeceras
    viejas, haberse corrido a otra. Se busca el que de verdad parezca un libro
    en vez de confiar en la posicion."""
    for v in reversed(fila):
        if isinstance(v, str) and '"asks"' in v:
            try:
                b = json.loads(v)
                if isinstance(b, dict):
                    return b.get("bids") or [], b.get("asks") or []
            except ValueError:
                continue
    return None, None


def caminar(niveles, monto):
    """Precio medio real de gastar `monto` subiendo por el libro.

    Devuelve (precio_medio, acciones, agotado) -- agotado=True si el libro no
    tenia suficiente para el monto entero."""
    resta = monto
    acciones = 0.0
    for n in niveles:
        try:
            p = float(n["price"])
            s = float(n["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if p <= 0 or s <= 0:
            continue
        costo_nivel = p * s
        if costo_nivel >= resta:
            acciones += resta / p
            return monto / acciones, acciones, False
        resta -= costo_nivel
        acciones += s
    if acciones <= 0:
        return None, 0.0, True
    return (monto - resta) / acciones, acciones, True


def main():
    filas = cargar()
    cuerpo = filas[1:] if filas and filas[0] and filas[0][0] == "polled_at_ms" else filas
    print(f"Fotos en el log: {len(cuerpo):,}\n")

    estados = Counter()
    slippages = []
    profundidad_ask = []
    ejemplos = []

    for fila in cuerpo:
        bids, asks = libro_de(fila)
        if bids is None:
            estados["sin libro guardado"] += 1
            continue
        if not asks and not bids:
            estados["los dos lados vacios"] += 1
            continue
        if not asks:
            estados["sin ofertas de venta (no se puede COMPRAR)"] += 1
            continue
        if not bids:
            estados["sin ofertas de compra (no se puede VENDER)"] += 1
            continue

        try:
            mejor_bid = float(bids[0]["price"])
            mejor_ask = float(asks[0]["price"])
        except (KeyError, TypeError, ValueError):
            estados["libro ilegible"] += 1
            continue
        mid = (mejor_bid + mejor_ask) / 2

        total_ask = sum(float(n["price"]) * float(n["size"])
                        for n in asks
                        if n.get("price") and n.get("size"))
        profundidad_ask.append(total_ask)

        real, acciones, agotado = caminar(asks, APUESTA)
        if real is None:
            estados["libro ilegible"] += 1
            continue
        if agotado:
            estados[f"no alcanza para ${APUESTA:.0f} en todo el libro"] += 1
            if len(ejemplos) < 5:
                ejemplos.append((mid, real, total_ask))
            continue

        estados["se puede ejecutar"] += 1
        slippages.append((real - mid) / mid)

    total = sum(estados.values())
    # Las filas sin libro guardado son cabeceras viejas del CSV, no falta de
    # liquidez. Meterlas en el denominador hace parecer ilíquido un mercado
    # que no lo es -- el porcentaje que importa se calcula sobre las filas
    # que de verdad traen un libro.
    sin_libro = estados.get("sin libro guardado", 0)
    con_libro = total - sin_libro
    print("=" * 70)
    print(f"¿SE PUEDE PONER ${APUESTA:.0f} EN CADA MOMENTO?")
    print("=" * 70)
    if sin_libro:
        print(f"  Filas sin libro guardado (cabecera vieja): {sin_libro:,} de {total:,}")
        print(f"  Los porcentajes van sobre las {con_libro:,} que si lo traen.\n")
    for k, v in estados.most_common():
        if k == "sin libro guardado":
            continue
        print(f"  {k:<48} {v:>7,}  {v/con_libro*100:>5.1f}%")

    if profundidad_ask:
        profundidad_ask.sort()
        n = len(profundidad_ask)
        print(f"\n  Dinero total disponible del lado vendedor:")
        print(f"    mediana ${profundidad_ask[n//2]:,.2f}   "
              f"p25 ${profundidad_ask[n//4]:,.2f}   "
              f"p75 ${profundidad_ask[3*n//4]:,.2f}")

    if slippages:
        slippages.sort()
        n = len(slippages)
        med = slippages[n // 2]
        print("\n" + "=" * 70)
        print("CUANTO PEOR ES EL PRECIO REAL QUE EL MID QUE VENIMOS USANDO")
        print("=" * 70)
        print(f"  mediana  {med*100:+.2f}%")
        print(f"  p75      {slippages[3*n//4]*100:+.2f}%")
        print(f"  p95      {slippages[int(n*0.95)]*100:+.2f}%")
        print()
        print("  Todo calculo de EV en este repo uso el mid. Pagando el ask real")
        print("  hay que restarle esto, ademas de la fee del 2%.")
        equivalente = med + FEE
        print(f"\n  Costo total por operacion: {FEE*100:.0f}% de fee "
              f"+ {med*100:.2f}% de ejecucion = {equivalente*100:.2f}%")

    if ejemplos:
        print("\n  Ejemplos donde el libro entero no alcanzaba para $10:")
        for mid, real, tot in ejemplos:
            print(f"    mid {mid:.3f}  ·  todo el lado vendedor sumaba ${tot:.2f}")

    print("\n" + "=" * 70)
    print("LECTURA")
    print("=" * 70)
    ok = estados["se puede ejecutar"]
    print(f"  De {con_libro:,} momentos con libro, en {ok:,} "
          f"({ok/con_libro*100:.1f}%) se podia")
    print(f"  poner ${APUESTA:.0f} de verdad.")
    print()
    if slippages:
        costo = FEE + slippages[len(slippages) // 2]
        print(f"  Costo total por operacion: {costo*100:.2f}%")
        print(f"  Los margenes medidos en el proyecto, netos de ese costo:")
        for et, m in (("modelo vs breakeven", 0.018),
                      ("favorito con sesgo longshot", 0.046),
                      ("modelo opinando siempre", 0.0207)):
            print(f"    {et:<32} {m*100:>+5.2f}%  ->  {(m-costo)*100:>+6.2f}%")
        print()
    if ok / con_libro < 0.5:
        print("  Menos de la mitad. Y no es un problema de velocidad ni de")
        print("  codigo: en el resto no hay contraparte publicada. Una ventaja")
        print("  que solo se puede tomar en la mitad de los momentos, y encima")
        print("  no en los elegidos, vale mucho menos que en la planilla.")
    else:
        print("  La mayoria de los momentos son ejecutables. El limite entonces")
        print("  no es la liquidez sino el precio, que es lo que ya se midio.")


if __name__ == "__main__":
    main()
