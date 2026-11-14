# Contrato 1 — TP Integrador · Grupo SDMER

Contrato inicial del grupo, 13/11/2026. Son las funcionalidades que el grupo se compromete a tener en la
blockchain, más allá del TP base. Para los puntos 1 a 4 se agregó una **investigación inicial**: de qué
protocolo o estándar de una blockchain real tomaríamos cada uno. Es una propuesta: el protocolo final de cada
punto lo confirma el grupo.

| # | Punto (tal como lo definió el grupo) | Estado |
|---|---|---|
| 1 | Servicio de registro de workers y personas (clave pública y privada) | Por hacer |
| 2 | Cuenta compartida / fee, crowdfunding (transferencia con aprobación extra) | Por hacer |
| 3 | Notificar por websocket, para no gastar ciclos de gusto | Por hacer |
| 4 | El gateway comprueba que el worker funciona y "dice la verdad" + clasificación de nodo por potencia y recompensa + websocket | **En parte** (canarios, calibración, clases y premio ya están; falta el websocket) |
| 5 | KEDA: ajuste de escalado dinámico, colas con quorum, recreación de un bloque a partir del cambio de dificultad | **Hecho** |

Dependencias: el **1** (claves y firmas) es la base del **2** (no se puede aprobar un pago sin firmas) y mejora
el **4** (cada worker se identifica con su clave en vez del token compartido). El **3** y la parte de websocket
del **4** comparten el mismo servicio de conexiones abiertas.

---

## 1. Registro de workers y personas (clave pública y privada)

**Qué significa:** hoy cualquiera manda una transacción en nombre de cualquier cuenta, y todos los workers
usan el mismo `MINER_TOKEN`. Con este punto, cada persona y cada worker tiene un **par de claves**: la privada la
guarda él y con ella **firma**; la pública queda registrada y con ella el sistema **verifica** que fue él.

**Protocolos de referencia:**

| Tema | Bitcoin | Ethereum | Otras |
|---|---|---|---|
| Algoritmo de firma | ECDSA sobre secp256k1 (nonce determinístico, RFC 6979) | ECDSA sobre secp256k1 | **Ed25519 (RFC 8032)**: Solana, Stellar, Cardano, Tezos |
| Dirección de la cuenta | Hash de la clave pública, en Base58Check o Bech32 (BIP-173) | Últimos 20 bytes de keccak256(clave pública), con checksum EIP-55 | Stellar y Solana: la clave pública misma, en texto |
| Evitar que se reenvíe una tx firmada | Cada UTXO se gasta una vez | **Nonce por cuenta** + id de la cadena (EIP-155) | Stellar: número de secuencia por cuenta |
| Firmar datos estructurados | — | EIP-712 | — |
| Iniciar sesión con la clave | — | **"Sign-In with Ethereum"** (EIP-4361): el servidor manda un texto al azar y el cliente lo firma | — |
| Claves recuperables con palabras | BIP-39 (frase de 12 o 24 palabras), BIP-32 (claves derivadas) | Igual | Igual |
| Identidad de personas | — | — | DID del W3C (identificadores descentralizados) |

**Propuesta:**
- **Ed25519 (RFC 8032)** para las firmas: firmas chicas y rápidas, sin las trampas de ECDSA. En Python está en la
  librería `cryptography`.
- **Dirección = la clave pública** en texto, como Stellar y Solana. Simple de mostrar y de verificar.
- **Nonce por cuenta + id de la cadena**, como Ethereum (EIP-155): cada cuenta numera sus transacciones 0, 1, 2…
  y una firma de otra red o repetida no sirve.
- **Workers:** se registran con su clave pública. Para entrar al gateway firman un texto al azar que les manda el
  gateway (como EIP-4361), en vez de presentar el token compartido. Cada resultado va firmado.
- **Registro:** una transacción `registrar` que asocia un nombre (`mateo`) a una clave pública, como un alias.

**Falta: cómo se da de alta un worker.** Hoy todos usan el mismo `MINER_TOKEN` y no hay forma de sumarse.
Dos opciones:
- **Página pública de alta:** cualquiera entra, pide unirse y recibe su credencial propia. **Es la que pensó el
  profe.** Como cualquier desconocido podría minar, los canarios, la cuarentena y poder revocar un worker solo
  pasan a ser imprescindibles.
- **Token generado por el servidor y entregado por el grupo:** más simple y cerrado, pero no es abierto.

Pendiente de confirmar por el grupo (la página es un componente nuevo).

---

## 2. Cuenta compartida / fee, crowdfunding (transferencia con aprobación extra)

**Qué significa:** tres servicios que tienen en común que la plata **no se mueve con una sola decisión**:
- **Cuenta compartida:** la plata sale solo si aprueban varios dueños (multifirma, por ejemplo 2 de 3).
- **Fee:** cada transacción paga una comisión al minero.
- **Crowdfunding:** muchos donan a una meta con fecha; si se llega, la plata va al organizador, y si no, vuelve
  a cada donante.

**Protocolos de referencia:**

| Servicio | Bitcoin | Ethereum | Stellar |
|---|---|---|---|
| Cuenta compartida | Multifirma m-de-n (BIP-11), dentro de P2SH (BIP-16) | **Safe** (ex Gnosis Safe): alguien propone, los dueños confirman, se ejecuta con el mínimo | **Multifirma nativa**: cada firmante tiene un peso y cada operación un umbral |
| Fee | Comisión por tamaño (satoshis por byte). El minero elige primero las que pagan más | **EIP-1559**: una comisión base que se quema y sube o baja según lo lleno que esté el bloque, más una propina para el validador | Comisión fija mínima por operación |
| Crowdfunding | **Contrato de garantía** (*assurance contract*, Lighthouse): cada donante firma solo su parte (`SIGHASH_ANYONECANPAY`) y la tx vale recién cuando juntan la meta | Contrato de crowdfunding "todo o nada" con devolución (patrón Kickstarter) | **Saldos reclamables** (CAP-23): plata que alguien puede cobrar si se cumple una condición, por ejemplo de tiempo |
| Bloqueo por tiempo | `CHECKLOCKTIMEVERIFY` (BIP-65) y `nLockTime` | Condición de tiempo dentro del contrato | Predicados de tiempo |

**Propuesta:**
- **Cuenta compartida al estilo Stellar** (pesos + umbral), que es del modelo de cuentas como el nuestro, con el
  flujo de **Safe**: *propuesta → confirmaciones → se ejecuta al llegar al mínimo*. Recién al juntar las firmas
  se reserva el saldo (el mismo Lua de hoy) y la tx entra a un bloque.
- **Fee al estilo Bitcoin, simplificado:** un campo `fee` en TPI que cobra el minero, y el armado del bloque
  prioriza las que pagan más dentro de cada ventana. EIP-1559 queda como mejora.
- **Crowdfunding al estilo de los saldos reclamables de Stellar + "todo o nada"**: la campaña tiene una cuenta que
  nadie puede gastar a mano; al llegar al bloque límite, la cadena arma sola la transferencia al organizador o
  las devoluciones, como hoy arma las transacciones de premio. Opcional: que el organizador reciba en una cuenta
  compartida con veedores.

Detalle de cada flujo en `docs/ideas.md` (ideas 2 y 3).

---

## 3. Notificar por websocket, para no gastar ciclos de gusto

**Qué significa:** hoy, para saber si una tx se confirmó o si hay un bloque nuevo, hay que **preguntar** cada
tanto (el frontend pregunta cada 3 s y los mineros piden tareas cada 1 s aunque no haya). Con websocket, la
conexión queda abierta y el servidor **avisa** cuando pasa algo.

**Protocolos de referencia:**

| Blockchain | Cómo notifica |
|---|---|
| **Ethereum** | **`eth_subscribe` por WebSocket** (JSON-RPC 2.0): suscripciones a `newHeads` (bloque nuevo), `logs` (eventos) y `newPendingTransactions` |
| Bitcoin Core | **ZMQ**: publica cada bloque (`hashblock`) y cada tx (`rawtx`) a quien esté escuchando |
| Billeteras de Bitcoin | **Protocolo Electrum**: `blockchain.headers.subscribe` y `blockchain.scripthash.subscribe` (avisa cuando una dirección recibe o gasta) |
| Servicios comerciales | **Webhooks**: el servicio hace un `POST` a tu URL cuando pasa algo |

**Propuesta:** un servicio `notifier` (2 réplicas) con una API **al estilo `eth_subscribe`**: JSON-RPC 2.0 por
WebSocket, con suscripciones a `bloques` (cada bloque sellado), `tx` (un `tx_id`, avisa al confirmarse) y
`cuenta` (cada vez que una cuenta recibe o paga, como Electrum). Escucha `block.solved` en RabbitMQ, con una cola
por réplica. Webhooks como variante. El frontend deja de preguntar cada 3 s.

---

## 4. El gateway comprueba que el worker funciona y "dice la verdad" + clasificación por potencia y recompensa + websocket

**Qué significa:** que el gateway verifique a cada worker (que mina de verdad y no miente), lo clasifique según
cuánto rinde, le pague según su trabajo, y se comunique con él por una conexión abierta en vez de que el worker
pregunte cada segundo.

**Qué tenemos hoy:** canarios (tareas con solución conocida), cuarentena, calibración al arrancar, clase rápido /
medio / lento, hashrate real por resultados entregados, premio proporcional a los nonces completados. **Falta el
websocket**, y la verificación hoy es por muestreo (1 de cada 20 tareas).

**Protocolo de referencia: Stratum**, el protocolo que usan todos los pools de minería reales. Tiene cada pieza de
este punto:

| Stratum V1 | Qué hace | Equivalente nuestro |
|---|---|---|
| `mining.subscribe` / `mining.authorize` | El worker se conecta y se identifica | Keep-alive con `miner_id` + token |
| **`mining.notify`** | El **pool le empuja** el trabajo nuevo por la conexión abierta (no hay que preguntar) | Hoy: `GET /tasks/next` cada 1 s |
| **`mining.submit` de "shares"** | El worker manda hashes que cumplen una dificultad **más fácil** que la del bloque. Cada share **prueba** que trabajó: el pool la verifica con un hash | Hoy: canarios por muestreo |
| **`mining.set_difficulty` (vardiff)** | El pool le ajusta a **cada worker** su dificultad de shares según su potencia, para que mande una cada pocos segundos | Calibración + clase |
| Pago por shares: **PPS, PPLNS, FPPS** | Se reparte según las shares de cada worker | Premio proporcional a los nonces completados |

**Stratum V2** (la versión nueva) suma cifrado de la conexión (protocolo Noise) y mensajes binarios más chicos.

**Propuesta:**
- **El mismo flujo de Stratum V1, pero sobre WebSocket con JSON-RPC 2.0**, porque el gateway ya es HTTP:
  `authorize` firmado con la clave del worker (punto 1), `notify` empujado por el gateway, `submit` de resultados
  **y de shares**.
- **Shares para "dice la verdad"**: verificación continua de cada worker, no solo 1 de cada 20. Los canarios
  quedan como control extra.
- **Vardiff para la clasificación**: la dificultad de shares de cada worker sale de su hashrate, y su clase
  queda a la vista.
- **PPLNS para la recompensa**: el premio se reparte según las shares de los últimos N bloques, lo que premia a
  los que minan seguido.

---

## 5. KEDA: ajuste de escalado dinámico, colas con quorum, recreación de un bloque a partir del cambio de dificultad

**Hecho**, el 13/11:
- **KEDA**: mineros CPU todo o nada (8 sin GPU y con trabajo, 0 a los 60 s, 0 con GPU), consultando cada 5 s.
- **Colas quorum**: `mining.tasks`, `mining.results` y `mining.dlq` (Raft). Con un solo nodo de RabbitMQ dan
  durabilidad y límite de entregas, no alta disponibilidad (3 nodos quedó a futuro).
- **Recreación del bloque**: si cae el hashrate, el coordinator rearma el bloque en curso con un prefijo más
  corto (mismas tx, `block_id` nuevo).

Detalle en `docs/decisiones.docx` y `pilar2/README.md`.
