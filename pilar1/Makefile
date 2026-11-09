# Build the CUDA miner. Override ARCH for your GPU: make ARCH=sm_75 (Colab T4)
ARCH ?= native
NVCC ?= nvcc

build/miner_gpu: gpu/miner_gpu.cu
	mkdir -p build
	$(NVCC) -O3 -arch=$(ARCH) -o $@ $<

check: build/miner_gpu
	python3 tools/check_contract.py -- ./build/miner_gpu

check-oracle:
	python3 tools/check_contract.py -- python3 oracle/miner_cpu.py

test:
	python3 -m pytest -q tests/

clean:
	rm -rf build

.PHONY: check check-oracle test clean
