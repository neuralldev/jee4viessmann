#!/bin/bash
# Installation du venv Python du démon jee4viessmann.
# Appelé en root par le gestionnaire de dépendances Jeedom (packages.json -> post-install).
#
# Pourquoi pas la section "pip3" native de packages.json : Jeedom crée le venv avec le python3
# SYSTÈME (et n'en crée même pas sur Debian < 12). Or PyViCare >= 2.60 exige Python >= 3.10.
# Ici on prend un Python système compatible s'il existe, sinon on télécharge un Python autonome
# (uv, python-build-standalone) rangé dans le plugin — sans toucher au Python de Jeedom.

set -u

MIN_MINOR=10                # Python 3.10 minimum (PyViCare)
FALLBACK_VERSION="3.12"     # version téléchargée si aucun Python système ne convient
REQUIREMENTS=("jeedomdaemon>=1.2.9" "PyViCare>=2.60")

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$BASE_DIR/python_venv"
UV_DIR="$BASE_DIR/python_uv"            # binaire uv
PY_RUNTIME_DIR="$BASE_DIR/python_runtime"  # Python autonome téléchargé par uv

log() { echo "[jee4viessmann] $*"; }
fail() { log "ERREUR : $*"; exit 1; }

# Retourne 0 si l'interpréteur $1 est un Python 3 >= 3.MIN_MINOR avec le module venv.
py_ok() {
    "$1" -c "import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" >/dev/null 2>&1
}

# Le venv est toujours recréé (install = relancée à la demande) : pas d'état hérité d'un
# venv construit avec un autre Python (cas typique : ancien venv natif Jeedom en 3.9).
PYTHON=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    path="$(command -v "$cand" 2>/dev/null)" || continue
    if py_ok "$path"; then
        PYTHON="$path"
        break
    fi
done

if [ -n "$PYTHON" ]; then
    log "Python système compatible : $PYTHON ($("$PYTHON" -V 2>&1))"
    rm -rf "$VENV_DIR"
    "$PYTHON" -m venv --upgrade-deps "$VENV_DIR" || fail "création du venv avec $PYTHON"
else
    log "Aucun Python système >= 3.$MIN_MINOR (système : $(python3 -V 2>&1)). Téléchargement de Python $FALLBACK_VERSION via uv."
    UV="$UV_DIR/uv"
    if [ ! -x "$UV" ]; then
        mkdir -p "$UV_DIR"
        if command -v curl >/dev/null 2>&1; then
            curl -LsSf https://astral.sh/uv/install.sh \
                | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh \
                || log "installateur uv (curl) en échec, repli sur pip"
        fi
        if [ ! -x "$UV" ]; then
            python3 -m pip install --quiet --target "$UV_DIR/pip" uv \
                && ln -sf "$UV_DIR/pip/bin/uv" "$UV" \
                || fail "impossible d'installer uv (ni curl ni pip)"
        fi
    fi
    export UV_PYTHON_INSTALL_DIR="$PY_RUNTIME_DIR"
    "$UV" python install "$FALLBACK_VERSION" || fail "téléchargement de Python $FALLBACK_VERSION"
    rm -rf "$VENV_DIR"
    "$UV" venv --seed --python "$FALLBACK_VERSION" "$VENV_DIR" || fail "création du venv Python $FALLBACK_VERSION"
fi

VPY="$VENV_DIR/bin/python3"
[ -x "$VPY" ] || fail "venv incomplet ($VPY absent)"
log "venv : $("$VPY" -V 2>&1)"

"$VPY" -m pip install --upgrade pip wheel || log "mise à jour pip en échec (non bloquant)"
"$VPY" -m pip install --upgrade "${REQUIREMENTS[@]}" || fail "installation de ${REQUIREMENTS[*]}"

"$VPY" -c "import jeedomdaemon, PyViCare" || fail "import jeedomdaemon/PyViCare impossible après installation"

# Le démon est lancé par www-data : le venv (et le Python téléchargé) doivent lui appartenir.
chown -R www-data:www-data "$VENV_DIR" 2>/dev/null
[ -d "$PY_RUNTIME_DIR" ] && chown -R www-data:www-data "$PY_RUNTIME_DIR" 2>/dev/null
[ -d "$UV_DIR" ] && chown -R www-data:www-data "$UV_DIR" 2>/dev/null

log "dépendances installées."
exit 0
