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

    /**
     * Envoie au démon la liste des équipements et leurs identifiants (déchiffrés).
     * Le démon (ré)authentifie via PyViCare et lance le polling.
     */
    public static function syncDaemonConfig()
    {
        $equipments = array();
        foreach (self::byType('jee4viessmann') as $eq) {
            if ($eq->getIsEnable() != 1) {
                continue;
            }
            $equipments[] = array(
                'id'       => $eq->getId(),
                'clientId' => trim($eq->getConfiguration('clientId', '')),
                'user'     => trim($eq->getConfiguration('userName', '')),
                'pwd'      => trim((string) utils::decrypt($eq->getConfiguration('password', ''))),
            );
        }
        self::sendToDaemon(array('type' => 'config', 'equipments' => $equipments));
    }

    /* ============================ démon -> PHP (callback) ============================ */

    /**
     * Appelée par core/php/jee4viessmann.php quand le démon pousse des données.
     * Crée les commandes manquantes (à partir du typage envoyé par le démon) puis met à jour les valeurs.
     *
     * $payload = ['eqLogicId' => int, 'commands' => [ ['logicalId','name','cmdType','subType','unit','value'], ... ]]
     */
    public static function pushData($payload)
    {
        if (!isset($payload['eqLogicId']) || !isset($payload['commands'])) {
            return;
        }
        $eq = self::byId($payload['eqLogicId']);
        if (!is_object($eq)) {
            return;
        }
        foreach ($payload['commands'] as $c) {
            if (!isset($c['logicalId'])) {
                continue;
            }
            $cmd = $eq->getCmd(null, $c['logicalId']);
            if (!is_object($cmd)) {
                // Création pilotée par le typage de l'API (pas de map manuel).
                $cmd = new jee4viessmannCmd();
                $cmd->setEqLogic_id($eq->getId());
                $cmd->setLogicalId($c['logicalId']);
                $cmd->setName(isset($c['name']) ? $c['name'] : $c['logicalId']);
                $cmd->setIsVisible(1);
                $cmd->setIsHistorized(0);
                $cmd->setType(isset($c['cmdType']) ? $c['cmdType'] : 'info');
                $cmd->setSubType(isset($c['subType']) ? $c['subType'] : 'string');
                if (!empty($c['unit'])) {
                    $cmd->setUnite($c['unit']);
                }
                $cmd->save();
            }
            if (($cmd->getType() == 'info') && array_key_exists('value', $c)) {
                $eq->checkAndUpdateCmd($c['logicalId'], $c['value']);
            }
        }
    }

    /* ============================ Cycle de vie ============================ */

    public function preSave()
    {
        // Chiffrement des identifiants sensibles (cf. plugin v1). Idempotent (préfixe 'crypt:').
        $password = $this->getConfiguration('password', '');
        if ($password !== '') {
            $this->setConfiguration('password', utils::encrypt($password));
        }
    }

    public function postSave()
    {
        // Toute modification d'un équipement est propagée au démon.
        self::syncDaemonConfig();
    }

    public function postUpdate()
    {
        self::syncDaemonConfig();
    }

    public function postRemove()
    {
        self::syncDaemonConfig();
    }
}

class jee4viessmannCmd extends cmd
{
    public function execute($_options = array())
    {
        if ($this->getType() != 'action') {
            return;
        }
        $eqLogic = $this->getEqLogic();
        // L'action est déléguée au démon Python qui appelle l'API viessmann.
        jee4viessmann::sendToDaemon(array(
            'type'       => 'action',
            'eqLogicId'  => $eqLogic->getId(),
            'logicalId'  => $this->getLogicalId(),
            'subType'    => $this->getSubType(),
            'options'    => $_options,
        ));
    }
}
