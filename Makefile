.PHONY: install browser-install dev backend frontend test test-e2e test-live-codex lint typecheck build compile clean

export UV_CACHE_DIR := $(CURDIR)/.uv-cache
export PATH := /opt/homebrew/opt/texlive/bin:/Library/TeX/texbin:$(PATH)

install:
	uv sync --all-extras
	npm --prefix frontend install

browser-install:
	cd frontend && npx playwright install chromium

dev:
	uv run python scripts/dev.py

backend:
	uv run uvicorn cloverleaf.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload

frontend:
	npm --prefix frontend run dev -- --host 127.0.0.1

test:
	uv run pytest

test-e2e:
	npm --prefix frontend run test:e2e

test-live-codex:
	cd frontend && CLOVERLEAF_LIVE_CODEX=1 npx playwright test tests/live-codex.spec.ts

lint:
	npm --prefix frontend run lint

typecheck:
	npm --prefix frontend run typecheck

build:
	npm --prefix frontend run build

compile:
	cd workspace && latexmk -pdf -interaction=nonstopmode -halt-on-error -file-line-error main.tex

clean:
	cd workspace && latexmk -C main.tex
