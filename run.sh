#!/usr/bin/env bash
# cron에서 쓰는 실행 스크립트. .env를 읽고 리포트를 만들어 발송한다.
set -euo pipefail
cd "$(dirname "$0")"
set -a; [ -f .env ] && . ./.env; set +a
exec python3 kimjang_report.py report "$@"
