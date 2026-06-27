# CLAUDE.md — jee4viessman

Plugin **Jeedom (>= 4.6)** pour chaudières/PAC Viessmann, réécriture nouvelle génération du plugin
historique `viessmannIot` (tout-PHP, cron + cURL). Ici : **démon Python 3 + PyViCare**, couche PHP minimale.

## Objectif de l'architecture
- Sortir l'I/O Viessmann du PHP (plus de cURL bloquant, plus de cron à la minute).
- Déléguer auth/découverte/typage à **PyViCare** (la lib de référence, utilisée par Home Assistant).
- **Générer les commandes depuis le typage de l'API** — pas de mapping manuel (le plugin v1 avait ~3100 lignes de boilerplate).

## Schéma
```
Jeedom (PHP)  ──socket TCP 127.0.0.1:55070──>  Démon Python (PyViCare)
   ^   {type:config|action}                         │ auth OAuth/PKCE, découverte device,
   │                                                 │ polling features, typage->commandes
   └──callback HTTP ?apikey  POST {eqLogicId,commands[]}──┘
```
Détail complet dans `ARCHITECTURE.md`.

## Carte des fichiers
- `plugin_info/info.json` — id `jee4viessman`, `require 4.6`, `hasOwnDeamon`, `hasDependency`.
- `plugin_info/install.php` — hooks install/update/remove (relance démon).
- `core/class/jee4viessman.class.php` — **cœur PHP** :
  - dépendances : `dependancy_info` / `dependancy_install` (venv via `resources/install.sh`).
  - démon : `deamon_info` (pid + `posix_kill`), `deamon_start` (lance le venv python + args), `deamon_stop`.
  - PHP→démon : `sendToDaemon()` (socket), `syncDaemonConfig()` (envoie identifiants déchiffrés).
  - démon→PHP : `pushData()` crée les commandes manquantes **depuis le typage reçu** puis `checkAndUpdateCmd`.
  - sécurité : `preSave()` chiffre `password` (`utils::encrypt`, idempotent préfixe `crypt:`).
  - `jee4viessmanCmd::execute()` → `sendToDaemon({type:action})`.
- `core/php/jee4viessman.php` — callback HTTP : valide apikey (`jeedom::apiAccess`) → `pushData()`.
- `desktop/php/jee4viessman.php` — page config réduite (clientId, email, password) + onglet commandes auto.
- `desktop/js/jee4viessman.js` — rendu d'une ligne de commande.
- `resources/jee4viessmand/jee4viessmand.py` — **démon** (argparse, signal, socket, poll, PyViCare).
- `resources/jee4viessmand/jeedom/jeedom.py` — **helper Python 3 maison** (jeedom_com HTTP, jeedom_socket TCP, jeedom_utils). ⚠️ NE PAS remplacer par le template historique : il est en Python 2.
- `resources/requirements.txt` — `PyViCare`, `requests`.

## Conventions
- Classe eqLogic = `jee4viessman`, classe cmd = `jee4viessmanCmd` (doit matcher l'id du plugin).
- Identifiants sensibles chiffrés via `utils::encrypt`/`decrypt` (jamais en clair en base).
- `logicalId` des commandes auto = `sanitize_logical_id(feature, property)` (a-z0-9 + `_`).
- Mapping typage : `number→numeric`, `boolean→binary` (valeur 0/1), `string→string`. array/Schedule ignorés (POC).

## Commandes utiles
```bash
# Lint / compile (depuis la racine du plugin)
php -l core/class/jee4viessman.class.php
python3 -m py_compile resources/jee4viessmand/jee4viessmand.py resources/jee4viessmand/jeedom/jeedom.py

# Démon en local (debug, hors Jeedom)
resources/venv/bin/python3 resources/jee4viessmand/jee4viessmand.py --loglevel debug --socketport 55070 \
  --callback "http://127.0.0.1/plugins/jee4viessman/core/php/jee4viessman.php" --apikey TEST --cyclepoll 30
```
Dans Jeedom (conteneur) : installer les dépendances (onglet santé/dépendances), activer le plugin, créer
un équipement avec les identifiants. Logs : `jee4viessman` (PHP) et `jee4viessmand` (démon).

## État (POC compilable, NON testé sur device réel)
Fait : structure plugin, plomberie PHP↔Python, auth/découverte PyViCare, polling + génération des
commandes **info** depuis le typage, chiffrement identifiants.

## TODO (par priorité)
1. `execute_action()` (démon) : aiguiller vers les setters PyViCare (setMode, setTargetTemperature…).
2. Générer les commandes **d'action** (boutons enum, sliders) depuis `commands`/`constraints` de l'API.
3. Robustesse démon : reconnexion/backoff, gestion HTTP 4xx/quota, heartbeat vers Jeedom.
4. Sécurité socket (actuellement localhost sans auth) ; envisager apikey aussi sur le socket.
5. UX : noms lisibles / `logicalId` stables si besoin (sinon technique brut). array/Schedule.
6. Tests : démarrage démon en conteneur sans device, puis avec compte Viessmann réel.

## Gotchas
- Le helper `jeedom.py` du template Jeedom historique est **Python 2** (`from Queue import Queue`,
  `import SocketServer`, `serial`, `pyudev`) → inutilisable en 3.13. Ici réécrit en Python 3.
- `deamon_start` attend que le socket soit ouvert avant `syncDaemonConfig()`.
- PyViCare gère lui-même PKCE/refresh : le champ `codeChallenge` du plugin v1 n'est plus nécessaire.
- Le `client_id` doit venir du Viessmann Developer Portal ; auth par mot de passe en voie de dépréciation
  côté Viessmann (risque externe suivi par PyViCare).
```
