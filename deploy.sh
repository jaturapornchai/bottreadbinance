#!/usr/bin/env bash
# Copy the code from this PC (the source of truth) to the Linux server and restart the containers.
# cache/ and reports/ on the server are its own live state: never overwritten from here. So is ~/bottread/.env: it alone
# holds DCA_PLAN_TOKEN after a rotation (README หัวข้อ Deploy server) — the PC's .env never goes over it.
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
    while IFS='=' read -r k v; do
        case "$k" in
            BOTTREAD_HOST|BOTTREAD_DIR|BOTTREAD_SSH_TARGET|BOTTREAD_SSH_ALIAS)
                v="${v%$'\r'}"
                [[ -z "${!k:-}" ]] && export "$k=$v"
                ;;
        esac
    done < .env
fi
HOST="${BOTTREAD_HOST:-${BOTTREAD_SSH_TARGET:?Set BOTTREAD_HOST in .env}}"
DIR="${BOTTREAD_DIR:-bottread}"
SSH_OPTS=(-o BatchMode=yes)
if [[ -n "${BOTTREAD_SSH_ALIAS:-}" ]]; then
    SSH_OPTS+=(-o "HostKeyAlias=${BOTTREAD_SSH_ALIAS}")
fi
# compose recreates the team container on every deploy, which kills a round mid-meeting: wait for it to end.
# FORCE=1 skips the check (e.g. runner.py died and left team_running stale).
if [[ -z "${FORCE:-}" && ( -z "${*:-}" || " $* " == *" team "* ) ]] \
    && ssh "${SSH_OPTS[@]}" "$HOST" "grep -qE '\"(team_running|closing)\": true' $DIR/cache/runner.json 2>/dev/null"; then
    echo "ทีมกำลังประชุมหรือกำลังปิดไม้ที่กด (cache/runner.json team_running/closing=true) รอให้จบแล้ว deploy ใหม่ (หรือ FORCE=1)" >&2
    exit 1
fi
tar czf - --exclude=./.env --exclude=./.venv --exclude=./tmp --exclude=./research --exclude=./images \
    --exclude=./cache --exclude=./reports --exclude=__pycache__ --exclude=./console/node_modules --exclude=./console/.next \
    --exclude='./console/.env*' . | ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p $DIR && tar xzf - -C $DIR && cd $DIR && mkdir -p cache reports \
    && docker compose up -d --build ${*:-} && docker compose ps --format '{{.Name}} {{.State}}'"
