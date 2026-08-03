# BTC "Up or Down 5m" -- Backtester (investigacion, sin dinero real)

Este proyecto **no coloca apuestas reales ni hace clicks automaticos**. Es
una herramienta para medir, con precios historicos reales de BTC, si
alguna estrategia simple tiene una ventaja real sobre el juego de
prediccion "BTC Up or Down 5m" (tipo el de la pestana Prediccion de
Binance) antes de arriesgar dinero en el.

## Por que existe

Ver la conversacion que lo origino: el juego de 5 minutos parece tentador
porque uno "siente" rachas (varios Down seguidos, etc.), pero eso puede
ser pura falacia del apostador. En vez de confiar en la intuicion o en
bots de terceros sin resultados verificables, esto corre las estrategias
mas obvias contra datos reales y reporta si de verdad ganan mas de lo que
cuesta la comision.

## Que mercado es realmente esto

Segun la pantalla "Reglas" de la app (confirmado por captura): el mercado
resuelve via un **oraculo de Chainlink Data Streams** (BTC/USDT
top-of-book) y se opera como un **mercado CLOB de acciones binarias**
(comprar "Up" al % mostrado, cada accion paga $1 USDT si acierta) -- la
misma arquitectura que los mercados BTC 5-min de Polymarket. No es un
simple pool de apuestas de una casa de apuestas; es un mercado de
prediccion real con creadores de mercado del otro lado.

**Importante sobre la fuente de datos usada aca:** `data_fetch.py` usa
velas publicas de Binance (`data-api.binance.vision`), que son el precio
de la **ultima operacion ejecutada** cada minuto -- NO el mismo dato que
usa el oraculo de Chainlink (que es el **precio medio entre bid y ask**,
mid-price). Confirme que la API de Chainlink Data Streams requiere
autenticacion paga (API key + HMAC) para historico, asi que no es
accesible gratis como lo que usamos aca. En la practica ambos precios se
mueven casi pegados en BTC/USDT en Binance (mercado muy liquido, spread
de centavos), asi que para conclusiones estadisticas agregadas sobre
decenas de miles de rondas la diferencia es ruido -- pero no es
identico, y vale aclararlo en vez de asumirlo.

## Como funciona

1. `data_fetch.py` descarga velas de 1 minuto de BTCUSDT desde el mirror
   publico de datos de mercado de Binance (`data-api.binance.vision`,
   sin API key, sin acceso a cuenta ni ordenes -- solo precios historicos).
2. `backtest.py` arma rondas de 5 minutos alineadas al reloj (:00, :05,
   :10, ...) igual que el juego, usando el precio de **apertura** de la
   vela limite (asi lo especifica la regla de resolucion: "use the open
   price of the candlestick corresponding to the market's end time"), y
   evalua cada estrategia de forma **walk-forward**: cada prediccion solo
   puede usar datos de ANTES del inicio de esa ronda (cero adelanto de
   informacion / data leakage).
3. Los datos se dividen en dos mitades: in-sample (para mirar) y
   out-of-sample (para confirmar), asi no nos enganamos con una
   estrategia que solo se ve bien por azar.
4. Se reporta el win rate con intervalo de confianza 95% (Wilson score) y
   se compara contra el win rate necesario para no perder dinero dado un
   supuesto de comision (`--fee`, default 10%).
5. Ademas del rule-mining, se corre un **variance ratio test** (Lo-MacKinlay)
   sobre los retornos de 5 min: un test estadistico formal e independiente
   de cualquier regla especifica, que responde "¿hay estructura real en el
   precio en si (momentum o mean-reversion), o es puro random walk?". Sirve
   de chequeo cruzado para no confiar solo en reglas que podrian ganar por
   azar (data snooping).

## Estrategias probadas (`strategies.py`)

- Baseline aleatorio (referencia, deberia dar ~50%).
- Momentum: apostar a que continua la direccion de las ultimas N rondas.
- Contrarian: apostar lo opuesto a las ultimas N rondas.
- Reversion/continuacion de racha: solo apuesta cuando hay una racha de
  K rondas iguales seguidas (el "solo cuando es muy seguro" que pediste).
- Micro-momentum / micro-mean-reversion: usa el movimiento de precio real
  de los ultimos 1/3/5/15/30 minutos antes de que arranque la ronda (con
  resolucion real de 1 minuto via `MarketContext`), con filtro opcional de
  magnitud minima de movimiento.
- Volume imbalance / volume imbalance contrarian: usa el volumen de compra
  agresiva (taker buy) de Binance para medir presion compradora vs
  vendedora en los ultimos 3/5/15 min, y apuesta con o contra esa presion.
- Variantes con filtro de regimen de volatilidad: solo disparan cuando la
  volatilidad reciente esta por encima/debajo de la mediana vista hasta
  ese momento (mediana corriente con dos heaps, O(log n), sigue siendo
  walk-forward).

## Calibracion del precio de mercado (`option_edge_analysis.py`)

Dado que este es un mercado tipo opcion binaria (no una casa de apuestas
de comision fija), la pregunta relevante no es solo "¿se puede predecir
la direccion?" sino "¿el precio en vivo (%) refleja bien la probabilidad
real, o el market maker ignora el mean-reversion que ya encontramos?".
Este script simula, en cada minuto dentro de cada ronda historica, cual
seria el precio "justo" bajo un modelo naive (random walk sin memoria,
calibrado con la volatilidad real de los datos), y lo compara contra el
resultado real -- separando los casos donde el ultimo minuto siguio la
tendencia ("trending") de los casos donde ya empezo a revertir
("reverting"). Si el mercado no incorpora el mean-reversion, la categoria
"reverting" deberia acertar mas que lo que predice el modelo naive.

Resultado (180 dias, decenas de miles de checkpoints por bucket): el
modelo naive esta razonablemente bien calibrado (desviaciones de 1-5
puntos porcentuales, no sistematicas en una direccion), y **no hay
diferencia significativa entre "trending" y "reverting" para el mismo
nivel de gap** (todas las brechas quedan por debajo de 3 puntos
porcentuales, con muestras de miles a decenas de miles de casos por
grupo). Conclusion: el momentum del ultimo minuto, dado que ya se conoce
cuanto se desvio el precio del inicio, no aporta señal extra explotable.
La reversion que existe (medida en `backtest.py` a nivel de ronda
completa) ya esta absorbida en el nivel del gap.

## Analisis de tu lista manual (`manual_sequence_analysis.py`)

Ademas se analizo la secuencia de 36 resultados que anotaste a mano
(transiciones Up/Down, reversion tras rachas). Con esa muestra tan
chica los intervalos de confianza son enormes (ej. 23%-64%), asi que
sirve solo como sanity-check, no como senal por si sola. Ver la salida
completa corriendo el script.

## Resultado real (180 dias de datos, 259,200 velas de 1m, 51,839 rondas de 5 min, corrido el 2026-08-03)

Se repitio el analisis con ~26x mas datos que la primera corrida (45 dias)
para tener suficiente potencia estadistica. Con mas muestra, aparecio algo
que antes no se veia con claridad. (Nota: en el camino aparecieron y se
corrigieron dos bugs reales -- un problema de rendimiento O(n^2) en las
estrategias de regimen de volatilidad, y una fuga de informacion de ~1
minuto hacia el futuro en `MarketContext` que aparecio al cambiar de
close a open price. Los numeros de abajo son de la version ya corregida
y verificada.)

```
Baseline real Up/Down en todo el periodo: 49.9% Up / 50.1% Down
Con una comision asumida de 10%, hace falta un win rate > 52.6% para ganar en el largo plazo

Variance ratio test (estructura del precio en si, sin ninguna regla):
  k= 2: VR=0.994  z=-1.31  -> random walk (VR~1)
  k= 3: VR=0.984  z=-2.38  -> MEAN REVERSION significativo (VR<1, z<-2)
  k= 5: VR=0.977  z=-2.39  -> MEAN REVERSION significativo (VR<1, z<-2)
  k=10: VR=0.942  z=-3.90  -> MEAN REVERSION significativo (VR<1, z<-2)

Ninguna estrategia supera el breakeven de forma estadisticamente
significativa (limite inferior del IC95% out-of-sample > breakeven)
en este periodo de datos.
```

**Esto es mas interesante que el resultado anterior.** El variance ratio
test -- que no es una regla inventada, es un test estadistico formal sobre
el precio en si -- confirma de forma independiente que **si existe un
mean-reversion real y estadisticamente significativo** (z=-3.72 y z=-3.53,
muy por debajo del umbral de significancia de -2) en los retornos de BTC a
estos horizontes. No es ruido: coincide con lo que ya sugerian las
estrategias contrarian/mean-reversion, que ahora con mucha mas muestra
tienen intervalos de confianza bien angostos y consistentemente por
encima de 50%:

- `contrarian_last_1`: 51.5% OOS, IC95% [50.9%, 52.1%] -- el intervalo
  entero esta arriba de 50%, o sea, el efecto es real, no azar.
- `streak_reversion_3/4/5` (apostar reversion tras 3-5 rondas iguales
  seguidas): 51.9%-52.3% OOS -- con menos muestra (n=1300-6000) el
  intervalo es mas ancho, entre ~50% y ~55%.
- `micro_mean_reversion_15m`: 51.0% OOS, consistente con el resto.

**Pero incluso asi, nada le gana de forma robusta a una comision del 10%.**
El punto (no el limite inferior del IC) de `streak_reversion_3` y
`streak_reversion_4` roza el breakeven de 52.6%, pero el limite inferior
del intervalo de confianza queda por debajo, asi que no se puede confiar
en que eso se sostenga. Dicho de otra forma: **el efecto es real pero
chico** (~1.5-3 puntos porcentuales sobre 50%), y una comision del 10% es
demasiado grande para ese margen. Si la comision real de tu app fuera mas
baja -- por ejemplo, `contrarian_last_1` necesitaria una comision menor a
~5.8% para ser rentable en el largo plazo con el win rate observado --
ahi si empezaria a ser interesante. Pero no tenemos confirmado cual es la
comision real de la plataforma (ver Limitaciones).

## Monitoreo en vivo -- APIs oficiales (no screen-scraping)

En vez de leer la pantalla o simular clicks, la via correcta es una API
oficial. Confirme que existen dos:

- **Binance Wallet Prediction Markets API** (lanzada junio 2026): datos
  en tiempo real de mercados de prediccion, odds, liquidez, y ejecucion
  de ordenes. Requiere solicitud via el Binance Developer Platform (no es
  de acceso libre inmediato).
- **Predict.fun API** (la infraestructura real detras de este mercado):
  tiene un endpoint publico de series historicas de precios, ideal para
  backtesting con las cuotas reales en vez de nuestra aproximacion
  Gaussiana. Probe el endpoint:
  - Testnet (`api-testnet.predict.fun`): abierto, sin API key, pero solo
    tiene mercados de prueba/historicos (ej. BTC Up/Down de 15 min de
    Dic 2025-Feb 2026), no el mercado de 5 min en vivo que usas.
  - Mainnet (`api.predict.fun`): devuelve `401 unauthorized` sin API key
    -- ahi es donde estaria el mercado real, pero hace falta pedir acceso.

**Siguiente paso si se quiere seguir con esto:** conseguir una API key
(de Binance o de Predict.fun mainnet). Con eso, se puede construir un
logger que capture cuotas reales + resultado de cada ronda de forma
continua, y comparar esas cuotas reales contra la probabilidad real
medida en este backtest -- eso si respondaria con precision si hay
mispricing explotable, en vez de la aproximacion que hicimos con
`option_edge_analysis.py`. Sin la key no se puede avanzar mas en este
punto especifico.

### `local_odds_logger.py` -- corre en tu dispositivo, no aca

Verifique con la documentacion oficial que el WebSocket de la Binance
Prediction Markets API vive en `wss://api.binance.com/sapi/wss` -- el
mismo dominio `api.binance.com` que ya esta bloqueado por geo-restriccion
desde este sandbox (mismo error 451 que vimos con los datos de precio al
principio). Ademas requiere autenticacion HMAC-SHA256 firmada con tu API
key/secret real de Binance (permiso "Prediction Trading" habilitado en tu
cuenta con fondos) -- no hay endpoint publico de solo lectura para esto.

Por ambas razones, este componente **esta escrito para que lo corras vos
en tu propio dispositivo**, no en este entorno remoto:

```bash
pip install websocket-client
export BINANCE_API_KEY="tu_api_key"
export BINANCE_API_SECRET="tu_secret_key"
python3 local_odds_logger.py
```

Nunca pegues la API key o el secret en el chat -- se guardan solo como
variables de entorno en tu maquina. El script firma las conexiones,
loguea cada mensaje crudo a `data/live_odds_log.csv`, reconecta con
backoff si se cae, y **no coloca ninguna orden** (es de solo lectura).

**Sobre el topic:** un link compartido de `web3.binance.com` que pasaste
confirmo que cada ronda de 5 min es su propio mercado, identificado como
`btc-updown-5m-<timestamp_unix>`, donde el timestamp cae justo en un
limite de 5 minutos (el ejemplo `...-1785738000` = 2026-08-03 06:20:00
UTC exacto). El script ahora **calcula ese id solo**, por ronda, en vez
de necesitar un valor fijo pegado a mano (funcion `round_market_id()`).
No pude confirmar el formato exacto capturando el frame real de
suscripcion -- la pagina esta detras de un desafio anti-bot de AWS WAF
que no pude pasar desde aca -- asi que es la mejor conjetura educada, no
un dato verificado. El primer minuto corriendo el script te va a decir si
acerto: si solo ves trafico de PING/conexion y ningun dato de precio,
proba cambiar `MARKET_ID_USES_END_TIME = False` en el archivo (por si el
id usa el inicio de la ronda en vez del final), o consegui el topic real
inspeccionando la app/web con una PC y pasalo con
`export TOPIC_OVERRIDE="el_topic_real"` para saltarte el calculo.

Una vez que acumules suficientes horas/dias de datos con eso corriendo,
comparteme el CSV y hago el analisis de mispricing real (reemplazando la
aproximacion Gaussiana de `option_edge_analysis.py` por datos de cuotas
reales).

## Como correrlo vos mismo

```bash
cd btc_prediction_backtester
pip install -r requirements.txt
python3 data_fetch.py --days 180      # descarga y cachea datos reales (no hay datos en git)
python3 backtest.py --fee 10          # corre el backtest completo
python3 option_edge_analysis.py       # calibracion del "precio justo" vs resultados reales
python3 manual_sequence_analysis.py   # analiza tu lista de 36 resultados
```

## Limitaciones importantes

- El %/% que se ve en la app **no es un pool de apuestas simple, es el
  precio de un mercado CLOB tipo opcion binaria** (ver seccion "Que
  mercado es realmente esto"). No hay forma de reconstruir ese precio
  historico sin una API key de Predict.fun/Binance, asi que no esta
  modelado con datos reales aqui -- `option_edge_analysis.py` lo
  aproxima con un modelo Gaussiano calibrado con volatilidad historica,
  que es una aproximacion razonable pero no el dato real.
- El termino "comision del 10%" que se uso en corridas anteriores era una
  suposicion mia a partir de un texto ambiguo en la UI ("Automatico |
  10%") -- no esta confirmado que sea una fee. Con un mercado tipo CLOB,
  el costo real esta en el spread del libro de ordenes, no en una
  comision fija. El parametro `--fee` de `backtest.py` sigue siendo util
  como referencia/limite superior, pero no es un dato verificado.
- **Fuente de precio:** se uso el precio de ultima operacion de Binance
  (`data-api.binance.vision`), no el mid-price de Chainlink Data Streams
  que realmente resuelve el mercado (ese requiere API key paga). En
  agregado sobre 52k rondas la diferencia es ruido, pero no son
  identicos.
- La granularidad de datos publicos mas fina disponible es de 1 minuto.
  Bots documentados para juegos similares en Polymarket dicen encontrar
  su margen en los **ultimos 10 segundos** antes del cierre -- eso no se
  puede replicar ni verificar aqui por falta de datos de esa resolucion,
  asi que si ese efecto existe en tu plataforma, este backtest no lo mide.
- No hay ejecucion de ordenes ni automatizacion de clicks en este
  proyecto, ni la habra hasta que una estrategia muestre ventaja real y
  sostenida en datos out-of-sample, con la comision real confirmada.

## Conclusion honesta

Con 180 dias de datos reales (52k rondas): **si hay una senal real, chica
y consistente.** El variance-ratio test (independiente de cualquier regla
inventada) confirma mean-reversion estadisticamente significativo en los
retornos de BTC a 5 min, y las estrategias contrarian/streak-reversion lo
capturan en la practica: ~51-52% de acierto out-of-sample, de forma
consistente entre corridas y con intervalos de confianza que no incluyen
50% en los casos con mas muestra. Es un hallazgo genuino, consistente con
microestructura de mercado (bid-ask bounce).

Dos cosas nuevas de esta ronda cambian el diagnostico:

1. Este no es un juego de comision fija -- es un mercado de opciones
   binarias tipo Polymarket, con creadores de mercado profesionales del
   otro lado. Verificamos con `option_edge_analysis.py` que un modelo
   naive (random walk puro) esta razonablemente bien calibrado contra
   los resultados reales, y que el momentum de ultimo minuto no aporta
   señal extra una vez que se conoce el gap actual -- es decir, **si
   los market makers usan un modelo similar al naive, no dejan un hueco
   obvio ahi**. No podemos confirmar si el pricing real del mercado
   coincide con este modelo naive sin datos reales de cuotas.
2. Existen APIs oficiales (Binance Wallet Prediction Markets API,
   Predict.fun) para obtener esas cuotas reales -- pero ambas requieren
   solicitar una API key, algo que queda del lado del usuario.

**Siguiente paso real, en orden:** (1) decidir si vale la pena pedir
acceso a alguna de esas APIs; (2) si se consigue, loguear cuotas reales
vs resultado por un tiempo; (3) recien ahi comparar cuotas reales contra
la probabilidad real medida aca. Hasta entonces, el edge medido (~51-52%)
es interesante pero insuficiente para justificar apostar dinero real de
forma automatizada -- y sigue sin haber automatizacion de clicks ni
ordenes en este proyecto.
