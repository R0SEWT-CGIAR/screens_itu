#!/bin/bash
# Inicia Quiosco en Docker
cd "$(dirname "$0")/.." || exit 1
docker compose up -d --build
