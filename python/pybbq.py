import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from bleak import BleakClient, BleakScanner

CREDENTIALS_MESSAGE = bytearray.fromhex("2107060504030201b8220000000000")
REALTIME_DATA_ENABLE = bytearray.fromhex("0B0100000000")
UNITS_FAHRENHEIT = bytearray.fromhex("020100000000")
UNITS_CELSIUS = bytearray.fromhex("020000000000")
BATTERY_LEVEL = bytearray.fromhex("082400000000")

IBBQ_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
SETTINGS_RESULTS_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
PAIR_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
REALTIMEDATA_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"
CMD_UUID = "0000fff5-0000-1000-8000-00805f9b34fb"

ES_URL = os.environ.get("ES_URL", "https://localhost:9200")
ES_USER = os.environ.get("ES_USER", "elastic")
ES_PASSWORD = os.environ.get("ES_PASSWORD", "")
ES_INDEX = os.environ.get("ES_INDEX", "bbq")
ES_VERIFY_TLS = os.environ.get("ES_VERIFY_TLS", "true").lower() == "true"
CH_URL = os.environ.get("CH_URL", "")
CH_USER = os.environ.get("CH_USER", "default")
CH_PASSWORD = os.environ.get("CH_PASSWORD", "")
CH_DATABASE = os.environ.get("CH_DATABASE", "bbq")
CH_TABLE = os.environ.get("CH_TABLE", "readings")
TEMP_UNITS = os.environ.get("TEMP_UNITS", "f").lower()
BULK_INTERVAL = int(os.environ.get("BULK_INTERVAL", "5"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
MAX_RECONNECT_ATTEMPTS = int(os.environ.get("MAX_RECONNECT_ATTEMPTS", "10"))
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "5"))

SESSION_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

INDEX_TEMPLATE = {
    "index_patterns": [],
    "data_stream": {},
    "priority": 200,
    "template": {
        "settings": {
            "number_of_replicas": 1
        },
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "bbq_temp": {"type": "float"},
                "bbq_probe": {"type": "integer"},
                "bbq_battery": {"type": "float"},
                "session_id": {"type": "keyword"},
            }
        }
    }
}


def log(msg):
    print(msg, flush=True)


def debug(msg):
    if DEBUG:
        log(f"[DEBUG] {msg}")


bulk_buffer = []


def ensure_index_template():
    if not ES_PASSWORD:
        return
    template_name = f"{ES_INDEX}-template"
    url = f"{ES_URL}/_index_template/{template_name}"
    auth = (ES_USER, ES_PASSWORD)

    try:
        resp = requests.get(url, auth=auth, verify=ES_VERIFY_TLS)
        if resp.status_code == 200:
            debug(f"Index template '{template_name}' already exists")
            return
    except requests.RequestException as e:
        log(f"ES connection error checking template: {e}")
        return

    template = json.loads(json.dumps(INDEX_TEMPLATE))
    template["index_patterns"] = [f"{ES_INDEX}*"]

    try:
        resp = requests.put(
            url, auth=auth, json=template, verify=ES_VERIFY_TLS
        )
        if resp.status_code == 200:
            log(f"Created index template '{template_name}' with data stream")
        else:
            log(f"Failed to create index template ({resp.status_code}): {resp.text[:200]}")
    except requests.RequestException as e:
        log(f"ES connection error creating template: {e}")


def _validate_identifier(name, label):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
        raise ValueError(f"Invalid {label}: '{name}' — must be alphanumeric/underscore only")


def ch_query(sql):
    try:
        resp = requests.post(
            CH_URL,
            params={"user": CH_USER, "password": CH_PASSWORD},
            data=sql,
        )
        if resp.status_code != 200:
            log(f"ClickHouse error ({resp.status_code}): {resp.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"ClickHouse connection error: {e}")
        return False


def ensure_ch_table():
    if not CH_URL:
        return
    _validate_identifier(CH_DATABASE, "CH_DATABASE")
    _validate_identifier(CH_TABLE, "CH_TABLE")
    if not ch_query(f"CREATE DATABASE IF NOT EXISTS {CH_DATABASE}"):
        return
    create_sql = (
        f"CREATE TABLE IF NOT EXISTS {CH_DATABASE}.{CH_TABLE} ("
        "  timestamp DateTime64(3, 'UTC'),"
        "  bbq_temp Nullable(Float32),"
        "  bbq_probe Nullable(UInt8),"
        "  bbq_battery Nullable(Float32),"
        "  session_id String"
        ") ENGINE = MergeTree()"
        " ORDER BY (session_id, timestamp)"
    )
    if ch_query(create_sql):
        log(f"ClickHouse table {CH_DATABASE}.{CH_TABLE} ready")


def flush_ch():
    if not bulk_buffer or not CH_URL:
        return

    ch_rows = []
    for doc in bulk_buffer:
        row = {
            "timestamp": doc["@timestamp"],
            "bbq_temp": doc.get("bbq_temp"),
            "bbq_probe": doc.get("bbq_probe"),
            "bbq_battery": doc.get("bbq_battery"),
            "session_id": doc.get("session_id", SESSION_ID),
        }
        ch_rows.append(json.dumps(row))

    body = "\n".join(ch_rows) + "\n"
    try:
        resp = requests.post(
            CH_URL,
            params={
                "user": CH_USER,
                "password": CH_PASSWORD,
                "query": f"INSERT INTO {CH_DATABASE}.{CH_TABLE} FORMAT JSONEachRow",
            },
            data=body,
            headers={"Content-Type": "application/x-ndjson"},
        )
        if resp.status_code != 200:
            log(f"ClickHouse insert error ({resp.status_code}): {resp.text[:200]}")
        else:
            debug(f"Flushed {len(ch_rows)} docs to ClickHouse")
    except requests.RequestException as e:
        log(f"ClickHouse connection error: {e}")


def handle_realtime_data(sender, data: bytearray):
    temps = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
    debug(f"Raw temp data: {temps}")
    now = datetime.now(timezone.utc).isoformat()

    for idx, raw in enumerate(temps):
        if raw == 0 or raw >= 65526:
            continue
        temp_c = raw / 10.0
        if TEMP_UNITS == "f":
            temp = round(temp_c * 1.8 + 32, 1)
        elif TEMP_UNITS == "k":
            temp = round(temp_c + 273.15, 1)
        else:
            temp = round(temp_c, 1)

        doc = {"@timestamp": now, "bbq_temp": temp, "bbq_probe": idx + 1, "session_id": SESSION_ID}
        bulk_buffer.append(doc)
        log(f"  Probe {idx + 1}: {temp} {TEMP_UNITS.upper()}")


def handle_settings(sender, data: bytearray):
    if len(data) < 6:
        debug(f"Settings data too short: {data.hex()}")
        return
    header = data[0]
    if header == 0x24:
        current_voltage = int.from_bytes(data[1:3], "little")
        max_voltage = int.from_bytes(data[3:5], "little")
        if max_voltage == 0:
            max_voltage = 6550
        pct = min(100.0, round(100.0 * current_voltage / max_voltage, 1))
        now = datetime.now(timezone.utc).isoformat()
        doc = {"@timestamp": now, "bbq_battery": pct, "session_id": SESSION_ID}
        bulk_buffer.append(doc)
        log(f"  Battery: {pct}%")
    else:
        debug(f"Settings header 0x{header:02x}, data: {data.hex()}")


def flush_es():
    if not ES_PASSWORD:
        return

    target = f"{ES_URL}/{ES_INDEX}/_bulk"
    auth = (ES_USER, ES_PASSWORD)
    headers = {"Content-Type": "application/x-ndjson"}

    body_lines = []
    for doc in bulk_buffer:
        body_lines.append(json.dumps({"create": {}}))
        body_lines.append(json.dumps(doc))
    body = "\n".join(body_lines) + "\n"

    try:
        resp = requests.post(
            target, auth=auth, headers=headers, data=body, verify=ES_VERIFY_TLS
        )
        if resp.status_code not in (200, 201):
            log(f"ES bulk error ({resp.status_code}): {resp.text[:200]}")
        else:
            result = resp.json()
            if result.get("errors"):
                for item in result["items"]:
                    err = item.get("create", {}).get("error")
                    if err:
                        log(f"ES doc error: {err}")
            else:
                debug(f"Flushed {len(bulk_buffer)} docs to ES")
    except requests.RequestException as e:
        log(f"ES connection error: {e}")


def flush_bulk():
    if not bulk_buffer:
        return
    if not ES_PASSWORD and not CH_URL:
        debug("No outputs configured, skipping upload")
        bulk_buffer.clear()
        return

    flush_es()
    flush_ch()
    bulk_buffer.clear()


async def find_ibbq(scan_seconds=10):
    log(f"Scanning for iBBQ devices ({scan_seconds}s)...")
    target = None
    best_rssi = -999

    def detection_callback(device, adv_data):
        nonlocal target, best_rssi
        svc_uuids = adv_data.service_uuids or []
        name = device.name or adv_data.local_name or ""
        if IBBQ_SERVICE_UUID in svc_uuids or "ibbq" in name.lower():
            if adv_data.rssi > best_rssi:
                target = device
                best_rssi = adv_data.rssi
                debug(f"Found: {name} [{device.address}] RSSI={adv_data.rssi}")

    scanner = BleakScanner(detection_callback=detection_callback)
    await scanner.start()
    await asyncio.sleep(scan_seconds)
    await scanner.stop()

    if target:
        log(f"Using iBBQ at {target.address} (RSSI {best_rssi})")
    return target


async def run_session(device):
    async with BleakClient(device.address) as client:
        log("Connected to iBBQ")

        await client.write_gatt_char(PAIR_UUID, CREDENTIALS_MESSAGE)
        debug("Authenticated")

        await client.write_gatt_char(CMD_UUID, REALTIME_DATA_ENABLE, response=True)
        if TEMP_UNITS == "f":
            await client.write_gatt_char(CMD_UUID, UNITS_FAHRENHEIT, response=True)
        else:
            await client.write_gatt_char(CMD_UUID, UNITS_CELSIUS, response=True)
        await client.write_gatt_char(CMD_UUID, BATTERY_LEVEL, response=True)

        await client.start_notify(REALTIMEDATA_UUID, handle_realtime_data)
        await client.start_notify(SETTINGS_RESULTS_UUID, handle_settings)

        log(f"Listening (units={TEMP_UNITS.upper()}, bulk every {BULK_INTERVAL}s)...\n")

        last_flush = time.monotonic()
        while client.is_connected:
            await asyncio.sleep(1)
            if time.monotonic() - last_flush >= BULK_INTERVAL:
                flush_bulk()
                last_flush = time.monotonic()

    flush_bulk()


async def main():
    log(f"Session: {SESSION_ID}")
    outputs = []
    if ES_PASSWORD:
        ensure_index_template()
        outputs.append("Elasticsearch")
    if CH_URL:
        ensure_ch_table()
        outputs.append("ClickHouse")
    if not outputs:
        log("WARNING: No outputs configured — readings will print only")
    else:
        log(f"Outputs: {', '.join(outputs)}")

    attempt = 0
    while attempt < MAX_RECONNECT_ATTEMPTS:
        device = await find_ibbq()
        if device is None:
            attempt += 1
            log(f"No iBBQ found (attempt {attempt}/{MAX_RECONNECT_ATTEMPTS})")
            await asyncio.sleep(RECONNECT_DELAY)
            continue

        try:
            attempt = 0
            await run_session(device)
            log("Device disconnected.")
        except Exception as e:
            log(f"Connection error: {e}")

        attempt += 1
        if attempt < MAX_RECONNECT_ATTEMPTS:
            log(f"Reconnecting in {RECONNECT_DELAY}s (attempt {attempt}/{MAX_RECONNECT_ATTEMPTS})...")
            await asyncio.sleep(RECONNECT_DELAY)

    log("Max reconnect attempts reached, exiting.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("\nShutting down.")
        flush_bulk()
