#!/bin/bash
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

APP_DIR=/opt/discord-summary-bot
STATE_DIR=/var/lib/discord-bot-deploy
IMAGE_REPOSITORY=ghcr.io/jwander0820/discordaisummarybot
CONTAINER=discord-summary-bot-bot-1
ROLLBACK_REQUIRED=0
PREVIOUS_IMAGE=

compose() {
    docker compose --project-name discord-summary-bot --profile bot \
        -f "$APP_DIR/compose.yaml" "$@"
}

write_image() {
    local image=$1 temporary
    [[ "$image" =~ ^[a-zA-Z0-9][a-zA-Z0-9./_:@-]*$ ]] || return 1
    temporary=$(mktemp "$STATE_DIR/image.XXXXXX")
    printf 'services:\n  bot:\n    image: "%s"\n    pull_policy: never\n' "$image" > "$temporary"
    mv -f -- "$temporary" "$STATE_DIR/compose.image.yaml"
}

update_bot() {
    compose -f "$STATE_DIR/compose.image.yaml" up -d --no-deps --no-build \
        --wait --wait-timeout 120 bot
}

finish() {
    local result=$?
    trap - EXIT HUP INT TERM
    if [[ "$ROLLBACK_REQUIRED" == 1 ]]; then
        echo 'Deployment failed; attempting to restore the previous image.' >&2
        if write_image "$PREVIOUS_IMAGE" && update_bot; then
            echo 'Previous image restored; deployment still reported as failed.' >&2
        else
            echo 'Rollback failed. Inspect the bot manually on the VPS.' >&2
        fi
        result=1
    fi
    exit "$result"
}

main() {
    local revision image
    if [[ $# != 1 ]]; then
        echo 'Expected one command: check or deploy <40-character commit SHA>.' >&2
        return 2
    fi
    if [[ "$1" != check && ! "$1" =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
        echo 'Command rejected.' >&2
        return 2
    fi
    [[ $(id -u) == 0 ]] || { echo 'Run through the configured sudo entry.' >&2; return 1; }
    [[ -f "$APP_DIR/compose.yaml" ]] || { echo 'Compose file missing.' >&2; return 1; }
    cd "$APP_DIR"
    compose config --quiet
    [[ $(docker inspect --format '{{.State.Running}}' "$CONTAINER") == true ]] || {
        echo 'Existing bot is not running; inspect manually before deployment.' >&2
        return 1
    }
    if [[ "$1" == check ]]; then
        echo 'Deployment entry ready. Compose is valid and the existing bot is running. No deployment performed.'
        return 0
    fi
    revision=${1#deploy }
    image="$IMAGE_REPOSITORY:sha-$revision"
    install -d -o root -g root -m 700 "$STATE_DIR"
    exec 9>"$STATE_DIR/deploy.lock"
    flock -n 9 || { echo 'Another deployment is active.' >&2; return 1; }
    PREVIOUS_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")
    [[ "$PREVIOUS_IMAGE" =~ ^[a-zA-Z0-9][a-zA-Z0-9./_:@-]*$ ]] || return 1

    # Pull first: registry failure must leave the existing container untouched.
    docker pull "$image"
    trap finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' HUP TERM
    ROLLBACK_REQUIRED=1
    write_image "$image"
    compose -f "$STATE_DIR/compose.image.yaml" config --quiet
    update_bot
    ROLLBACK_REQUIRED=0
    printf '%s\n' "$PREVIOUS_IMAGE" > "$STATE_DIR/previous-image"
    printf 'Deployed %s; container health check passed.\n' "$image"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
