"""fetch_live() parsing, fed the (placeholder-scrubbed) captured responses in
recon/sample_responses instead of the network. Only `call` is replaced - signing is
covered separately in test_polycab_client.py."""

import json
from datetime import datetime
from pathlib import Path

import pytest

import polycab_client
from common import UTC

SAMPLES = Path(__file__).resolve().parent.parent / "recon" / "sample_responses"


def fake_call(responses: dict):
    def call(endpoint, params, token):
        result = responses[endpoint]
        if isinstance(result, Exception):
            raise result
        return result
    return call


def sample(name: str):
    return json.loads((SAMPLES / f"{name}.json").read_text())


def test_fetch_live_parses_sample(monkeypatch):
    monkeypatch.setattr(polycab_client, "call", fake_call({
        "InverterDetailInfoNewone": sample("InverterDetailInfoNewone"),
        "GroupList": sample("GroupList"),
    }))
    live = polycab_client.fetch_live("goods", "member", "token")
    # DataTime "2026-07-20 21:13:18" is the device's UTC+8 clock -> 13:13:18 UTC
    assert live["reported_at"].astimezone(UTC) == datetime(2026, 7, 20, 13, 13, 18, tzinfo=UTC)
    assert live["daily_yield_kwh"] == 10.27 and live["total_yield_kwh"] == 26.28  # Wh -> kWh
    assert live["pv_power_w"] == 0.0
    assert live["temperature_c"] == 43.0
    assert live["status"] == "offline"  # GroupList sample: gray=1
    assert live["firmware"]["MDSP"] == "030100-05_411000"


def test_fetch_live_missing_datatime_is_schema_error(monkeypatch):
    detail = sample("InverterDetailInfoNewone")
    del detail["DataTime"]
    monkeypatch.setattr(polycab_client, "call", fake_call({"InverterDetailInfoNewone": detail}))
    with pytest.raises(polycab_client.SchemaError):
        polycab_client.fetch_live("goods", "member", "token")


def test_fetch_live_status_failure_propagates_auth_errors(monkeypatch):
    monkeypatch.setattr(polycab_client, "call", fake_call({
        "InverterDetailInfoNewone": sample("InverterDetailInfoNewone"),
        "GroupList": polycab_client.AuthError("expired"),
    }))
    with pytest.raises(polycab_client.AuthError):
        polycab_client.fetch_live("goods", "member", "token")


def test_fetch_live_empty_plant_list_means_unknown_status(monkeypatch):
    """An empty AllGroupList used to crash the dashboard's live snapshot (IndexError)."""
    monkeypatch.setattr(polycab_client, "call", fake_call({
        "InverterDetailInfoNewone": sample("InverterDetailInfoNewone"),
        "GroupList": {"AllGroupList": []},
    }))
    live = polycab_client.fetch_live("goods", "member", "token")
    assert live["status"] is None
    assert live["daily_yield_kwh"] == 10.27
