"""HTTP boundaries for the optional account and session switching services."""
from functools import wraps

from flask import Blueprint, current_app, jsonify, request

from .tmux_backend import PaneMissing, TmuxUnavailable


def create_blueprint(services):
    bp = Blueprint('subscriptions', __name__)

    def boundary(write=False):
        def decorate(function):
            @wraps(function)
            def call(*args, **kwargs):
                if not current_app.config['ACCOUNTS_ENABLED']:
                    return jsonify(error='Gestione abbonamenti disabilitata'), 403
                if write and not current_app.config['ALLOW_SEND']:
                    return jsonify(error='Questa istanza è in sola lettura'), 403
                try:
                    return function(*args, **kwargs)
                except PaneMissing:
                    return jsonify(error='Pannello non più disponibile. Riapri la sessione.'), 404
                except TmuxUnavailable:
                    return jsonify(error='tmux non disponibile. Verifica il terminale prima di riprovare.'), 503
                except ValueError as exc:
                    status = getattr(exc, 'http_status', getattr(exc, 'status_code', 400))
                    return jsonify(error=str(exc)), status
                except OSError:
                    return jsonify(error='Operazione non disponibile. Verifica il servizio locale.'), 503
            return call
        return decorate

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('Invia un oggetto JSON valido')
        return value

    def identity(value):
        if not isinstance(value, str) or not value or len(value) > 200:
            raise ValueError('Identità del pannello richiesta')
        return value

    @bp.get('/api/subscriptions')
    def subscriptions():
        if not current_app.config['ACCOUNTS_ENABLED']:
            return jsonify(enabled=False, accounts=[], providers=[], allow_changes=False)
        return load_subscriptions()

    @boundary()
    def load_subscriptions():
        service = services()
        payload = service['accounts'].list_accounts(force=request.args.get('refresh') == '1')
        return jsonify(**payload, enabled=True, providers=[service['usage'].codex_status()],
                       allow_changes=current_app.config['ALLOW_SEND'])

    @bp.get('/api/accounts')
    @boundary()
    def accounts():
        return jsonify(services()['accounts'].list_accounts(force=request.args.get('refresh') == '1'))

    @bp.post('/api/accounts')
    @boundary(write=True)
    def create():
        data = body()
        return jsonify(services()['accounts'].create(data.get('slug'), data.get('email')))

    @bp.post('/api/accounts/<slug>/web-login')
    @boundary(write=True)
    def start_login(slug):
        data = body()
        return jsonify(services()['accounts'].start_login(slug, data.get('email')))

    @bp.get('/api/accounts/<slug>/web-login')
    @boundary()
    def poll_login(slug):
        return jsonify(services()['accounts'].poll_login(slug))

    @bp.post('/api/accounts/<slug>/web-login/code')
    @boundary(write=True)
    def code(slug):
        data = body()
        return jsonify(services()['accounts'].submit_code(slug, data.get('code')))

    @bp.delete('/api/accounts/<slug>/web-login')
    @boundary(write=True)
    def cancel_login(slug):
        return jsonify(services()['accounts'].cancel_login(slug))

    @bp.post('/api/accounts/<slug>/logout')
    @boundary(write=True)
    def logout(slug):
        return jsonify(services()['accounts'].logout(slug))

    @bp.delete('/api/accounts/<slug>')
    @boundary(write=True)
    def remove(slug):
        return jsonify(services()['accounts'].remove(slug))

    @bp.get('/api/panes/<int:pane>/session')
    @boundary()
    def inspect(pane):
        generation = identity(request.args.get('identity'))
        return jsonify(**services()['switch'].inspect(pane, generation),
                       allow_changes=current_app.config['ALLOW_SEND'])

    @bp.post('/api/panes/<int:pane>/switch-account')
    @boundary(write=True)
    def switch(pane):
        data = body()
        generation = identity(data.get('identity'))
        account = data.get('account')
        conversation = data.get('conversation_id')
        if not isinstance(account, str) or len(account) > 64:
            raise ValueError('Abbonamento di destinazione richiesto')
        if conversation is not None and (not isinstance(conversation, str) or len(conversation) > 64):
            raise ValueError('Identificatore conversazione non valido')
        return jsonify(services()['switch'].switch(pane, generation, account, conversation))

    return bp
