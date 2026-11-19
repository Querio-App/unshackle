#!/bin/sh
set -eu
# Cloud Run gen2 secret volumes are root-owned; OpenSSH needs a private user-owned key.
if [ -f /var/run/git-key/key ]; then
  umask 077
  mkdir -p /home/unshackle/.ssh
  cp /var/run/git-key/key /home/unshackle/.ssh/id_ed25519
  cp /var/run/git-hosts/known_hosts /home/unshackle/.ssh/known_hosts
  chmod 600 /home/unshackle/.ssh/id_ed25519 /home/unshackle/.ssh/known_hosts
  printf 'Host *\n  IdentitiesOnly yes\n  StrictHostKeyChecking yes\n' > /home/unshackle/.ssh/config
fi
exec /usr/local/bin/unshackle-http "$@"
