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
   ^   {type:config,account}                        │ auth OAuth/PKCE (compte unique),
   │   {type:action,device}                         │ découverte multi-device, polling
   │                                                 │ features, typage->commandes
   └──callback HTTP ?apikey  POST {device,commands[]}──┘
```
**Compte unique** : les identifiants (clientId/email/password) sont au niveau **config plugin**
(`config::byKey`, password chiffré), pas par eqLogic. Chaque **device** Viessmann ayant des features
actives devient un eqLogic auto-créé (`isDevice=1`, clé `gatewaySerial_deviceId`). Détail dans `ARCHITECTURE.md`.

> **Méthode démon/venv/deps calquée sur le plugin `jee4lm5`** : dépendances via le gestionnaire
> natif Jeedom (`plugin_info/packages.json` → venv `resources/python_venv`), démon basé sur le
> package pip **`jeedomdaemon`** (`BaseDaemon`). Plus de `install.sh`, plus de helper `jeedom.py` maison.
> Tout le nommage est en **double-n** `jee4viessmann` (= id info.json, classe, repo GitHub, URLs callback).

## Carte des fichiers
- `plugin_info/info.json` — id `jee4viessmann`, `require 4.6`, `hasOwnDeamon`, `hasDependency`, `maxDependancyInstallTime 60`.
- `plugin_info/packages.json` — **dépendances** gérées nativement par Jeedom : `apt python3-pip` + `pip3`
  (`jeedomdaemon`, `PyViCare`, `authlib`, `deprecated`, `requests` + transitifs). Jeedom crée et peuple
  `resources/python_venv` tout seul.
- `plugin_info/install.php` — hooks install/update/remove (relance démon).
- `core/class/jee4viessmann.class.php` — **cœur PHP** :
  - constantes `PLUGINNAME='jee4viessmann'`, `JEEDOM_DAEMON_PORT=55070`.
  - démon : `deamon_info` (pid + `posix_getsid`), `deamon_start` (venv `python_venv`, `fuser -k`, args
    BaseConfig), `deamon_stop` (SIGTERM puis SIGKILL), `backupExclude` (exclut `resources/python_venv`).
    Pas de `dependancy_info/install` (gérés par `packages.json`).
  - identifiants : `saveCredentials()` (chiffre+stocke en config plugin), `detect()` (re-sync), `syncDaemonConfig()`
    (envoie le compte unique `{type:config,account}` au démon).
  - démon→PHP : `pushData({device,commands})` → `findOrCreateDeviceEq()` (eqLogic par device) puis `applyCommands()`.
  - `jee4viessmannCmd::execute()` → `sendToDaemon({type:action,device})` (device depuis la config de l'eqLogic).
- `core/ajax/jee4viessmann.ajax.php` — actions `login` (enregistre identifiants) et `sync` (détection).
- `core/php/jee4viessmann.php` — callback HTTP : valide apikey, ACK `?test=1` (exigé par jeedomdaemon) → `pushData()`.
- `desktop/modal/login.php` — modal de connexion (clientId + email + password).
- `plugin_info/configuration.php` — boutons « Se connecter » (modal) / « Détecter » + log level + cyclePoll.
- `desktop/php/jee4viessmann.php` — liste des équipements (devices auto-créés), plus de saisie identifiants.
- `desktop/js/jee4viessmann.js` — rendu d'une ligne de commande.
- `resources/jee4viessmannd/jee4viessmannd.py` — **démon** : hérite de `jeedomdaemon.BaseDaemon`
  (`on_start/on_message/on_stop`, `send_to_jeedom`, `run()`). PyViCare (bloquant) déporté via
  `run_in_executor`. Config étendue `JeeConfig(BaseConfig)` pour l'arg `--cyclepoll`.
- `resources/requirements-----.txt` — **référence uniquement** (non utilisé à l'install, deps via packages.json).

## Conventions
- Classe eqLogic = `jee4viessmann`, classe cmd = `jee4viessmannCmd` (doit matcher l'id du plugin, double-n).
- Identifiants sensibles chiffrés via `utils::encrypt`/`decrypt` (jamais en clair en base).
- `logicalId` des commandes auto = `sanitize_logical_id(feature, property)` (a-z0-9 + `_`).
- Mapping typage : `number→numeric`, `boolean→binary` (valeur 0/1), `string→string`. array/Schedule ignorés (POC).
- Tout message PHP→démon sur le socket porte `apikey` (vérifié par `BaseDaemon`).

## Commandes utiles
```bash
# Lint / compile (depuis la racine du plugin)
php -l core/class/jee4viessmann.class.php
python3 -m py_compile resources/jee4viessmannd/jee4viessmannd.py

# Démon en local (debug, hors Jeedom) — nécessite jeedomdaemon + PyViCare dans le python utilisé
resources/python_venv/bin/python3 resources/jee4viessmannd/jee4viessmannd.py --loglevel debug \
  --socketport 55070 --apikey TEST --cyclepoll 30 --pid /tmp/jee4viessmannd.pid \
  --callback "http://127.0.0.1/plugins/jee4viessmann/core/php/jee4viessmann.php"
```
Dans Jeedom (conteneur) : installer les dépendances (onglet santé/dépendances — Jeedom lit `packages.json`
et crée `resources/python_venv`), activer le plugin, créer un équipement avec les identifiants.
Logs : `jee4viessmann` (PHP) et `jee4viessmannd` (démon).

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
- PyViCare gère lui-même PKCE/refresh : le champ `codeChallenge` du plugin v1 n'est plus nécessaire
  (v1 réutilisait `codeChallenge` comme `code_challenge` ET `code_verifier` = PKCE « plain » manuel).
- **redirect_uri** : `initWithCredentials` (PyViCareOAuthManager) impose `vicare://oauth-callback/everest`.
  Le client_id du Developer Portal doit autoriser cette URI. Le plugin v1 utilisait `http://localhost:4200/`
  → réutiliser tel quel ce client provoque un échec d'auth **sans message** (`PyViCareInvalidCredentialsError`,
  levée quand l'IAM ne renvoie pas de header `Location`). Mêmes symptômes si email/mot de passe erronés.
- Le `client_id` doit venir du Viessmann Developer Portal ; auth par mot de passe en voie de dépréciation
  côté Viessmann (risque externe suivi par PyViCare).
```
