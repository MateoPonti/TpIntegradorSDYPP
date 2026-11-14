# Contrato v2 — TP Integrador · Grupo SDMER

Borrador del 14/11/2026. Junta lo que hay en `docs/contrato-1.md`, el Word *"Contrato SDMER ampliado puntos 1
a 4"* (13/11) y lo charlado después. **Nada de esto está decidido**: lo que dice "propuesta" lo confirma el
grupo.

## 0. Qué pidió el profe

- Para los puntos **1 a 4**: fijarse **cómo lo implementan otros protocolos**. Entendemos que se refiere a
  otras blockchains (Bitcoin, Ethereum, Stellar, Solana…), pero **conviene confirmarlo con él**. Algunos puntos
  también se apoyan en protocolos que no son blockchain (Stratum, TLS, WebSocket).
- El punto **5** no necesita investigación, pero **tiene que estar en el contrato**. El Word ampliado no lo
  incluye: hay que agregarle un párrafo corto.
- Para el alta de workers, el profe pensó en una **página pública** (ver punto 4.3).

| # | Punto | Estado |
|---|---|---|
| 1 | Registro de workers y personas (clave pública y privada) | Por hacer |
| 2 | Cuenta compartida / fee, crowdfunding (transferencia con aprobación extra) | Por hacer |
| 3 | Notificar por websocket, para no gastar ciclos de gusto | Por hacer |
| 4 | Gateway: el worker funciona y "dice la verdad" + clasificación por potencia y recompensa + websocket | En parte |
| 5 | KEDA, colas quorum, recreación del bloque por cambio de dificultad | Hecho |

---

## 1. Registro de personas y workers

### Cómo lo hacen otros protocolos

| Protocolo | Cómo lo hace |
|---|---|
| Bitcoin | No hay registro: cualquiera genera un par de claves y ya tiene cuenta. Firma con ECDSA o Schnorr (Taproot). La dirección es un hash de la clave pública. |
| Ethereum | Tampoco hay registro. Dirección = últimos 20 bytes del hash de la clave pública. **Nonce por cuenta** + `chainId` (EIP-155) para que una tx firmada no se repita. Inicio de sesión firmando un texto al azar (EIP-4361). |
| **Stellar** | Ed25519, la dirección es la clave pública. **Sí hay alta**: la cuenta existe recién cuando otra la crea con un saldo mínimo. Número de secuencia por cuenta. |
| Solana | Ed25519, la dirección es la clave pública. Sin alta. |
| **Hyperledger Fabric** | Blockchain privada: una autoridad emite **certificados X.509** a personas y nodos, y se pueden revocar. Es lo más parecido al mTLS que propone el Word. |
| ENS (Ethereum) | Asocia un nombre (`mateo.eth`) a una dirección: referencia para los alias. |

### Qué propone el Word ampliado

- Personas: **Ed25519**. La firma cubre tipo de operación, red, origen, destino, importe, comisión y secuencia
  de la cuenta. JSON canónico (RFC 8785), montos enteros, reserva atómica contra el doble gasto. La secuencia es
  distinta del nonce del PoW.
- Workers: **mTLS** (certificado propio por worker, con vencimiento y revocación). La clave del worker separada
  de la clave con la que su dueño gasta.
- mTLS no hace privado al gateway: si se pide que sea privado a nivel de red, hace falta además una VPN.

**Diferencia con contrato-1:** ahí los workers entraban firmando un texto al azar (EIP-4361). **mTLS o firma
de desafío: lo decide el grupo.**

---

## 2. Cuenta compartida, fee y crowdfunding

### Cómo lo hacen otros protocolos

**Cuenta compartida (multifirma)**

| Protocolo | Cómo lo hace |
|---|---|
| Bitcoin | Script m de n (P2SH/P2WSH). Con Taproot, MuSig2 junta las firmas en una sola. |
| Ethereum | **Safe** (contrato): alguien propone, los dueños confirman, se ejecuta al llegar al mínimo. |
| **Stellar** | Incluida en la red: cada firmante tiene un **peso** y cada operación pide un **umbral** (bajo, medio o alto). |
| Solana | Squads, un programa parecido a Safe. |

**Fee**

| Protocolo | Cómo lo hace |
|---|---|
| Bitcoin | Por tamaño (satoshis por byte), como un mercado; la cobra el minero. |
| Ethereum | EIP-1559: una base que **se quema** y sube o baja según lo lleno del bloque, más una propina al validador. |
| **Stellar** | **Fija** por operación; no la cobra el validador, va a un fondo de la red. |
| Solana | Fija por firma, más una de prioridad opcional. |

**Crowdfunding**

| Protocolo | Cómo lo hace |
|---|---|
| Bitcoin | *Assurance contract* (Lighthouse): cada donante firma su parte y la tx vale recién cuando se junta la meta. |
| Ethereum | Contrato "todo o nada": si no se llega, **cada donante pide su devolución**. |
| **Stellar** | **Saldos reclamables**: plata bloqueada con condiciones de tiempo y de quién puede cobrarla. |

### Qué propone el Word ampliado

- **Multifirma m de n** con el flujo de Safe. El saldo se reserva recién al juntar las firmas. Cambiar monto o
  destino invalida las firmas. Cada propuesta se ejecuta una sola vez. Estados: pendiente de firmas, admitida,
  confirmada, vencida o rechazada.
- **Fee fija y configurable**, a una cuenta definida, **sin ordenar las tx por fee** (se mantiene el orden de
  llegada).
- **Crowdfunding todo o nada**: cuenta restringida, cierre por **hora del servidor** registrado en la cadena,
  liquidaciones y devoluciones que no se repiten aunque se reinicie, devolución del aporte completo.

**Diferencias con contrato-1:** ahí la fee la cobraba el minero y se priorizaban las que pagan más, y el
crowdfunding cerraba en un bloque límite.

---

## 3. Notificaciones por WebSocket

### Qué es WebSocket

No es para ejecutar código en la web (eso es JavaScript o WebAssembly). Es una **forma de comunicarse**: la
conexión se abre una vez y queda abierta, y **el servidor puede avisar sin que le pregunten**. Con HTTP normal,
el cliente tiene que preguntar cada tanto ("¿se confirmó?", "¿hay tarea?"), y la mayoría de las veces la
respuesta es "no". Eso son los "ciclos de gusto" del punto 3.

### Cómo lo hacen otros protocolos

| Protocolo | Cómo lo hace |
|---|---|
| Ethereum | `eth_subscribe` por WebSocket: bloques nuevos, eventos, tx pendientes. Si se corta, se pierde lo de ese rato. |
| Bitcoin Core | **ZMQ** publica cada bloque y tx; también `-blocknotify` corre un comando por bloque. |
| Electrum | Suscripción a una dirección: avisa cuando cambia su estado. |
| **Solana** | `accountSubscribe`, `signatureSubscribe`, con **niveles de confirmación** (procesada, confirmada, finalizada), como nuestros estados admitida/confirmada. |
| **Stellar Horizon** | Streaming con **cursor**: al reconectarse se pide "desde el último evento que vi" y no se pierde nada. |
| Fabric | Eventos de bloque, pedibles desde un bloque dado. |

### Qué propone el Word ampliado

- Servicio **notifier** que mantiene las conexiones; recibe eventos de RabbitMQ con una cola por réplica (una
  cola compartida repartiría los eventos y algún cliente se quedaría sin aviso).
- **Transactional outbox**: el cambio y su evento se guardan juntos y después se publican con reintentos. Los
  consumidores toleran repetidos.
- Al reconectarse, el cliente pide una **foto actualizada** del estado. No se garantiza cada aviso perdido; el
  historial con cursor (como Stellar) queda como extensión.
- API propia; toma `eth_subscribe` como idea, sin ser compatible.

---

## 4. Validar workers, clasificarlos y pagarles

### 4.1 Qué ya tenemos (revisado en el código)

- **Calibración** al arrancar cada worker.
- **Canarios**: 1 de cada 20 tareas (`CANARY_EVERY`) tiene solución conocida.
- **Cuarentena** (`tpi_common/quarantine.py`): por canario fallado, "encontré" inválido o 3 préstamos vencidos
  seguidos.
- **Clases** rápido / medio / lento por el hashrate **real** (lo entregado, no lo declarado).
- **Premio proporcional** a los nonces completados (`coordinator/reward.py`), pagado en el bloque siguiente.
  Ojo: ahí la variable se llama `shares`, pero cuenta nonces completados, **no** son shares de Stratum.

### 4.2 Qué falta

- **WebSocket**: los mineros siguen pidiendo tarea cada 1 s (`GET /tasks/next`).
- **Shares** al estilo Stratum (mejora a confirmar).
- **Identidad propia de cada worker**: todos usan el mismo `MINER_TOKEN` (depende del punto 1 y del 4.3).

### 4.3 Alta de workers y token (falta)

Hoy no hay forma de sumarse: el token es **uno solo**, en una variable de entorno. Dos opciones:

- **Página pública de alta** (la que pensó el profe): cualquiera pide unirse y recibe su credencial propia.
- **Token generado por el servidor y entregado por el grupo**: más simple, pero cerrado.

La página es un componente nuevo: lo confirma el grupo.

### 4.4 Problemas de la cuarentena actual si el alta es pública

- La cuarentena dura **5 minutos** (`QUARANTINE_SECONDS=300`) y después el worker vuelve como si nada.
- El **`miner_id` lo elige el propio worker** en el keep-alive; el gateway solo controla el token compartido.
  Un tramposo cambia de nombre y entra de nuevo al instante.
- **No hay registro fijo de workers.** En Redis solo hay claves que se borran solas: `worker:<id>` (vivo, unos
  segundos), `miner:<id>:calibration` y `miner:<id>:account` (1 día), `quarantine:<id>` (5 min).

Con workers propios no importa; con alta pública es el agujero principal.

### 4.5 Propuesta para el alta pública

1. **Registro fijo en Redis** (sin vencimiento): `worker:<id>` → dueño, cuenta de cobro, fecha de alta, estado
   (`en_prueba`, `activo`, `revocado`).
2. **Token generado por el servidor** con `secrets.token_urlsafe(32)` (256 bits al azar: que dos coincidan es
   prácticamente imposible). Igual se guarda con `SET NX`, que falla si ya existe; si falla, se genera otro.
3. **Se guarda el hash del token, no el token.** Si alguien lee Redis, no se lleva los tokens. El gateway busca
   el hash del token que llega y de ahí saca el `miner_id`: **el worker ya no elige su nombre**.
4. **Canarios y cuarentena:**
   - Worker nuevo **en prueba**: más canarios (por ejemplo, 1 de cada 5) y sin premio hasta pasar varios.
   - Mentira comprobada (canario fallado o "encontré" inválido): **se revoca el token**; para volver hay que
     darse de alta otra vez.
   - Préstamos vencidos (se colgó, no mintió): cuarentena temporal, como hoy.
5. **Altas repetidas:** limitar cuántas se aceptan, o exigir que cada worker esté asociado a una cuenta con
   clave (punto 1).

### 4.6 Cómo lo hacen otros protocolos

| Protocolo | Cómo lo hace |
|---|---|
| **Stratum V1** | El de los pools reales. Conexión abierta; el pool **empuja** el trabajo (`mining.notify`). El worker manda **shares** (`mining.submit`): hashes que cumplen una dificultad más fácil que la del bloque. **Vardiff**: dificultad de shares ajustada a cada worker. El alta es mínima: `usuario.worker` y contraseña casi siempre ignorada. |
| Stratum V2 | Lo mismo, con mensajes binarios, conexión cifrada y el servidor autenticado. |
| Monero P2Pool | Las shares van en una cadena paralela: no hay que confiar en el pool para el reparto. |
| **BOINC** | Computación voluntaria (no blockchain). Le da **la misma tarea a 2 workers y compara**. Es lo único de la lista que comprueba un rango que terminó **sin solución**, algo que las shares no prueban. |

**Formas de repartir el premio en los pools**

| Esquema | Cómo reparte |
|---|---|
| PROP | Proporcional al trabajo de la ronda. **Es el nuestro.** Tiene el problema del *pool hopping* (conviene entrar y salir según cómo venga la ronda). |
| PPS / FPPS | Monto fijo por share aunque no se encuentre bloque; el riesgo lo asume el pool. |
| PPLNS | Según las últimas N **shares** (no N bloques, como decía contrato-1). Castiga el pool hopping. |

### Qué propone el Word ampliado

- WSS entre gateway y workers; las conexiones de workers las atiende el gateway y las de personas el notifier.
- **Shares** estilo Stratum como mejora a confirmar; si la dificultad es por worker, se pondera por dificultad.
- Clases con ventana de rendimiento y umbrales documentados. PPLNS opcional.

---

## 5. KEDA, colas quorum y recreación del bloque

**Hecho** el 13/11 (sin investigación de protocolos, pero va en el contrato):

- **KEDA**: mineros CPU todo o nada (8 sin GPU y con trabajo, 0 a los 60 s, 0 con GPU), consultando cada 5 s.
- **Colas quorum**: `mining.tasks`, `mining.results` y `mining.dlq`. Con un nodo de RabbitMQ dan durabilidad y
  límite de entregas, no alta disponibilidad.
- **Recreación del bloque**: si cae el hashrate, el coordinator rearma el bloque en curso con un prefijo más
  corto (mismas tx, `block_id` nuevo).

---

## Blockchains más parecidas a nuestras ideas (búsqueda del 14/11/2026)

El profe confirmó que "otros protocolos" son **otras blockchains que implementen lo mismo**. Buscamos las que
más se parecen a cada idea y qué les podemos copiar. Fuentes al final de la sección.

### Punto 1 — Registro

**Bittensor** es la más parecida: es una red pública donde cualquiera registra mineros y validadores que los
puntúan.
- Cada participante tiene **dos claves**: la *coldkey* guarda la plata y paga el registro; la *hotkey* es la
  identidad con la que opera el minero. Es lo mismo que pide el Word: **clave del worker separada de la clave
  con la que el dueño gasta**. [B1]
- El registro es **público y sin permiso**, pero cuesta: hay un **precio que sube con cada alta y baja con el
  tiempo**. Eso frena que alguien se registre mil veces. [B1, B2]
- Los lugares son limitados: cuando entra uno nuevo, sale el que menos ganó (salvo los recién llegados, que
  tienen un período de inmunidad). [B3]

**Polkadot (pallet de identidad)**: la persona carga sus datos en la cadena y le pide a un **registrador** que
los verifique; el registrador da un "juicio" con un nivel de confianza. **Si cambia un dato, pierde la
verificación** y tiene que pedirla de nuevo. [P1, P2]

**Ideas para nosotros:**
- Alta pública de workers (lo que pensó el profe) con **costo que sube con cada alta**: en monedas del TP o
  como espera. Es la respuesta al "límite de altas" del punto 4.5.
- Worker = *hotkey* (su token o certificado); dueño = *coldkey* (la clave Ed25519 de su cuenta). La cuenta del
  dueño es la que cobra el premio.
- El alias de una persona lo puede "verificar" el grupo como registrador, y cambiarlo saca la verificación.

### Punto 2 — Cuenta compartida, fee y crowdfunding

**Polkadot (pallet multisig)** es casi idéntico al flujo de Safe, pero dentro de la red:
- El primero que aprueba **abre la operación y deja un depósito**, que se le devuelve al ejecutarse o cancelarse.
  Las aprobaciones se hacen sobre el **hash de la operación**. Aprobar dos veces con el mismo firmante falla. La
  última aprobación, la que llega al umbral, es la que ejecuta. Cancelar pide los mismos datos. [P3, P4]

**Cardano (native scripts)**: la condición de gasto se arma combinando reglas: "al menos M de N firmas"
(`atLeast`), "todas" (`all`) y **límites de tiempo en slots** (válido antes o después de tal slot). Advierten
que un script mal armado **puede dejar la plata bloqueada para siempre**. [C1, C2]

**Algorand**: la cuenta compartida es nativa; su dirección sale de la **lista de firmantes + umbral + versión**.
La fee de una tx multifirma es la mínima de la red, apenas más grande por llevar varias firmas. [A1, A2]

**Polkadot (crowdloans)**: los aportes quedan **bloqueados hasta un bloque de cierre**. Si la campaña no lo
logra, se "retira" y **cada aportante pide su devolución**. El *cap* es un **tope** de lo que se puede juntar,
no la meta. [P5, P6]

**Stellar (fee, verificado)**: 100 stroops por operación cuando hay lugar; cuando el ledger se llena, pasa a
**subasta** y entran primero las que ofrecen más. [S1] Lo que decíamos de que la fee "va a un fondo de la red"
**no lo pudimos confirmar**: no usarlo en el informe sin fuente.

**Ideas para nosotros:**
- Multifirma estilo Polkadot: **depósito al proponer** (frena propuestas basura y se devuelve), aprobar sobre el
  hash de la propuesta, cancelación explícita.
- Vencimiento de la propuesta **en bloques** (como los slots de Cardano), no en hora.
- Dirección de la cuenta compartida = hash de (firmantes, umbral), como Algorand: no hace falta un alta aparte.
- Crowdfunding: **cierre en un bloque** (como contrato-1 y Polkadot) en vez de hora del servidor, y devolución
  **a pedido de cada aportante** (más simple que pagarle a todos de una). Separar **meta** de **tope**.
- Fee fija mientras haya lugar en el bloque; prioridad por fee solo si el bloque se llena (como Stellar).

### Punto 3 — Notificaciones

**Polkadot (`author_submitAndWatchExtrinsic`)**: mandás la tx y te queda una **suscripción a esa tx** por
WebSocket que avisa cada estado: `ready` → `broadcast` → `inBlock` → `finalized`, o una salida de error
(`dropped`, `invalid`, `usurped`, `retracted`). Si se corta la conexión, se pierde la suscripción y hay que
consultar el estado de nuevo. [P7, P8]

**Kaspa (wRPC)**: suscripciones por WebSocket a **cambios de una dirección** (`utxos-changed`) y a cambios de la
cadena. Recomiendan **volver a suscribirse dentro del evento de conexión** y **suscribirse antes de pedir el
estado actual**, para no perder nada entre medio. [K1, K2]

**Ideas para nosotros:**
- `POST /transactions` puede devolver el `tx_id` y el cliente **se suscribe a ese `tx_id`**, con estados
  propios: `pendiente` → `en_bloque` → `confirmada`, o `rechazada`.
- Al reconectar: **primero suscribirse, después pedir la foto del estado** (orden de Kaspa). Así se resuelve lo
  que el Word dejaba abierto sin necesitar historial.

### Punto 4 — Validar workers, clasificarlos y pagarles

**Bittensor** otra vez: los **validadores puntúan a los mineros** según su rendimiento y ese puntaje define
cuánto cobra cada uno. La cadena no juzga ni banea: el que rinde poco cobra poco y termina desplazado. Una
subred puede pedir **colateral**: plata bloqueada que vuelve si el minero trabaja bien. [B1, B4]

**Golem**: le da **la misma tarea a dos proveedores** y compara; si no coinciden, un tercero desempata. Lleva una
**reputación** por proveedor que **baja más por un resultado falso que por una falla técnica**, y el que pide
trabajo filtra por reputación. Un paper aclara que redundancia y reputación **reducen el riesgo, no prueban** que
el resultado sea correcto. [G1, G2, G3]

**Akash**: los proveedores se registran en la cadena y un **auditor** verifica lo que declaran (con una carga de
prueba larga) y lo **firma**. El que contrata puede exigir proveedores auditados. [K3, K4]

**Monero P2Pool**: pool sin operador y **sin registro**: la share lleva la dirección de pago. Cada share **pesa
según su dificultad** y queda en una ventana PPLNS de 2160 shares (unas 6 horas). El premio se reparte según la
dificultad total de cada uno en la ventana. [M1]

**Ideas para nosotros:**
- Cambiar la cuarentena binaria por una **reputación** como Golem: baja fuerte con canario fallado o "encontré"
  falso, baja poco con préstamos vencidos. Con reputación baja: más canarios o revocación.
- Worker nuevo **en prueba** hasta pasar la calibración y varios canarios: es la "auditoría" de Akash, hecha por
  el gateway.
- **Colateral opcional** (Bittensor): el dueño bloquea algo de saldo al dar de alta el worker y lo pierde si se
  comprueba que mintió. Hace caro el fraude con alta pública.
- Doble verificación de algunos rangos "no encontré" con otro worker (Golem / BOINC).
- Si se adoptan shares: pesarlas por dificultad y ventana PPLNS, como P2Pool.

### Fuentes

- [B1] Bittensor — Mining: https://docs.learnbittensor.org/miners
- [B2] Bittensor — burned-register: https://pr-2953.preview.bittensor.com/docs/tx/burned-register
- [B3] Taostats — Neuron registration: https://docs.taostats.io/docs/node-registration
- [B4] Bittensor — Miner collateral: https://www.bittensor.com/docs/guides/mining/collateral
- [P1] Polkadot Wiki — Identity: https://wiki.polkadot.network/docs/learn-identity
- [P2] pallet_identity: https://paritytech.github.io/polkadot-sdk/master/pallet_identity/index.html
- [P3] pallet_multisig `as_multi`: https://paritytech.github.io/polkadot-sdk/master/pallet_multisig/pallet/dispatchables/fn.as_multi.html
- [P4] OpenZeppelin — Multisig pallet: https://docs.openzeppelin.com/substrate-runtimes/pallets/multisig
- [P5] Polkadot Wiki — Crowdloans: https://wiki.polkadot.network/docs/learn-crowdloans
- [P6] Kusama Guide — Crowdloans: https://guide.kusama.network/docs/learn/learn-crowdloans
- [P7] Dwellir — author_submitAndWatchExtrinsic: https://www.dwellir.com/docs/polkadot/author_submitAndWatchExtrinsic
- [P8] Dwellir (Bifrost), diagrama de estados: https://www.dwellir.com/docs/bifrost/author_submitAndWatchExtrinsic
- [C1] Cardano — Simple scripts: https://developers.cardano.org/docs/learn/cardano-cli/simple-scripts/
- [C2] Evolution SDK — Native scripts: https://intersectmbo.github.io/evolution-sdk/docs/smart-contracts/native-scripts/
- [A1] Algorand — Multisig accounts: https://dev.algorand.co/concepts/accounts/multisig
- [A2] Algorand spec — Autorización: https://specs.algorand.co/ledger/ledger-txn-authorization
- [S1] Stellar — Fees: https://developers.stellar.org/docs/learn/fundamentals/fees-resource-limits-metering
- [K1] Kaspa Python SDK — Subscriptions: https://kaspanet.github.io/kaspa-python-sdk/dev/learn/rpc/subscriptions/
- [K2] Kaspa — RpcClient: https://kaspa.aspectron.org/docs/classes/RpcClient.html
- [G1] Golem — Verificación en gWASM: https://blog.golem.network/gwasm-verification/
- [G2] Golem — Reputation system: https://blog.golem.network/introducing-golem-networks-reputation-system/
- [G3] Paper — Blockchain-based result verification: https://arxiv.org/pdf/2110.11090
- [K3] Akash — Audited attributes: https://akash.network/docs/providers/audited-attributes
- [K4] Akash — Providers & leases: https://akash.network/docs/learn/core-concepts/providers-leases/
- [M1] Monero P2Pool: https://github.com/SChernykh/p2pool

---

## Qué tomaríamos de cada protocolo (propuesta)

| Punto | De dónde |
|---|---|
| 1 | Ed25519 + secuencia por cuenta (Stellar). Worker = hotkey, dueño = coldkey, alta pública con costo creciente (Bittensor). Workers: certificados (Fabric) o desafío firmado (Ethereum). |
| 2 | Multifirma con depósito y aprobación sobre el hash (Polkadot), vencimiento en bloques (Cardano), dirección = hash de firmantes y umbral (Algorand). Fee fija con subasta solo si se llena el bloque (Stellar). Crowdfunding con cierre en bloque y devolución a pedido (Polkadot crowdloans). |
| 3 | Suscripción por `tx_id` con estados (Polkadot). Reconexión: suscribirse primero y después pedir el estado (Kaspa). Niveles de confirmación (Solana). |
| 4 | Reputación en vez de cuarentena binaria (Golem), período de prueba / auditoría (Akash), colateral opcional (Bittensor), shares por dificultad con PPLNS (P2Pool, Stratum), doble verificación de rangos sin solución (Golem, BOINC). |

## Pendiente de decidir

1. Workers: **mTLS o firma de desafío**; gateway privado a nivel de red o no.
2. **Alta de workers**: página pública o token entregado por el grupo; límite de altas.
3. Recuperación y revocación de claves.
4. Multifirma: umbral y vencimiento de propuestas.
5. Fee: monto, a quién va, si prioriza tx.
6. Crowdfunding: cierre por hora o por bloque; si se devuelve la fee del aporte.
7. Shares: si se adoptan, y con qué reparto (PROP, PPS o PPLNS).
8. Agregar el punto 5 al Word ampliado.
9. Alta pública: costo creciente, colateral o ninguno de los dos.
10. Cuarentena binaria (como hoy) o reputación.

> Las tablas de las secciones 1 a 4 se armaron con conocimiento previo. La sección "Blockchains más parecidas"
> sí tiene fuentes, consultadas el 14/11/2026. Antes de pasar algo de las tablas al informe, buscarle fuente.
