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

require_once dirname(__FILE__) . '/../../../core/php/core.inc.php';

function jee4viessmann_install()
{
    if (version_compare(jeedom::version(), '4.6', '<')) {
        event::add('jeedom::alert', array(
            'level' => 'danger',
            'title' => __('Plugin Jee4viessmann', __FILE__),
            'message' => __('Ce plugin nécessite Jeedom >= 4.6', __FILE__),
        ));
    }
}

function jee4viessmann_update()
{
    jee4viessmann::deamon_start();
}

function jee4viessmann_remove()
{
    jee4viessmann::deamon_stop();
}
