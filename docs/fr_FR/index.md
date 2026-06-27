# Plugin Jee4Viessman

Plugin Viessmann nouvelle génération pour Jeedom **>= 4.6**.

## Principe

Contrairement au plugin historique (tout en PHP, cron + cURL), Jee4Viessman repose sur un
**démon Python 3** (testé sous 3.13, en environnement virtuel) qui utilise la bibliothèque
**PyViCare** pour :

- l'authentification (OAuth2 + PKCE + refresh token, gérés par la lib) ;
- la découverte automatique de l'installation, de la passerelle et de l'appareil ;
- le polling des *features* et la **génération automatique des commandes** depuis le typage de l'API.

La couche PHP reste minimale : cycle de vie Jeedom, supervision du démon, et passerelle
(callback HTTP démon → PHP, socket PHP → démon).

## Configuration d'un équipement

1. Créer un équipement.
2. Renseigner **Id Client** (Viessmann Developer Portal), **Email** et **Mot de passe** du compte Viessmann.
3. Sauvegarder. Les commandes apparaissent automatiquement après le premier cycle du démon.

## Dépendances

Installées automatiquement (venv + `PyViCare`). Voir `resources/requirements.txt`.
