#!/bin/bash
# Création d'un environnement virtuel Python isolé et installation des dépendances.
# Appelé par jee4viessman::dependancy_install().

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="${PLUGIN_DIR}/resources/venv"

echo "== Jee4Viessman : installation des dépendances =="
echo "Répertoire plugin : ${PLUGIN_DIR}"

# Python 3 (3.11+ recommandé ; testé sous 3.13)
PYTHON_BIN="$(command -v python3)"
if [ -z "${PYTHON_BIN}" ]; then
    echo "ERREUR : python3 introuvable"
    exit 1
fi
echo "Python : $(${PYTHON_BIN} --version)"

sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

# (Re)création du venv
rm -rf "${VENV_DIR}"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"

"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${PLUGIN_DIR}/resources/requirements.txt"

echo "== Installation terminée =="
