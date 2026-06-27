# HANDOFF — contexte de reprise (jee4viessman)

> Document de passation pour reprendre le travail dans une nouvelle session Claude Code
> sans tout réexpliquer. Lis aussi `CLAUDE.md` (chargé automatiquement) et `ARCHITECTURE.md`.

## D'où vient ce projet

Tout est parti d'un **audit du plugin Jeedom existant `viessmannIot`** (fonctionne sous Jeedom
4.4→4.6, dépôt `github.com/neuralldev/viessmannIot`, fork de `PhilippeJ-code/viessmannIot`).

L'audit a identifié, par ordre d'importance :
1. **#1 TLS désactivé** (`CURLOPT_SSL_VERIFYPEER => false`) sur tous les appels → MITM.
2. **#4 Aucun timeout cURL** → démon bloqué possible.
3. **#2 Identifiants en clair** (password, codeChallenge) dans la config eqLogic.
4. **#5 Échecs cURL silencieux** (aucun contrôle `curl_errno`).
5. **#6 `getFeatures()`** : `return $message` hors du bloc `statusCode` → null + warning PHP 8.
6. Code répétitif : `createCommands` (1549 l.) + `rafraichir` (1578 l.) ≈ moitié du fichier.
7. `demond.py` en **Python 2** = code mort.

### Correctifs APPLIQUÉS sur l'ancien plugin (`viessmannIot`, branche master du fork)
Déjà commités et poussés sur `neuralldev/viessmannIot` (testés dans un Jeedom de dev en conteneur) :
- #1 TLS réactivé (8 appels), #4 timeouts (connect 10s / total 30s).
- #2 chiffrement `password` + `codeChallenge` via `utils::encrypt/decrypt` (+ migration auto dans
  `viessmannIot_update()`, détection par absence du préfixe `crypt:`).
- #5 helper `httpExec()` : contrôle erreur transport + log (warning/HTTP>=400, debug sinon, + URL).
- #6 retour de `getFeatures()` corrigé.
- Durcissement PHP 8.1+ (`trim(null)`, `utils::encrypt('')` renvoie null → on ne chiffre que le non-vide).
- `.gitignore` géré en **local uniquement** (via `.git/info/exclude`, non poussé) à la demande de l'utilisateur.

Validé en conteneur : migration OK, idempotence OK, serveur joint (un 404 « Device not found » a
permis de diagnostiquer un `deviceId` mal configuré — pas un bug du plugin).

> NB : le refactor du code répétitif (#6/point 6) n'a **pas** été fait sur l'ancien plugin — la
> conclusion a été qu'il vaut mieux repartir d'une archi Python (ce projet-ci).

## Pourquoi cette réécriture (jee4viessman)

Décision de l'utilisateur : créer un **nouveau plugin séparé**, **Jeedom 4.6 uniquement**, **démon
Python 3.13 (venv)**, pour :
- éviter les blocages cURL PHP et la dépendance au cron Jeedom ;
- abandonner l'interface HTML lourde du v1 au profit d'une UI qui « calque les champs API » ;
- **utiliser le typage de l'API au lieu d'un mapping manuel** de centaines de lignes.

Choix d'architecture validés ensemble :
- On NE PEUT PAS supprimer tout le PHP : Jeedom impose des classes eqLogic/cmd, les hooks démon,
  une page de config, et `cmd::execute()`. → PHP réduit à de la plomberie.
- **PyViCare** retenu : règle auth (OAuth/PKCE/refresh), découverte device (le 404 du v1), et fournit
  le typage des features pour générer les commandes automatiquement.
- Plugin **coexistant** avec l'ancien (id distinct `jee4viessman`), pas une migration in-place
  (sinon on casse scénarios/historique qui référencent les `logicalId` du v1).

## Discussion importante sur le typage API vs mapping manuel
Question de l'utilisateur : « utiliser directement le typage de l'API pour répliquer les noms et
éviter le map manuel ? ». Réponse : oui mais **en augmentation**, pas en remplacement total, à cause de :
1. **stabilité des `logicalId`** (les installs v1 en dépendent — ici nouveau plugin donc liberté) ;
2. UX non déductible du typage (sliders liés, boutons par valeur d'enum, flags binaires curés).
Dans ce POC on génère les `logicalId` depuis le chemin de feature (acceptable car nouveau plugin).
Si une UX soignée est voulue, prévoir une table d'overrides de noms (petite), pas une cascade.

## État actuel du POC
Squelette **compilable** (PHP lint OK, `py_compile` OK sous 3.13) mais **non testé sur device réel**.
Voir `CLAUDE.md` section « État » et « TODO ».

## Prochaines étapes proposées
1. `git init` + dépôt `neuralldev/jee4viessman` + premier commit.
2. Test conteneur : install dépendances (venv/PyViCare), démarrage démon, affichage page config.
3. Compte Viessmann réel : valider découverte + remontée auto des commandes info.
4. Implémenter `execute_action()` + génération des commandes d'action (enum/sliders).

## Environnement de l'utilisateur
- Dev sur macOS (`~/Documents/Dev/`), Jeedom cible en **conteneur** (dev), tests via pull du fork.
- Core Jeedom disponible en lecture locale : `~/Documents/core` (v4.5, API démon ≈ 4.6).
- Préférence : commits factorisés, messages en français, marqueurs de revue retirés avant PR.
