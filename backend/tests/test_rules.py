import copy

import pytest
import yaml
from pydantic import ValidationError

from app.rules import ClinicRules


def raw():
    with open("config/clinic.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_shipped_rules_are_valid(rules):
    assert rules.clinic.name and len(rules.services) >= 5 and len(rules.providers) >= 3


def test_provider_with_unknown_service_is_rejected():
    data = copy.deepcopy(raw())
    data["providers"][0]["services"].append("teleportation")
    with pytest.raises(ValidationError, match="unknown services"):
        ClinicRules.model_validate(data)


def test_backwards_working_window_is_rejected():
    data = copy.deepcopy(raw())
    data["providers"][0]["schedule"]["mon"] = [["12:00", "09:00"]]
    with pytest.raises(ValidationError, match="not increasing"):
        ClinicRules.model_validate(data)


def test_service_nobody_offers_is_rejected():
    data = copy.deepcopy(raw())
    data["services"].append({"key": "orphan", "name": "orphan", "duration_minutes": 10})
    with pytest.raises(ValidationError, match="has no provider"):
        ClinicRules.model_validate(data)


def test_bad_weekday_key_is_rejected():
    data = copy.deepcopy(raw())
    data["providers"][0]["schedule"]["funday"] = [["09:00", "10:00"]]
    with pytest.raises(ValidationError, match="bad weekday"):
        ClinicRules.model_validate(data)
