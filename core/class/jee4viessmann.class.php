<?php

/* This file is part of Jeedom.
 *
 * Jeedom is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * Jeedom is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with Jeedom. If not, see <http://www.gnu.org/licenses/>.
 */

require_once __DIR__ . '/../../../../core/php/core.inc.php';

/**
 * Architecture :
 *  - Tout l'I/O viessmann (auth PyViCare, polling, exécution des actions) est dans le démon Python.
 *  - Cette classe PHP reste mince : cycle de vie Jeedom, supervision du démon, et passerelle :
 *      * démon -> PHP : callback HTTP (core/php/jee4viessmann.php) -> pushData() crée/MAJ les commandes
 *      * PHP -> démon : socket TCP (config des équipements + exécution des actions)
 */
class jee4viessmann extends eqLogic
{
    /* Constantes plugin (cf. jee4lm5) — id et port socket dédié du démon. */
    const PLUGINNAME = 'jee4viessmann';
    const JEEDOM_DAEMON_PORT = 55070;

    /* ============================ Démon ============================ */
    /* Les dépendances sont gérées nativement par Jeedom via plugin_info/packages.json
       (apt python3-pip + pip3 dans resources/python_venv). Pas de dependancy_info/install
       ni de install.sh : Jeedom crée et peuple le venv tout seul. */

    public static function deamon_info()
    {
        $return = array('log' => self::PLUGINNAME . 'd', 'launchable' => 'ok', 'state' => 'nok');
        $pidFile = jeedom::getTmpFolder(self::PLUGINNAME) . '/' . self::PLUGINNAME . 'd.pid';
        if (file_exists($pidFile)) {
            $pid = intval(trim(file_get_contents($pidFile)));
            $isRunning = false;
            if ($pid > 0) {
                if (function_exists('posix_getsid')) {
                    $isRunning = (posix_getsid($pid) !== false);
                } else {
                    $isRunning = file_exists('/proc/' . $pid);
                }
            }
            if ($isRunning) {
                $return['state'] = 'ok';
            }
        }
        return $return;
    }

    public static function deamon_start()
    {
        self::deamon_stop();

        // Nettoie un éventuel processus résiduel sur le port du démon.
        exec('fuser -k ' . self::JEEDOM_DAEMON_PORT . '/tcp 2>/dev/null');
        sleep(1);

        $info = self::deamon_info();
        if ($info['launchable'] != 'ok') {
            log::add(self::PLUGINNAME, 'error', __('Le démon n\'est pas démarrable', __FILE__) . ' : ' . $info['launchable']);
            return false;
        }

        $path = realpath(__DIR__ . '/../../resources');
        $python = $path . '/python_venv/bin/python3';
        $script = $path . '/' . self::PLUGINNAME . 'd/' . self::PLUGINNAME . 'd.py';
        $pidFile = jeedom::getTmpFolder(self::PLUGINNAME) . '/' . self::PLUGINNAME . 'd.pid';

        $cmd = $python . ' ' . $script;
        // Niveau de log piloté par le log du plugin (cf. jee4lm5) : le sélecteur de la page
        // config (log::level) s'applique ainsi réellement au démon, dont la sortie va dans le log 'd'.
        $cmd .= ' --loglevel ' . log::convertLogLevel(log::getLogLevel(self::PLUGINNAME));
        // Bind explicite sur la boucle locale : le socket n'est jamais exposé sur le réseau.
        $cmd .= ' --sockethost 127.0.0.1';
        $cmd .= ' --socketport ' . self::JEEDOM_DAEMON_PORT;
        $cmd .= ' --apikey ' . jeedom::getApiKey(self::PLUGINNAME);
        $cmd .= ' --cyclepoll ' . config::byKey('cyclePoll', self::PLUGINNAME, 120);
        $cmd .= ' --pid ' . $pidFile;

        $callback = network::getNetworkAccess('internal', 'proto:127.0.0.1:port:comp');
        if (empty($callback)) {
            $callback = 'http://127.0.0.1:80';
            log::add(self::PLUGINNAME, 'warning', 'network internal non configuré, fallback 127.0.0.1');
        }
        $cmd .= ' --callback ' . $callback . '/plugins/' . self::PLUGINNAME . '/core/php/' . self::PLUGINNAME . '.php';

        log::add(self::PLUGINNAME, 'info', 'Lancement du démon : ' . $cmd);
        $result = exec($cmd . ' >> ' . log::getPathToLog(self::PLUGINNAME . 'd') . ' 2>&1 &');
        log::add(self::PLUGINNAME, 'debug', 'exec result=' . $result);

        // Laisse au démon le temps d'ouvrir son socket, puis pousse la configuration.
        $i = 0;
        while ($i < 20) {
            if (self::deamon_info()['state'] == 'ok') {
                break;
            }
            sleep(1);
            $i++;
        }
        if ($i >= 20) {
            log::add(self::PLUGINNAME, 'error', __('Impossible de lancer le démon, vérifiez le log', __FILE__), 'unableStartDeamon');
            return false;
        }
        message::removeAll(self::PLUGINNAME, 'unableStartDeamon');
        self::syncDaemonConfig();
        return true;
    }

    public static function deamon_stop()
    {
        $pidFile = jeedom::getTmpFolder(self::PLUGINNAME) . '/' . self::PLUGINNAME . 'd.pid';
        if (file_exists($pidFile)) {
            $pid = intval(trim(file_get_contents($pidFile)));
            if ($pid > 0) {
                exec('kill -SIGTERM ' . $pid . ' 2>&1');
                sleep(1);
                if (file_exists('/proc/' . $pid)) {
                    exec('kill -SIGKILL ' . $pid . ' 2>&1');
                }
            }
            @unlink($pidFile);
        }
    }

    public static function backupExclude()
    {
        return array('resources/python_venv');
    }

    /* ============================ PHP -> démon (socket) ============================ */

    public static function sendToDaemon($message)
    {
        if (self::deamon_info()['state'] != 'ok') {
            log::add('jee4viessmann', 'debug', 'Démon arrêté, message ignoré');
            return;
        }
        $port = self::JEEDOM_DAEMON_PORT;
        $message['apikey'] = jeedom::getApiKey(self::PLUGINNAME);
        $payload = json_encode($message);
        $socket = socket_create(AF_INET, SOCK_STREAM, SOL_TCP);
        if ($socket === false) {
            log::add('jee4viessmann', 'error', 'Impossible de créer le socket');
            return;
        }
        if (@socket_connect($socket, '127.0.0.1', (int) $port) === false) {
            log::add('jee4viessmann', 'error', 'Connexion au démon impossible');
            socket_close($socket);
            return;
        }
        socket_write($socket, $payload, strlen($payload));
        socket_close($socket);
    }

    /* ============================ Identifiants (config plugin) ============================ */

    /**
     * Enregistre les identifiants au niveau du plugin (mot de passe chiffré), puis pousse
     * la configuration au démon. Appelée depuis l'ajax (modal de connexion).
     */
    public static function saveCredentials($clientId, $user, $password)
    {
        config::save('clientId', trim($clientId), 'jee4viessmann');
        config::save('userName', trim($user), 'jee4viessmann');
        // Ne ré-écrit le mot de passe que s'il est fourni (permet de re-valider sans le retaper).
        if (trim((string) $password) !== '') {
            config::save('password', utils::encrypt(trim($password)), 'jee4viessmann');
        }
        self::syncDaemonConfig();
    }

    /**
     * Envoie au démon le compte unique (identifiants déchiffrés) configuré au niveau du plugin.
     * Le démon (ré)authentifie via PyViCare et lance le polling/découverte des devices.
     */
    public static function syncDaemonConfig()
    {
        $clientId = trim(config::byKey('clientId', 'jee4viessmann', ''));
        $user     = trim(config::byKey('userName', 'jee4viessmann', ''));
        $pwd      = trim((string) utils::decrypt(config::byKey('password', 'jee4viessmann', '')));
        if ($clientId === '' || $user === '' || $pwd === '') {
            log::add('jee4viessmann', 'info', 'Identifiants incomplets, configuration non envoyée au démon');
            return;
        }
        self::sendToDaemon(array(
            'type'    => 'config',
            'account' => array('clientId' => $clientId, 'user' => $user, 'pwd' => $pwd),
        ));
    }

    /** Force une (ré)authentification + découverte (bouton "Détecter"). */
    public static function detect()
    {
        self::syncDaemonConfig();
    }

    /**
     * Heartbeat reçu du démon : mémorise l'état et l'horodatage de dernier contact.
     * Permet d'afficher dans la config "démon vivant / en pause quota / dernière synchro".
     */
    public static function heartbeat($data)
    {
        config::save('daemonState', isset($data['state']) ? $data['state'] : 'unknown', 'jee4viessmann');
        config::save('lastHeartbeat', date('Y-m-d H:i:s'), 'jee4viessmann');
        // pausedUntil arrive en UTC naïf (datetime.isoformat côté démon) : on le réinterprète
        // en UTC puis on le réaffiche dans la TZ locale de Jeedom, comme lastHeartbeat.
        $pausedUntil = '';
        if (!empty($data['pausedUntil'])) {
            try {
                $dt = new DateTime($data['pausedUntil'], new DateTimeZone('UTC'));
                $dt->setTimezone(new DateTimeZone(date_default_timezone_get()));
                $pausedUntil = $dt->format('Y-m-d H:i:s');
            } catch (Exception $e) {
                $pausedUntil = $data['pausedUntil'];
            }
        }
        config::save('pausedUntil', $pausedUntil, 'jee4viessmann');
        log::add('jee4viessmann', 'debug', 'heartbeat: ' . json_encode($data));
    }

    /* ============================ démon -> PHP (callback) ============================ */

    /**
     * Appelée par core/php/jee4viessmann.php quand le démon pousse des données.
     * Le démon envoie un message par *device* viessmann. On crée (si besoin) l'eqLogic
     * dédié au device, puis on crée/MAJ ses commandes depuis le typage reçu.
     *
     * $payload = [
     *   'device'     => ['installationId','gatewaySerial','deviceId','model','deviceType','online'],
     *   'group'      => 'circuits', 'groupLabel' => 'Circuits chauffage',  // sous-système
     *   'commands'   => [ ['logicalId','name','cmdType','subType','unit','visible','value'], ... ],
     * ]
     * Un eqLogic est créé par couple (device, sous-système).
     */
    public static function pushData($payload)
    {
        if (empty($payload['device']) || !is_array($payload['device']) || !isset($payload['commands'])) {
            return;
        }
        $group = isset($payload['group']) ? (string) $payload['group'] : 'general';
        $groupLabel = isset($payload['groupLabel']) ? (string) $payload['groupLabel'] : 'Général';
        $eq = self::findOrCreateDeviceEq($payload['device'], $group, $groupLabel);
        if (!is_object($eq)) {
            return;
        }
        self::applyCommands($eq, $payload['commands']);
    }

    /**
     * Retrouve (ou crée) l'eqLogic correspondant à un (device, sous-système).
     * Clé stable : logicalId = sanitize("<gatewaySerial>_<deviceId>_<group>").
     */
    protected static function findOrCreateDeviceEq($device, $group, $groupLabel)
    {
        $gateway = isset($device['gatewaySerial']) ? (string) $device['gatewaySerial'] : '';
        $deviceId = isset($device['deviceId']) ? (string) $device['deviceId'] : '';
        if ($deviceId === '') {
            return null;
        }
        $logicalId = preg_replace('/[^a-zA-Z0-9]+/', '_', $gateway . '_' . $deviceId . '_' . $group);
        $logicalId = trim($logicalId, '_');

        $eq = self::byLogicalId($logicalId, 'jee4viessmann');
        if (!is_object($eq)) {
            $model = !empty($device['model']) ? $device['model'] : $deviceId;
            $eq = new jee4viessmann();
            $eq->setLogicalId($logicalId);
            $eq->setEqType_name('jee4viessmann');
            $eq->setName($model . ' - ' . $groupLabel);
            $eq->setIsEnable(1);
            $eq->setIsVisible(1);
            $eq->setConfiguration('isDevice', 1);
            $eq->setConfiguration('group', $group);
            $eq->setConfiguration('installationId', isset($device['installationId']) ? $device['installationId'] : '');
            $eq->setConfiguration('gatewaySerial', $gateway);
            $eq->setConfiguration('deviceId', $deviceId);
            $eq->setConfiguration('deviceType', isset($device['deviceType']) ? $device['deviceType'] : '');
            $eq->save();
            log::add('jee4viessmann', 'info', 'Équipement créé : ' . $eq->getName());
        }
        return $eq;
    }

    /** Crée les commandes manquantes (typage API) puis met à jour les valeurs info. */
    protected static function applyCommands($eq, $commands)
    {
        // Pose une valeur de configuration uniquement si elle change réellement, et signale
        // le changement. Sans ça, chaque commande d'action était réécrite en base à *chaque*
        // cycle de polling (720 UPDATE/jour/commande avec le cyclePoll par défaut).
        // Comparaison souple : la configuration fait un aller-retour JSON en base ('12' vs 12).
        $setCfg = function ($cmd, $key, $value) {
            if ($cmd->getConfiguration($key, null) != $value) {
                $cmd->setConfiguration($key, $value);
                return true;
            }
            return false;
        };

        foreach ($commands as $c) {
            if (!isset($c['logicalId'])) {
                continue;
            }
            // Une commande en erreur ne doit pas faire échouer tout le lot (sinon 400 global).
            try {
                $cmd = $eq->getCmd(null, $c['logicalId']);
                $isNew = !is_object($cmd);
                $type = isset($c['cmdType']) ? $c['cmdType'] : 'info';
                $subType = isset($c['subType']) ? $c['subType'] : 'string';
                if ($isNew) {
                    // Création pilotée par le typage de l'API (pas de map manuel).
                    $cmd = new jee4viessmannCmd();
                    $cmd->setEqLogic_id($eq->getId());
                    $cmd->setLogicalId($c['logicalId']);
                    // Historisation : posée à la création seulement (préférence/donnée user ensuite).
                    $cmd->setIsHistorized(!empty($c['historized']) ? 1 : 0);
                }
                // Métadonnées rafraîchies à chaque cycle (nom FR, unité, ordre, generic_type, type/subType,
                // visibilité pilotée par les règles) pour propager les améliorations sans recréer.
                $name = isset($c['name']) ? $c['name'] : $c['logicalId'];
                $unit = isset($c['unit']) ? $c['unit'] : '';
                $gtype = isset($c['genericType']) ? $c['genericType'] : '';
                $order = isset($c['order']) ? (int) $c['order'] : 0;
                $visible = array_key_exists('visible', $c) ? ((int) $c['visible']) : 1;
                $changed = $isNew
                    || $cmd->getName() != $name
                    || $cmd->getUnite() != $unit
                    || $cmd->getGeneric_type() != $gtype
                    || $cmd->getType() != $type
                    || $cmd->getSubType() != $subType
                    || (int) $cmd->getIsVisible() != $visible
                    || (int) $cmd->getOrder() != $order;
                if ($changed) {
                    $cmd->setName($name);
                    $cmd->setUnite($unit);
                    $cmd->setGeneric_type($gtype);
                    $cmd->setType($type);
                    $cmd->setSubType($subType);
                    $cmd->setIsVisible($visible);
                    $cmd->setOrder($order);
                }

                // Widget graphique éventuel (ex. « thermomètre » pour les températures du
                // ballon tampon) : template + plage de la jauge (#min#/#max#). Posé/rafraîchi
                // sans recréer la commande ; l'utilisateur peut toujours le changer à la main.
                if (!empty($c['template'])) {
                    $tpl = $c['template'];
                    if ($cmd->getTemplate('dashboard') != $tpl || $cmd->getTemplate('mobile') != $tpl) {
                        $cmd->setTemplate('dashboard', $tpl);
                        $cmd->setTemplate('mobile', $tpl);
                        $changed = true;
                    }
                    if (isset($c['min'])) {
                        $changed = $setCfg($cmd, 'minValue', $c['min']) || $changed;
                    }
                    if (isset($c['max'])) {
                        $changed = $setCfg($cmd, 'maxValue', $c['max']) || $changed;
                    }
                }

                if ($type == 'action') {
                    // Stocke le mapping d'exécution (feature/action/param) + contraintes widget.
                    // Ces valeurs sont stables d'un cycle à l'autre : on ne réécrit qu'en cas
                    // de changement réel (cf. $setCfg), sinon rien ne part en base.
                    $changed = $setCfg($cmd, 'feature', isset($c['feature']) ? $c['feature'] : '') || $changed;
                    $changed = $setCfg($cmd, 'action', isset($c['action']) ? $c['action'] : '') || $changed;
                    $changed = $setCfg($cmd, 'param', isset($c['param']) ? $c['param'] : '') || $changed;
                    if ($subType === 'slider') {
                        if (isset($c['min'])) {
                            $changed = $setCfg($cmd, 'minValue', $c['min']) || $changed;
                        }
                        if (isset($c['max'])) {
                            $changed = $setCfg($cmd, 'maxValue', $c['max']) || $changed;
                        }
                        if (isset($c['step'])) {
                            $changed = $setCfg($cmd, 'step', $c['step']) || $changed;
                        }
                    } elseif ($subType === 'select' && isset($c['listValue'])) {
                        $changed = $setCfg($cmd, 'listValue', $c['listValue']) || $changed;
                    }
                    // Lie l'action à la commande info qu'elle pilote : le widget (slider/select)
                    // affiche alors la valeur courante au lieu de partir de zéro.
                    if (!empty($c['link'])) {
                        $linked = $eq->getCmd(null, $c['link']);
                        if (is_object($linked) && $cmd->getValue() != $linked->getId()) {
                            $cmd->setValue($linked->getId());
                            $changed = true;
                        }
                    }
                }

                // Un seul save() par commande et par cycle, uniquement si quelque chose a bougé.
                if ($changed) {
                    $cmd->save();
                }

                if ($type != 'action' && array_key_exists('value', $c)) {
                    $eq->checkAndUpdateCmd($c['logicalId'], $c['value']);
                }
            } catch (Exception $e) {
                log::add('jee4viessmann', 'warning', 'Commande ignorée ' . $c['logicalId'] . ' : ' . $e->getMessage());
            }
        }
    }

    /* ============================ Widget circuit ============================ */
    /* Pour le sous-équipement « Circuits chauffage » (group=circuits), on remplace le rendu
       par défaut (liste de commandes) par une carte graphique pilotable (cadran thermostat).
       On réutilise le wrapper eqLogic du core (drag/menu/refresh) et on n'injecte que la carte
       dans #cmd#. Tout échec retombe sur le rendu standard (jamais de tuile cassée). */

    /* Libellé + icône FR par programme de chauffe. */
    private static $PROG_META = array(
        'reduced' => array('Réduit', '🌙'),
        'normal'  => array('Normal', '☀️'),
        'comfort' => array('Confort', '🔥'),
        'eco'     => array('Éco', '🌿'),
        'fixed'   => array('Fixe', '🔒'),
        'standby' => array('Veille', '⏻'),
    );
    private static $PROG_ORDER = array('reduced', 'normal', 'comfort', 'eco', 'fixed', 'standby');
    private static $MODE_FR = array(
        'standby' => 'Veille', 'heating' => 'Chauffage', 'dhw' => 'ECS',
        'dhwAndHeating' => 'ECS + Chauffage', 'cooling' => 'Rafraîchissement',
        'heatingCooling' => 'Chauffage + Rafraîchissement', 'normalStandby' => 'Veille',
    );

    public function toHtml($_version = 'dashboard')
    {
        if ($this->getConfiguration('group', '') !== 'circuits') {
            return parent::toHtml($_version);
        }
        try {
            $version = jeedom::versionAlias($_version);
            $cards = $this->renderCircuitCards($version);
            if ($cards === '') {
                return parent::toHtml($_version);
            }
            $replace = $this->preToHtml($_version);
            if (!is_array($replace)) {
                return $replace;
            }
            $replace['#calledFrom#'] = 'eqLogic';
            $replace['#eqLogic_class#'] = 'eqLogic_layout_default';
            $replace['#cmd#'] = $cards;
            $tpl = getTemplate('core', $version, 'eqLogic');
            return $this->postToHtml($_version, template_replace($replace, $tpl));
        } catch (\Throwable $e) {
            log::add('jee4viessmann', 'warning', 'widget circuit toHtml: ' . $e->getMessage());
            return parent::toHtml($_version);
        }
    }

    /* Concatène une carte par index de circuit présent dans l'équipement. */
    private function renderCircuitCards($version)
    {
        $tpl = getTemplate('core', $version, 'circuit', self::PLUGINNAME);
        if ($tpl === '' || $tpl === null) {
            return '';
        }
        $byIdx = array();
        foreach (cmd::byEqLogicId($this->getId()) as $cmd) {
            if (preg_match('/^heating_circuits_(\d+)_/', $cmd->getLogicalId(), $m)) {
                $byIdx[(int) $m[1]][] = $cmd;
            }
        }
        if (empty($byIdx)) {
            return '';
        }
        ksort($byIdx);
        $html = '';
        foreach ($byIdx as $idx => $list) {
            $replace = $this->buildCircuitCard($idx, $list);
            if (is_array($replace)) {
                $html .= template_replace($replace, $tpl);
            }
        }
        return $html;
    }

    /* Construit les placeholders d'une carte (cfg JSON + segments + bloc mode) pour un circuit.
       Retourne null si le circuit n'a aucun élément pilotable/affichable. */
    private function buildCircuitCard($idx, $cmds)
    {
        $cfg = array(
            'min' => 10, 'max' => 30, 'active' => 'normal',
            'activeCmd' => null, 'modeCmd' => null, 'modeInfoCmd' => null,
            'modeStandby' => null, 'modeOn' => null,
            'roomCmd' => null, 'supplyCmd' => null, 'pumpCmd' => null,
            'programs' => array(), 'init' => array(),
        );
        $modeEnum = array();
        $haveRange = false;

        foreach ($cmds as $cmd) {
            $lid = $cmd->getLogicalId();
            $id = (int) $cmd->getId();
            $isAction = ($cmd->getType() === 'action');
            $action = $cmd->getConfiguration('action', '');
            $feature = $cmd->getConfiguration('feature', '');

            // ---- commandes info (valeurs affichées) ----
            if (!$isAction) {
                $cfg['init'][$id] = $cmd->execCmd();
                if (preg_match('/_sensors_temperature_supply_value$/', $lid)) {
                    $cfg['supplyCmd'] = $id;
                } elseif (preg_match('/_sensors_temperature_room_value$/', $lid)
                    || preg_match('/^heating_circuits_' . $idx . '_temperature_value$/', $lid)) {
                    $cfg['roomCmd'] = $id;
                } elseif (preg_match('/_operating_programs_active_value$/', $lid)) {
                    $cfg['activeCmd'] = $id;
                } elseif (preg_match('/_operating_modes_active_value$/', $lid)) {
                    $cfg['modeInfoCmd'] = $id;
                } elseif (preg_match('/_circulation_pump_status$/', $lid)) {
                    $cfg['pumpCmd'] = $id;
                } elseif (preg_match('/_operating_programs_([a-z0-9]+)_temperature$/', $lid, $mm)) {
                    $p = strtolower($mm[1]);
                    $cfg['programs'][$p]['tempCmd'] = $id;
                    $cfg['programs'][$p]['temp'] = is_numeric($cfg['init'][$id]) ? 0 + $cfg['init'][$id] : null;
                }
                continue;
            }

            // ---- commandes action (pilotage) ----
            if ($action === 'setMode') {
                $cfg['modeCmd'] = $id;
                $lv = $cmd->getConfiguration('listValue', '');
                foreach (explode(';', $lv) as $pair) {
                    $v = explode('|', $pair);
                    if ($v[0] !== '') {
                        $modeEnum[] = $v[0];
                    }
                }
            } elseif (preg_match('/operating\.programs\.([a-z0-9]+)/i', $feature, $mm)) {
                $p = strtolower($mm[1]);
                if ($action === 'setTemperature') {
                    $cfg['programs'][$p]['setTempCmd'] = $id;
                    $mn = $cmd->getConfiguration('minValue', '');
                    $mx = $cmd->getConfiguration('maxValue', '');
                    if ($mn !== '' && $mx !== '' && !$haveRange) {
                        $cfg['min'] = 0 + $mn;
                        $cfg['max'] = 0 + $mx;
                        $haveRange = true;
                    }
                } elseif ($action === 'activate') {
                    $cfg['programs'][$p]['activateCmd'] = $id;
                } elseif ($action === 'deactivate') {
                    $cfg['programs'][$p]['deactivateCmd'] = $id;
                }
            }
        }

        // Étiquettes des programmes + filtrage (on garde ceux réellement exploitables).
        $programs = array();
        $segments = '';
        $extras = array_diff(array_keys($cfg['programs']), self::$PROG_ORDER);
        foreach (array_merge(self::$PROG_ORDER, $extras) as $p) {
            if (!isset($cfg['programs'][$p])) {
                continue;
            }
            $P = $cfg['programs'][$p];
            if (!isset($P['setTempCmd']) && !isset($P['tempCmd']) && !isset($P['activateCmd'])) {
                continue;
            }
            $meta = isset(self::$PROG_META[$p]) ? self::$PROG_META[$p] : array(ucfirst($p), '•');
            $P['label'] = $meta[0];
            $programs[$p] = $P;
            $segments .= '<button type="button" data-prog="' . $p . '"><span class="i">'
                . $meta[1] . '</span>' . $meta[0] . '</button>';
        }
        $cfg['programs'] = $programs;

        // Rien d'exploitable -> pas de carte (rendu par défaut).
        if (empty($programs) && $cfg['activeCmd'] === null && $cfg['supplyCmd'] === null) {
            return null;
        }
        if (!isset($programs[$cfg['active']])) {
            $keys = array_keys($programs);
            $cfg['active'] = $keys ? $keys[0] : 'normal';
        }

        // Bloc mode (sélecteur) + détection veille/marche pour le bouton power.
        $modeBlock = '';
        if ($cfg['modeCmd'] !== null && !empty($modeEnum)) {
            $opts = '';
            foreach ($modeEnum as $v) {
                $label = isset(self::$MODE_FR[$v]) ? self::$MODE_FR[$v] : $v;
                $opts .= '<option value="' . htmlspecialchars($v, ENT_QUOTES) . '">' . htmlspecialchars($label, ENT_QUOTES) . '</option>';
                if (stripos($v, 'standby') !== false) {
                    $cfg['modeStandby'] = $v;
                } elseif ($cfg['modeOn'] === null) {
                    $cfg['modeOn'] = $v;
                }
            }
            $modeBlock = '<div class="jee4v-mode"><label>Mode</label><select>' . $opts . '</select></div>';
        }

        $uid = 'jee4vcc' . $this->getId() . '_' . $idx . '_' . mt_rand();
        $json = json_encode($cfg, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        return array(
            '#uid#' => $uid,
            '#title#' => 'Circuit ' . $idx,
            '#segments#' => $segments,
            '#mode_block#' => $modeBlock,
            '#cfg#' => htmlspecialchars($json, ENT_QUOTES),
        );
    }

    /* ============================ Cycle de vie ============================ */
    /* Les identifiants sont désormais au niveau du plugin (config) — les eqLogic sont
       uniquement des devices auto-créés. Leur sauvegarde ne déclenche donc pas de re-sync. */
}

class jee4viessmannCmd extends cmd
{
    public function execute($_options = array())
    {
        if ($this->getType() != 'action') {
            return;
        }
        $eqLogic = $this->getEqLogic();
        // La commande est portée par l'eqLogic "device" : on indique au démon (compte unique)
        // quel device cibler via son identité.
        $device = array(
            'installationId' => $eqLogic->getConfiguration('installationId', ''),
            'gatewaySerial'  => $eqLogic->getConfiguration('gatewaySerial', ''),
            'deviceId'       => $eqLogic->getConfiguration('deviceId', ''),
        );

        // Valeur selon le type d'action : slider (#slider#), sélecteur (#select#), sinon aucune.
        $value = null;
        if ($this->getSubType() == 'slider' && isset($_options['slider'])) {
            $value = $_options['slider'];
        } elseif ($this->getSubType() == 'select' && isset($_options['select'])) {
            $value = $_options['select'];
        } elseif (isset($_options['value'])) {
            $value = $_options['value'];
        }

        // L'action est déléguée au démon Python qui appelle le setter de l'API viessmann.
        jee4viessmann::sendToDaemon(array(
            'type'      => 'action',
            'device'    => $device,
            'feature'   => $this->getConfiguration('feature', ''),
            'action'    => $this->getConfiguration('action', ''),
            'param'     => $this->getConfiguration('param', ''),
            'value'     => $value,
            'logicalId' => $this->getLogicalId(),
        ));
    }
}
