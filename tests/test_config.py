"""Configuration comes from the environment and refuses what it cannot use."""

import pytest

from lacre_gateway import config

GOOD = {
    "LACRE_ROUTER": "0x" + "a1" * 20,
    "LACRE_API_KEYS": "k" * 24 + ", " + "j" * 30,
    "LACRE_BLOB_BASE_URL": "https://gateway.example.org/h/",
    "LACRE_DATA_DIR": "/var/lib/lacre-gateway",
}


def test_a_complete_environment():
    settings = config.load(dict(GOOD, LACRE_POLL_S="5"))
    assert settings.api_keys == ("k" * 24, "j" * 30)
    assert settings.blob_base_url == "https://gateway.example.org/h"
    assert settings.poll_s == 5 and settings.network == "bradbury"
    assert settings.signing_key_file is None
    assert "k" * 24 not in repr(settings)


@pytest.mark.parametrize("change,message", [
    ({"LACRE_ROUTER": "0x1234"}, "LACRE_ROUTER"),
    ({"LACRE_API_KEYS": ""}, "LACRE_API_KEYS"),
    ({"LACRE_API_KEYS": "short"}, "24 characters"),
    ({"LACRE_BLOB_BASE_URL": "http://gateway.example.org"}, "https://"),
    ({"LACRE_DATA_DIR": ""}, "LACRE_DATA_DIR"),
    ({"LACRE_POLL_S": "soon"}, "integer"),
])
def test_what_is_refused(change, message):
    with pytest.raises(config.ConfigError, match=message):
        config.load(dict(GOOD, **change))
