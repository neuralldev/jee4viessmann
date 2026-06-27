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
    /* ============================ Dépendances ============================ */

    public static function dependancy_info()
    {
        $return = array();
        $return['log'] = 'jee4viessmann_dep';
        $return['progress_file'] = jeedom::getTmpFolder('jee4viessmann') . '/dependance';
        $return['state'] = file_exists(__DIR__ . '/../../resources/venv/bin/python3') ? 'ok' : 'nok';
        return $return;
    }

    public static function dependancy_install()
    {
        log::remove('jee4viessmann_dep');
        return array(
            'script' => __DIR__ . '/../../resources/install.sh ' . jeedom::getTmpFolder('jee4viessmann') . '/dependance',
            'log' => log::getPathToLog('jee4viessmann_dep'),
        );
    }

    /* ============================ Démon ============================ */

    public static function deamon_info()
    {
        $return = array();
        $return['log'] = 'jee4viessmannd';
        $return['state'] = 'nok';
        $pidFile = jeedom::getTmpFolder('jee4viessmann') . '/deamon.pid';
        if (file_exists($pidFile)) {
            $pid = trim(file_get_contents($pidFile));
            if ($pid !== '' && @posix_kill((int) $pid, 0)) {
                $return['state'] = 'ok';
            }
        }
        $return['launchable'] = 'ok';
        if (self::dependancy_info()['state'] != 'ok') {
            $return['launchable'] = 'nok';
            $return['launchable_message'] = __('Dépendances non installées', __FILE__);
        }
        return $return;
    }

    public static function deamon_start()
    {
        self::deamon_stop();
        $info = self::deamon_info();
        if ($info['launchable'] != 'ok') {
            throw new Exception(__('Le démon n\'est pas démarrable, vérifiez la configuration', __FILE__));
        }

        $python = realpath(__DIR__ . '/../../resources/venv/bin/python3');
        $script = realpath(__DIR__ . '/../../resources/jee4viessmannd/jee4viessmannd.py');
        $pidFile = jeedom::getTmpFolder('jee4viessmann') . '/deamon.pid';

        $cmd = $python . ' ' . $script;
        $cmd .= ' --loglevel ' . log::convertLogLevel(log::getLogLevel('jee4viessmannd'));
        $cmd .= ' --socketport ' . config::byKey('socketport', 'jee4viessmann', 55070);
        $cmd .= ' --callback ' . network::getNetworkAccess('internal') . '/plugins/jee4viessmann/core/php/jee4viessmann.php';
        $cmd .= ' --apikey ' . jeedom::getApiKey('jee4viessmann');
        $cmd .= ' --cyclepoll ' . config::byKey('cyclePoll', 'jee4viessmann', 120);
        $cmd .= ' --pid ' . $pidFile;

        log::add('jee4viessmann', 'info', 'Lancement du démon : ' . $cmd);
        $result = exec(system::getCmdSudo() . 'nohup ' . $cmd . ' >> ' . log::getPathToLog('jee4viessmannd') . ' 2>&1 &');

        // Laisse au démon le temps d'ouvrir son socket, puis pousse la configuration.
        for ($i = 1; $i <= 20; $i++) {
            if (self::deamon_info()['state'] == 'ok') {
                break;
            }
            sleep(1);
        }
        self::syncDaemonConfig();
    }

    public static function deamon_stop()
    {
        $pidFile = jeedom::getTmpFolder('jee4viessmann') . '/deamon.pid';
        if (file_exists($pidFile)) {
            $pid = trim(file_get_contents($pidFile));
            if ($pid !== '') {
                @posix_kill((int) $pid, SIGTERM);
            }
            @unlink($pidFile);
        }
    }

    /* ============================ PHP -> démon (socket) ============================ */

    public static function sendToDaemon($message)
    {
        if (self::deamon_info()['state'] != 'ok') {
            log::add('jee4viessmann', 'debug', 'Démon arrêté, message ignoré');
            return;
        }
        $port = config::byKey('socketport', 'jee4viessmann', 55070);
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
