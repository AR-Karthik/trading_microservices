# Trading Microservices - Build & Development Automation
# Simplifies complex Docker and testing commands into single words

.PHONY: build up down ps logs test shell lint clean help

IMAGE_TAG ?= v1.0.0

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

build: ## Build docker images
	docker-compose build

up: ## Start all services in docker (detached)
	docker-compose up -d

down: ## Stop and remove all containers
	docker-compose down

ps: ## List running containers
	docker-compose ps

logs: ## View logs of all services
	docker-compose logs -f

test: ## Run unit tests with pytest
	PYTHONPATH=. pytest tests/

run-local: ## Run microservices locally using run_local.py
	python run_local.py

run-live: ## Run microservices locally in LIVE mode
	python run_local.py --live

lint: ## Run linting checks (flake8/mypy)
	@echo "Running linting..."
	flake8 core daemons utils scripts || true
	mypy core daemons utils scripts --ignore-missing-imports || true

clean: ## Remove temporary files and docker volumes
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache
	docker-compose down -v
