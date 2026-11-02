import json
from pathlib import Path

import pytest

from common import Config, TrustedSource

FIXTURE = Path(__file__).resolve().parents[2] / "host" / "lib" / "fwcheck-expected.json"
MAC = "aa:bb:cc:dd:ee:ff"


@pytest.fixture
def fixture_json():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def cfg():
    return Config(
        region="ap-northeast-1",
        queue_url="https://sqs.ap-northeast-1.amazonaws.com/123456789012/craftlatch-q",
        java_port=25565, bedrock_port=19132, grant_timeout_sec=600,
        idle_stop_sec=900, slp_timeout_sec=3, key_warn_days=335,
        trusted_sources=(
            TrustedSource("enp3s0", "192.0.2.0/24", MAC),
            TrustedSource("wg0", "198.51.100.0/24", None),
        ),
    )
