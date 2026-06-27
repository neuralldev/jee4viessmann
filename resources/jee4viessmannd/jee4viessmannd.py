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
import re
import traceback
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


def sanitize_logical_id(feature: str, prop: str) -> str:
    raw = (feature + "." + prop).lower()
    return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")


def feature_to_commands(feature_entry: dict) -> list:
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

    # ------------------------------------------------------------------ #
    #  Cycle de vie
    # ------------------------------------------------------------------ #

    async def on_start(self) -> None:
        if PyViCare is None:
            self._logger.error("PyViCare non installé — voir plugin_info/packages.json")
            await self.stop()
            return
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

    async def _ensure_session(self):
        if self._vicare is not None:
            return self._vicare
        if not self._account:
            return None
        loop = asyncio.get_running_loop()
        try:
            vicare = await loop.run_in_executor(None, self._authenticate_blocking, self._account)
        except Exception as e:
            self._logger.error("échec authentification : %s: %s", type(e).__name__, e)
            # PyViCareInvalidCredentialsError est levée sans message quand l'IAM ne renvoie pas
            # de redirect 'Location' : soit identifiants erronés, soit (le plus souvent en migration
            # depuis le plugin v1) la redirect_uri du client n'est pas celle attendue par PyViCare.
            if type(e).__name__ == "PyViCareInvalidCredentialsError":
                self._logger.error(
                    "vérifiez email/mot de passe ET que le client_id du Viessmann Developer Portal "
                    "autorise la redirect_uri 'vicare://oauth-callback/everest' (le plugin v1 utilisait "
                    "'http://localhost:4200/', incompatible avec PyViCare).")
            self._logger.debug("trace auth :\n%s", traceback.format_exc())
            return None
        if not getattr(vicare, "devices", None):
            self._logger.warning("aucun device supporté découvert")
            return None
        self._vicare = vicare
        self._logger.info("authentifié, %d device(s) supporté(s)", len(vicare.devices))
        return vicare

    async def _poll_all(self) -> None:
        vicare = await self._ensure_session()
        if vicare is None:
            return
        loop = asyncio.get_running_loop()
        for device in vicare.devices:
            ident = self._device_identity(device)
            try:
                features = await loop.run_in_executor(None, self._fetch_blocking, device)
            except Exception as e:
                self._logger.warning(
                    "device %s : échec fetch_all_features (%s: %s), ré-auth au prochain cycle",
                    ident["deviceId"], type(e).__name__, e)
                self._logger.debug("trace fetch :\n%s", traceback.format_exc())
                self._vicare = None
                return
            commands = []
            for entry in features.get("data", []):
                commands.extend(feature_to_commands(entry))
            # Seuls les devices avec au moins une feature active génèrent un objet/commandes.
            if commands:
                self._logger.debug("device %s (%s) : %d commandes poussées",
                                   ident["deviceId"], ident["model"], len(commands))
                await self.send_to_jeedom({"device": ident, "commands": commands})
            else:
                self._logger.debug("device %s (%s) : aucune feature active, ignoré",
                                   ident["deviceId"], ident["model"])

    async def _poll_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.cyclepoll)
                await self._poll_all()
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

    async def _execute_action(self, message: dict) -> None:
        logical_id = message.get("logicalId")
        options = message.get("options", {})
        ident = message.get("device", {})
        if self._vicare is None:
            self._logger.warning("action %s : pas de session active", logical_id)
            return
        device = self._find_device(self._vicare, ident)
        if device is None:
            self._logger.warning("action %s : device %s introuvable", logical_id, ident.get("deviceId"))
            return
        # TODO : aiguiller vers les setters PyViCare (setMode, setTargetTemperature, ...).
        self._logger.info("action reçue (à implémenter) : device=%s id=%s options=%s",
                          ident.get("deviceId"), logical_id, options)


if __name__ == "__main__":
    Jee4Viessmann().run()
