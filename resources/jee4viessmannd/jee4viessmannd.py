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
import traceback
from datetime import datetime, timedelta
from typing import Optional

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
    """Visibilité par défaut : on masque le bruit (santé capteur, config, séries, flags)."""
    f = feature.lower()
    # Santé capteur (connected/notConnected) : créée mais masquée.
    if prop == "status" and str(value) in ("connected", "notConnected"):
        return 0
    # Identifiants/série, configuration, contrôleur, infos device brutes : masqués.
    if "serial" in f or "configuration" in f or "controller" in f or f.startswith("device"):
        return 0
    if "useapproved" in f or "mainecu" in f:
        return 0
    # Flags binaires par mode/programme : on garde seulement le résumé '...active' (value).
    if ("operating.modes" in f or "operating.programs" in f) and prop == "active":
        return 0
    # Flags hydrauliques internes du RoomControl : peu utiles.
    if f.startswith("rooms.features"):
        return 0
    if prop in ("name", "demand"):
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
        group_key, group_label = classify(feature)
        commands.append({
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
        })
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
        common = {
            "logicalId": sanitize_logical_id(feature, cname),
            "name": action_name(feature, cname),
            "cmdType": "action",
            "visible": 1,
            "group": group_key,
            "groupLabel": group_label,
            "feature": feature,
            "action": cname,
        }
        if len(params) == 0:
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


def finalize_commands(commands: list) -> list:
    """Garantit l'unicité des noms (Jeedom l'impose) et fixe l'ordre d'affichage."""
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
        elif mtype == "action":
            await self._execute_action(message)
        else:
            self._logger.debug("message ignoré : %s", message)

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
        return device.service.fetch_all_features()

    @staticmethod
    def _device_identity(device) -> dict:
        """Identité stable d'un device PyViCare (pour mapper vers un eqLogic)."""
        accessor = device.service.accessor
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
        if not isinstance(reset, datetime):
            reset = datetime.utcnow() + timedelta(hours=1)
        self._paused_until = reset
        self._vicare = None
        self._logger.warning("quota API Viessmann atteint : polling en pause jusqu'à %s UTC", reset.isoformat())
        return True

    def _is_paused(self) -> bool:
        if self._paused_until is None:
            return False
        if datetime.utcnow() >= self._paused_until:
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
            self._paused_until = datetime.utcnow() + timedelta(seconds=self._config.cyclepoll * self._auth_backoff)
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
            self._logger.warning(
                "device %s : échec fetch_all_features (%s: %s), ré-auth au prochain cycle",
                ident["deviceId"], type(e).__name__, e)
            self._logger.debug("trace fetch :\n%s", traceback.format_exc())
            self._vicare = None
            return
        commands = []
        for entry in features.get("data", []):
            commands.extend(feature_to_commands(entry))
            commands.extend(feature_to_actions(entry))
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
            while True:
                await asyncio.sleep(self._config.cyclepoll)
                try:
                    await self._poll_all()
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
                    and device.service.accessor.serial == ident.get("gatewaySerial"):
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
        return device.service.setProperty(feature, action, data)

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
