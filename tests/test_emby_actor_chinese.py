from __future__ import annotations

import sys
import types
from pathlib import Path

import test_plugin_security as security


security._install_stubs()
PLUGIN_ROOT = Path(__file__).parents[1] / "plugins.v2"
sys.path.insert(0, str(PLUGIN_ROOT))

from embyactorchinese import EmbyActorChinese
from embyactorchinese.client import EmbyActorClient, EmbyActorError, validate_base_url


def test_actor_mapping_is_unique_exact_and_actor_only():
    people = [
        {"Name": "Tom Hanks", "Type": "Actor", "Role": "Forrest"},
        {"Name": "Robert Zemeckis", "Type": "Director"},
        {"Name": "周迅", "Type": "Actor"},
        {"Name": "Ambiguous Name", "Type": "Actor"},
    ]
    credits = [
        types.SimpleNamespace(name="汤姆·汉克斯", latin_name="Tom Hanks"),
        types.SimpleNamespace(name="甲", latin_name="Ambiguous Name"),
        types.SimpleNamespace(name="乙", latin_name="Ambiguous-Name"),
    ]
    updated, changes = EmbyActorChinese.build_actor_mapping(people, credits)
    assert updated[0]["Name"] == "汤姆·汉克斯"
    assert updated[0]["Role"] == "Forrest"
    assert updated[1]["Name"] == "Robert Zemeckis"
    assert updated[2]["Name"] == "周迅"
    assert updated[3]["Name"] == "Ambiguous Name"
    assert changes == [{"index": 0, "from": "Tom Hanks", "to": "汤姆·汉克斯"}]
    assert people[0]["Name"] == "Tom Hanks"


def test_emby_item_selection_requires_exact_title_year_and_unique_result():
    items = [
        {"Id": "1", "Name": "沙丘", "OriginalTitle": "Dune", "ProductionYear": 2021, "Type": "Movie"},
        {"Id": "2", "Name": "沙丘", "OriginalTitle": "Dune", "ProductionYear": 1984, "Type": "Movie"},
    ]
    assert EmbyActorChinese.select_exact_item(items, "沙丘", 2021)["Id"] == "1"
    try:
        EmbyActorChinese.select_exact_item(items + [dict(items[0], Id="3")], "沙丘", 2021)
    except ValueError as exc:
        assert str(exc) == "EMBY_ITEM_AMBIGUOUS"
    else:
        raise AssertionError("ambiguous Emby match was accepted")


def test_form_defaults_to_preview_and_hides_manual_credentials():
    form, defaults = EmbyActorChinese().get_form()
    serialized = repr(form)
    assert defaults["enabled"] is False
    assert defaults["run_once"] is False
    assert defaults["use_mp_config"] is True
    assert defaults["action"] == "preview"
    assert "确认同步到Emby" in serialized
    assert "{{!use_mp_config}}" in serialized
    assert "password" in serialized


def test_emby_client_never_puts_key_in_url_and_blocks_redirect():
    assert validate_base_url("https://emby.example.test:8920/") == "https://emby.example.test:8920"
    for unsafe in ("ftp://emby", "https://u:p@emby.test", "https://emby.test/?api_key=x"):
        try:
            validate_base_url(unsafe)
        except EmbyActorError:
            pass
        else:
            raise AssertionError("unsafe URL accepted")

    class Response:
        status_code = 302
        content = b""
        def close(self):
            pass

    class Session:
        headers = {}
        def __init__(self):
            self.url = ""
        def request(self, method, url, **kwargs):
            self.url = url
            return Response()
        def close(self):
            pass

    key = "never-log-this-key"
    client = EmbyActorClient("http://emby:8096", key)
    fake = Session()
    client.session = fake
    try:
        client._request("GET", "/Users")
    except EmbyActorError as exc:
        assert exc.code == "REDIRECT_BLOCKED"
    else:
        raise AssertionError("redirect accepted")
    assert key not in fake.url


def test_preview_never_writes_and_sync_verifies_write():
    class Client:
        def __init__(self):
            self.item = {
                "Id": "1", "Name": "沙丘", "Type": "Movie", "ProductionYear": 2021,
                "ProviderIds": {"Douban": "3001114"},
                "People": [{"Name": "Timothee Chalamet", "Type": "Actor"}],
            }
            self.writes = 0

        def search_items(self, _title, _item_type):
            return [dict(self.item)]

        def get_item(self, _item_id):
            return {
                **self.item,
                "ProviderIds": dict(self.item["ProviderIds"]),
                "People": [dict(person) for person in self.item["People"]],
            }

        def update_item(self, _item_id, item):
            self.writes += 1
            self.item = item

    client = Client()
    plugin = EmbyActorChinese()
    plugin._client = client
    plugin._load_douban_credits = lambda *_args: (
        "3001114", "沙丘", [types.SimpleNamespace(name="提莫西·查拉梅", latin_name="Timothee Chalamet")]
    )
    base = {"title": "沙丘", "year": 2021, "media_type": "movie"}
    plugin.run_test({**base, "action": "preview"})
    assert client.writes == 0
    assert plugin.get_data("history")[0]["status"] == "预览完成"
    plugin.run_test({**base, "action": "sync"})
    assert client.writes == 1
    assert client.item["People"][0]["Name"] == "提莫西·查拉梅"
    assert plugin.get_data("history")[0]["status"] == "同步成功"
    assert plugin.get_data("last_backup")["people"][0]["Name"] == "Timothee Chalamet"
