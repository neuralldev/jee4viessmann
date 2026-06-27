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

if (!isConnect('admin')) {
    throw new Exception('{{401 - Accès non autorisé}}');
}

// Pré-remplissage : clientId/email en clair, mot de passe jamais renvoyé (laisser vide = inchangé).
$clientId = config::byKey('clientId', 'jee4viessmann', '');
$userName = config::byKey('userName', 'jee4viessmann', '');
?>
<form class="form-horizontal">
  <fieldset>
    <div class="form-group">
      <label class="col-sm-3 control-label">{{Client ID}}</label>
      <div class="col-sm-5">
        <input type="text" class="form-control" id="in_jeeViLogin_clientId"
               value="<?php echo htmlspecialchars($clientId); ?>"
               placeholder="{{Client ID du Viessmann Developer Portal}}" />
        <small class="text-muted">{{redirect_uri du client : vicare://oauth-callback/everest}}</small>
      </div>
    </div>
    <div class="form-group">
      <label class="col-sm-3 control-label">{{Adresse email}}</label>
      <div class="col-sm-5">
        <input type="text" class="form-control" id="in_jeeViLogin_username"
               value="<?php echo htmlspecialchars($userName); ?>"
               placeholder="{{Email du compte ViCare}}" />
      </div>
    </div>
    <div class="form-group">
      <label class="col-sm-3 control-label">{{Mot de passe}}</label>
      <div class="col-sm-5">
        <input type="password" class="form-control" id="in_jeeViLogin_password"
               placeholder="{{Mot de passe (laisser vide pour conserver l'actuel)}}" />
      </div>
    </div>
    <div class="form-group">
      <label class="col-sm-3 control-label"></label>
      <div class="col-sm-7">
        <a class="btn btn-success" id="bt_validateViLogin">{{Valider}}</a>
      </div>
    </div>
  </fieldset>
</form>

<script>
  document.getElementById('bt_validateViLogin').addEventListener('click', function () {
    const clientId = document.getElementById('in_jeeViLogin_clientId').value;
    const username = document.getElementById('in_jeeViLogin_username').value;
    const password = document.getElementById('in_jeeViLogin_password').value;

    fetch('plugins/jee4viessmann/core/ajax/jee4viessmann.ajax.php', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'login', clientId: clientId, username: username, password: password })
    })
      .then(response => response.json())
      .then(data => {
        if (data.state !== 'ok') {
          jeedomUtils.showAlert({ message: data.result, level: 'danger' });
          return;
        }
        jeedomUtils.showAlert({ message: '{{Identifiants enregistrés, découverte en cours (voir les logs)}}', level: 'success' });
      })
      .catch(error => {
        jeedomUtils.showAlert({ message: error.message || String(error), level: 'danger' });
      });
  });
</script>
