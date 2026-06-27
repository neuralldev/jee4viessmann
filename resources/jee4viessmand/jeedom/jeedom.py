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
#
# Helper Jeedom minimal, Python 3 (testé 3.11+ / 3.13).
# Volontairement réduit à ce dont le démon a besoin : callback HTTP, socket TCP, utils.
# (Pas de dépendance serial/pyudev contrairement au template historique.)

import logging
import os
import queue
import threading

import requests
from socketserver import TCPServer, StreamRequestHandler


# File d'attente des messages reçus par le socket (PHP -> démon).
SOCKET_QUEUE = queue.Queue()


class jeedom_com:
    """Envoi de changements vers Jeedom via le callback HTTP (?apikey=...)."""

    def __init__(self, apikey='', url='', cycle=0.5):
        self.apikey = apikey
        self.url = url
        self.cycle = cycle

    def send_change_immediate(self, change):
        try:
            r = requests.post(self.url + '?apikey=' + self.apikey, json=change, timeout=(2, 30))
            if r.status_code != requests.codes.ok:
                logging.error('Echec envoi vers Jeedom (HTTP %s)', r.status_code)
                return False
            return True
        except Exception as e:
            logging.error('Echec envoi vers Jeedom : %s', str(e))
            return False

    def test(self):
        try:
            r = requests.get(self.url + '?apikey=' + self.apikey, timeout=(2, 10))
            return r.status_code == requests.codes.ok
        except Exception as e:
            logging.error('Callback injoignable : %s', str(e))
            return False


class jeedom_utils:

    @staticmethod
    def set_log_level(level='error'):
        levels = {
            'debug': logging.DEBUG, '100': logging.DEBUG,
            'info': logging.INFO, '200': logging.INFO,
            'warning': logging.WARNING, '300': logging.WARNING,
            'error': logging.ERROR, '400': logging.ERROR,
        }
        logging.basicConfig(
            level=levels.get(str(level).lower(), logging.ERROR),
            format='%(asctime)s %(levelname)s : %(message)s',
        )

    @staticmethod
    def write_pid(path):
        with open(path, 'w') as f:
            f.write(str(os.getpid()))


class _JeedomSocketHandler(StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline()
            if line:
                SOCKET_QUEUE.put(line.decode('utf-8', errors='replace').strip())
        except Exception as e:
            logging.error('Erreur handler socket : %s', str(e))


class jeedom_socket:
    """Serveur socket TCP local recevant les messages de Jeedom (PHP -> démon)."""

    def __init__(self, address='127.0.0.1', port=55000):
        self.address = address
        self.port = port
        self.server = None
        TCPServer.allow_reuse_address = True

    def open(self):
        self.server = TCPServer((self.address, self.port), _JeedomSocketHandler)
        t = threading.Thread(target=self.server.serve_forever)
        t.daemon = True
        t.start()
        logging.debug('Socket à l\'écoute sur %s:%d', self.address, self.port)

    def close(self):
        if self.server is not None:
            self.server.shutdown()

    def get_message(self):
        """Retourne le prochain message (str) ou None si la file est vide."""
        if SOCKET_QUEUE.empty():
            return None
        return SOCKET_QUEUE.get()
