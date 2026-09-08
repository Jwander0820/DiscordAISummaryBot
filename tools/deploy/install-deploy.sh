#!/bin/bash
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

[[ $(id -u) == 0 ]] || { echo 'Run this installer with sudo.' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
public_key=/home/ubuntu/.ssh/github_discord_deploy.pub
authorized_keys=/home/ubuntu/.ssh/authorized_keys
[[ -f "$public_key" ]] || { echo 'Deployment public key missing.' >&2; exit 1; }
ssh-keygen -lf "$public_key" >/dev/null
read -r key_type key_body _ < "$public_key"
[[ "$key_type" == ssh-ed25519 ]] || { echo 'Expected an Ed25519 public key.' >&2; exit 1; }
forced='restrict,command="/usr/bin/sudo -n /usr/local/sbin/discord-bot-deploy \"$SSH_ORIGINAL_COMMAND\""'
entry="$forced $key_type $key_body github-actions-discord-deploy"
if [[ -f "$authorized_keys" ]] && grep -Fq -- "$key_body" "$authorized_keys"; then
    grep -Fxq -- "$entry" "$authorized_keys" || {
        echo 'This key already has a different authorized_keys entry. Inspect it before proceeding.' >&2
        exit 1
    }
fi

bash -n "$source_dir/discord-bot-deploy.sh"
install -o root -g root -m 755 "$source_dir/discord-bot-deploy.sh" /usr/local/sbin/discord-bot-deploy
temporary=$(mktemp)
trap 'rm -f -- "$temporary"' EXIT
printf '%s\n' 'ubuntu ALL=(root) NOPASSWD: /usr/local/sbin/discord-bot-deploy *' > "$temporary"
visudo -cf "$temporary"
install -o root -g root -m 440 "$temporary" /etc/sudoers.d/discord-bot-deploy

install -d -o ubuntu -g ubuntu -m 700 /home/ubuntu/.ssh
touch "$authorized_keys"
if ! grep -Fxq -- "$entry" "$authorized_keys"; then
    printf '\n%s\n' "$entry" >> "$authorized_keys"
fi
chown ubuntu:ubuntu "$authorized_keys"
chmod 600 "$authorized_keys"

sudo -u ubuntu /usr/bin/sudo -n /usr/local/sbin/discord-bot-deploy check
echo 'Installed restricted deployment entry. No containers were updated.'
