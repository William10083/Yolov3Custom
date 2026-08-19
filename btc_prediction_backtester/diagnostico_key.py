"""¿Que le pasa a la API key? Acota entre IP, permisos y key revocada.

El error -2015 mezcla tres causas distintas en un solo mensaje: "Invalid
API-key, IP, or permissions". Cual de las tres es se decide probando
endpoints que exigen permisos distintos:

  - /api/v3/ping                sin firma: dice si hay red y si Binance
                                responde a esta IP en general
  - /api/v3/account             firmado, solo pide "Enable Reading"
  - /sapi/v1/w3w/wallet/...     firmado, ademas del scope de wallet/w3w

Si `account` funciona y el de prediccion no, es un permiso del scope. Si los
dos fallan, es la key o la IP. Si ademas cambio tu IP publica desde que
creaste la key, ahi esta la respuesta.

    source ~/.btc_env && python3 diagnostico_key.py

Solo lectura. No mueve fondos ni coloca ordenes.
"""
import os

os.environ.setdefault("SIGNAL_ALERTS", "0")
import requests

import local_odds_logger as L


def probar(nombre, path, params=None, signed=True):
    try:
        r = L.api_get(path, params or {}, signed=signed)
    except Exception as exc:
        print(f"  {nombre:<34} error de red: {exc}")
        return None
    cuerpo = r.text[:110].replace("\n", " ")
    estado = "OK" if r.status_code == 200 else "FALLA"
    print(f"  {nombre:<34} {r.status_code}  {estado}")
    if r.status_code != 200:
        print(f"  {'':<34} {cuerpo}")
    return r.status_code


def main():
    print("=" * 68)
    print("DIAGNOSTICO DE LA API KEY")
    print("=" * 68)

    key = os.environ.get("BINANCE_API_KEY", "")
    print(f"  key cargada: {key[:6]}...{key[-4:]}  ({len(key)} caracteres)")
    off = L.sincronizar_reloj(forzar=True)
    print(f"  desfase de reloj: {off:+d} ms  "
          f"({'bien' if abs(off) < 5000 else 'PROBLEMA'})")

    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
        print(f"  tu IP publica ahora: {ip}")
    except Exception:
        print("  no se pudo averiguar la IP publica")

    print("\n  Probando endpoints de menor a mayor exigencia:\n")
    probar("ping (sin firma, sin key)", "/api/v3/ping", signed=False)
    cuenta = probar("account (solo 'Enable Reading')", "/api/v3/account")
    pred = probar("prediction/market/list", "/sapi/v1/w3w/wallet/prediction/market/list",
                  {"limit": 5})

    print("\n" + "=" * 68)
    print("LECTURA")
    print("=" * 68)
    if cuenta == 200 and pred == 200:
        print("  Todo responde. Si fallaba antes, era pasajero.")
    elif cuenta == 200 and pred != 200:
        print("  La key es valida y la IP esta permitida -- 'account' responde.")
        print("  Lo que falta es el permiso del scope de wallet/w3w.")
        print()
        print("  En Binance -> API Management -> tu key -> Edit restrictions,")
        print("  revisa que este habilitado el acceso a Wallet. Si la pantalla")
        print("  de Prediccion funciona en la app pero la API no, es eso.")
    elif cuenta != 200:
        print("  Falla hasta 'account', que solo pide permiso de lectura.")
        print("  Entonces no es un permiso puntual: es la key o la IP.")
        print()
        print("  1. Si restringiste la key por IP, comparala con la de arriba.")
        print("     En datos moviles la IP cambia sola y rompe la key.")
        print("  2. Si la revocaste, esta bien hecho -- hay que crear otra y")
        print("     actualizar ~/.btc_env.")
        print("  3. Binance desactiva keys sin uso o al cambiar la contraseña.")
    print()
    print("  Nada de esto afecta a predict_next.py: ese usa solo datos publicos")
    print("  y sigue funcionando sin credenciales.")


if __name__ == "__main__":
    main()
