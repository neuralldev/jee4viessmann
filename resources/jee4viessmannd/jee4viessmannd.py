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
Démon jee4viessmann — basé sur jeedomdaemon.BaseDaemon (même socle que jee4lm5).

Responsabilités :
  - authentification viessmann via PyViCare (OAuth2 + PKCE + refresh gérés par la lib) ;
  - découverte installation/gateway/device ;
  - polling périodique des features et génération des commandes à partir du *typage*
    de l'API (aucun mapping manuel) ;
  - push des valeurs vers Jeedom (send_to_jeedom -> callback HTTP) ;
  - réception, par socket, de la configuration (identifiants) et des actions.

Le socle jeedomdaemon gère pour nous : parsing des args (BaseConfig), socket TCP,
écriture/suppression du PID, test du callback, signaux, et la boucle asyncio.
"""

import asyncio
import logging
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Europe/Paris")  # gère CET/CEST (GMT+1 hiver, GMT+2 été)
except Exception:
    _LOCAL_TZ = timezone(timedelta(hours=1))  # repli : GMT+1 fixe si tzdata absent


def _utcnow() -> datetime:
    """Instant courant en UTC *aware*.

    datetime.utcnow() est déprécié depuis Python 3.12 et renvoie un naïf : mélangé à un
    datetime aware, toute comparaison lève TypeError. On travaille donc uniquement en aware.
    """
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    """Normalise un datetime en UTC aware.

    PyViCare construit aujourd'hui limitResetDate avec utcfromtimestamp() -> naïf UTC ; le jour
    où la lib passera en aware (correctif attendu de la dépréciation), cette fonction absorbe
    le changement au lieu de faire exploser la comparaison de _is_paused().
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _use_local_log_time() -> None:
    """Force l'horodatage des logs (asctime de jeedomdaemon) en heure locale Europe/Paris.

    jeedomdaemon configure le logging avec le converter par défaut (time.localtime), qui
    dépend de la TZ du conteneur — souvent fausse. Le converter est lu à chaque formatage,
    donc le surcharger ici suffit pour toutes les lignes suivantes, sans toucher la lib.
    """
    def _local_converter(secs=None):
        return datetime.fromtimestamp(secs if secs is not None else time.time(), _LOCAL_TZ).timetuple()
    # staticmethod : sinon, assignée comme attribut de classe, la fonction se lierait à
    # l'instance Formatter et recevrait un 'self' parasite (converter est appelé avec un arg).
    logging.Formatter.converter = staticmethod(_local_converter)

from jeedomdaemon.base_daemon import BaseDaemon
from jeedomdaemon.base_config import BaseConfig

try:
    from PyViCare.PyViCare import PyViCare
except ImportError:
    PyViCare = None


# --------------------------------------------------------------------------- #
#  Typage API -> Jeedom
# --------------------------------------------------------------------------- #

TYPE_MAP = {
    "number": "numeric",
    "boolean": "binary",
    "string": "string",
}

# Normalisation des unités renvoyées par l'API viessmann -> unités lisibles Jeedom.
UNIT_MAP = {
    "celsius": "°C",
    "kelvin": "K",
    "percent": "%",
    "bar": "bar",
    "watt": "W",
    "kilowatt": "kW",
    "wattHour": "Wh",
    "kilowattHour": "kWh",
    "cubicMeter": "m³",
    "liter": "L",
    "hour": "h",
    "minute": "min",
    "second": "s",
    "seconds": "s",
    "revolutionsPerMinute": "tr/min",
}

# generic_type Jeedom par unité (pour widgets/graphes).
GENERIC_TYPE_BY_UNIT = {
    "°C": "TEMPERATURE",
}

# Propriété "générique" : son nom n'est pas accolé au libellé (la feature suffit).
# On garde 'value' seulement : 'status'/'active'/... doivent rester distincts pour éviter
# des noms de commande identiques (Jeedom impose l'unicité du nom par équipement).
GENERIC_PROPS = {"value"}

# Classement des features en sous-systèmes -> un eqLogic Jeedom par (device, sous-système).
# Premier préfixe correspondant (sur la feature en minuscules) l'emporte.
GROUP_RULES = [
    ("heating.circuits", "circuits", "Circuits chauffage"),
    ("heating.dhw", "ecs", "Eau chaude sanitaire"),
    ("heating.domestichotwater", "ecs", "Eau chaude sanitaire"),
    ("heating.compressors", "compresseur", "Compresseur"),
    ("heating.heatingrod", "appoint", "Résistance d'appoint"),
    ("heating.buffer", "tampon", "Ballon tampon"),
    ("heating.cop", "energie", "COP / Énergie"),
    ("heating.power", "energie", "COP / Énergie"),
    ("heating.primarycircuit", "frigo", "Circuit frigorifique"),
    ("heating.secondarycircuit", "frigo", "Circuit frigorifique"),
    ("heating.evaporators", "frigo", "Circuit frigorifique"),
    ("heating.condensors", "frigo", "Circuit frigorifique"),
    ("heating.sensors.pressure", "frigo", "Circuit frigorifique"),
    ("heating.sensors.temperature.hotgas", "frigo", "Circuit frigorifique"),
    ("heating.sensors.temperature.liquidgas", "frigo", "Circuit frigorifique"),
    ("heating.sensors.temperature.suctiongas", "frigo", "Circuit frigorifique"),
    ("heating.configuration", "config", "Configuration"),
]
DEFAULT_GROUP = ("general", "Général")


def classify(feature: str):
    """Retourne (clé_groupe, libellé_groupe) pour une feature."""
    f = feature.lower()
    for prefix, key, label in GROUP_RULES:
        if f.startswith(prefix):
            return key, label
    return DEFAULT_GROUP


def is_visible(feature: str, prop: str, value) -> int:
    """Visibilité par défaut : on n'affiche que l'essentiel exploitable, le reste est créé
       mais masqué (récupérable à la main). Objectif : un tableau lisible, pas 47 champs."""
    f = feature.lower()
    # Santé capteur (connected/notConnected) : masquée.
    if prop == "status" and str(value) in ("connected", "notConnected"):
        return 0
    # Identifiants/série, configuration, contrôleur, infos device brutes : masqués.
    if "serial" in f or "configuration" in f or "controller" in f or f.startswith("device"):
        return 0
    if "useapproved" in f or "mainecu" in f:
        return 0
    # Bruit/avancé : RoomControl interne, courbe de chauffe, niveaux mini/maxi, planification, type.
    if f.startswith("rooms.features"):
        return 0
    if "heating.curve" in f or "temperature.levels" in f or "schedule" in f:
        return 0
    if prop in ("name", "demand", "type"):
        return 0
    # Programmes/modes : on garde uniquement les résumés '...modes.active' / '...programs.active'
    # (valeur = mode/programme courant). On masque :
    #   - les sous-flags binaires par mode/programme (operating.modes.X.active, operating.programs.X.active)
    #   - les températures par programme (redondantes avec le slider de consigne qui les affiche)
    if "operating.modes." in f and prop == "active":
        return 0
    if "operating.programs." in f and prop in ("active", "temperature"):
        return 0
    return 1


# Traduction des segments de feature -> libellé FR. "" = segment supprimé (redondant).
# Les segments inconnus passent tels quels ; les index numériques sont conservés.
TERM_FR = {
    "heating": "chauffage", "sensors": "", "operating": "",
    "temperature": "température", "supply": "départ", "return": "retour",
    "room": "ambiante", "outside": "extérieure", "ambient": "ambiante",
    "circuits": "circuit", "circuit": "circuit",
    "compressors": "compresseur", "compressor": "compresseur",
    "pressure": "pression", "hotgas": "gaz chaud", "suctiongas": "gaz aspiration",
    "liquidgas": "gaz liquide", "liquid": "liquide",
    "inlet": "entrée", "outlet": "sortie", "overheat": "surchauffe",
    "subcooling": "sous-refroidissement",
    "power": "puissance", "rotation": "rotation",
    "statistics": "statistiques", "starts": "démarrages", "hours": "heures",
    "runtime": "temps fonctionnement", "load": "charge",
    "buffer": "tampon", "buffercylinder": "ballon tampon", "top": "haut", "main": "bas",
    "cop": "COP", "total": "total", "green": "vert", "photovoltaic": "photovoltaïque",
    "cooling": "rafraîchissement", "dhw": "ECS",
    "programs": "programme", "program": "programme", "modes": "mode", "mode": "mode",
    "active": "actif", "comfort": "confort", "eco": "éco", "normal": "normal",
    "reduced": "réduit", "standby": "veille", "fixed": "fixe", "demand": "demande",
    "curve": "courbe", "slope": "pente", "shift": "translation", "schedule": "programmation",
    "circulation": "circulation", "pump": "pompe", "frostprotection": "hors-gel",
    "evaporators": "évaporateur", "evaporator": "évaporateur",
    "condensors": "condenseur", "condensor": "condenseur",
    "primarycircuit": "circuit primaire", "secondarycircuit": "circuit secondaire",
    "levels": "niveaux", "level": "niveau", "min": "min", "max": "max",
    "heatingrod": "résistance appoint", "phase": "phase", "holiday": "vacances",
    "boiler": "chaudière", "controller": "régulateur", "serial": "n° série",
    "type": "type", "name": "nom", "status": "état",
    "wifi": "wifi", "strength": "signal", "bmuconnection": "connexion BMU",
    "start": "début", "end": "fin", "configuration": "configuration",
}


# Traduction FR des commandes (setters) de l'API.
COMMAND_FR = {
    "settemperature": "consigne",
    "settargettemperature": "consigne",
    "setmode": "mode",
    "setname": "nom",
    "activate": "activer",
    "deactivate": "désactiver",
    "setcurve": "courbe",
    "setschedule": "programmation",
    "sethysteresis": "hystérésis",
    "setmin": "min",
    "setmax": "max",
    "changeenddate": "fin",
    "setlevels": "niveaux",
}

# Propriété info "pilotée" par une commande d'action -> permet de lier le slider/select à la
# valeur courante (le widget affiche et modifie la même donnée).
LINK_PROP = {
    "settemperature": "temperature",
    "settargettemperature": "temperature",
    "setmode": "value",
    "setmin": "min",
    "setmax": "max",
    "activate": "active",
    "deactivate": "active",
}


def clean_name(s: str) -> str:
    """Retire les caractères spéciaux problématiques des noms (apostrophes, tirets longs, #...)
       et normalise les espaces. On conserve lettres accentuées, chiffres et espaces."""
    for ch in ("'", "’", "—", "–", "#", "|", ";"):
        s = s.replace(ch, " ")
    return re.sub(r"\s+", " ", s).strip()


def sanitize_logical_id(feature: str, prop: str) -> str:
    raw = (feature + "." + prop).lower()
    return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")


def humanize(feature: str, prop: str) -> str:
    """Libellé FR lisible depuis le chemin de la feature (+ propriété si non générique)."""
    parts = list(feature.split("."))
    # Supprime le segment racine redondant (le device/groupe donne déjà le contexte).
    if parts and parts[0].lower() in ("heating", "device"):
        parts = parts[1:]
    if prop not in GENERIC_PROPS:
        parts.append(prop)
    out = []
    for seg in parts:
        if seg.isdigit():
            out.append(seg)  # index de circuit/compresseur conservé
            continue
        fr = TERM_FR.get(seg.lower(), seg)
        if fr:
            out.append(fr)
    label = clean_name(" ".join(out))
    return (label[:1].upper() + label[1:]) if label else (prop or feature)


def feature_to_commands(feature_entry: dict) -> list:
    """Transforme une feature *active* de l'API en commandes info typées et lisibles."""
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
        raw_value = meta["value"]
        value = (1 if raw_value else 0) if ptype == "boolean" else raw_value
        raw_unit = meta.get("unit", "") or ""
        unit = UNIT_MAP.get(raw_unit, raw_unit)
        # Durées en secondes -> heures (1 décimale) pour la lisibilité.
        if raw_unit in ("second", "seconds") and isinstance(value, (int, float)):
            value = round(value / 3600.0, 1)
            unit = "h"
        group_key, group_label = classify(feature)
        cmd = {
            "logicalId": sanitize_logical_id(feature, prop),
            "name": humanize(feature, prop),
            "cmdType": "info",
            "subType": TYPE_MAP[ptype],
            "unit": unit,
            "genericType": GENERIC_TYPE_BY_UNIT.get(unit, ""),
            # Historise automatiquement les capteurs numériques (températures, puissances...).
            "historized": 1 if ptype == "number" else 0,
            "visible": is_visible(feature, prop, raw_value),
            "group": group_key,
            "groupLabel": group_label,
            "value": value,
        }
        # Ballon tampon : températures affichées avec le widget « thermomètre » graphique
        # (jauge colorée bleu→rouge). Plage de la jauge passée en min/max de la commande.
        if group_key == "tampon" and unit == "°C":
            cmd["template"] = "thermometre"
            cmd["min"] = 0
            cmd["max"] = 80
        commands.append(cmd)
    return commands


def action_name(feature: str, cmd_name: str) -> str:
    """Libellé FR d'une commande d'action (feature + verbe traduit, sans séparateur spécial)."""
    base = humanize(feature, "value")  # contexte sans propriété
    verb = COMMAND_FR.get(cmd_name.lower(), cmd_name)
    return clean_name(base + " " + verb)


def feature_to_actions(feature_entry: dict) -> list:
    """Génère les commandes d'action depuis le bloc 'commands' d'une feature active.
       - 1 paramètre numérique (min/max) -> slider
       - 1 paramètre enum               -> sélecteur
       - 0 paramètre                    -> bouton
       (les commandes multi-paramètres / texte libre / schedule sont ignorées pour le POC)
    """
    actions = []
    if not feature_entry.get("isEnabled", False):
        return actions
    feature = feature_entry.get("feature", "")
    cmds = feature_entry.get("commands", {}) or {}
    group_key, group_label = classify(feature)
    for cname, cdef in cmds.items():
        if not isinstance(cdef, dict):
            continue
        # On NE filtre PAS sur 'isExecutable' : l'API ne le met à true que pour la commande
        # pertinente selon l'état courant (ex. activate vs deactivate), ce qui rendrait l'ensemble
        # des boutons instable d'un cycle à l'autre. On génère depuis la *définition* -> jeu de
        # commandes stable ; une commande non pertinente à l'instant T renverra une erreur (gérée).
        # Actions de planification (setSchedule/resetSchedule/unschedule) : inutiles sans agenda.
        if "schedule" in cname.lower():
            continue
        params = cdef.get("params", {}) or {}
        link_prop = LINK_PROP.get(cname.lower())
        # Visible par défaut : consignes (setTemperature), sélecteur de mode (setMode) et
        # 'activate' (sélecteur de programme : confort/éco/normal/réduit, mutuellement exclusifs).
        # Masqué par défaut : 'deactivate' (redondant), niveaux min/max... (récupérables à la main).
        default_visible = 1 if cname.lower() in (
            "settemperature", "settargettemperature", "setmode", "activate") else 0
        common = {
            "logicalId": sanitize_logical_id(feature, cname),
            "name": action_name(feature, cname),
            "cmdType": "action",
            "visible": default_visible,
            "group": group_key,
            "groupLabel": group_label,
            "feature": feature,
            "action": cname,
            # logicalId de la commande info pilotée (pour lier le widget à la valeur courante).
            "link": sanitize_logical_id(feature, link_prop) if link_prop else "",
        }
        # activate/deactivate : toujours des boutons (même si l'API expose un paramètre optionnel),
        # pour une UX cohérente (sinon "activer" devient un slider selon les programmes).
        if cname.lower() in ("activate", "deactivate"):
            actions.append({**common, "subType": "other", "param": ""})
        elif len(params) == 0:
            actions.append({**common, "subType": "other", "param": ""})
        elif len(params) == 1:
            pname, pdef = next(iter(params.items()))
            constraints = (pdef or {}).get("constraints", {}) or {}
            if "min" in constraints and "max" in constraints:
                actions.append({**common, "subType": "slider", "param": pname,
                                "min": constraints.get("min"), "max": constraints.get("max"),
                                "step": constraints.get("stepping", 1)})
            elif "enum" in constraints:
                listv = ";".join("%s|%s" % (v, v) for v in constraints["enum"])
                actions.append({**common, "subType": "select", "param": pname, "listValue": listv})
            else:
                logging.getLogger(__name__).debug(
                    "action ignorée %s.%s (param %s sans contraintes exploitables)", feature, cname, pname)
        else:
            logging.getLogger(__name__).debug(
                "action ignorée %s.%s (%d paramètres)", feature, cname, len(params))
    return actions


# Défauts de l'appareil : features « liste » (ignorées par feature_to_commands) synthétisées en
# 3 commandes sur un équipement dédié. Entrée API : {errorCode, priority, timestamp, ...} — pas
# de libellé texte côté Viessmann, d'où code + gravité traduite + date.
ERROR_FEATURES = ("device.messages.errors.raw", "heating.errors.active")
ERROR_GROUP = ("defauts", "Défauts")
PRIORITY_FR = {
    "criticalError": "critique",
    "error": "erreur",
    "warning": "avertissement",
    "info": "information",
    "status": "état",
    "serviceHint": "entretien",
}


def _error_local_time(ts: str) -> str:
    """Horodatage API (ISO UTC, suffixe Z) -> heure locale lisible ; brut si illisible."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return _as_utc(dt).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(ts or "")


def error_entries(feature_entry: dict):
    """Entrées de défaut d'une feature, ou None si la feature n'est pas une liste de défauts."""
    if feature_entry.get("feature") not in ERROR_FEATURES or not feature_entry.get("isEnabled", False):
        return None
    entries = ((feature_entry.get("properties") or {}).get("entries") or {}).get("value")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def error_counter(fmap: dict):
    """Somme des compteurs device.messages.errors.counter.* (modèles sans liste de défauts,
       ex. CU401B_S), ou None si l'appareil n'en expose aucun."""
    total = None
    for name, entry in fmap.items():
        if isinstance(name, str) and name.startswith("device.messages.errors.counter."):
            v = _prop_value(entry, "value")
            if isinstance(v, (int, float)):
                total = (total or 0) + v
    return total


def errors_to_commands(entries, counter=None) -> list:
    """3 commandes info : défaut actif (0/1), nombre de défauts, dernier défaut (texte).

    entries : liste de défauts de l'API (None si l'appareil n'en fournit pas) ; counter : repli
    sur le compteur de défauts quand la liste est absente (code alors inconnu)."""
    last = "Aucun défaut"
    if entries:
        count = len(entries)
        e = max(entries, key=lambda x: str(x.get("timestamp", "")))
        prio = e.get("priority", "")
        last = "%s - %s (%s)" % (e.get("errorCode", "?"), PRIORITY_FR.get(prio, prio or "?"),
                                 _error_local_time(e.get("timestamp")))
    else:
        count = int(counter or 0) if entries is None else 0
        if count:
            last = "%d défaut(s) signalé(s), code non fourni par l'API" % count
    gkey, glabel = ERROR_GROUP
    common = {"cmdType": "info", "unit": "", "genericType": "", "visible": 1,
              "group": gkey, "groupLabel": glabel}
    return [
        {**common, "logicalId": "errors_active", "name": "Défaut actif",
         "subType": "binary", "historized": 1, "value": 1 if count else 0},
        {**common, "logicalId": "errors_count", "name": "Nombre de défauts",
         "subType": "numeric", "historized": 1, "value": count},
        {**common, "logicalId": "errors_last", "name": "Dernier défaut",
         "subType": "string", "historized": 0, "value": last},
    ]


# Thermostat (Homebridge / HomeKit, assistants vocaux) : équipement dédié par circuit, composé de
# commandes synthétiques portant les generic_type THERMOSTAT_* du core. Ces types ne peuvent pas
# être posés à la main (applyCommands réécrit generic_type à chaque cycle) : le démon les émet.
# La consigne pilotée est celle du programme ACTIF : feature « @active » résolue par le démon à
# l'exécution (cf. _thermo_targets), le programme actif changeant au fil du planning.
ACTIVE_SETPOINT_SUFFIX = ".operating.programs.@active"
MODE_FR = {
    "standby": "Arrêt",
    "heating": "Chauffage",
    "dhw": "ECS seule",
    "dhwAndHeating": "ECS et chauffage",
    "cooling": "Rafraîchissement",
    "heatingCooling": "Chauffage et rafraîchissement",
    "dhwAndHeatingCooling": "ECS chauffage et rafraîchissement",
    "forcedReduced": "Réduit forcé",
    "forcedNormal": "Normal forcé",
}


def _prop_value(entry, prop):
    """Valeur d'une propriété d'une feature active, None si absente/inactive."""
    if not entry or not entry.get("isEnabled", False):
        return None
    meta = (entry.get("properties") or {}).get(prop)
    return meta.get("value") if isinstance(meta, dict) else None


def _set_temp_def(entry):
    """(nom_du_commande, nom_param, contraintes) de la consigne d'un programme, ou None."""
    if not entry or not entry.get("isEnabled", False):
        return None
    for cname, cdef in (entry.get("commands") or {}).items():
        if cname.lower() in ("settemperature", "settargettemperature") and isinstance(cdef, dict):
            params = cdef.get("params") or {}
            if len(params) == 1:
                pname, pdef = next(iter(params.items()))
                return cname, pname, (pdef or {}).get("constraints") or {}
    return None


def thermostat_commands(fmap: dict):
    """Commandes des équipements « Thermostat » (un par circuit actif) + cibles de consigne.

    fmap : {nom_feature: entrée_API}. Retourne (commandes, {feature_@active: (feature, action, param)}).
    """
    commands, targets = [], {}
    circuits = sorted({int(m.group(1)) for f in fmap
                       for m in [re.match(r"heating\.circuits\.(\d+)\.operating\.programs\.active$", f)] if m})
    outside = _prop_value(fmap.get("heating.sensors.temperature.outside"), "value")
    for n in circuits:
        base = "heating.circuits.%d" % n
        active_prog = _prop_value(fmap.get(base + ".operating.programs.active"), "value")
        if active_prog is None:
            continue
        gkey = "thermostat_%d" % n
        glabel = "Thermostat" if n == 0 else "Thermostat circuit %d" % n
        common = {"unit": "", "genericType": "", "visible": 1, "group": gkey, "groupLabel": glabel,
                  "historized": 0}

        # Consigne du programme actif ; repli sur « normal » si le programme actif n'a pas de
        # consigne propre (standby, eco dérivé...) — c'est alors la consigne qui s'applique.
        prog_feat = "%s.operating.programs.%s" % (base, active_prog)
        if _set_temp_def(fmap.get(prog_feat)) is None:
            prog_feat = base + ".operating.programs.normal"
        setpoint = _prop_value(fmap.get(prog_feat), "temperature")
        sdef = _set_temp_def(fmap.get(prog_feat))

        # Ambiante : sonde d'ambiance du circuit ; à défaut la consigne (HomeKit exige une
        # température courante, et une PAC sans sonde d'ambiance n'en fournit pas).
        room = _prop_value(fmap.get(base + ".sensors.temperature.room"), "value")
        if room is None:
            room = setpoint
        if room is not None:
            commands.append({**common, "logicalId": "thermo_temperature", "name": "Température ambiante",
                             "cmdType": "info", "subType": "numeric", "unit": "°C", "historized": 1,
                             "genericType": "THERMOSTAT_TEMPERATURE", "value": room})
        if setpoint is not None:
            commands.append({**common, "logicalId": "thermo_setpoint", "name": "Consigne",
                             "cmdType": "info", "subType": "numeric", "unit": "°C", "historized": 1,
                             "genericType": "THERMOSTAT_SETPOINT", "value": setpoint})
        if sdef is not None:
            cname, pname, cons = sdef
            active_feat = base + ACTIVE_SETPOINT_SUFFIX
            targets[active_feat] = (prog_feat, cname, pname)
            commands.append({**common, "logicalId": "thermo_set_setpoint", "name": "Régler consigne",
                             "cmdType": "action", "subType": "slider", "unit": "°C",
                             "genericType": "THERMOSTAT_SET_SETPOINT",
                             "feature": active_feat, "action": cname, "param": pname,
                             "min": cons.get("min", 10), "max": cons.get("max", 30),
                             "step": cons.get("stepping", 0.5), "link": "thermo_setpoint"})
        if outside is not None:
            commands.append({**common, "logicalId": "thermo_outdoor", "name": "Température extérieure",
                             "cmdType": "info", "subType": "numeric", "unit": "°C",
                             "genericType": "THERMOSTAT_TEMPERATURE_OUTDOOR", "value": outside})

        # Mode : info (libellé FR) + un bouton par mode (THERMOSTAT_SET_MODE). Le libellé de
        # l'info est identique au nom du bouton correspondant (mapping des modes Homebridge).
        mode_entry = fmap.get(base + ".operating.modes.active")
        mode = _prop_value(mode_entry, "value")
        if mode is not None:
            commands.append({**common, "logicalId": "thermo_mode", "name": "Mode",
                             "cmdType": "info", "subType": "string",
                             "genericType": "THERMOSTAT_MODE", "value": MODE_FR.get(mode, mode)})
        set_mode = ((mode_entry or {}).get("commands") or {}).get("setMode")
        if isinstance(set_mode, dict):
            params = set_mode.get("params") or {}
            if len(params) == 1:
                pname, pdef = next(iter(params.items()))
                for m in ((pdef or {}).get("constraints") or {}).get("enum", []):
                    commands.append({**common, "logicalId": sanitize_logical_id("thermo_set_mode", m),
                                     "name": clean_name(MODE_FR.get(m, m)), "cmdType": "action",
                                     "subType": "other", "genericType": "THERMOSTAT_SET_MODE",
                                     "feature": base + ".operating.modes.active", "action": "setMode",
                                     "param": pname, "fixedValue": m, "link": ""})

        # État de chauffe : pompe de circulation du circuit en marche.
        pump = _prop_value(fmap.get(base + ".circulation.pump"), "status")
        if pump is not None:
            on = str(pump).lower() == "on"
            commands.append({**common, "logicalId": "thermo_state", "name": "En chauffe",
                             "cmdType": "info", "subType": "binary",
                             "genericType": "THERMOSTAT_STATE", "value": 1 if on else 0})
            commands.append({**common, "logicalId": "thermo_state_name", "name": "État",
                             "cmdType": "info", "subType": "string",
                             "genericType": "THERMOSTAT_STATE_NAME", "value": "Chauffe" if on else "Arrêt"})
    return commands, targets


def finalize_commands(commands: list) -> list:
    """Garantit l'unicité des noms (Jeedom l'impose) et fixe l'ordre d'affichage.

    Tri préalable par logicalId : ni le suffixe de dédup (« Truc 2 ») ni l'ordre ne doivent
    dépendre de l'ordre d'itération de l'API. Sans ça, un simple réordonnancement côté
    Viessmann fait sauter le suffixe d'une commande à l'autre et applyCommands() renomme
    puis réordonne tout l'équipement en base au cycle suivant. Le logicalId dérive du chemin
    de la feature : le tri regroupe naturellement les commandes d'une même feature.
    """
    commands = sorted(commands, key=lambda c: c["logicalId"])
    seen = {}
    for i, c in enumerate(commands):
        base = c["name"]
        n = seen.get(base, 0) + 1
        seen[base] = n
        if n > 1:
            c["name"] = "%s %d" % (base, n)
        c["order"] = i
    return commands


# --------------------------------------------------------------------------- #
#  Configuration (arg supplémentaire --cyclepoll)
# --------------------------------------------------------------------------- #

class JeeConfig(BaseConfig):
    def __init__(self):
        super().__init__()
        # Période de polling viessmann (s), configurable depuis la page plugin.
        self.add_argument("--cyclepoll", help="Période de polling (s)", type=int, default=120)


# --------------------------------------------------------------------------- #
#  Démon
# --------------------------------------------------------------------------- #

class Jee4Viessmann(BaseDaemon):

    def __init__(self) -> None:
        super().__init__(
            config=JeeConfig(),
            on_start_cb=self.on_start,
            on_message_cb=self.on_message,
            on_stop_cb=self.on_stop,
        )
        # Compte unique configuré au niveau du plugin (clientId/user/pwd).
        self._account: Optional[dict] = None
        self._vicare = None           # instance PyViCare (porte tous les devices)
        self._poll_task: Optional[asyncio.Task] = None
        self._paused_until: Optional[datetime] = None  # pause polling jusqu'à reset quota (UTC)
        self._auth_backoff = 0        # backoff progressif sur échecs d'auth
        # Cible réelle de la consigne « programme actif » des thermostats, recalculée à chaque
        # poll : {(gatewaySerial, deviceId): {feature_@active: (feature, action, param)}}.
        self._thermo_targets: dict = {}

    # ------------------------------------------------------------------ #
    #  Cycle de vie
    # ------------------------------------------------------------------ #

    async def on_start(self) -> None:
        if PyViCare is None:
            self._logger.error("PyViCare non installé — voir plugin_info/packages.json")
            await self.stop()
            return
        # jeedomdaemon met le logger racine au niveau demandé : en debug, PyViCare/urllib3
        # déversent chaque payload HTTP (illisible). On muselle ces loggers tiers à WARNING
        # pour ne garder que nos lignes de synthèse.
        for name in ("PyViCare", "urllib3", "authlib", "requests", "asyncio"):
            logging.getLogger(name).setLevel(logging.WARNING)
        _use_local_log_time()  # horodatage des logs en heure locale (cf. _use_local_log_time)
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._logger.info("démon prêt (cyclepoll=%ss)", self._config.cyclepoll)

    async def on_stop(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()

    async def on_message(self, message: dict) -> None:
        mtype = message.get("type")
        if mtype == "config":
            account = message.get("account")
            self._account = account if account and account.get("clientId") else None
            self._vicare = None  # force ré-auth avec les nouveaux identifiants
            self._logger.info("config reçue : compte %s", "défini" if self._account else "absent")
            await self._poll_all()  # poll immédiat après (re)config
            await self._send_heartbeat()
        elif mtype == "action":
            await self._execute_action(message)
        else:
            self._logger.debug("message ignoré : %s", message)

    # ------------------------------------------------------------------ #
    #  Heartbeat (démon -> Jeedom) : état + dernière synchro
    # ------------------------------------------------------------------ #

    def _current_state(self) -> str:
        if not self._account:
            return "no_account"
        if self._paused_until is not None and _utcnow() < self._paused_until:
            return "paused"
        if self._vicare is not None:
            return "ok"
        return "connecting"

    async def _send_heartbeat(self) -> None:
        # Tout est dans le try : le heartbeat est purement informatif, il ne doit sous aucun
        # prétexte remonter une exception dans la boucle de poll (cf. _poll_loop).
        try:
            payload = {
                "type": "heartbeat",
                "state": self._current_state(),
                "ts": _utcnow().isoformat(),
                "cyclepoll": self._config.cyclepoll,
            }
            if self._paused_until is not None:
                payload["pausedUntil"] = self._paused_until.isoformat()
            await self.send_to_jeedom(payload)
        except Exception as e:
            self._logger.debug("heartbeat non envoyé : %s: %s", type(e).__name__, e)

    # ------------------------------------------------------------------ #
    #  PyViCare (bloquant) déporté dans un executor
    # ------------------------------------------------------------------ #

    TOKEN_FILE = "/tmp/jee4viessmann_token.save"

    def _authenticate_blocking(self, account: dict):
        vicare = PyViCare()
        vicare.initWithCredentials(account["user"], account["pwd"], account["clientId"], self.TOKEN_FILE)
        return vicare

    @staticmethod
    def _fetch_blocking(device):
        return device.service.fetch_all_features(device.accessor)

    @staticmethod
    def _device_identity(device) -> dict:
        """Identité stable d'un device PyViCare (pour mapper vers un eqLogic)."""
        accessor = device.accessor
        return {
            "installationId": accessor.id,
            "gatewaySerial": accessor.serial,
            "deviceId": device.getId(),
            "model": device.getModel(),
            "deviceType": device.getDeviceType(),
            "online": device.isOnline(),
        }

    def _handle_rate_limit(self, e) -> bool:
        """Si l'exception est un dépassement de quota Viessmann, met le polling en pause
           jusqu'au reset annoncé par l'API. Retourne True si c'était bien un rate-limit."""
        if type(e).__name__ != "PyViCareRateLimitError":
            return False
        reset = getattr(e, "limitResetDate", None)
        if isinstance(reset, datetime):
            reset = _as_utc(reset)  # PyViCare renvoie un naïf UTC -> on le rend aware
        else:
            reset = _utcnow() + timedelta(hours=1)
        self._paused_until = reset
        self._vicare = None
        self._logger.warning("quota API Viessmann atteint : polling en pause jusqu'à %s UTC", reset.isoformat())
        return True

    # Erreurs *device-level* (la session OAuth reste valide) : ne doivent pas
    # invalider self._vicare ni déclencher de ré-auth (cf. _poll_device).
    _DEVICE_ERROR_NAMES = frozenset({
        "PyViCareDeviceCommunicationError",  # DEVICE_OFFLINE (PAC éteinte / gateway injoignable)
        "PyViCareInternalServerError",       # panne transitoire côté Viessmann
        "PyViCareNotSupportedFeatureError",
    })

    @classmethod
    def _is_device_error(cls, e) -> bool:
        return type(e).__name__ in cls._DEVICE_ERROR_NAMES

    @staticmethod
    def _is_device_offline(e) -> bool:
        """DEVICE_OFFLINE : état opérationnel attendu (PAC/appareil éteint), pas une erreur."""
        return type(e).__name__ == "PyViCareDeviceCommunicationError" and "DEVICE_OFFLINE" in str(e)

    def _is_paused(self) -> bool:
        if self._paused_until is None:
            return False
        if _utcnow() >= self._paused_until:
            self._logger.info("fin de pause quota, reprise du polling")
            self._paused_until = None
            return False
        return True

    async def _ensure_session(self):
        if self._vicare is not None:
            return self._vicare
        if not self._account:
            return None
        loop = asyncio.get_running_loop()
        try:
            vicare = await loop.run_in_executor(None, self._authenticate_blocking, self._account)
        except Exception as e:
            if self._handle_rate_limit(e):
                return None
            self._auth_backoff = min(self._auth_backoff + 1, 6)
            self._logger.error("échec authentification (#%d) : %s: %s", self._auth_backoff, type(e).__name__, e)
            # PyViCareInvalidCredentialsError est levée sans message quand l'IAM ne renvoie pas
            # de redirect 'Location' : soit identifiants erronés, soit (le plus souvent en migration
            # depuis le plugin v1) la redirect_uri du client n'est pas celle attendue par PyViCare.
            if type(e).__name__ == "PyViCareInvalidCredentialsError":
                self._logger.error(
                    "vérifiez email/mot de passe ET que le client_id du Viessmann Developer Portal "
                    "autorise la redirect_uri 'vicare://oauth-callback/everest' (le plugin v1 utilisait "
                    "'http://localhost:4200/', incompatible avec PyViCare).")
            self._logger.debug("trace auth :\n%s", traceback.format_exc())
            # Backoff : repousse la prochaine tentative (multiples du cyclepoll).
            self._paused_until = _utcnow() + timedelta(seconds=self._config.cyclepoll * self._auth_backoff)
            return None
        self._auth_backoff = 0
        if not getattr(vicare, "devices", None):
            self._logger.warning("aucun device supporté découvert")
            return None
        self._vicare = vicare
        self._logger.info("authentifié, %d device(s) supporté(s)", len(vicare.devices))
        return vicare

    async def _poll_all(self) -> None:
        if self._is_paused():
            self._logger.debug("polling en pause (quota/backoff) jusqu'à %s UTC", self._paused_until.isoformat())
            return
        vicare = await self._ensure_session()
        if vicare is None:
            return
        for device in vicare.devices:
            await self._poll_device(device)

    async def _poll_device(self, device) -> None:
        """Récupère les features d'un device, génère les commandes et les pousse à Jeedom."""
        ident = self._device_identity(device)
        loop = asyncio.get_running_loop()
        try:
            features = await loop.run_in_executor(None, self._fetch_blocking, device)
        except Exception as e:
            if self._handle_rate_limit(e):
                return
            # Erreur device-level (PAC éteinte, gateway injoignable, panne serveur côté
            # Viessmann) : la session OAuth reste valide. On saute juste ce device pour ce
            # cycle — surtout PAS de self._vicare = None, qui forcerait une ré-auth à CHAQUE
            # cycle et cramerait le quota API. Seules les erreurs d'auth invalident la session.
            if self._is_device_offline(e):
                # Appareil simplement éteint : état attendu, on n'inonde pas le log.
                self._logger.info("device %s : hors-ligne, on réessaie au prochain cycle",
                                  ident["deviceId"])
                return
            if self._is_device_error(e):
                self._logger.warning(
                    "device %s : injoignable (%s: %s), on réessaie au prochain cycle",
                    ident["deviceId"], type(e).__name__, e)
                self._logger.debug("trace fetch :\n%s", traceback.format_exc())
                return
            self._logger.warning(
                "device %s : échec fetch_all_features (%s: %s), ré-auth au prochain cycle",
                ident["deviceId"], type(e).__name__, e)
            self._logger.debug("trace fetch :\n%s", traceback.format_exc())
            self._vicare = None
            return
        commands = []
        errors = None  # None = l'appareil n'expose aucune feature de défauts
        for entry in features.get("data", []):
            commands.extend(feature_to_commands(entry))
            commands.extend(feature_to_actions(entry))
            found = error_entries(entry)
            if found is not None:
                errors = (errors or []) + found
        fmap = {e.get("feature"): e for e in features.get("data", []) if isinstance(e, dict)}
        # Équipement « Défauts » toujours créé pour un générateur (features heating.*), même si
        # l'API ne fournit pas la liste des défauts : les scénarios/alertes de l'utilisateur ne
        # doivent pas dépendre du modèle. Pas pour la gateway ni les accessoires (RoomControl...).
        counter = error_counter(fmap)
        if errors is not None or counter is not None \
                or any(isinstance(f, str) and f.startswith("heating.") for f in fmap):
            commands.extend(errors_to_commands(errors, counter))
        thermo, targets = thermostat_commands(fmap)
        commands.extend(thermo)
        dkey = (ident["gatewaySerial"], ident["deviceId"])
        self._thermo_targets[dkey] = targets
        if not commands:
            self._logger.debug("device %s (%s) : aucune feature active, ignoré",
                               ident["deviceId"], ident["model"])
            return
        # Regroupe par sous-système -> un eqLogic Jeedom par (device, groupe).
        groups = {}
        for c in commands:
            groups.setdefault((c["group"], c["groupLabel"]), []).append(c)
        self._logger.info("device %s (%s) : %d commandes actives, %d groupe(s)",
                          ident["deviceId"], ident["model"], len(commands), len(groups))
        for (gkey, glabel), gcmds in groups.items():
            gcmds = finalize_commands(gcmds)  # noms uniques au sein de l'eqLogic
            self._logger.info("  [%s] %s : %d commande(s)", gkey, glabel, len(gcmds))
            for c in gcmds:
                if c.get("cmdType") == "action":
                    self._logger.info("    ⚙ %s [%s %s]", c["name"], c["subType"], c.get("action", ""))
                else:
                    vis = "" if c.get("visible") else " (masqué)"
                    self._logger.info("    • %s = %s %s%s", c["name"], c.get("value", ""), c.get("unit", ""), vis)
            await self.send_to_jeedom({
                "device": ident,
                "group": gkey,
                "groupLabel": glabel,
                "commands": gcmds,
            })

    async def _poll_loop(self) -> None:
        try:
            await self._send_heartbeat()  # battement initial
            while True:
                await asyncio.sleep(self._config.cyclepoll)
                # Le try couvre TOUT le corps du cycle (poll *et* heartbeat) : une exception
                # échappée ici sortirait du while et arrêterait le polling définitivement —
                # démon toujours vivant et répondant au socket, mais plus aucune donnée,
                # panne silencieuse jusqu'au prochain redémarrage.
                try:
                    await self._poll_all()
                    await self._send_heartbeat()  # à chaque cycle, même en pause quota
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Le démon ne doit jamais mourir sur une erreur de cycle.
                    self._logger.error("erreur cycle de polling : %s: %s", type(e).__name__, e)
                    self._logger.debug("trace cycle :\n%s", traceback.format_exc())
        except asyncio.CancelledError:
            self._logger.info("boucle de poll arrêtée")

    # ------------------------------------------------------------------ #
    #  Actions (PHP -> démon)
    # ------------------------------------------------------------------ #

    def _find_device(self, vicare, ident: dict):
        """Retrouve le device PyViCare correspondant à l'identité reçue."""
        if not ident:
            return None
        for device in vicare.devices:
            if device.getId() == ident.get("deviceId") \
                    and device.accessor.serial == ident.get("gatewaySerial"):
                return device
        return None

    @staticmethod
    def _coerce(value):
        """Tente de convertir une valeur slider/select en nombre, sinon laisse tel quel."""
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                f = float(value)
                return int(f) if f.is_integer() else f
            except (TypeError, ValueError):
                return value
        return value

    def _set_property_blocking(self, device, feature, action, data):
        return device.service.setProperty(device.accessor, feature, action, data)

    async def _execute_action(self, message: dict) -> None:
        ident = message.get("device", {})
        feature = message.get("feature")
        action = message.get("action")
        param = message.get("param") or ""
        value = message.get("value")
        if not feature or not action:
            self._logger.warning("action incomplète : feature/action manquant (%s)", message)
            return
        if self._is_paused():
            self._logger.warning("action %s.%s ignorée : quota API en pause jusqu'à %s UTC",
                                 feature, action, self._paused_until.isoformat())
            return
        vicare = await self._ensure_session()
        if vicare is None:
            self._logger.warning("action %s.%s : pas de session active", feature, action)
            return
        device = self._find_device(vicare, ident)
        if device is None:
            self._logger.warning("action %s.%s : device %s introuvable", feature, action, ident.get("deviceId"))
            return
        if feature.endswith(ACTIVE_SETPOINT_SUFFIX):
            # Consigne thermostat : programme actif connu au dernier poll.
            target = self._thermo_targets.get((ident.get("gatewaySerial"), ident.get("deviceId")), {}).get(feature)
            if target is None:
                self._logger.warning("action %s : programme actif inconnu (pas encore de poll ?)", feature)
                return
            feature, action, param = target
        data = {param: self._coerce(value)} if param else {}
        self._logger.info("action -> %s.%s %s", feature, action, data)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._set_property_blocking, device, feature, action, data)
        except Exception as e:
            if self._handle_rate_limit(e):
                return
            self._logger.error("échec action %s.%s : %s: %s", feature, action, type(e).__name__, e)
            self._logger.debug("trace action :\n%s", traceback.format_exc())
            return
        self._logger.info("action %s.%s OK, rafraîchissement", feature, action)
        # Rafraîchit uniquement le device concerné (économie de quota) ; l'API peut être
        # asynchrone : la valeur définitive arrivera au cycle suivant si "pending".
        await self._poll_device(device)


if __name__ == "__main__":
    Jee4Viessmann().run()
