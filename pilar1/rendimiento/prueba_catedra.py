# Script que dio la cátedra en clase (tal cual, solo se agregó este comentario).
# Busca secuencialmente un nonce tal que sha256(f"{data}-{nonce}") empiece con `param` ceros.
# Ojo: usa SHA-256 (nuestro PoW es MD5) y mide el tiempo hasta UNA solución, que depende de la
# suerte con ese `data`. Para elegir valores usamos hashrate.py, que mide hashes por segundo.
#     python pilar1/rendimiento/prueba_catedra.py 5
import hashlib
import sys
import time

data = "index=1;previous=000abc;tx=ana->beto:1"
data = "index=2;previous=000DDD;tx=mateo->david:8"
param = 1

try:
    param = int(sys.argv[1])
except:
    print(f"use default parameter: {param}")
print("hello", param)

target = "0" * param

nonce = 0


while True:
    calculated_hash = hashlib.sha256(f"{data}-{nonce}".encode()).hexdigest()
    # if calculated_hash.startswith(target):
    #     print(f"Nonce combination found , on .... {nonce} result is {calculated_hash}")
    # print(f"Data {data} / nonce {nonce} : hash {calculated_hash}")
    if calculated_hash.startswith(target):
        break

    nonce += 1

print(
    f"Nonce {nonce} found to calculate hash starting with Target: {target} // hash : {calculated_hash}"
)
