#!/usr/bin/env bash
# Run the built image on REAL rootless Podman as the current (non-root) user,
# against the three bind shapes from the support matrix. Any failure, including
# rootless Podman being unavailable, fails the script: nothing here skips.
set -euo pipefail
IMAGE=${1:-ollama-test}
BIND=/var/lib/ollama/.ollama

[ "$(id -u)" != 0 ] || { echo "must run as a non-root user"; exit 1; }
[ "$(podman info --format '{{.Host.Security.Rootless}}')" = true ] || { echo "podman is not rootless here"; exit 1; }
podman unshare true || { echo "podman unshare unusable (subuid/subgid missing?)"; exit 1; }
docker save "$IMAGE" | podman load
IMAGE=docker.io/library/$IMAGE
podman image exists "$IMAGE" || { echo "image did not load into podman"; exit 1; }

work=$(mktemp -d)
trap 'for c in $(podman ps -aq --filter name=rl-); do podman rm -f "$c" >/dev/null; done; podman unshare rm -rf "$work"' EXIT

py='import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:11434/api/version").read())'
check() { # name owner mode expected_uid expected_gid
  local name=$1 owner=$2 mode=$3 want=$4 wantg=$5 dir=$work/$1
  mkdir -p "$dir"
  podman unshare sh -c "mkdir -p '$dir/models/blobs' && echo blob > '$dir/models/blobs/sha256-aa' && chown $owner '$dir' && chmod $mode '$dir'"
  echo "== $name: bind $(podman unshare stat -c '%u:%g %a' "$dir") (as seen in the user namespace), want $want:$wantg"
  podman run -d --name "rl-$name" --cap-add CHOWN -e AW_OLLAMA_ACCELERATOR=cpu -v "$dir:$BIND" "$IMAGE" >/dev/null
  trap_logs() { podman logs "rl-$name" 2>&1 | tail -20; }
  for _ in $(seq 1 30); do
    [ "$(podman inspect -f '{{.State.Status}}' "rl-$name")" = running ] || { trap_logs; echo "FAIL $name: container exited with $(podman inspect -f '{{.State.ExitCode}}' "rl-$name")"; return 1; }
    podman exec "rl-$name" /app/venv/bin/python -c "$py" >/dev/null 2>&1 && break
    sleep 2
  done
  podman exec "rl-$name" /app/venv/bin/python -c "$py" || { trap_logs; echo "FAIL $name: /api/version"; return 1; }
  podman exec "rl-$name" sh -c '
    n=0
    for pat in "^/app/venv/bin/python /app/gateway.py" "^ollama serve"; do
      pids=$(pgrep -f "$pat") || { echo "no process for $pat"; exit 1; }
      for p in $pids; do
        grep -E "^(Uid|CapEff|NoNewPrivs):" /proc/$p/status | tr "\n" " "; echo "(pid $p: $pat)"
        grep -qE "^Uid:\s+'"$want"'\s+'"$want"'\s+'"$want"'\s+'"$want"'" /proc/$p/status || { echo "wrong uid"; exit 1; }
        grep -E "^Gid:" /proc/$p/status
        grep -qE "^Gid:\s+'"$wantg"'\s+'"$wantg"'\s+'"$wantg"'\s+'"$wantg"'" /proc/$p/status || { echo "wrong gid"; exit 1; }
        grep -qE "^CapEff:\s+0+$" /proc/$p/status || { echo "capabilities not cleared"; exit 1; }
        grep -qE "^NoNewPrivs:\s+1$" /proc/$p/status || { echo "no_new_privs not set"; exit 1; }
        n=$((n+1))
      done
    done
    test $n -ge 2' || { echo "FAIL $name: process identity"; return 1; }
  echo "ok $name"
}

check supported 1001:1001 755 1001 1001
got=$(podman unshare stat -c %u:%g "$work/supported/models/blobs/sha256-aa")
[ "$got" = 1001:1001 ] || { echo "FAIL supported: nested file owned by $got, expected 1001:1001"; exit 1; }
echo "ok supported: nested root-owned file re-owned to $got"
check uid1001-gid0 1001:0 700 1001 0
check root 0:0 755 0 0
echo "all rootless Podman cases passed"
