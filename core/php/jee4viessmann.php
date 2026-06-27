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

// Point d'entrée du callback démon -> PHP.
// Le démon POST un JSON {apikey, eqLogicId, commands:[...]} ; on crée/MAJ les commandes.

require_once __DIR__ . '/../../../../core/php/core.inc.php';

try {
    if (!jeedom::apiAccess(init('apikey'), 'jee4viessmann')) {
        // L'apikey peut aussi arriver dans le corps JSON.
        $raw = file_get_contents('php://input');
        $data = json_decode($raw, true);
        if (!is_array($data) || !isset($data['apikey']) || !jeedom::apiAccess($data['apikey'], 'jee4viessmann')) {
            throw new Exception(__('Clé API non valide', __FILE__));
        }
    } else {
        $raw = file_get_contents('php://input');
        $data = json_decode($raw, true);
    }

    if (!is_array($data)) {
        throw new Exception(__('Charge utile invalide', __FILE__));
    }

    jee4viessmann::pushData($data);
    echo 'ok';
} catch (Exception $e) {
    http_response_code(400);
    echo $e->getMessage();
}
