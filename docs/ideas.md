# Ideas para sumar al TPI

Lista de servicios propuestos el 13/11/2026, después de que el profe pidiera definir para qué sirve la blockchain
y pensar servicios a nivel de sistema distribuido. **Nada de esto está decidido ni hecho** (los puntos que el grupo se comprometió a hacer están en `docs/contrato-1.md`): son propuestas para
que el grupo elija. Lo que pide el enunciado (kernel CUDA, pruebas de carga del Pilar 3, informe) va antes.

Hoy la blockchain es de **pagos con saldos** (modelo de cuentas): "A le paga X a B". Ver `decisiones.docx`.

| # | Idea | Depende de | Costo |
|---|---|---|---|
| 1 | Notificaciones de confirmación | — | Bajo |
| 2 | Pagos entre varios (multifirma) | Firmas digitales | Medio |
| 3 | Crowdfunding (donaciones a una meta) | 2, y bloqueo por tiempo | Medio a alto |

Las tres usan una base común que hoy no existe: un campo **`type`** en la transacción (si no viene, es un pago,
así `bench.py` sigue igual). La 2 y la 3 además necesitan **firmas digitales** (Ed25519), que hoy tampoco hay:
cualquiera puede mandar una transacción en nombre de cualquier cuenta.

---

## 1. Notificaciones de confirmación

**Qué resuelve:** hoy, para saber si una transacción se confirmó, hay que preguntar (`GET /transactions/{id}`).
El frontend pregunta cada 3 s.

**Cómo funcionaría:**
1. Un servicio nuevo, `notifier` (2 réplicas en GKE), se suscribe a los bloques sellados por RabbitMQ: una cola
   propia por réplica atada a `block.solved`, igual que los gateways.
2. Un cliente se conecta por **websocket** (`wss://…/ws`) y dice qué le interesa: una transacción (`tx_id`), una
   cuenta (`mateo`) o todos los bloques.
3. Cuando se sella un bloque, el `notifier` lo lee y le avisa a cada cliente interesado:
   `{"evento": "confirmada", "tx_id": "…", "bloque": 12}`.
4. Variante **webhook**: el cliente registra una URL y el `notifier` le hace un `POST` cuando se confirma.

**Qué cambia:** un servicio nuevo, un manifiesto, el frontend se actualiza al instante en vez de cada 3 s, y una
métrica de clientes conectados y avisos enviados. El minado y la cadena no cambian.

**Cuidado:** con varias réplicas, un cliente está conectado a una sola, así que cada réplica tiene que recibir
todos los bloques (por eso una cola por réplica, como el gateway).

---

## 2. Pagos entre varios (multifirma)

**Qué resuelve:** una cuenta de la que la plata solo se puede mover si firman **varios** de sus dueños. Ejemplo:
la cuenta del centro de estudiantes, con 3 dueños, donde un pago necesita **2 de las 3 firmas**. Es lo que en
Bitcoin se llama *multisig* (2-de-3).

**Cómo funcionaría:**
1. **Crear la cuenta compartida:** `{"type": "crear_multifirma", "cuenta": "centro", "duenios": [clave_A, clave_B,
   clave_C], "minimo": 2}`. Se guarda en `multisig:centro`.
2. **Proponer un pago:** uno de los dueños manda `{"type": "pago", "sender": "centro", "receiver": "imprenta",
   "amount": 500, "firmas": [firma_A]}`. Con una sola firma no alcanza: queda **pendiente de firmas**, sin
   reservar saldo ni entrar a un bloque.
3. **Firmar:** otro dueño manda `{"type": "firmar", "propuesta": <id>, "firma": firma_B}`. Con 2 firmas válidas se
   cumple el mínimo: recién ahí se reserva el saldo (el mismo Lua de hoy) y el pago va a `tx:pending`.
4. Las propuestas vencen solas si no juntan las firmas en, por ejemplo, 24 h.

**Qué cambia:** firmas digitales (cada dueño tiene su clave; el coordinator verifica cada firma), las claves
`multisig:<cuenta>` y `propuesta:<id>`, `verify_chain` comprueba que cada pago desde una cuenta compartida tenga
las firmas mínimas, y el frontend muestra las propuestas y cuántas firmas faltan.

**Por qué es distribuido:** nadie solo controla la plata; hace falta el acuerdo de varios, y la cadena lo hace
cumplir.

---

## 3. Crowdfunding (donaciones a una meta)

**Qué resuelve:** juntar plata entre muchos para algo, con una meta y una fecha. Si se llega a la meta, la plata
va al organizador; si no, **se le devuelve a cada uno lo que puso**. Lo hace la cadena, sin que nadie tenga que
confiar en el organizador. Usa la idea 2 (varios ponen plata en una cuenta que no controla uno solo) más un
**bloqueo por tiempo**.

**Cómo funcionaría:**
1. **Crear la campaña:** `{"type": "crear_campania", "id": "viaje-egresados", "organizador": "mateo",
   "meta": 50000, "hasta_bloque": 5000}`. Se guarda en `campania:<id>`, con una cuenta propia que **nadie puede
   gastar a mano**: solo la cadena la mueve.
2. **Donar:** `{"type": "donar", "sender": "user-7", "campania": "viaje-egresados", "amount": 1000}`. Es un pago
   normal a la cuenta de la campaña (misma reserva de saldo y doble gasto), y se anota quién puso cuánto.
3. **Cierre:** cuando se sella el bloque `hasta_bloque`, el coordinator mira el total:
   - **Si llegó a la meta**: arma una transacción `campania → organizador` por el total.
   - **Si no llegó**: arma una transacción de **devolución** por cada donante, con lo que puso.
   Son transacciones automáticas, como las de premio, que van al principio del bloque siguiente.
4. Variante con la idea 2: que el organizador no reciba la plata directo, sino una cuenta **multifirma**
   (organizador + 2 veedores), y para gastarla hagan falta 2 de 3 firmas.

**Qué cambia:** los tipos `crear_campania` y `donar`, las claves `campania:<id>` y `campania:<id>:aportes`, el
cierre automático al sellar (igual que el premio), `verify_chain` comprueba que al cierre todo vaya al
organizador o vuelva a los donantes (nunca a otro lado), y el frontend lista las campañas con barra de progreso.
Firmas, para que nadie done en nombre de otro.

**Por qué es distribuido:** el que guarda la plata es la cadena, no una persona; las reglas (meta, fecha,
devolución) las aplica el sistema y cualquiera puede auditarlas con `verify_chain`.

---

## Otras ideas que quedaron anotadas

- **Nodos validadores réplica** que reciben cada bloque por un exchange fanout, lo validan por su cuenta y
  guardan su copia, más la sincronización inicial de un nodo nuevo (lo más distribuido).
- **Tokens** propios (como ERC-20), **registro de documentos** y **certificados** (necesita firmas).
- **Shares** estilo Stratum (prueban el trabajo de cada minero) y **árbol de Merkle** con prueba de inclusión.
- Faucet, billetera, indexador, comisiones y estimador de comisiones.
- Red: HTTPS en el gateway (hoy es HTTP y el token viaja sin cifrar), un solo Ingress, NetworkPolicies.
- Observabilidad: latencia de confirmación de tx, métricas HTTP, que las alertas lleguen a algún lado, tracing.
