# Architecture Jee4Viessman

```
                 callback HTTP (apikey)            ┌────────────────────────┐
   ┌─────────┐   POST {eqLogicId, commands[]}      │  Démon Python 3 (venv) │
   │  Jeedom  │ <────────────────────────────────  │  jee4viessmand.py      │
   │  (PHP)   │                                     │  + PyViCare            │
   │          │   socket TCP 127.0.0.1:55070        │                        │
   │  classe  │ ──────────────────────────────────>│  - auth OAuth/PKCE      │
   │ eqLogic  │   {type:config|action, ...}         │  - découverte device    │
   └─────────┘                                      │  - polling features     │
                                                    │  - typage -> commandes  │
                                                    └────────────────────────┘
```

## Flux

1. **Config** : `postSave()` / `deamon_start()` envoient au démon la liste des équipements
   et leurs identifiants déchiffrés (`syncDaemonConfig` → socket).
2. **Polling** : le démon interroge l'API toutes les `cyclePoll` secondes, transforme chaque
   *property* typée en commande (`feature_to_commands`) et POST le tout au callback PHP.
3. **Réception** : `core/php/jee4viessman.php` valide l'apikey et appelle `jee4viessman::pushData()`
   qui crée les commandes manquantes (depuis le typage) puis met à jour les valeurs.
4. **Action** : `jee4viessmanCmd::execute()` envoie l'action au démon par socket ; le démon
   appelle le setter PyViCare correspondant.

## Ce qui reste en PHP (incompressible)

- Classes `eqLogic` / `cmd` (cycle de vie, historique, widgets, `execute()`).
- Hooks démon (`deamon_info/start/stop`) et dépendances (`dependancy_*`).
- Page de configuration (identifiants uniquement) + callback de réception.

## Points POC à finaliser avant production

- `execute_action()` : implémenter l'aiguillage vers les setters PyViCare (setMode, setTargetTemperature…).
- Création des **commandes d'action** depuis les `commands`/`constraints` de l'API (boutons enum, sliders).
- Robustesse : reconnexion, backoff, gestion fine des erreurs HTTP/quota.
- Sécurité socket : restreindre/authentifier les messages (actuellement localhost only).
- Mapping optionnel de **noms lisibles** et d'`logicalId` stables si l'on veut une UX soignée.
```
