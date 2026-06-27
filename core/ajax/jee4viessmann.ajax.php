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

try {
    require_once dirname(__FILE__) . '/../../../../core/php/core.inc.php';
    include_file('core', 'authentification', 'php');

    if (!isConnect('admin')) {
        throw new Exception(__('401 - Accès non autorisé', __FILE__));
    }

    // Jeedom 4.5+ envoie un corps JSON plutôt qu'un POST form classique.
    $data = json_decode(file_get_contents('php://input'), true);
    if (is_array($data)) {
        foreach ($data as $key => $value) {
            $_POST[$key] = $value;
        }
    }

    $action = init('action');
    log::add('jee4viessmann', 'debug', 'ajax action=' . $action);

    switch ($action) {

        case 'login':
            // Enregistre les identifiants (chiffrés) au niveau du plugin et pousse au démon.
            jee4viessmann::saveCredentials(init('clientId'), init('username'), init('password'));
            ajax::success();
            break;

        case 'sync':
            // (Ré)authentification + découverte des devices.
            jee4viessmann::detect();
            ajax::success();
            break;

        default:
            throw new Exception(__('Aucune méthode correspondant à : ', __FILE__) . $action);
    }
} catch (Exception $e) {
    ajax::error(displayException($e), $e->getCode());
}
