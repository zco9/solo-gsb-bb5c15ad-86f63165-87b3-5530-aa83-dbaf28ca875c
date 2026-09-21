import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="parser-test-"), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.checksum import crc16_ccitt  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

SENSOR_DEF = {
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "description": "温湿度传感器报文",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 4},
        {"name": "flags", "type": "uint", "bit_length": 4},
        {
            "name": "temperature",
            "type": "int",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "humidity",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 2},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}


def make_message(body: bytes) -> str:
    crc = crc16_ccitt(body)
    return (body + crc.to_bytes(2, "big")).hex()


def test_register_definition():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["device_type"] == "temp_sensor"
    assert data["version"] == "1.0.0"
    assert data["active"] is True
    assert len(data["fields"]) == 6


def test_register_duplicate_conflict():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 409


def test_register_invalid_condition_reference():
    bad = {
        "device_type": "bad_dev",
        "version": "1.0.0",
        "fields": [
            {"name": "a", "type": "uint", "bit_length": 8,
             "condition": {"field": "ghost", "op": "eq", "value": 1}},
        ],
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_register_checksum_width_mismatch():
    bad = {
        "device_type": "bad_crc",
        "version": "1.0.0",
        "fields": [{"name": "crc", "type": "uint", "bit_length": 8}],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_parse_temperature_branch():
    # header=0xAA, msg_type=1, flags=0, temperature raw=235 (LE) -> 23.5 °C
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "version": "1.0.0", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["checksum"]["valid"] is True
    assert data["checksum"]["algorithm"] == "crc16-ccitt"
    assert data["consumed_bits"] == 48

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["header"]["value"] == 0xAA
    assert fields["header"]["bit_offset"] == 0
    assert fields["msg_type"]["value"] == 1
    assert fields["msg_type"]["bit_offset"] == 8
    assert fields["temperature"]["value"] == 23.5
    assert fields["temperature"]["raw_value"] == 235
    assert fields["temperature"]["bit_offset"] == 16
    assert "humidity" not in fields  # skipped by branch

    branch = {b["field"]: b for b in data["branch_path"]}
    assert branch["temperature"]["matched"] is True
    assert branch["temperature"]["action"] == "parsed"
    assert branch["humidity"]["matched"] is False
    assert branch["humidity"]["action"] == "skipped"


def test_parse_humidity_branch():
    # msg_type=2 -> humidity branch, temperature skipped
    body = bytes([0xAA, 0x20, 0x55])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["humidity"]["value"] == 0x55
    assert fields["humidity"]["bit_offset"] == 16
    assert "temperature" not in fields
    assert data["checksum"]["valid"] is True


def test_parse_checksum_mismatch():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    good = make_message(body)
    bad = good[:-2] + ("00" if good[-2:] != "00" else "ff")
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": bad},
    )
    assert resp.status_code == 200
    assert resp.json()["checksum"]["valid"] is False


def test_parse_truncated_message():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "AA10"},
    )
    assert resp.status_code == 422


def test_parse_invalid_hex():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "zzzz"},
    )
    assert resp.status_code == 422


def test_parse_unknown_device():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "nope", "hex": "AABB"},
    )
    assert resp.status_code == 404


def test_trailing_bits_warning():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    msg = make_message(body) + "FF"  # extra byte beyond definition
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": msg},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["trailing_bits"] == 8
    assert data["warnings"]


def _register(device_type, version):
    payload = {
        "device_type": device_type,
        "version": version,
        "fields": [{"name": "val", "type": "uint", "bit_length": 8}],
    }
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text


def test_latest_version_and_deactivate():
    _register("meter", "1.9.0")
    _register("meter", "1.10.0")  # semver-ish: 1.10.0 > 1.9.0

    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.10.0"

    resp = client.post("/api/v1/definitions/meter/1.10.0/deactivate")
    assert resp.status_code == 200
    assert resp.json()["active"] is False

    # latest now falls back to the still-active older version
    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.json()["version"] == "1.9.0"

    # explicit-version parse of an inactive definition still works
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "meter", "version": "1.10.0", "hex": "2A"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition_active"] is False
    assert resp.json()["fields"][0]["value"] == 0x2A


def test_list_definitions():
    resp = client.get("/api/v1/definitions")
    assert resp.status_code == 200
    data = resp.json()
    types = {item["device_type"] for item in data["items"]}
    assert {"temp_sensor", "meter"} <= types
    # inactive 1.10.0 hidden by default
    versions = {(i["device_type"], i["version"]) for i in data["items"]}
    assert ("meter", "1.10.0") not in versions

    resp = client.get("/api/v1/definitions", params={"include_inactive": True})
    versions = {(i["device_type"], i["version"]) for i in resp.json()["items"]}
    assert ("meter", "1.10.0") in versions

    resp = client.get("/api/v1/definitions", params={"device_type": "meter"})
    assert all(i["device_type"] == "meter" for i in resp.json()["items"])


def test_get_definition_and_404s():
    resp = client.get("/api/v1/definitions/temp_sensor/1.0.0")
    assert resp.status_code == 200
    assert resp.json()["checksum"]["algorithm"] == "crc16-ccitt"

    assert client.get("/api/v1/definitions/temp_sensor/9.9.9").status_code == 404
    assert client.get("/api/v1/definitions/ghost/latest").status_code == 404
    assert client.post("/api/v1/definitions/ghost/1.0.0/deactivate").status_code == 404


# ---------- encode ----------

SIGNED_DEF = {
    "device_type": "signed_dev",
    "version": "1.0.0",
    "fields": [
        {"name": "temp", "type": "int", "bit_length": 16,
         "endianness": "little", "scale": 0.1},
        {"name": "delta", "type": "int", "bit_length": 8},
    ],
}

MIXED_DEF = {
    "device_type": "mixed_dev",
    "version": "1.0.0",
    "fields": [
        {"name": "preamble", "type": "hex", "bit_length": 16},
        {"name": "code", "type": "ascii", "bit_length": 16},
        {"name": "enabled", "type": "bool", "bit_length": 1},
        {"name": "level", "type": "uint", "bit_length": 3},
        {"name": "mode", "type": "uint", "bit_length": 4},
        {"name": "gain", "type": "uint", "bit_length": 8, "scale": 0.5, "offset": -1.0},
    ],
}

NIBBLE_DEF = {
    "device_type": "nibble_dev",
    "version": "1.0.0",
    "fields": [
        {"name": "hi", "type": "uint", "bit_length": 4},
        {"name": "lo", "type": "uint", "bit_length": 4},
        {"name": "tail", "type": "uint", "bit_length": 4},
    ],
}

MISALIGNED_DEF = {
    "device_type": "misaligned_dev",
    "version": "1.0.0",
    "fields": [
        {"name": "nib", "type": "uint", "bit_length": 4},
        {"name": "chk", "type": "uint", "bit_length": 8},
    ],
    "checksum": {"field": "chk", "algorithm": "sum8"},
}


def _register_ok(payload):
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code in (201, 409), resp.text


def test_encode_temperature_branch_roundtrip():
    _register_ok(SENSOR_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "version": "1.0.0",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # crc computed automatically; identical to a hand-built message
    assert data["hex"] == make_message(bytes([0xAA, 0x10, 0xEB, 0x00]))
    assert data["encoded_bits"] == 48
    assert data["padded_bits"] == 0
    assert data["checksum"]["algorithm"] == "crc16-ccitt"
    assert data["checksum"]["range"] == "bytes [0, 4)"

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["temperature"]["value"] == 23.5
    assert fields["temperature"]["raw_value"] == 235
    assert fields["temperature"]["bit_offset"] == 16
    assert "humidity" not in fields  # branch not taken, not required

    branch = {b["field"]: b for b in data["branch_path"]}
    assert branch["temperature"]["matched"] is True
    assert branch["temperature"]["action"] == "encoded"
    assert branch["humidity"]["matched"] is False
    assert branch["humidity"]["action"] == "skipped"

    # round-trip: parsing the encoded hex yields the same values
    again = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": data["hex"]},
    )
    assert again.status_code == 200
    parsed = {f["name"]: f for f in again.json()["fields"]}
    assert parsed["temperature"]["value"] == 23.5
    assert again.json()["checksum"]["valid"] is True


def test_encode_humidity_branch_and_ignored_skipped_value():
    _register_ok(SENSOR_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 2, "flags": 0, "humidity": 0x55},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hex"] == make_message(bytes([0xAA, 0x20, 0x55]))

    # a value for a skipped branch field is ignored (with a warning), not an error
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {
                "header": 0xAA, "msg_type": 2, "flags": 0,
                "humidity": 0x55, "temperature": 99.9,
            },
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hex"] == make_message(bytes([0xAA, 0x20, 0x55]))
    assert any("temperature" in w for w in data["warnings"])


def test_encode_missing_matched_field_422():
    _register_ok(SENSOR_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0},  # temperature missing
        },
    )
    assert resp.status_code == 422


def test_encode_unknown_field_422():
    _register_ok(SENSOR_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 2, "flags": 0, "humidity": 1, "ghost": 1},
        },
    )
    assert resp.status_code == 422


def test_encode_checksum_field_not_accepted():
    _register_ok(SENSOR_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {
                "header": 0xAA, "msg_type": 2, "flags": 0, "humidity": 1, "crc": 0x1234,
            },
        },
    )
    assert resp.status_code == 422


def test_encode_out_of_range_422():
    _register_ok(SENSOR_DEF)
    base = {"header": 0xAA, "msg_type": 2, "flags": 0, "humidity": 0x55}
    for bad in ({"humidity": 256}, {"flags": 16}, {"header": -1}):
        resp = client.post(
            "/api/v1/encode",
            json={"device_type": "temp_sensor", "values": {**base, **bad}},
        )
        assert resp.status_code == 422, bad


def test_encode_unrepresentable_scaled_value_422():
    _register_ok(SENSOR_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            # 23.55 -> raw 235.5 is not an integer
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.55},
        },
    )
    assert resp.status_code == 422


def test_encode_signed_twos_complement_and_little_endian():
    _register_ok(SIGNED_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "signed_dev", "values": {"temp": -5.0, "delta": -1}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # temp raw = -50 -> 0xFFCE little-endian -> CE FF; delta -1 -> FF
    assert data["hex"] == "ceffff"
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["temp"]["raw_value"] == -50
    assert fields["delta"]["raw_value"] == -1

    again = client.post("/api/v1/parse", json={"device_type": "signed_dev", "hex": data["hex"]})
    parsed = {f["name"]: f for f in again.json()["fields"]}
    assert parsed["temp"]["value"] == -5.0
    assert parsed["delta"]["value"] == -1

    # int8 range is [-128, 127]
    for bad in ({"delta": 128}, {"delta": -129}, {"temp": 3276.8}):
        values = {"temp": 0, "delta": 0, **bad}
        resp = client.post(
            "/api/v1/encode", json={"device_type": "signed_dev", "values": values}
        )
        assert resp.status_code == 422, bad


def test_encode_mixed_types_roundtrip():
    _register_ok(MIXED_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "mixed_dev",
            "values": {
                "preamble": "0xABCD",
                "code": "OK",
                "enabled": True,
                "level": 5,
                "mode": 2,
                "gain": 2.5,  # raw = (2.5 - (-1.0)) / 0.5 = 7
            },
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 16+16 bits, then 1|101|0010 -> 0xD2, then 00000111 -> 0x07
    assert data["hex"] == "abcd4f4bd207"
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["preamble"]["value"] == "0xabcd"
    assert fields["enabled"]["value"] is True
    assert fields["gain"]["raw_value"] == 7

    again = client.post("/api/v1/parse", json={"device_type": "mixed_dev", "hex": data["hex"]})
    parsed = {f["name"]: f for f in again.json()["fields"]}
    assert parsed["code"]["value"] == "OK"
    assert parsed["enabled"]["value"] is True
    assert parsed["level"]["value"] == 5
    assert parsed["gain"]["value"] == 2.5


def test_encode_mixed_type_validation():
    _register_ok(MIXED_DEF)
    base = {
        "preamble": "0xABCD", "code": "OK", "enabled": True,
        "level": 5, "mode": 2, "gain": 2.5,
    }
    bad_values = [
        {"preamble": "0xFFFFF"},   # does not fit in 16 bits
        {"preamble": "not-hex"},
        {"code": "TOOLONG"},       # must be exactly 2 chars
        {"code": "中文"},           # not ASCII
        {"enabled": "yes"},
        {"level": 8},              # 3 bits max 7
    ]
    for bad in bad_values:
        resp = client.post(
            "/api/v1/encode",
            json={"device_type": "mixed_dev", "values": {**base, **bad}},
        )
        assert resp.status_code == 422, bad


def test_encode_non_byte_aligned_padding():
    _register_ok(NIBBLE_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "nibble_dev", "values": {"hi": 0xA, "lo": 0xB, "tail": 0xC}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hex"] == "abc0"  # 12 bits + 4 zero padding bits
    assert data["encoded_bits"] == 12
    assert data["padded_bits"] == 4
    assert data["message_bits"] == 16
    assert data["warnings"]


def test_encode_checksum_not_byte_aligned_422():
    _register_ok(MISALIGNED_DEF)
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "misaligned_dev", "values": {"nib": 1}},
    )
    assert resp.status_code == 422


def test_encode_sum8_checksum_auto_computed():
    _register_ok({
        "device_type": "sum8_dev",
        "version": "1.0.0",
        "fields": [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "b", "type": "uint", "bit_length": 8},
            {"name": "chk", "type": "uint", "bit_length": 8},
        ],
        "checksum": {"field": "chk", "algorithm": "sum8"},
    })
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "sum8_dev", "values": {"a": 0x10, "b": 0x20}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hex"] == "102030"
    assert data["checksum"]["calculated"] == 0x30
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["chk"]["value"] == 0x30


def test_encode_version_selection_and_deactivate():
    _register_ok({
        "device_type": "enc_meter", "version": "1.0.0",
        "fields": [{"name": "val", "type": "uint", "bit_length": 8}],
    })
    _register_ok({
        "device_type": "enc_meter", "version": "2.0.0",
        "fields": [
            {"name": "val", "type": "uint", "bit_length": 8},
            {"name": "extra", "type": "uint", "bit_length": 8},
        ],
    })

    # latest active (2.0.0) is used when version is omitted
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "enc_meter", "values": {"val": 1, "extra": 2}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "2.0.0"
    assert resp.json()["hex"] == "0102"

    # ... and its required fields are enforced
    resp = client.post("/api/v1/encode", json={"device_type": "enc_meter", "values": {"val": 1}})
    assert resp.status_code == 422

    # explicit older version
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "enc_meter", "version": "1.0.0", "values": {"val": 1}},
    )
    assert resp.status_code == 200
    assert resp.json()["hex"] == "01"

    # after deactivating 2.0.0, latest falls back to 1.0.0
    client.post("/api/v1/definitions/enc_meter/2.0.0/deactivate")
    resp = client.post(
        "/api/v1/encode", json={"device_type": "enc_meter", "values": {"val": 7}}
    )
    assert resp.json()["version"] == "1.0.0"
    assert resp.json()["hex"] == "07"

    # explicit-version encode of an inactive definition still works
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "enc_meter", "version": "2.0.0", "values": {"val": 1, "extra": 2}},
    )
    assert resp.status_code == 200
    assert resp.json()["definition_active"] is False
    assert resp.json()["hex"] == "0102"


def test_encode_unknown_device_404():
    assert client.post(
        "/api/v1/encode", json={"device_type": "nope", "values": {"a": 1}}
    ).status_code == 404
    assert client.post(
        "/api/v1/encode",
        json={"device_type": "temp_sensor", "version": "9.9.9", "values": {}},
    ).status_code == 404
