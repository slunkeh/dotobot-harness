import json
from unittest.mock import patch

import pytest

from connectors.base import ConnectorContext
from harness import delegated_oauth as broker
from harness import mcp_oauth
from harness.connectors import CATALOG, WORKSPACE_TYPES, Connectors
from harness.paths import HarnessPaths


@pytest.mark.parametrize('service', sorted(WORKSPACE_TYPES))
def test_delegation_keeps_only_capability_and_routes_each_account(tmp_path, service, monkeypatch):
    paths = HarnessPaths(home=tmp_path)
    store = Connectors(paths)
    a, b = store.add(service, 'A'), store.add(service, 'B')
    calls = []
    def request(bundle, connector, kind, action='token'):
        calls.append((bundle['grant_id'], connector, kind, action))
        return {'access_token': 'short-lived-' + bundle['grant_id'], 'service': kind, 'email': 'a@example.com'}
    monkeypatch.setattr(broker, 'request', request)
    for record in [a, b]:
        broker.install(paths, record, {'broker_url': 'https://broker.example/integrations', 'grant_id': record['id'], 'capability': 'cap-' + record['id']})
        stored = mcp_oauth.load_tokens(paths, record['id'])
        assert not {'client_id', 'client_secret', 'refresh_token', 'access_token'} & stored.keys()
        assert ConnectorContext(paths, 'test', record).secret() == 'short-lived-' + record['id']
    assert len({c[0] for c in calls}) == 2
    store.remove(a['id'])
    assert calls[-1][-1] == 'revoke'
    assert broker.connected(paths, b['id'])
    assert not broker.connected(paths, a['id'])


def test_legacy_google_tokens_never_used(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add('google_drive', 'Drive')
    mcp_oauth.save_tokens(paths, record['id'], {'access_token': 'legacy', 'refresh_token': 'legacy-refresh'})
    assert not Connectors(paths).list()[0]['oauth_configured']
    with pytest.raises(mcp_oauth.OAuthError, match='Reconnect'):
        broker.token(paths, record)


@pytest.mark.parametrize('url', ['http://broker.example', 'https://user:password@broker.example', 'https://broker.example?token=x', 'https://127.0.0.1'])
def test_broker_rejects_unsafe_urls(url):
    with pytest.raises(mcp_oauth.OAuthError):
        broker.request({'broker_url': url, 'capability': 'secret', 'grant_id': 'one'}, 'c', 'gmail')


def test_redirects_and_provider_errors_never_leak_credentials():
    import urllib.error
    with patch.object(broker.netguard, 'check_destination'), patch('urllib.request.build_opener') as opener:
        opener.return_value.open.side_effect = urllib.error.HTTPError('https://broker.example', 302, 'secret', {}, None)
        with pytest.raises(mcp_oauth.OAuthError) as error:
            broker.request({'broker_url': 'https://broker.example', 'capability': 'secret', 'grant_id': 'one'}, 'c', 'gmail')
    assert 'secret' not in str(error.value)
    assert broker.NoRedirect().redirect_request(None, None, None, None, None, None) is None


def test_workspace_catalog_is_oauth_only():
    catalog = {r['type']: r for r in CATALOG}
    assert 'google' not in catalog
    for service in WORKSPACE_TYPES:
        assert catalog[service]['auth_broker'] == 'control_plane'
        assert catalog[service]['multi_account']
        assert catalog[service]['fields'] == []
        assert 'client_secret' not in json.dumps(catalog[service])


@pytest.mark.parametrize('service', ['google_calendar', 'google_drive', 'google_sheets', 'google_docs'])
def test_multiple_workspace_accounts_have_distinct_bound_tools(tmp_path, service, monkeypatch):
    from connectors import generic
    from connectors.registry import tools_for_bot
    paths = HarnessPaths(home=tmp_path)
    store = Connectors(paths)
    a = store.add(service, 'Work')
    b = store.add(service, 'Home')
    seen = []
    def read(ctx, args):
        seen.append(ctx.record['id'])
        return ctx.record['name']
    monkeypatch.setattr(generic, '_get', read)
    tools = tools_for_bot(paths, 'atlas')
    assert tools[service + '_work_get'][1]({'path': '/sample'}) == 'Work'
    assert tools[service + '_home_get'][1]({'path': '/sample'}) == 'Home'
    assert seen == [a['id'], b['id']]
