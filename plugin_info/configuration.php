<?php
if (!isConnect('admin')) {
    throw new Exception('{{401 - Accès non autorisé}}');
}
?>
<form class="form-horizontal">
    <fieldset>
        <legend><i class="fas fa-key"></i> {{Connexion au compte Viessmann}}</legend>
        <div class="form-group">
            <label class="col-md-4 control-label">{{Identifiants}}</label>
            <div class="col-md-4">
                <a class="btn btn-default" id="bt_viLogin"><i class="fas fa-sign-in-alt"></i> {{Se connecter}}</a>
            </div>
        </div>
        <div class="form-group">
            <label class="col-md-4 control-label">{{Détection}}</label>
            <div class="col-md-4">
                <a class="btn btn-default" id="bt_viSync"><i class="fas fa-sync"></i> {{Détecter mes équipements}}</a>
            </div>
        </div>
        <div class="alert alert-info">
            {{Saisissez le Client ID (Viessmann Developer Portal, redirect_uri vicare://oauth-callback/everest), l'email et le mot de passe du compte ViCare. Les équipements (devices) sont ensuite découverts et créés automatiquement.}}
        </div>

        <legend><i class="fas fa-cog"></i> {{Démon}}</legend>
        <div class="form-group">
            <label class="col-md-4 control-label">{{Niveau de log du démon}}</label>
            <div class="col-md-4">
                <select class="configKey form-control" data-l1key="log::level">
                    <option value="100">{{Debug}}</option>
                    <option value="200">{{Info}}</option>
                    <option value="300" selected>{{Warning}}</option>
                    <option value="400">{{Erreur}}</option>
                </select>
            </div>
        </div>
        <div class="form-group">
            <label class="col-md-4 control-label">{{Période de rafraîchissement (s)}}</label>
            <div class="col-md-4">
                <input type="number" class="configKey form-control" data-l1key="cyclePoll" placeholder="120" />
            </div>
        </div>
    </fieldset>
</form>

<script>
document.getElementById('bt_viLogin').addEventListener('click', function () {
    jeeDialog.dialog({
        id: 'jee_viLoginModal',
        title: '{{Connexion de Jeedom au compte Viessmann}}',
        width: '60vw',
        height: '50vh',
        top: '10vh',
        contentUrl: 'index.php?v=d&modal=login&plugin=jee4viessmann'
    });
});

document.getElementById('bt_viSync').addEventListener('click', function () {
    domUtils.showLoading();
    domUtils.ajax({
        type: 'POST',
        url: 'plugins/jee4viessmann/core/ajax/jee4viessmann.ajax.php',
        data: { action: 'sync' },
        dataType: 'json',
        global: false,
        error: function (error) {
            jeedomUtils.showAlert({ message: error.message, level: 'danger' });
            domUtils.hideLoading();
        },
        success: function () {
            jeedomUtils.showAlert({ message: '{{Détection lancée, regardez les logs}}', level: 'success' });
            domUtils.hideLoading();
        }
    });
});
</script>
