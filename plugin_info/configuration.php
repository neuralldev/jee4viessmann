<?php
if (!isConnect('admin')) {
    throw new Exception('{{401 - Accès non autorisé}}');
}
?>
<form class="form-horizontal">
    <fieldset>
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
