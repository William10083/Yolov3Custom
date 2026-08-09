"""Todo en una sola terminal: predicciones + precios de mercado.

Hacen falta dos flujos de datos para cerrar la pregunta abierta del proyecto:

  predict_next.py     -> que lado predice el modelo, anotado antes del resultado
  local_odds_logger.py -> a cuanto lo cotiza el mercado en esa misma ronda

Cruzarlos es lo que hace `market_vs_model.py`, y hasta ahora habia que abrir
dos sesiones de Termux para juntarlos. Esto los levanta juntos, marca cada
linea con su origen, y los reinicia si se caen -- que en 4 dias de corrida
sobre una red movil va a pasar.

Si faltan BINANCE_API_KEY / BINANCE_API_SECRET arranca igual, solo con las
predicciones, y lo dice. Media respuesta medida es mejor que ninguna, y esa
mitad no necesita credenciales.

    python3 run_all.py             # ambos
    python3 run_all.py --solo-pred # solo predicciones, sin API key
    Ctrl+C para parar los dos.

En Termux, corre `termux-wake-lock` antes: si no, Android mata el proceso al
apagar la pantalla y no se junta nada.
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PRED = os.path.join(HERE, "predict_next.py")
ODDS = os.path.join(HERE, "local_odds_logger.py")

COLORES = {"pred": "\033[36m", "odds": "\033[33m", "sys": "\033[35m"}
RESET = "\033[0m"
USAR_COLOR = sys.stdout.isatty()

_lock = threading.Lock()
_parando = threading.Event()


def emitir(tag, linea):
    """Una linea de un hijo, marcada con su origen y la hora."""
    linea = linea.rstrip("\n")
    if not linea.strip():
        return
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if USAR_COLOR:
        prefijo = f"{COLORES.get(tag, '')}[{tag}]{RESET}"
    else:
        prefijo = f"[{tag}]"
    with _lock:
        print(f"{ts} {prefijo} {linea}", flush=True)


def bombear(proc, tag):
    """Lee el hijo linea por linea y la reemite. Sale cuando el hijo cierra."""
    try:
        for linea in proc.stdout:
            if _parando.is_set():
                break
            emitir(tag, linea)
    except Exception as exc:
        emitir("sys", f"error leyendo {tag}: {exc}")


def supervisar(tag, cmd):
    """Mantiene vivo un hijo. Reinicia con backoff si muere.

    Un fallo de red a las 3 de la mañana no puede terminar la recoleccion: lo
    unico peor que no tener datos es tener datos con un agujero de 6 horas y
    no saberlo.
    """
    espera = 5
    entorno = dict(os.environ, PYTHONUNBUFFERED="1")
    while not _parando.is_set():
        arranco = time.time()
        emitir("sys", f"arrancando {tag}: {' '.join(os.path.basename(c) for c in cmd[1:])}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=HERE,
                env=entorno,
            )
        except Exception as exc:
            emitir("sys", f"no se pudo arrancar {tag}: {exc}")
            return

        hilo = threading.Thread(target=bombear, args=(proc, tag), daemon=True)
        hilo.start()
        codigo = proc.wait()
        hilo.join(timeout=2)

        if _parando.is_set():
            return

        vivio = time.time() - arranco
        emitir("sys", f"{tag} termino (codigo {codigo}) despues de {vivio/60:.1f} min")

        # Un hijo que muere en segundos esta roto, no con mala suerte: no tiene
        # sentido reintentar cada 5 s para siempre. Uno que aguanto un rato es
        # un corte de red, y ese si se reintenta rapido.
        if vivio > 120:
            espera = 5
        else:
            espera = min(espera * 2, 300)
        emitir("sys", f"reintentando {tag} en {espera}s")
        for _ in range(espera):
            if _parando.is_set():
                return
            time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo-pred", action="store_true",
                    help="solo predicciones, sin el logger de precios")
    args = ap.parse_args()

    faltan_llaves = not (os.environ.get("BINANCE_API_KEY")
                         and os.environ.get("BINANCE_API_SECRET"))
    telegram = bool(os.environ.get("TELEGRAM_BOT_TOKEN")
                    and os.environ.get("TELEGRAM_CHAT_ID"))

    print("=" * 66)
    print("  RECOLECCION UNIFICADA")
    print("=" * 66)

    tareas = [("pred", [sys.executable, PRED, "--loop"])]

    if args.solo_pred:
        print("  Solo predicciones (--solo-pred).")
    elif faltan_llaves:
        print("  BINANCE_API_KEY / BINANCE_API_SECRET no estan en el entorno,")
        print("  asi que el logger de precios no puede arrancar. Va solo la")
        print("  parte de predicciones.")
        print()
        print("  Para sumar los precios, en esta misma terminal antes de correr:")
        print("    export BINANCE_API_KEY=...")
        print("    export BINANCE_API_SECRET=...")
        print("  (nunca los pegues en un chat -- mejor en ~/.btc_env y")
        print("   'source ~/.btc_env' al arrancar)")
    elif not os.path.exists(ODDS):
        print(f"  No encuentro {os.path.basename(ODDS)}. Va solo la prediccion.")
    else:
        tareas.append(("odds", [sys.executable, ODDS]))
        print("  Predicciones + precios de mercado.")

    if not os.environ.get("TERMUX_VERSION"):
        pass
    else:
        print()
        print("  Termux: si no lo hiciste, corre 'termux-wake-lock' en otra")
        print("  sesion o Android va a matar esto al apagar la pantalla.")

    print()
    if telegram:
        print("  Telegram: ACTIVO. Avisa cuando el modelo se compromete")
        print("  (~43 veces al dia). Las rondas sin opinion no se mandan.")
    else:
        print("  Telegram: apagado (faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        print("  Todo se sigue guardando en los CSV igual.")

    print()
    print("  Ctrl+C para parar todo.")
    print("=" * 66)
    print()

    hilos = []
    for tag, cmd in tareas:
        h = threading.Thread(target=supervisar, args=(tag, cmd), daemon=True)
        h.start()
        hilos.append(h)
        time.sleep(1)  # que no se pisen los banners de arranque

    def parar(_signo, _frame):
        if _parando.is_set():
            return
        _parando.set()
        emitir("sys", "parando... (los CSV quedan donde estan)")

    signal.signal(signal.SIGINT, parar)
    signal.signal(signal.SIGTERM, parar)

    try:
        while not _parando.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        parar(None, None)

    # Los hijos comparten el grupo de procesos, asi que el Ctrl+C de la
    # terminal ya les llego; esto solo espera a que cierren.
    time.sleep(2)
    print()
    print("Listo. Para ver como va:")
    print("  python3 market_vs_model.py     # modelo vs precio real")
    print("  python3 predict_next.py        # imprime el acumulado al arrancar")


if __name__ == "__main__":
    main()
