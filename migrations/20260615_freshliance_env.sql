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
    last_sync_at TEXT,
    record_id TEXT,
    device_sn TEXT,
    gateway_sn TEXT,
    product_model TEXT
);

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
    created_at TEXT,
    record_id TEXT,
    device_sn TEXT
);

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
    record_id TEXT,
    device_sn TEXT,
    UNIQUE(device_code, user_device_trip_id, probe_type, recorded_at)
);
