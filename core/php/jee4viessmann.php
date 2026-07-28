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

// Point d'entrée du callback démon -> PHP (socle jeedomdaemon).
// - le démon teste le callback au démarrage : GET ?test=1&apikey=...  -> on répond 'OK'
// - puis il POST un JSON {eqLogicId, commands:[...]} (apikey en query ?apikey=...)

require_once __DIR__ . '/../../../../core/php/core.inc.php';

try {
    if (!jeedom::apiAccess(init('apikey'), 'jee4viessmann')) {
        throw new Exception(__('Clé API non valide', __FILE__));
    }

    // Test de connectivité émis par le démon au démarrage.
    if (init('test') != '') {
        echo 'OK';
        die();
    }

    $data = json_decode(file_get_contents('php://input'), true);
    if (!is_array($data)) {
        throw new Exception(__('Charge utile invalide', __FILE__));
    }

    if (isset($data['type']) && $data['type'] === 'heartbeat') {
        jee4viessmann::heartbeat($data);
    } else {
        jee4viessmann::pushData($data);
    }
    echo 'ok';
} catch (Throwable $e) {
    // Throwable et pas Exception : une Error (TypeError, argument manquant...) dans pushData
    // s'échapperait en fatal PHP, donc en réponse HTML 500 que le démon ne sait pas interpréter.
    // On garde un 400 propre et une trace exploitable côté log.
    http_response_code(400);
    log::add('jee4viessmann', 'error', 'callback : ' . $e->getMessage());
    echo $e->getMessage();
}
