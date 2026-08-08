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

**Actualizacion importante -- fee real confirmada EN VIVO (no un ejemplo
generico de documentacion).** Corriendo `local_odds_logger.py` contra el
endpoint real `market/list`, la respuesta trae, para el topic "BTC Up or
Down 5m" en si:

```json
{"feeRateBps": 200, "slippageBps": 1000}
```

Eso es **fee real = 2.00%** (200 basis points), y **10.00% de tolerancia
maxima de slippage** (1000 bps) -- este ultimo numero coincide con el
"Automatico | 10%" que se veia en la UI desde la primera captura al
principio de todo esto; con esto casi seguro **no era una comision como
asumi originalmente, era el limite de slippage configurado**. La primera
suposicion (10% de fee) estaba mal, y la segunda (0.4%, de un ejemplo
generico en la doc de Agentic Wallet) tambien estaba mal -- esta es la
buena, porque sale del propio mercado, no de un ejemplo ilustrativo.

Con **2% de fee real**, el breakeven es **50.51%**:

```
Con una comision asumida de 2.00%, hace falta un win rate > 50.51% para ganar en el largo plazo

23 de ~60 estrategias probadas superan ese breakeven de forma
estadisticamente significativa (limite inferior del IC95% > 50.51%):
  contrarian_last_1:          51.5% OOS  IC95% [50.9%, 52.1%]
  streak_reversion_2:         51.6% OOS  IC95% [50.7%, 52.5%]
  streak_reversion_3:         52.3% OOS  IC95% [51.1%, 53.6%]
  micro_mean_reversion_5m:    51.5% OOS  IC95% [50.9%, 52.1%]
  volume_imbalance_contrarian: varias variantes tambien cruzan
```

**Pero ojo con dos cosas antes de entusiasmarse:**

1. **No son 23 hallazgos independientes.** Son variantes muy parecidas
   del mismo efecto de fondo (contrarian/mean-reversion) que ya habiamos
   identificado con el variance-ratio test. Que crucen el umbral sirve
   como confirmacion de que el efecto es consistente entre variantes, no
   como "encontramos 23 estrategias distintas" -- serian, en la
   practica, la misma apuesta expresada de formas ligeramente distintas.
2. **El slippage/price-impact real de ejecutar la orden sigue sin estar
   medido.** El `slippageBps: 1000` es la tolerancia MAXIMA configurada
   (lo que el sistema permite antes de rechazar la orden), no el costo
   promedio real de ejecutar -- eso depende de cuanta liquidez haya en
   el order book en ese momento. Con precios entre ~51% y ~53% de
   acierto, el margen sobre el 50.51% de breakeven-por-fee es de apenas
   0.5-3 puntos porcentuales -- un price impact real de solo 1-2% en una
   operacion ya se come buena parte de esa ventaja. Esto es lo que falta
   medir con datos reales de order book, no con un limite maximo
   configurado.

Dicho de otra forma: con la fee real confirmada, el efecto que
encontramos **podria** ser explotable, pero falta el dato mas
importante -- cuanto cuesta realmente ejecutar (price impact real,
medido con el order book en vivo, no un limite maximo configurado ni
la doc) -- y eso solo se mide con cuotas/order-book reales en vivo, no
con nuestra aproximacion de precio historico.

## Descubrimiento: "Agentic Wallet" -- el propio bot de Binance para esto

Mientras buscaba la fee real, encontre que Binance tiene un producto
llamado **Agentic Wallet** cuya documentacion describe, casi literal,
lo que este proyecto intenta hacer: un asistente conversacional dentro
de la wallet de Binance al que se le puede pedir en lenguaje natural que
seguido el precio de BTC, juzgue Up/Down ronda a ronda, apueste dentro de
limites de exposicion, y aplique take-profit/stop-loss -- con la
salvedad explicita de que "los datos de precio de BTC deben venir de tu
propia fuente de datos o un Skill" (o sea, el propio agente de Binance
NO trae su propio analisis de precio -- exactamente el hueco que este
backtester intenta llenar). Esto **no es una API externa que yo pueda
llamar directamente** -- es la propia IA de Binance dentro de su app,
pensada para que el usuario le hable directamente. Lo dejo documentado
porque fue el hilo que me llevo a buscar la fee real (encontrada despues
en vivo via `market/list`, no en esta doc), y porque si en algun momento
se quiere ir por ese camino en vez de construir todo a mano, esa es la
puerta oficial.

## Monitoreo en vivo -- endpoints REST reales (no el WebSocket que probamos)

La primera version de `local_odds_logger.py` uso un WebSocket
(`wallet-events`) que resulto ser para **notificaciones de tus propias
ordenes** (compra exitosa, orden llena, etc.), no para cuotas de mercado
-- por eso conectaba bien (`REGISTER` exitoso) pero nunca llegaba nada
util. Encontre la documentacion real
(`developers.binance.com/en/docs/products/w3w-prediction/`, accesible
via su `llms.txt`/`llms-full.txt` -- un indice en texto plano pensado
para que lo lean agentes como yo, sin necesidad de renderizar JavaScript)
y confirme que existen dos canales separados:

- `web3_prediction_pm_*` (wallet-events): tus propias ordenes -- el que
  probamos por error.
- `web3_prediction_orderbook_{marketId}`: **el canal real de cuotas en
  vivo**, con `marketId` siendo un ID numerico interno de Binance (no el
  slug `btc-updown-5m-<timestamp>` de la URL, que era una conjetura
  incorrecta). Tambien existe `web3_prediction_orderbook_data`, un topic
  agregado que trae todos los mercados en un solo stream.

Ademas existen endpoints REST de solo lectura, bajo `market-data` (no
marcados `USER_DATA` como los de posiciones/balance, lo que sugiere un
nivel de autenticacion mas liviano, aunque no lo pude confirmar en vivo):

```
GET /sapi/v1/w3w/wallet/prediction/category/list
GET /sapi/v1/w3w/wallet/prediction/market/list
GET /sapi/v1/w3w/wallet/prediction/market/search
GET /sapi/v1/w3w/wallet/prediction/market/detail
GET /sapi/v1/w3w/wallet/prediction/order-book
GET /sapi/v1/w3w/wallet/prediction/order-book/last-trade-price
```

`local_odds_logger.py` fue reescrito para usar **REST en vez del
WebSocket** -- mas simple de tener bien la primera vez que volver a pelear
con la firma de otro canal WS. Sigue sin poder confirmarse en vivo desde
este entorno (mismo bloqueo geografico de `api.binance.com`), asi que el
script imprime la respuesta cruda de cada llamada para poder ajustar
nombres de parametros/campos juntos si mi conjetura inicial no pega.

### Como correrlo

```bash
pip install requests
export BINANCE_API_KEY="tu_api_key"
export BINANCE_API_SECRET="tu_secret_key"
python3 local_odds_logger.py
```

Nunca pegues la API key o el secret en el chat -- se guardan solo como
variables de entorno en tu maquina. El script:

1. Busca el mercado "BTC Up or Down 5m" via `market/list` (`market/search`
   devuelve `-1022 signature invalid` con cualquier nombre de parametro
   que probe, asi que el script ya no lo usa -- `market/list` funciona
   bien y trae todo lo necesario). La respuesta real, confirmada en vivo,
   tiene esta forma: un array `marketTopics`, cada uno con `feeRateBps`,
   `slippageBps`, y una lista anidada `markets` donde cada ronda actual
   trae su `marketId` numerico (el dato que hacia falta -- el slug
   `btc-updown-5m-<timestamp>` de la URL compartida NO es el mismo id que
   usan estos endpoints). El script imprime la respuesta cruda en cada
   paso -- si algun campo no matchea en tu corrida, queda guardado en
   `data/raw_api_responses.jsonl` para ajustarlo juntos.
2. Pollea `order-book` (con `vendor`, `tokenId` y `conditionId`, los
   parametros reales que el endpoint exige segun errores `-3026` en vivo)
   cada 5 segundos y loguea cada snapshot a `data/live_odds_log.csv` --
   **no coloca ninguna orden**. Cada fila trae, ademas del JSON crudo,
   columnas ya parseadas: `best_bid`, `best_ask`, `mid_price` (la cuota),
   y tambien `btc_price`, `round_start_price`, `price_gap_usd` -- el mismo
   "Precio actual -$X.XX" que se ve en la app, calculado con el ticker
   publico de `data-api.binance.vision` (independiente de la API privada),
   para poder comparar directamente cuota vs. gap de precio real en cada
   momento sin tener que reconstruirlo despues.
3. Cuando una ronda termina (alineado al limite real de 5 minutos, no a
   un timer relativo a cuando arranco el script), **resuelve el
   resultado Up/Down de esa ronda** usando el mismo metodo publico y ya
   verificado que usa `data_fetch.py`/`backtest.py` -- compara el precio
   de apertura de la vela de 1m al inicio vs al final de la ronda en
   `data-api.binance.vision` (no hace falta adivinar el esquema de
   "mercado resuelto" de la API privada). Guarda el resultado en
   `data/round_outcomes.csv` (`market_id`, precios de inicio/fin,
   `outcome`).

Con `live_odds_log.csv` + `round_outcomes.csv` juntos (cruzando por
`market_id`) se puede finalmente comparar la cuota real del mercado en
cada momento contra lo que efectivamente paso -- el analisis de
mispricing real que `option_edge_analysis.py` solo podia aproximar.

Una vez que acumules suficientes horas/dias de datos con eso corriendo,
comparteme ambos CSVs (o los primeros errores/respuestas crudas si algo
no matchea) y sigo desde ahi.

### Alertas en vivo (dos tipos, con distinto respaldo)

El script manda alertas por **tres canales independientes** (consola
siempre, Telegram si lo configuras, `termux-notification` si esta
instalado) de **dos tipos distintos** -- importa no mezclarlos, porque
tienen niveles de evidencia muy diferentes:

#### Configurar el bot de Telegram (opcional pero recomendado)

1. En Telegram, habla con **@BotFather** -> `/newbot` -> segui las
   instrucciones -> te da un **token** (algo como
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`).
2. Mandale cualquier mensaje a tu bot nuevo (para que Telegram registre
   la conversacion).
3. Consegui tu `chat_id` corriendo esto (reemplaza el token):
   ```bash
   curl -s "https://api.telegram.org/bot<TU_TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9-]*'
   ```
   El numero que aparece despues de `"id":` es tu `chat_id`.
4. Agrega ambos a tu archivo de credenciales:
   ```bash
   cat >> ~/.btc_env << 'EOF'
   export TELEGRAM_BOT_TOKEN="tu_token"
   export TELEGRAM_CHAT_ID="tu_chat_id"
   EOF
   source ~/.btc_env
   ```

Igual que con las credenciales de Binance: **nunca pegues el token de
Telegram en el chat** -- solo en tu terminal.

1. **Que lado considerar, al inicio de cada ronda** (`check_streak_signal`):
   se activa cuando hubo una racha de 3 o 4 resultados iguales seguidos,
   basado en `streak_reversion_3/4` del backtest historico (con la fee
   real de 2% ya confirmada). Esto tiene respaldo estadistico
   preliminar -- pero **no esta confirmado contra datos de order-book
   en vivo todavia** (eso es justo lo que este logger esta juntando), y
   el backtest no incluye price impact real. No es una garantia, es
   informacion para tu propio criterio. Racha de 2 no dispara alerta a
   proposito (pasa en ~50% de las rondas, seria puro ruido).
2. **Spread angosto = buen momento de ejecucion** (`check_spread_alert`):
   esto **no predice Up/Down para nada**. Ya investigamos con
   `option_edge_analysis.py` si el timing dentro de la ronda agrega
   señal direccional y la respuesta fue no -- el momentum del ultimo
   minuto no aporta nada una vez que se conoce el gap de precio. Esta
   alerta es solo sobre calidad de ejecucion (spread angosto = mas
   barato entrar/salir ahora mismo), para *si ya decidiste* un lado, no
   para decidir cual.

Ninguna de las dos coloca ordenes -- son notificaciones, no acciones.

## Como correrlo vos mismo

```bash
cd btc_prediction_backtester
pip install -r requirements.txt
python3 data_fetch.py --days 180      # descarga y cachea datos reales (no hay datos en git)
python3 backtest.py --fee 2.0         # corre el backtest completo (2.0% = feeRateBps=200, confirmado en vivo)
python3 option_edge_analysis.py       # calibracion del "precio justo" vs resultados reales
python3 manual_sequence_analysis.py   # analiza tu lista de 36 resultados
```

## Limitaciones importantes

- El %/% que se ve en la app **no es un pool de apuestas simple, es el
  precio de un mercado CLOB tipo opcion binaria** (ver seccion "Que
  mercado es realmente esto"). No hay forma de reconstruir ese precio
  historico sin una API key real, asi que no esta modelado con datos
  reales aqui -- `option_edge_analysis.py` lo aproxima con un modelo
  Gaussiano calibrado con volatilidad historica, que es una aproximacion
  razonable pero no el dato real.
- **La fee de 2.0% (`feeRateBps: 200`) esta confirmada en vivo** via
  `market/list` para el topic BTC Up or Down 5m -- ya no es una
  suposicion. Pero el `slippageBps: 1000` (10%) es la tolerancia MAXIMA
  configurada, no el price impact real promedio de ejecutar una orden --
  eso depende de la liquidez del order book en cada momento y no esta
  incluido en el `--fee` de `backtest.py`. El breakeven de 50.51% que
  reporta el backtest es, por lo tanto, un piso optimista (solo fee), no
  el costo total real de operar.
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

Con 180 dias de datos reales (52k rondas): **hay una senal real, chica y
consistente.** El variance-ratio test (independiente de cualquier regla
inventada) confirma mean-reversion estadisticamente significativo en los
retornos de BTC a 5 min, y las estrategias contrarian/streak-reversion lo
capturan en la practica: ~51-53% de acierto out-of-sample, de forma
consistente entre corridas y con intervalos de confianza que no incluyen
50% en los casos con mas muestra. Es un hallazgo genuino, consistente con
microestructura de mercado (bid-ask bounce).

Lo que cambio en esta ronda:

1. **La fee real (2.0%, `feeRateBps: 200`, confirmada en vivo via
   `market/list`) es mucho mas chica que el 10% que se habia asumido al
   principio** por un texto ambiguo de la UI -- que ahora creo que en
   realidad era el `slippageBps: 1000` (10% de tolerancia maxima de
   slippage), no una fee. Con el breakeven correcto (50.51% en vez de
   52.6%), 23 de ~60 variantes contrarian/mean-reversion lo superan de
   forma estadisticamente significativa.
2. **Pero eso no incluye el price impact real de ejecutar** -- el 10%
   que trae la API es un limite maximo configurado, no el costo promedio
   real, que depende de la liquidez del order book en cada momento. Con
   un margen de apenas 0.5-3 puntos porcentuales sobre breakeven-por-fee,
   un price impact real de 1-2% ya podria borrar la ventaja. Esto es lo
   unico que falta medir con datos reales de order book, no con un
   limite configurado.
3. Encontramos los endpoints REST y WebSocket reales para leer cuotas en
   vivo (`order-book`, topic `web3_prediction_orderbook_{marketId}` con
   `marketId` numerico real, ej. `6815131`) -- el canal que se probo
   primero (`wallet-events`) resulto ser solo notificaciones de ordenes
   propias, no cuotas de mercado. Con eso, `local_odds_logger.py` ahora
   apunta al lugar correcto y ya identifico en vivo el topic/mercado
   actual la primera vez que corrio.

**Siguiente paso real, en orden:** (1) terminar de correr
`local_odds_logger.py` en tu dispositivo con una API key nueva (nunca la
anterior expuesta en el chat) para loguear cuotas + resultado reales por
un tiempo; (2) con eso, medir el price impact real en la practica, no un
limite maximo configurado; (3) recien ahi comparar el costo total real
contra el ~51-53% de acierto medido aca para saber si de verdad sobra
margen. Hasta entonces, el edge medido es prometedor y ahora con una fee
real confirmada en vez de adivinada, pero **todavia no confirmado como
rentable neto de todos los costos reales** -- y sigue sin haber
automatizacion de clicks ni ordenes en este proyecto.
