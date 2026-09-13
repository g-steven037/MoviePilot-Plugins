from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _read_json(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_v3_manifest_declares_adapted_subscription_plugins():
    manifest = _read_json("package.v3.json")
    for plugin_id in ("VarietySubscribeAssistant", "SubscribeLinkRenamer"):
        entry = manifest[plugin_id]
        assert entry["v3"] is True
        assert entry["system_version"] == ">=3.0.0"
        assert entry["version"].split(".")[0] == "1"
        assert entry["history"]


def test_v2_manifest_blocks_old_contract_for_v3_plugins():
    manifest = _read_json("package.v2.json")
    for plugin_id in ("VarietySubscribeAssistant", "SubscribeLinkRenamer"):
        entry = manifest[plugin_id]
        assert entry["v3"] is False


def test_v3_sources_use_public_oper_and_sdk_paths():
    variety = (ROOT / "plugins.v3/varietysubscribeassistant/__init__.py").read_text(
        encoding="utf-8"
    )
    renamer = (ROOT / "plugins.v3/subscribelinkrenamer/__init__.py").read_text(
        encoding="utf-8"
    )

    assert "from app.db.oper.subscribe import SubscribeOper" in variety
    assert "SubscribeHistoryOper" in variety
    assert "DownloadHistoryOper" in variety
    assert "app.db.models.subscribehistory" not in variety
    assert "app.db.downloadhistory_oper" not in variety
    assert "_db" not in variety
    assert "from app.sdk.media import WordsMatcher" in renamer
    assert "from app.sdk.utilities import SystemUtils" in renamer
    assert "from app.db.oper.subscribe import SubscribeOper" in renamer
    assert "app.db.subscribe_oper" not in renamer


def test_v3_subscription_event_payload_accepts_typed_objects():
    source = (ROOT / "plugins.v3/varietysubscribeassistant/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "model_dump" in source
    assert "to_dict" in source
    assert "isinstance(event.event_data, dict)" not in source


def test_cross_version_emby_actor_metadata_is_not_capped_below_v3():
    manifest = _read_json("package.v2.json")
    assert "<3" not in manifest["EmbyActorChinese"]["system_version"]
