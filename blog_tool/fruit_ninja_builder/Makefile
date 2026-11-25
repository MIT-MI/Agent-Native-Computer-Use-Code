# Windows Fruit Ninja HD: GUI player versus an agent-built MCP server (RE and no-RE variants).
# Run on a Docker host. Containers carry the label below; cleanup touches only them.
SHELL := /bin/bash
NODE_IMAGE ?= node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
RUNTIME := fruit-ninja-builder/runtime:v1
BUILDER := fruit-ninja-builder/builder:v1
NO_RE := fruit-ninja-builder/no-re:v1
LABEL_KV := fnb.project=fruit-ninja-builder
LABEL := --label $(LABEL_KV)
DEV = docker run --rm $(LABEL) $(1) --user $(shell id -u):$(shell id -g) -e HOME=/tmp -e npm_config_cache=/tmp/npm \
	-v $(CURDIR):$(CURDIR) -w $(CURDIR) --entrypoint bash $(RUNTIME) -c
RUNS ?= $(CURDIR)/.local/runs
EXPERIMENT_OUT ?= $(RUNS)/experiment-$(shell date -u +%Y%m%dT%H%M%SZ)
ROUNDS ?= 3

.PHONY: all images setup check test smoke lockcheck experiment analyze clean-containers
all: images setup check test smoke

images:
	docker build $(LABEL) --build-arg NODE_IMAGE=$(NODE_IMAGE) -t $(RUNTIME) -f sandbox/Dockerfile.windows sandbox
	docker build $(LABEL) --build-arg RUNTIME_IMAGE=$(RUNTIME) -t $(BUILDER) -f sandbox/Dockerfile.windows-builder sandbox
	docker build $(LABEL) --build-arg RUNTIME_IMAGE=$(RUNTIME) -t $(NO_RE) -f sandbox/Dockerfile.windows-no-re sandbox

# Install dependencies, download and verify the pinned game, build DXVK, then freeze the game and a
# fresh Wine prefix as .local/windows/template, which every desktop copies.
setup:
	$(call DEV,) 'set -e; npm ci --no-audit --no-fund; python3 native/setup.py'
	$(call DEV,--network none) 'set -e; L=.local/windows; rm -rf $$L/wine $$L/template; mkdir -p $$L/wine $$L/template; \
	  export WINEPREFIX=$$PWD/$$L/wine WINEARCH=win32 WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" DISPLAY=:99; \
	  Xvfb :99 -screen 0 1024x768x24 -nolisten tcp > /dev/null 2>&1 & sleep 1; wineboot -u; wineserver -w; \
	  cp -a $$L/wine $$L/game $$L/template/'

check:
	$(call DEV,) 'npm run typecheck && npm run format:check'

test:
	$(call DEV,--network none) 'npm test'

# Model-free end to end: scripted Builders (with and without RE), two MCP rounds and one GUI round.
smoke:
	rm -rf $(RUNS)/smoke-build $(RUNS)/smoke-build-no-re $(RUNS)/smoke-mcp $(RUNS)/smoke-mcp-no-re $(RUNS)/smoke-gui
	node --import tsx src/cli.ts build --mock --smoke --out $(RUNS)/smoke-build
	node --import tsx src/cli.ts build --mock --smoke --variant no-re --out $(RUNS)/smoke-build-no-re
	node --import tsx src/cli.ts solve --mock --smoke --mode mcp --mcp $(RUNS)/smoke-build/delivery.json --out $(RUNS)/smoke-mcp
	node --import tsx src/cli.ts solve --mock --smoke --mode mcp --mcp $(RUNS)/smoke-build-no-re/delivery.json --out $(RUNS)/smoke-mcp-no-re
	node --import tsx src/cli.ts solve --mock --smoke --mode gui --out $(RUNS)/smoke-gui

lockcheck:
	bash scripts/lockcheck.sh

# Full experiment: RE and no-RE builds in parallel, then 3 groups x ROUNDS in rotating order.
# Supply model API keys in the environment before running.
experiment:
	bash scripts/experiment.sh --out $(EXPERIMENT_OUT) --rounds $(ROUNDS)

analyze:
	python3 scripts/analyze.py $(EXPERIMENT_OUT)

clean-containers:
	docker ps -aq --filter label=$(LABEL_KV) | xargs -r docker rm -f
