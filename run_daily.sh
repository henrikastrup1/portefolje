#!/usr/bin/env bash
# Daglig kørsel (cron kl. 23:00) — 100% tokenfri, ingen AI.
set -e
cd "$(dirname "$0")"
python3 update_portfolio.py
git add portfolio.json
git commit -m "Daglig opdatering $(date +%F)" || echo "Ingen ændringer"
git push
