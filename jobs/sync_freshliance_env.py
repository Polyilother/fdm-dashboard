import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.freshliance_client import (  # noqa: E402
    FreshlianceAPIError,
    get_gateway_data,
    get_gateway_devices,
    get_gateway_records,
    load_local_env,
)


logging.basicConfig(
    level=os.getenv("FRESHLIANCE_SYNC_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("freshliance_sync")
load_local_env()


def get_pg_connection():
    return psycopg2.connect(
        host=os.getenv("FDM_PGHOST", "localhost"),
        port=int(os.getenv("FDM_PGPORT", "5432")),
        dbname=os.getenv("FDM_PGDATABASE", "fdm_dashboard"),
        user=os.getenv("FDM_PGUSER", "postgres"),
        password=os.getenv("FDM_PGPASSWORD", ""),
        sslmode=os.getenv("FDM_PGSSLMODE", "prefer"),
    )


def ensure_column(cur, table_name, column_name, column_type):
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = %s
        """,
        (table_name, column_name),
    )
    if not cur.fetchone():
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def ensure_env_tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS env_devices (
                id SERIAL PRIMARY KEY,
                user_device_id TEXT UNIQUE NOT NULL,
                device_code TEXT,
                custom_name TEXT,
                device_status TEXT,
                use_type TEXT,
                battery REAL,
                battery_time TEXT,
                in_probe_property TEXT,
                ext_probe_property TEXT,
                last_sync_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS env_device_trips (
                id SERIAL PRIMARY KEY,
                user_device_trip_id TEXT UNIQUE NOT NULL,
                user_device_id TEXT,
                device_code TEXT,
                trip_status TEXT,
                trip_code TEXT,
                custom_name TEXT,
                actual_begin_time TEXT,
                actual_end_time TEXT,
                collect_interval TEXT,
                active_time TEXT,
                created_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS env_readings (
                id SERIAL PRIMARY KEY,
                device_code TEXT NOT NULL,
                user_device_trip_id TEXT NOT NULL,
                probe_type INTEGER NOT NULL,
                recorded_at TEXT NOT NULL,
                temperature REAL,
                humidity REAL,
                light REAL,
                shock REAL,
                longitude TEXT,
                latitude TEXT,
                created_at TEXT,
                UNIQUE(device_code, user_device_trip_id, probe_type, recorded_at)
            )
            """
        )
        for column_name, column_type in (
            ("record_id", "TEXT"),
            ("device_sn", "TEXT"),
            ("gateway_sn", "TEXT"),
            ("product_model", "TEXT"),
        ):
            ensure_column(cur, "env_devices", column_name, column_type)
        for column_name, column_type in (
            ("record_id", "TEXT"),
            ("device_sn", "TEXT"),
        ):
            ensure_column(cur, "env_device_trips", column_name, column_type)
            ensure_column(cur, "env_readings", column_name, column_type)
    conn.commit()


def pick(row, *names, default=None):
    if not isinstance(row, dict):
        return default
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def as_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_api_time(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        number = int(float(text))
        if number > 100000000000:
            return datetime.fromtimestamp(number / 1000).strftime("%Y-%m-%d %H:%M:%S")
        if number > 1000000000:
            return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, TypeError, ValueError):
        pass
    return text


def extract_items(response):
    data = response.get("data", response) if isinstance(response, dict) else response
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("rows", "records", "list", "items", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def fetch_all(fetcher, page_size=50):
    page_num = 1
    while True:
        response = fetcher(page_num, page_size)
        items = extract_items(response)
        if not items:
            break
        yield from items
        if len(items) < page_size:
            break
        page_num += 1


def fetch_probe_readings_with_retry(record_id, probe_type, begin_ms, end_ms, page_size=50, attempts=3):
    for attempt in range(1, attempts + 1):
        readings = list(fetch_all(
            lambda p, s: get_gateway_data(
                record_id,
                probe_type=probe_type,
                begin_time=begin_ms,
                end_time=end_ms,
                page_num=p,
                page_size=s,
            ),
            page_size=page_size,
        ))
        if readings or attempt == attempts:
            return readings
        logger.warning(
            "Freshliance returned empty probe data, retrying %s/%s: recordId=%s probeType=%s",
            attempt,
            attempts,
            record_id,
            probe_type,
        )
        time.sleep(2 * attempt)
    return []


def seconds_until_next_hour(now=None):
    now = now or datetime.now()
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return max(1, int((next_hour - now).total_seconds()))


def get_device_info(device_row):
    if isinstance(device_row, dict) and isinstance(device_row.get("deviceInfo"), dict):
        return device_row["deviceInfo"]
    return device_row if isinstance(device_row, dict) else {}


def get_record_info(record_row):
    if isinstance(record_row, dict) and isinstance(record_row.get("deviceRecord"), dict):
        return record_row["deviceRecord"]
    return record_row if isinstance(record_row, dict) else {}


def select_recent_records(records, current_record_id=None, max_records=3):
    if not max_records or max_records <= 0:
        return records
    selected = []
    seen = set()
    current_record_id = str(current_record_id or "").strip()

    def add_record(record_row):
        info = get_record_info(record_row)
        record_id = str(pick(info, "recordId", default="")).strip()
        key = record_id or id(record_row)
        if key in seen:
            return
        seen.add(key)
        selected.append(record_row)

    if current_record_id:
        for record_row in records:
            info = get_record_info(record_row)
            if str(pick(info, "recordId", default="")).strip() == current_record_id:
                add_record(record_row)
                break

    for record_row in records:
        if len(selected) >= max_records:
            break
        add_record(record_row)
    return selected


def get_sensor_rows(record_row):
    if not isinstance(record_row, dict):
        return []
    value = record_row.get("sensorConfig")
    return value if isinstance(value, list) else []


def get_initial_probe_rows(device_row):
    if not isinstance(device_row, dict):
        return []
    value = device_row.get("subDeviceLastDataList")
    return value if isinstance(value, list) else []


def upsert_device(conn, device_info, sync_time):
    user_device_id = str(pick(device_info, "userDeviceId", "userParentId", "deviceId", default="")).strip()
    if not user_device_id:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO env_devices (
                user_device_id, device_code, custom_name, device_status, use_type,
                battery, battery_time, in_probe_property, ext_probe_property, last_sync_at,
                record_id, device_sn, gateway_sn, product_model
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_device_id) DO UPDATE SET
                device_code = EXCLUDED.device_code,
                custom_name = EXCLUDED.custom_name,
                device_status = EXCLUDED.device_status,
                use_type = EXCLUDED.use_type,
                battery = EXCLUDED.battery,
                battery_time = EXCLUDED.battery_time,
                in_probe_property = EXCLUDED.in_probe_property,
                ext_probe_property = EXCLUDED.ext_probe_property,
                last_sync_at = EXCLUDED.last_sync_at,
                record_id = EXCLUDED.record_id,
                device_sn = EXCLUDED.device_sn,
                gateway_sn = EXCLUDED.gateway_sn,
                product_model = EXCLUDED.product_model
            """,
            (
                user_device_id,
                pick(device_info, "deviceCode"),
                pick(device_info, "deviceName", "customName"),
                str(pick(device_info, "deviceStatus", default="")),
                str(pick(device_info, "productType", "useType", default="")),
                as_float(pick(device_info, "devicePower", "battery")),
                format_api_time(pick(device_info, "powerTime", "batteryTime")),
                str(pick(device_info, "inProbeProperty", default="")),
                str(pick(device_info, "extProbeProperty", default="")),
                sync_time,
                str(pick(device_info, "recordId", default="")),
                pick(device_info, "deviceSn"),
                pick(device_info, "gatewaySn"),
                pick(device_info, "productModel"),
            ),
        )
    return True


def upsert_record(conn, record_info, device_info):
    record_id = str(pick(record_info, "recordId", default=pick(device_info, "recordId", default=""))).strip()
    if not record_id:
        return False
    record_key = f"gw:{record_id}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO env_device_trips (
                user_device_trip_id, user_device_id, device_code, trip_status, trip_code,
                custom_name, actual_begin_time, actual_end_time, collect_interval,
                active_time, created_at, record_id, device_sn
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_device_trip_id) DO UPDATE SET
                user_device_id = EXCLUDED.user_device_id,
                device_code = EXCLUDED.device_code,
                trip_status = EXCLUDED.trip_status,
                trip_code = EXCLUDED.trip_code,
                custom_name = EXCLUDED.custom_name,
                actual_begin_time = EXCLUDED.actual_begin_time,
                actual_end_time = EXCLUDED.actual_end_time,
                collect_interval = EXCLUDED.collect_interval,
                active_time = EXCLUDED.active_time,
                record_id = EXCLUDED.record_id,
                device_sn = EXCLUDED.device_sn
            """,
            (
                record_key,
                str(pick(device_info, "userDeviceId", "deviceId", default="")),
                pick(device_info, "deviceCode"),
                str(pick(record_info, "recordStatus", default="")),
                record_id,
                pick(record_info, "deviceName", "parentDeviceName", default=pick(device_info, "deviceName")),
                format_api_time(pick(record_info, "actualBeginTime", "beginTime", "startTime")),
                format_api_time(pick(record_info, "actualEndTime", "endTime", "stopTime")),
                str(pick(record_info, "collectInterval", default="")),
                str(pick(record_info, "activeTime", default="")),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                record_id,
                pick(record_info, "deviceSn", default=pick(device_info, "deviceSn")),
            ),
        )
    return True


def upsert_reading(conn, row, device_code, record_id, device_sn, probe_type):
    recorded_at = format_api_time(pick(row, "dataTime", "ts", "recordedAt", "recordTime", "time", "collectTime"))
    if not recorded_at or not device_code or not record_id:
        return False
    record_key = f"gw:{record_id}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO env_readings (
                device_code, user_device_trip_id, probe_type, recorded_at,
                temperature, humidity, light, shock, longitude, latitude, created_at,
                record_id, device_sn
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (device_code, user_device_trip_id, probe_type, recorded_at) DO UPDATE SET
                temperature = EXCLUDED.temperature,
                humidity = EXCLUDED.humidity,
                light = EXCLUDED.light,
                shock = EXCLUDED.shock,
                longitude = EXCLUDED.longitude,
                latitude = EXCLUDED.latitude,
                record_id = EXCLUDED.record_id,
                device_sn = EXCLUDED.device_sn
            """,
            (
                device_code,
                record_key,
                probe_type,
                recorded_at,
                as_float(pick(row, "temperature", "temp", "t")),
                as_float(pick(row, "humidity", "hum", "h")),
                as_float(pick(row, "light")),
                as_float(pick(row, "shock")),
                pick(row, "longitude", "lng"),
                pick(row, "latitude", "lat"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(record_id),
                device_sn,
            ),
        )
    return True


def probe_types_for_record(record_row, fallback_probe_type):
    # Freshliance gateway devices in the print room use 3 probes:
    # 0 = internal probe, 1/2 = external probes.
    types = {0, 1, 2}
    if fallback_probe_type is not None:
        types.add(int(fallback_probe_type))
    for sensor in get_sensor_rows(record_row):
        value = pick(sensor, "probeType")
        if value is not None:
            try:
                types.add(int(value))
            except (TypeError, ValueError):
                pass
    if not types:
        types.add(0)
    return sorted(types)


def sync(hours, probe_type, page_size, max_records=3):
    end_dt = datetime.now()
    begin_dt = end_dt - timedelta(hours=hours)
    begin_ms = int(begin_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    sync_time = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    summary = {"devices": 0, "records": 0, "readings": 0}

    with get_pg_connection() as conn:
        ensure_env_tables(conn)

        devices = list(fetch_all(lambda p, s: get_gateway_devices(p, s), page_size=page_size))
        for device_row in devices:
            device_info = get_device_info(device_row)
            if not device_info:
                continue
            if upsert_device(conn, device_info, sync_time):
                summary["devices"] += 1

            device_code = pick(device_info, "deviceCode")
            device_id = pick(device_info, "deviceId")
            device_sn = pick(device_info, "deviceSn")
            current_record_id = pick(device_info, "recordId")

            if current_record_id:
                for probe_row in get_initial_probe_rows(device_row):
                    row_probe_type = pick(probe_row, "probeType", default=0)
                    row_record_id = pick(probe_row, "recordId", "userDeviceTripId", default=current_record_id)
                    if upsert_reading(conn, probe_row, device_code, row_record_id, device_sn, int(row_probe_type)):
                        summary["readings"] += 1

            records = list(fetch_all(
                lambda p, s, did=device_id, dsn=device_sn: get_gateway_records(did, dsn, p, s),
                page_size=page_size,
            ))
            if not records and current_record_id:
                records = [{"deviceRecord": {"recordId": current_record_id, "deviceSn": device_sn}}]
            records = select_recent_records(records, current_record_id, max_records)

            for record_row in records:
                record_info = get_record_info(record_row)
                record_id = pick(record_info, "recordId", default=current_record_id)
                if not record_id:
                    continue
                if upsert_record(conn, record_info, device_info):
                    summary["records"] += 1

                for probe in probe_types_for_record(record_row, probe_type):
                    for reading in fetch_probe_readings_with_retry(record_id, probe, begin_ms, end_ms, page_size):
                        if upsert_reading(conn, reading, device_code, record_id, device_sn, probe):
                            summary["readings"] += 1
                conn.commit()

        conn.commit()
    return summary


def main():
    parser = argparse.ArgumentParser(description="Sync Freshliance gateway temperature/humidity data to PostgreSQL")
    parser.add_argument("--hours", type=int, default=72, help="Sync recent hours, default 72")
    parser.add_argument("--probe-type", type=int, default=0, help="Preferred probe type, default 0")
    parser.add_argument("--page-size", type=int, default=50, help="Page size, default 50")
    parser.add_argument("--max-records", type=int, default=3, help="Max recent records per device to sync, default 3")
    parser.add_argument("--loop", action="store_true", help="Keep syncing automatically")
    parser.add_argument("--interval-minutes", type=int, default=60, help="Loop sync interval in minutes, default 60")
    parser.add_argument("--align-hour", action="store_true", help="After the first sync, run subsequent syncs at the top of each hour")
    args = parser.parse_args()

    while True:
        try:
            summary = sync(args.hours, args.probe_type, args.page_size, args.max_records)
            logger.info("Freshliance gateway sync finished: %s", summary)
        except (FreshlianceAPIError, psycopg2.Error) as exc:
            logger.exception("Freshliance gateway sync failed: %s", exc)
            if not args.loop:
                raise SystemExit(1) from exc
        if not args.loop:
            break
        if args.align_hour:
            sleep_seconds = seconds_until_next_hour()
            next_time = datetime.now() + timedelta(seconds=sleep_seconds)
            logger.info("Next Freshliance gateway sync at %s.", next_time.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            sleep_seconds = max(1, args.interval_minutes) * 60
            logger.info("Next Freshliance gateway sync after %s minutes.", args.interval_minutes)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
