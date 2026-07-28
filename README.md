# jee4viessmann

Plugin [Jeedom](https://jeedom.com) (>= 4.6) pour chaudières et pompes à chaleur **Viessmann**,
reposant sur un démon **Python 3** et la bibliothèque [PyViCare](https://github.com/openviess/PyViCare).

Réécriture nouvelle génération du plugin historique `viessmannIot` (tout-PHP, cron + cURL).

## Principe

L'objectif est de sortir l'I/O Viessmann du PHP et de **ne plus écrire un seul mapping de commande
à la main** : le démon lit le typage exposé par l'API et en déduit les commandes Jeedom.

- **Authentification** OAuth2 / PKCE et refresh token : délégués à PyViCare.
- **Découverte** automatique de l'installation, des passerelles et des appareils.
- **Génération des commandes** depuis le typage des *features* : `number → numeric`,
  `boolean → binary`, `string → string`. Les commandes d'action (slider / liste / bouton) sont
  déduites du bloc `commands` de l'API.
- La couche PHP se limite au cycle de vie Jeedom, à la supervision du démon et à la passerelle
  entre les deux.

```
Jeedom (PHP)  ──socket TCP 127.0.0.1:55070──>  Démon Python (PyViCare)
   ^   {type:config,account}                        │ auth OAuth/PKCE (compte unique),
   │   {type:action,device}                         │ découverte multi-device, polling
   │                                                │ features, typage -> commandes
   └──callback HTTP ?apikey  POST {device,commands[]}──┘
```

Le socket n'écoute que sur `127.0.0.1` et chaque message porte une apikey ; le callback HTTP est
validé par `jeedom::apiAccess`.

## Installation

1. Installer le plugin, puis lancer l'installation des dépendances depuis l'onglet **Santé →
   Dépendances**. Jeedom lit `plugin_info/packages.json` et crée le venv `resources/python_venv`
   (`jeedomdaemon`, `PyViCare`).
2. Activer le plugin.
3. Dans la configuration du plugin, cliquer sur **Se connecter** et renseigner :
   - **Id Client** obtenu sur le [Viessmann Developer Portal](https://app.developer.viessmann.com/) ;
   - **Email** et **Mot de passe** du compte Viessmann.
4. Cliquer sur **Détecter**. Chaque appareil Viessmann exposant des features actives devient un
   équipement Jeedom, créé automatiquement ; ses commandes apparaissent après le premier cycle.

> **Important — redirect_uri.** Le client déclaré sur le Developer Portal doit autoriser
> `vicare://oauth-callback/everest`. Un client configuré pour `http://localhost:4200/` (comme dans
> le plugin v1) échoue à l'authentification **sans message explicite**.

Les identifiants sont stockés au niveau de la configuration du plugin (compte unique, pas par
équipement) et chiffrés via `utils::encrypt`.

## Fonctionnement au quotidien

- Les équipements sont regroupés en sous-ensembles par sous-système, avec des libellés en français.
- Deux widgets sont fournis : un **thermomètre** (jauge verticale) auto-assigné aux températures du
  ballon tampon, et une **carte circuit de chauffage** (cadran de consigne, programme, mode, départ,
  pompe).
- Le démon poste un **heartbeat** à chaque cycle : l'état et la date du dernier contact sont
  affichés dans la page de configuration du plugin.
- En cas de dépassement de **quota** API, le polling se met en pause jusqu'à la date de reset
  renvoyée par Viessmann, puis reprend seul.

C'est l'API Viessmann qui dicte le jeu de commandes : quand la PAC est éteinte, des features
disparaissent puis réapparaissent. Le plugin resynchronise donc nom, unité, type et visibilité des
commandes à chaque cycle — un renommage ou un masquage manuel sera écrasé.

## Logs

- `jee4viessmann` — couche PHP ;
- `jee4viessmannd` — démon Python.

## Développement

```bash
# Lint / compile
php -l core/class/jee4viessmann.class.php
python3 -m py_compile resources/jee4viessmannd/jee4viessmannd.py

# Démon en local, hors Jeedom
resources/python_venv/bin/python3 resources/jee4viessmannd/jee4viessmannd.py --loglevel debug \
  --socketport 55070 --apikey TEST --cyclepoll 30 --pid /tmp/jee4viessmannd.pid \
  --callback "http://127.0.0.1/plugins/jee4viessmann/core/php/jee4viessmann.php"
```

## Documentation

- [Documentation utilisateur](docs/fr_FR/index.md)
- [Changelog](docs/fr_FR/changelog.md)

## Limitations connues

- Les actions à **plusieurs paramètres** (`setCurve` pente + parallèle, `setSchedule`) ne sont pas
  encore générées.
- Les features de type `array` et `Schedule` sont ignorées.
- L'authentification par mot de passe est en voie de dépréciation côté Viessmann.

## Licence

AGPL. Voir [LICENSE](LICENSE).
