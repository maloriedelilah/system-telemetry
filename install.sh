#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install.sh — interactive installer. Runs identically on Linux and on Windows
# via Git Bash / msys. Run it after update.sh on a fresh checkout (README, First install):
#
#     cd /opt/telemetry        (or  cd /c/telemetry  in Git Bash)
#     ./install.sh             # first run: prompts and writes .env
#     ./install.sh --reconfigure   # re-prompt and overwrite .env
#
# Fleet mode (Linux, no questions; pane's telemetry-install.sh uses it, OPS-47):
#
#     sudo ./install.sh --profile host|gpu|vm --pane-url http://<pane>:8427 \
#         [--models llamaserver=URL[,URL] | --models ollama=URL] [--run-user telemetry] [--token-stdin] [--sites]
#
#   host  CPU/RAM/disk (psutil + hwmon)       gpu  host + NVIDIA       vm  host, for VMs (no sensors)
#   MQTT is off in every profile (fleet boxes report to Pane only). The profile always
#   rewrites .env, so rerunning the same command puts a box that drifted back on the standard.
#   --run-user   the account the service runs as (created as a system user if missing);
#                default: the account that ran sudo.
#   --token-stdin  read this box's Pane token (from pane's add-box.sh) on stdin and save it
#                  to secrets/pane_token, 0400, owned by the run user.
#   --sites        also probe the public sites in sites.txt (TELEMETRY_SITES=1; pane only, OPS-59).
#   The install dir must be root's in fleet mode, as update.sh requires: secrets/ is safe only
#   inside a directory the run user can't rearrange.
#
# First run with no .env: prompts for MQTT, roles, and model source, writes
# .env, builds the venv, and registers the service. Re-running with an existing
# .env keeps your config and just (re)builds the venv + service.
#
# Linux  -> venv + systemd service & timer (single-shot every 30s).
# Windows-> venv + NSSM service (long-running loop), started as LocalSystem.
#           The venv makes a run-as-user account unnecessary.
# ---------------------------------------------------------------------------
set -euo pipefail
# Every pattern here ([!-~], [0-9], [a-z]) means ASCII. Under a locale such as en_US.UTF-8, bash's
# =~ takes ranges from the collation order instead, and [!-~] then refused every real Pane token
# (Nov 13, the first box after pane). Assigning LC_ALL makes bash switch locale right here.
export LC_ALL=C

SERVICE_NAME="system-telemetry"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TTY_R="${TELEMETRY_TTY_R:-/dev/tty}"      # terminal for reading answers (overridable for tests)
TTY_W="${TELEMETRY_TTY_W:-/dev/tty}"      # terminal for writing prompts
RECONFIGURE=0
PROFILE="" PANE_URL_ARG="" MODELS_ARG="" RUN_USER_ARG="" TOKEN_STDIN=0 SITES=0
ORIG_ARGS=("$@")

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }
# The house error format, for fleet mode: what was tried, expected, got, what to do next.
report() {
    printf '\n\033[1;31m!!! %s\033[0m\n    Did:      %s\n    Expected: %s\n    Got:      %s\n    Next:     %s\n' \
        "$1" "$2" "$3" "$4" "$5" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --reconfigure) RECONFIGURE=1; shift ;;
        --profile)     PROFILE="${2:-}"; shift 2 || shift ;;
        --pane-url)    PANE_URL_ARG="${2:-}"; shift 2 || shift ;;
        --models)      MODELS_ARG="${2:-}"; shift 2 || shift ;;
        --run-user)    RUN_USER_ARG="${2:-}"; shift 2 || shift ;;
        --token-stdin) TOKEN_STDIN=1; shift ;;
        --sites)       SITES=1; shift ;;
        *) report "unknown argument" "read the command line" \
               "--reconfigure, or --profile host|gpu|vm --pane-url URL [--models ...] [--run-user NAME] [--token-stdin] [--sites]" \
               "'$1'" "check the command (the header of install.sh lists the options). Nothing was changed."
           exit 2 ;;
    esac
done

case "$(uname -s)" in
    Linux*)               OS=linux ;;
    MINGW*|MSYS*|CYGWIN*) OS=windows ;;
    *) die "Unsupported OS: $(uname -s)" ;;
esac

# ---------------------------------------------------------------------------
# Linux: /opt + systemd need root. Re-exec under sudo, keeping the tty so
# prompts still work.
# ---------------------------------------------------------------------------
if [[ "$OS" == linux && "$INSTALL_DIR" == /opt/* && "$(id -u)" -ne 0 ]]; then
    say "Re-executing under sudo for /opt + systemd…"
    if [[ -n "$PROFILE" ]]; then exec sudo -E bash "$INSTALL_DIR/install.sh" "${ORIG_ARGS[@]}"; fi
    exec sudo -E bash "$INSTALL_DIR/install.sh" "${ORIG_ARGS[@]}" </dev/tty
fi

# ---------------------------------------------------------------------------
# Windows: NSSM self-elevates (UAC) for `nssm install`, but `nssm start`
# silently fails from a non-elevated Git Bash, leaving the service
# registered-but-stopped. Hard-require an elevated shell and fail fast. The
# LocalService hive (S-1-5-19) is readable only when elevated — a check that
# does not depend on the Server service the way `net session` does.
# ---------------------------------------------------------------------------
if [[ "$OS" == windows ]] && ! reg query 'HKU\S-1-5-19' >/dev/null 2>&1; then
    die "Administrator privileges required. Re-run from an ELEVATED Git Bash (right-click Git Bash, then 'Run as administrator'). NSSM needs admin to register AND start the service."
fi

say "Detected OS: $OS"
say "Install dir: $INSTALL_DIR"

# ---------------------------------------------------------------------------
# Interactive prompt helpers (read the real terminal, not piped stdin).
# ---------------------------------------------------------------------------
# Prompts are written to FD 4, answers read from FD 3 — both bound to the
# terminal once (see write_env). Opening once and reading sequentially is the
# correct behavior for /dev/tty and keeps the flow testable.
ask() {  # ask <prompt> [default] -> echoes answer (or default)
    local prompt="$1" default="${2:-}" ans
    if [[ -n "$default" ]]; then printf '%s [%s]: ' "$prompt" "$default" >&4
    else printf '%s: ' "$prompt" >&4; fi
    read -r ans <&3 || ans=""
    echo "${ans:-$default}"
}
ask_secret() {  # ask_secret <prompt> -> echoes answer, no echo to terminal
    local prompt="$1" ans
    printf '%s: ' "$prompt" >&4
    read -rs ans <&3 || ans=""
    printf '\n' >&4
    echo "$ans"
}
ask_yn() {  # ask_yn <prompt> [y|n] -> returns 0 for yes
    local prompt="$1" default="${2:-y}" ans hint="[Y/n]"
    [[ "$default" == n ]] && hint="[y/N]"
    printf '%s %s: ' "$prompt" "$hint" >&4
    read -r ans <&3 || ans=""
    ans="${ans:-$default}"
    [[ "$ans" =~ ^[Yy] ]]
}

write_env() {
    exec 3<"$TTY_R" 4>"$TTY_W"     # bind terminal once for the whole prompt flow
    say "Let's configure this box. Press Enter to accept [defaults]."
    printf '\n' >&4

    local MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS HOSTNAME_FRIENDLY PANE_URL
    MQTT_HOST="$(ask 'MQTT broker host ("none" = no MQTT, Pane only)' '192.168.68.76')"
    if [[ "$MQTT_HOST" == none ]]; then
        # MQTT_HOST= (empty) turns the MQTT output off; no broker questions.
        MQTT_HOST="" MQTT_PORT=1883 MQTT_USER="" MQTT_PASS=""
    else
        MQTT_PORT="$(ask 'MQTT broker port' '1883')"
        MQTT_USER="$(ask 'MQTT username' 'gpu-telemetry')"
        while :; do
            MQTT_PASS="$(ask_secret 'MQTT password')"
            [[ -n "$MQTT_PASS" ]] && break
            printf '  password cannot be empty\n' >&4
        done
    fi
    PANE_URL="$(ask 'Pane URL, e.g. http://100.92.61.42:8427 (blank = no Pane; the token goes in a file, see README)' '')"
    HOSTNAME_FRIENDLY="$(ask 'Friendly host name (blank = OS hostname)' '')"

    # Roles
    local NVIDIA=0 LHM=0 LHM_GPUS=0 LHM_URL="http://localhost:8085/data.json" LHM_INC="" HOST_STATS=0
    if ask_yn 'Collect NVIDIA GPU metrics (nvidia-smi)?' y; then NVIDIA=1; fi
    if [[ "$OS" == windows ]]; then
        if ask_yn 'Collect host stats via LibreHardwareMonitor?' y; then
            LHM=1
            LHM_URL="$(ask 'LibreHardwareMonitor JSON URL' "$LHM_URL")"
            if ask_yn 'Include non-NVIDIA GPUs from LHM (e.g. Arc)?' n; then
                LHM_GPUS=1
                LHM_INC="$(ask 'GPU-name substring to include (blank = all)' 'Arc')"
            fi
        fi
    else
        # LibreHardwareMonitor is Windows-only; Linux host stats come from psutil.
        if ask_yn 'Collect host CPU/RAM/disk stats (psutil + lm-sensors)?' y; then
            HOST_STATS=1
        fi
    fi

    # Model source
    local MODEL_SOURCE MODEL_URL MODEL_PATH="/api/v0/models" MODEL_FILTER=""
    while :; do
        MODEL_SOURCE="$(ask 'Loaded-model labels source — ollama / lmstudio / docker / llamaserver / none' 'none')"
        case "$MODEL_SOURCE" in ollama|lmstudio|docker|llamaserver|none) break ;; esac
        printf '  enter one of: ollama, lmstudio, docker, llamaserver, none\n' >&4
    done
    case "$MODEL_SOURCE" in
        ollama)
            MODEL_URL="$(ask 'Ollama API URL' 'http://127.0.0.1:11434')"
            MODEL_PATH="/api/ps" ;;
        lmstudio)
            MODEL_URL="$(ask 'LM Studio API URL' 'http://127.0.0.1:1234')"
            MODEL_PATH="/api/v0/models"
            MODEL_FILTER="$(ask 'Model-name filter, keeps only local models (blank = all)' '')" ;;
        docker)
            # Reads `docker ps`/`docker inspect`: the service user must be in the docker group.
            MODEL_URL="http://127.0.0.1:11434"
            MODEL_FILTER="$(ask 'Model-name filter, keeps only matching models (blank = all)' '')" ;;
        llamaserver)
            # llama.cpp llama-server; one base URL per instance, comma-separated.
            MODEL_URL="$(ask 'llama-server base URL(s), comma-separated' 'http://127.0.0.1:1234,http://127.0.0.1:1235')"
            MODEL_FILTER="$(ask 'Model-name filter, keeps only matching models (blank = all)' '')" ;;
        none) MODEL_URL="http://127.0.0.1:11434" ;;
    esac

    # Loop cadence: Windows service loops; Linux timer handles cadence.
    local LOOP=""
    [[ "$OS" == windows ]] && LOOP=30

    exec 3<&- 4>&-                  # release the terminal
    umask 077   # .env holds the MQTT password (the Pane token is never in .env)
    # A fresh temp file, then mv: mv replaces whatever .env is (even a symlink) instead of
    # writing through it.
    local tmp
    tmp="$(mktemp "$INSTALL_DIR/.env.XXXXXX")"
    cat > "$tmp" <<ENV
# system-telemetry config — generated by install.sh. Never commit this file.
MQTT_HOST=$MQTT_HOST
MQTT_PORT=$MQTT_PORT
MQTT_USER=$MQTT_USER
MQTT_PASS=$MQTT_PASS
MQTT_DISCOVERY_PREFIX=homeassistant

# Pane (empty = off). The token is NOT here: it lives in PANE_TOKEN_FILE
# (default: secrets/pane_token under the install dir on Linux; see README).
PANE_URL=$PANE_URL

TELEMETRY_HOST_NAME=$HOSTNAME_FRIENDLY

TELEMETRY_NVIDIA=$NVIDIA
TELEMETRY_LHM=$LHM
LHM_GPUS=$LHM_GPUS
LHM_URL=$LHM_URL
LHM_GPU_INCLUDE=$LHM_INC
TELEMETRY_HOST=$HOST_STATS

MODEL_SOURCE=$MODEL_SOURCE
MODEL_API_URL=$MODEL_URL
MODEL_API_PATH=$MODEL_PATH
MODEL_FILTER=$MODEL_FILTER

LOOP_INTERVAL=$LOOP
DEBUG=0
ENV
    mv -f -T "$tmp" "$INSTALL_DIR/.env"
    say "Wrote $INSTALL_DIR/.env"
}

# ---------------------------------------------------------------------------
# Fleet mode: validate the arguments before anything changes.
# ---------------------------------------------------------------------------
MODEL_SOURCE_F=none MODEL_URL_F="http://127.0.0.1:11434" MODEL_PATH_F="/api/v0/models"
if [[ -n "$PROFILE" ]]; then
    if [[ "$OS" != linux ]]; then
        report "fleet mode is Linux-only" "read --profile" "a Linux box" "$OS" \
            "on Windows, run ./install.sh with no options and answer the questions. Nothing was changed."
        exit 2
    fi
    case "$PROFILE" in host|gpu|vm) ;; *)
        report "unknown profile" "read --profile" "host, gpu or vm" "'$PROFILE'" "pick one of the three. Nothing was changed."
        exit 2 ;;
    esac
    # Same rule as telemetry/pane.py: scheme and host[:port] only. No userinfo (a token there
    # would end up in logs), no path, query or fragment.
    if [[ ! "$PANE_URL_ARG" =~ ^https?://[A-Za-z0-9.:-]+$ ]]; then
        report "not a Pane URL" "read --pane-url" "http://<pane's tailnet address>:8427 (no user@, no path)" \
            "'${PANE_URL_ARG:0:60}'" "copy the URL add-box.sh printed on pane. Nothing was changed."
        exit 2
    fi
    if [[ -n "$MODELS_ARG" ]]; then
        case "$MODELS_ARG" in
            llamaserver=*) MODEL_SOURCE_F=llamaserver; MODEL_URL_F="${MODELS_ARG#llamaserver=}" ;;
            ollama=*)      MODEL_SOURCE_F=ollama; MODEL_URL_F="${MODELS_ARG#ollama=}"; MODEL_PATH_F="/api/ps" ;;
            *) report "unknown --models" "read --models" "llamaserver=URL[,URL] or ollama=URL" "'$MODELS_ARG'" \
                   "fix the option. Nothing was changed."
               exit 2 ;;
        esac
        if [[ ! "$MODEL_URL_F" =~ ^https?://[A-Za-z0-9.:-]+(,https?://[A-Za-z0-9.:-]+)*$ ]]; then
            report "bad model URL" "read --models" "http://host:port (comma-separated for several)" "'$MODEL_URL_F'" \
                "fix the option. Nothing was changed."
            exit 2
        fi
    fi
    if [[ -n "$RUN_USER_ARG" && ! "$RUN_USER_ARG" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
        report "bad --run-user" "read --run-user" "a Linux user name, e.g. telemetry" "'$RUN_USER_ARG'" \
            "fix the option. Nothing was changed."
        exit 2
    fi
    # --run-user exists to keep the pusher away from root; root would undo that (Errata, OPS-47 Low).
    if [[ -n "$RUN_USER_ARG" ]] && id -u "$RUN_USER_ARG" >/dev/null 2>&1 && [[ "$(id -u "$RUN_USER_ARG")" == 0 ]]; then
        report "--run-user can't be root" "read --run-user" "an unprivileged account, e.g. telemetry" \
            "'$RUN_USER_ARG' (uid 0)" "use --run-user telemetry (created if missing). Nothing was changed."
        exit 2
    fi
    # The same check update.sh makes (Errata, OPS-47 Low): a run user that owned the install dir
    # could swap secrets/ itself for a link between our check and our chown.
    for p in "$INSTALL_DIR" "$INSTALL_DIR/.git"; do
        if [[ -e "$p" && "$(stat -c %u "$p")" != 0 ]]; then
            report "the install dir isn't root's" "stat -c %U $p" "owned by root (fleet mode keeps the token here)" \
                "owned by $(stat -c %U "$p")" "if nobody changed it on purpose, find out who did; then: chown root:root $p, and run this again. Nothing was changed."
            exit 2
        fi
    done
elif [[ "$SITES" -eq 1 ]]; then
    report "--sites is for fleet mode" "read --sites" "--profile ... --sites" "--sites without --profile" \
        "add --profile (the interactive install doesn't set TELEMETRY_SITES). Nothing was changed."
    exit 2
fi

# The token, read now (before anything changes) so a bad paste stops the run cleanly.
TOKEN=""
if [[ "$TOKEN_STDIN" -eq 1 ]]; then
    IFS= read -r TOKEN || true
    TOKEN="${TOKEN%$'\r'}"
    if [[ ! "$TOKEN" =~ ^[!-~]{20,512}$ ]]; then
        report "that isn't a Pane token" "read the token on stdin" \
            "one line of printable characters with no spaces (pane's add-box.sh prints it)" \
            "${#TOKEN} character(s) that don't fit" "paste the token add-box.sh printed, on its own. Nothing was changed."
        exit 2
    fi
fi

write_env_profile() {
    local nvidia=0
    [[ "$PROFILE" == gpu ]] && nvidia=1
    umask 077
    local tmp
    tmp="$(mktemp "$INSTALL_DIR/.env.XXXXXX")"   # then mv, as in write_env
    cat > "$tmp" <<ENV
# system-telemetry config: written by install.sh --profile $PROFILE (fleet mode). Rerun the same
# command to restore it; hand edits are overwritten. Never commit this file.
MQTT_HOST=
MQTT_PORT=1883
MQTT_USER=
MQTT_PASS=
MQTT_DISCOVERY_PREFIX=homeassistant

# Pane. The token is NOT here: it lives in secrets/pane_token (PANE_TOKEN_FILE).
PANE_URL=$PANE_URL_ARG

TELEMETRY_HOST_NAME=

TELEMETRY_NVIDIA=$nvidia
TELEMETRY_LHM=0
LHM_GPUS=0
LHM_URL=http://localhost:8085/data.json
LHM_GPU_INCLUDE=
TELEMETRY_HOST=1
TELEMETRY_SITES=$SITES

MODEL_SOURCE=$MODEL_SOURCE_F
MODEL_API_URL=$MODEL_URL_F
MODEL_API_PATH=$MODEL_PATH_F
MODEL_FILTER=

LOOP_INTERVAL=
DEBUG=0
ENV
    mv -f -T "$tmp" "$INSTALL_DIR/.env"
    say "Wrote $INSTALL_DIR/.env (profile $PROFILE)"
}

# ---------------------------------------------------------------------------
# .env: prompt on first run or --reconfigure; otherwise keep existing.
# ---------------------------------------------------------------------------
if [[ -n "$PROFILE" ]]; then
    write_env_profile
elif [[ ! -f "$INSTALL_DIR/.env" || "$RECONFIGURE" -eq 1 ]]; then
    write_env
else
    say "Existing .env found — keeping it (run ./install.sh --reconfigure to change)."
    # Safety: Windows loop service needs LOOP_INTERVAL set.
    if [[ "$OS" == windows ]] && ! grep -qE '^LOOP_INTERVAL=[0-9]' "$INSTALL_DIR/.env"; then
        warn "LOOP_INTERVAL blank in .env — setting 30 (Windows loop mode)."
        tmp="$INSTALL_DIR/.env.tmp"
        sed 's/^LOOP_INTERVAL=.*/LOOP_INTERVAL=30/' "$INSTALL_DIR/.env" > "$tmp"
        mv "$tmp" "$INSTALL_DIR/.env"
    fi
fi

# ===========================================================================
# LINUX — venv + systemd
# ===========================================================================
if [[ "$OS" == linux ]]; then
    RUN_USER="${RUN_USER_ARG:-${SUDO_USER:-$USER}}"
    if [[ -n "$RUN_USER_ARG" ]] && ! id -u "$RUN_USER" >/dev/null 2>&1; then
        say "Creating system user '$RUN_USER' for the service…"
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$RUN_USER"
    fi

    # The token lives in secrets/, which root owns (0750, group = the run user's group): the run
    # user can read its token but can never add, swap or symlink anything in there, so every
    # root write and chown below lands on the file it names. (Errata, OPS-47 Blocker: with
    # secrets/ owned by the run user, a planted `pane_token -> /etc/shadow` turned a rerun's
    # chown into /etc/shadow owned by the run user.)
    secure_token() {
        local dir="$INSTALL_DIR/secrets" tok="$INSTALL_DIR/secrets/pane_token" group tmp
        group="$(id -gn "$RUN_USER")"
        if [[ -L "$dir" || ( -e "$dir" && ! -d "$dir" ) ]]; then
            report "secrets/ isn't a plain directory" "check $dir" "a directory, or nothing yet" \
                "a symlink or a file" "move it aside (it isn't safe to use) and run this again. The token was not touched."
            exit 1
        fi
        mkdir -p "$dir"
        chown -h "root:$group" "$dir"
        chmod 0750 "$dir"
        # From here on, only root can change what is in secrets/.
        if [[ -n "$TOKEN" ]]; then
            tmp="$(mktemp "$dir/.pane_token.XXXXXX")"
            printf '%s' "$TOKEN" > "$tmp"
            chown -h "$RUN_USER:$group" "$tmp"
            chmod 0400 "$tmp"
            # A directory (or anything) planted at the name would swallow the token, so it goes
            # first, and mv -T never moves into a directory (Errata, OPS-47 Low).
            if [[ -L "$tok" || ( -e "$tok" && ! -f "$tok" ) ]]; then rm -rf -- "$tok"; fi
            mv -f -T "$tmp" "$tok"
            say "Saved the Pane token to $tok (0400, $RUN_USER)."
        elif [[ -L "$tok" || ( -e "$tok" && ( ! -f "$tok" || "$(stat -c %h "$tok")" != 1 ) ) ]]; then
            rm -rf -- "$tok"
            report "the saved Pane token wasn't a plain file (removed)" "check $tok" \
                "a regular file with one link" "a symlink, a hard link or not a file" \
                "run this again with the token add-box.sh printed on pane (--token-stdin)."
        elif [[ -f "$tok" ]]; then
            chown -h "$RUN_USER:$group" "$tok"   # the run user may have changed
            chmod 0400 "$tok"
        fi
    }

    if [[ "${TELEMETRY_SKIP_SERVICE:-0}" == 1 ]]; then
        # Tests: .env and the token only; no venv, no systemd.
        chown -h "$RUN_USER" "$INSTALL_DIR/.env" 2>/dev/null || true
        secure_token
        say "TELEMETRY_SKIP_SERVICE=1: .env and token only."
        exit 0
    fi

    # What a fresh Ubuntu box lacks (pane's first install, Nov 13): venv/ensurepip and pip.
    missing=()
    command -v python3 >/dev/null || missing+=(python3)
    # Ubuntu's python3 imports ensurepip even without python3-venv; only making a venv tells.
    venv_probe="$(mktemp -d)"
    python3 -m venv "$venv_probe/v" >/dev/null 2>&1 || missing+=(python3-venv)
    rm -rf "$venv_probe"
    python3 -m pip --version >/dev/null 2>&1 || missing+=(python3-pip)
    command -v git >/dev/null || missing+=(git)
    if [[ ${#missing[@]} -gt 0 ]]; then
        if [[ -n "$PROFILE" ]] && command -v apt-get >/dev/null; then
            say "Installing ${missing[*]}…"
            DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null
            DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}" >/dev/null
        else
            report "missing packages" "checked for python3, venv, pip and git" "all present" "missing: ${missing[*]}" \
                "sudo apt install -y ${missing[*]}, then run ./install.sh again. .env is kept."
            exit 1
        fi
    fi

    say "Creating venv…"
    # A venv the service user already owns is rebuilt AS that user: running its pip as root would
    # run whatever that user put there as root (Errata, OPS-19 L5).
    AS=()
    if [[ -d "$INSTALL_DIR/venv" && "$(stat -c %U "$INSTALL_DIR/venv")" != root ]]; then
        AS=(runuser -u "$(stat -c %U "$INSTALL_DIR/venv")" --)
    fi
    "${AS[@]}" python3 -m venv "$INSTALL_DIR/venv"
    "${AS[@]}" "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
    "${AS[@]}" "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
    chown -R "$RUN_USER" "$INSTALL_DIR/venv" 2>/dev/null || true
    # .env is written root-owned (umask 077 under the sudo re-exec), but the
    # service runs as $RUN_USER. Hand it over so config.py can read it; mode
    # stays 0600 so the MQTT password remains owner-only.
    chown -h "$RUN_USER" "$INSTALL_DIR/.env" 2>/dev/null || true

    secure_token

    say "Installing systemd unit + timer (run user: $RUN_USER)…"
    sed -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" -e "s#__RUN_USER__#$RUN_USER#g" \
        "$INSTALL_DIR/service/telemetry.service" \
        > "/etc/systemd/system/${SERVICE_NAME}.service"
    cp "$INSTALL_DIR/service/telemetry.timer" \
        "/etc/systemd/system/${SERVICE_NAME}.timer"
    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}.timer"
    say "Done. Timer active. Run now:  sudo systemctl start ${SERVICE_NAME}.service"
    say "Logs:  journalctl -u ${SERVICE_NAME}.service -f"
    # Only when there is no token at all: a kept token needs nothing (pane's rerun, Nov 13).
    [[ -n "$TOKEN" || -s "$INSTALL_DIR/secrets/pane_token" ]] || say "Pane: put this box's token in $INSTALL_DIR/secrets/pane_token (0400, owned by $RUN_USER) — see README."
    [[ "$INSTALL_DIR" == /opt/telemetry ]] || \
        say "      Install dir isn't /opt/telemetry: also set PANE_TOKEN_FILE=$INSTALL_DIR/secrets/pane_token in .env."
    exit 0
fi

# ===========================================================================
# WINDOWS — venv + NSSM
# ===========================================================================
if [[ "$OS" == windows ]]; then
    PY="$(command -v python || command -v py || true)"
    [[ -n "$PY" ]] || die "python not found on PATH (need a system Python to build the venv)."
    say "Creating venv…"
    "$PY" -m venv "$INSTALL_DIR/venv"
    VENV_PY="$INSTALL_DIR/venv/Scripts/python.exe"
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"
    mkdir -p "$INSTALL_DIR/logs"

    command -v nssm >/dev/null || die "nssm not found on PATH. Install NSSM, then re-run."

    win() { command -v cygpath >/dev/null && cygpath -w "$1" || echo "$1"; }
    WDIR="$(win "$INSTALL_DIR")"
    WVENV_PY="$(win "$VENV_PY")"
    WOUT="$(win "$INSTALL_DIR/logs/telemetry.out.log")"
    WERR="$(win "$INSTALL_DIR/logs/telemetry.err.log")"

    say "Registering NSSM service '$SERVICE_NAME'…"
    nssm install "$SERVICE_NAME" "$WVENV_PY" "-m" "telemetry" 2>/dev/null || \
        nssm set "$SERVICE_NAME" Application "$WVENV_PY"
    nssm set "$SERVICE_NAME" AppDirectory "$WDIR"
    nssm set "$SERVICE_NAME" AppParameters "-m telemetry"
    nssm set "$SERVICE_NAME" AppStdout "$WOUT"
    nssm set "$SERVICE_NAME" AppStderr "$WERR"
    nssm set "$SERVICE_NAME" Start SERVICE_AUTO_START

    say "Starting service (runs as LocalSystem — the venv makes that sufficient)…"
    nssm start "$SERVICE_NAME" 2>/dev/null || nssm restart "$SERVICE_NAME"

    say "Done."
    cat <<EOF
Logs:               $INSTALL_DIR/logs/telemetry.{out,err}.log
Reconfigure later:  ./install.sh --reconfigure
Pane token (if PANE_URL is set): C:\ProgramData\telemetry\secrets\pane_token, see README
EOF
    exit 0
fi
