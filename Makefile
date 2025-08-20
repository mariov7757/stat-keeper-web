# --- Config (edit if you like) ---
IMAGE      := stat-keeper-web
PORT       ?= 8000
LOG_LEVEL  ?= DEBUG
AUTOSAVE   ?= 0

# Path to this Makefile's directory (robust even if you run `make` from elsewhere)
PROJECT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
MODEL_DIR   := $(PROJECT_DIR)/models/vosk-model-small-en-us-0.15
DATA_DIR    := $(PROJECT_DIR)/data

.PHONY: run build print paths

build:
	@echo "Using PROJECT_DIR = $(PROJECT_DIR)"
	docker build -t $(IMAGE) .

print paths:
	@echo "PROJECT_DIR = $(PROJECT_DIR)"
	@echo "MODEL_DIR   = $(MODEL_DIR)"
	@echo "DATA_DIR    = $(DATA_DIR)"

run: build
	docker run --rm -p $(PORT):8000 \
		-e LOG_LEVEL=$(LOG_LEVEL) \
		-e AUTOSAVE=$(AUTOSAVE) \
		-v "$(MODEL_DIR)":/models/vosk \
		-v "$(DATA_DIR)":/data \
		$(IMAGE)
