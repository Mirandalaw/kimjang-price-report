#!/usr/bin/env bash
# cron에서 쓰는 실행 스크립트. 최신 코드를 받고, .env를 읽고, 리포트를 만들어 발송한다.
# 맥에서 push만 해 두면 다음 실행 때 서버에 자동 반영된다.
# 서버에 커밋되지 않은 수정이 있으면 pull을 건너뛰고 경고만 남긴다. NO_PULL=1 이면 pull 안 함.
set -euo pipefail
cd "$(dirname "$0")"

if [ -d .git ] && [ "${NO_PULL:-}" != "1" ]; then
  if git diff --quiet && git diff --cached --quiet; then
    git pull --ff-only --quiet || echo "[run.sh] git pull 실패, 기존 코드로 실행" >&2
  else
    echo "[run.sh] 커밋되지 않은 변경이 있어 git pull 건너뜀" >&2
  fi
fi

set -a; [ -f .env ] && . ./.env; set +a
exec python3 kimjang_report.py report "$@"
