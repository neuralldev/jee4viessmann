#!/usr/bin/env python3
# This file is part of Jeedom.
#
# Jeedom is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Jeedom is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.

"""
Démon Jee4viessmann.

Responsabilités :
  - authentification viessmann via PyViCare (OAuth2 + PKCE + refresh token gérés par la lib) ;
  - découverte installation/gateway/device ;
  - polling périodique des features et génération des commandes à partir du *typage* de l'API
    (aucun mapping manuel) ;
  - push des valeurs vers Jeedom (callback HTTP) ;
  - réception, par socket, de la configuration (identifiants) et des actions à exécuter.
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
import traceback

try:
    from jeedom.jeedom import jeedom_com, jeedom_socket, jeedom_utils
except ImportError:
    print("Erreur : module jeedom.jeedom introuvable")
    sys.exit(1)

try:
    from PyViCare.PyViCare import PyViCare
except ImportError:
    print("Erreur : PyViCare non installé (voir resources/requirements.txt)")
    sys.exit(1)


# --------------------------------------------------------------------------- #
#  État global
# --------------------------------------------------------------------------- #

EQUIPMENTS = {}        # {eqId: {clientId, user, pwd}}
SESSIONS = {}          # {eqId: device PyViCare}

JEEDOM_COM = None      # instance jeedom_com
JEEDOM_SOCKET = None   # instance jeedom_socket
PID_FILE = "/tmp/jee4viessmannd.pid"


# --------------------------------------------------------------------------- #
#  Typage API -> Jeedom
# --------------------------------------------------------------------------- #

TYPE_MAP = {
    "number": "numeric",
    "boolean": "binary",
    "string": "string",
}


def sanitize_logical_id(feature, prop):
    raw = (feature + "." + prop).lower()
    return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")


def feature_to_commands(feature_entry):
    """Transforme une feature de l'API en une liste de commandes info typées."""
    commands = []
    if not feature_entry.get("isEnabled", False):
        return commands
    feature = feature_entry.get("feature", "")
    properties = feature_entry.get("properties", {}) or {}
    for prop, meta in properties.items():
        if not isinstance(meta, dict):
            continue
        ptype = meta.get("type")
        if ptype not in TYPE_MAP:
            # array / Schedule / objets : ignorés pour ce POC.
            continue
        if "value" not in meta:
            continue
        value = meta["value"]
        if ptype == "boolean":
            value = 1 if value else 0
        commands.append({
            "logicalId": sanitize_logical_id(feature, prop),
            "name": feature + " - " + prop,
            "cmdType": "info",
            "subType": TYPE_MAP[ptype],
            "unit": meta.get("unit", ""),
            "value": value,
        })
    return commands


# --------------------------------------------------------------------------- #
#  viessmann (PyViCare)
# --------------------------------------------------------------------------- #

def token_file(eq_id):
    return "/tmp/jee4viessmann_token_%s.save" % eq_id


def authenticate(eq_id, cfg):
    """(Ré)authentifie un équipement et mémorise le premier device découvert."""
    try:
        vicare = PyViCare()
        vicare.initWithCredentials(cfg["user"], cfg["pwd"], cfg["clientId"], token_file(eq_id))
        if not vicare.devices:
            logging.warning("Équipement %s : aucun device découvert", eq_id)
            return None
        device = vicare.devices[0]
        SESSIONS[eq_id] = device
        logging.info("Équipement %s : authentifié", eq_id)
        return device
    except Exception as e:
        logging.error("Équipement %s : échec authentification : %s", eq_id, str(e))
        return None


def poll_equipment(eq_id):
    device = SESSIONS.get(eq_id)
    if device is None:
        device = authenticate(eq_id, EQUIPMENTS[eq_id])
        if device is None:
            return
    try:
        features = device.service.fetch_all_features()
    except Exception as e:
        logging.warning("Équipement %s : échec fetch_all_features (%s), ré-auth au prochain cycle", eq_id, str(e))
        SESSIONS.pop(eq_id, None)
        return

    commands = []
    for entry in features.get("data", []):
        commands.extend(feature_to_commands(entry))

    if commands:
        logging.debug("Équipement %s : %d commandes poussées", eq_id, len(commands))
        JEEDOM_COM.send_change_immediate({"eqLogicId": eq_id, "commands": commands})


# --------------------------------------------------------------------------- #
#  Réception socket (PHP -> démon)
# --------------------------------------------------------------------------- #

def handle_message(message):
    mtype = message.get("type")
    if mtype == "config":
        EQUIPMENTS.clear()
        SESSIONS.clear()
        for eq in message.get("equipments", []):
            EQUIPMENTS[eq["id"]] = eq
        logging.info("Configuration reçue : %d équipement(s)", len(EQUIPMENTS))
    elif mtype == "action":
        execute_action(message)
    else:
        logging.debug("Message ignoré : %s", str(message))


def execute_action(message):
    eq_id = message.get("eqLogicId")
    logical_id = message.get("logicalId")
    options = message.get("options", {})
    device = SESSIONS.get(eq_id)
    if device is None:
        logging.warning("Action %s : pas de session pour l'équipement %s", logical_id, eq_id)
        return
    # POC : aiguillage des actions vers les setters PyViCare (setMode, setTargetTemperature, ...).
    # À compléter selon les commandes exposées par l'appareil.
    logging.info("Action reçue (à implémenter) : eq=%s id=%s options=%s", eq_id, logical_id, json.dumps(options))


def read_socket():
    raw = JEEDOM_SOCKET.get_message()
    if raw is None:
        return
    try:
        handle_message(json.loads(raw))
    except Exception as e:
        logging.error("Erreur lecture socket : %s", str(e))


# --------------------------------------------------------------------------- #
#  Boucle principale
# --------------------------------------------------------------------------- #

def listen(cycle_poll):
    JEEDOM_SOCKET.open()
    logging.info("Démon démarré, socket ouvert")
    last_poll = 0
    while True:
        time.sleep(0.5)
        read_socket()
        now = time.time()
        if now - last_poll >= cycle_poll:
            last_poll = now
            for eq_id in list(EQUIPMENTS.keys()):
                try:
                    poll_equipment(eq_id)
                except Exception:
                    logging.error("Erreur poll %s : %s", eq_id, traceback.format_exc())


def shutdown():
    logging.info("Arrêt du démon")
    try:
        JEEDOM_SOCKET.close()
    except Exception:
        pass
    try:
        os.remove(PID_FILE)
    except Exception:
        pass
    sys.exit(0)


def handler(signum=None, frame=None):
    shutdown()


# --------------------------------------------------------------------------- #
#  Entrée
# --------------------------------------------------------------------------- #

def main():
    global JEEDOM_COM, JEEDOM_SOCKET, PID_FILE

    parser = argparse.ArgumentParser()
    parser.add_argument("--loglevel", default="error")
    parser.add_argument("--socketport", default=55070, type=int)
    parser.add_argument("--callback", default="")
    parser.add_argument("--apikey", default="")
    parser.add_argument("--cyclepoll", default=120, type=int)
    parser.add_argument("--pid", default="/tmp/jee4viessmannd.pid")
    args = parser.parse_args()

    PID_FILE = args.pid
    jeedom_utils.set_log_level(args.loglevel)

    logging.info("Démarrage jee4viessmannd (port=%s, cycle=%ss)", args.socketport, args.cyclepoll)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    try:
        jeedom_utils.write_pid(str(PID_FILE))
        JEEDOM_COM = jeedom_com(apikey=args.apikey, url=args.callback, cycle=0.5)
        if not JEEDOM_COM.test():
            logging.error("Callback Jeedom injoignable au démarrage")
        JEEDOM_SOCKET = jeedom_socket(port=args.socketport, address="127.0.0.1")
        listen(args.cyclepoll)
    except KeyboardInterrupt:
        shutdown()
    except Exception as e:
        logging.error("Erreur fatale : %s", str(e))
        shutdown()


if __name__ == "__main__":
    main()
