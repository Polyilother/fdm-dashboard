import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.freshliance_client import (  # noqa: E402
    API_URL,
    APP_ID_ENV,
    PRIVATE_KEY_ENV,
    build_sign,
    load_local_env,
)


def now_millis():
    return int(datetime.now().timestamp() * 1000)


def to_millis(value, fallback=None):
    if value in (None, "", "-", 0, "0"):
        return fallback
    text = str(value).strip()
    if text.replace(".", "", 1).isdigit():
        number = int(float(text))
        if number > 100000000000:
            return number
        if number > 1000000000:
            return number * 1000
        return fallback
    cleaned = text.replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return int(datetime.strptime(cleaned[: len(datetime.now().strftime(fmt))], fmt).timestamp() * 1000)
        except ValueError:
            continue
    return fallback


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def safe_value(row, *names, default="-"):
    if not isinstance(row, dict):
        return default
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def extract_data(response):
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        rows = data.get("rows")
        if rows is None:
            for key in ("records", "list", "items", "data"):
                if isinstance(data.get(key), list):
                    rows = data.get(key)
                    break
        return data.get("total", len(rows or [])), rows or [], data
    if isinstance(data, list):
        return len(data), data, {}
    return 0, [], data


def call_raw(method, biz_content=None, include_biz_content=True):
    app_id = os.getenv(APP_ID_ENV, "").strip()
    private_key = os.getenv(PRIVATE_KEY_ENV, "").strip()
    if not app_id or not private_key:
        return {
            "code": "__LOCAL_CONFIG_ERROR__",
            "msg": f"缺少 {APP_ID_ENV} 或 {PRIVATE_KEY_ENV}",
            "data": None,
        }

    payload = {
        "appId": app_id,
        "method": method,
        "format": "JSON",
        "charset": "UTF-8",
        "signType": "RSA2",
        "timestamp": str(now_millis()),
        "version": "1.0",
    }
    if include_biz_content:
        payload["bizContent"] = biz_content or {}
    try:
        payload["sign"] = build_sign(payload, private_key)
        response = requests.post(API_URL, json=payload, timeout=30)
        try:
            result = response.json()
        except ValueError:
            return {"code": "__NON_JSON__", "msg": response.text[:300], "data": None}
        if response.status_code >= 400:
            result.setdefault("code", f"HTTP_{response.status_code}")
            result.setdefault("msg", response.text[:300])
        return result
    except Exception as exc:
        return {"code": "__REQUEST_ERROR__", "msg": str(exc), "data": None}


def print_header(title):
    print("\n" + "=" * 88)
    print(title)
    print("=" * 88)


def print_code_msg(method, response):
    print(f"{method}: code={response.get('code')} msg={response.get('msg') or response.get('message') or response.get('subMsg')}")


def print_rows_preview(rows, limit=3):
    preview = rows[:limit]
    if not preview:
        print("rows 前 3 条：[]")
        return
    print("rows 前 3 条：")
    for index, row in enumerate(preview, start=1):
        print(f"  [{index}] {compact_json(row)}")


def diagnose(args):
    load_local_env()
    conclusions = set()
    trip_ids = set()
    trips_by_id = {}
    probe_success = {0: False, 1: False}
    probe_nonempty = {0: False, 1: False}
    probe_total_calls = 0
    probe_permission_denied = False
    humidity_seen = False
    any_probe_error = False
    auth_ok = False
    user_info_error = False

    print_header("1. tracker.userInfo.get")
    user_info = call_raw("tracker.userInfo.get", include_biz_content=False)
    print_code_msg("tracker.userInfo.get", user_info)
    if str(user_info.get("code")) == "0":
        auth_ok = True
    else:
        user_info_error = True
    data = user_info.get("data") if isinstance(user_info.get("data"), dict) else {}
    print(f"email={safe_value(data, 'email')}")
    print(f"temperatureType={safe_value(data, 'temperatureType', 'tempType')}")

    print_header("2. tracker.userDevice.page")
    device_page = call_raw("tracker.userDevice.page", {"pageNum": 1, "pageSize": args.page_size})
    print_code_msg("tracker.userDevice.page", device_page)
    if str(device_page.get("code")) == "0":
        auth_ok = True
    device_total, devices, _ = extract_data(device_page)
    print(f"total={device_total}")
    if str(device_page.get("code")) == "0" and not devices:
        conclusions.add("appid 下无设备")
    for device in devices:
        print(
            "deviceCode={deviceCode} userDeviceId={userDeviceId} userDeviceTripId={userDeviceTripId} "
            "customName={customName} battery={battery}".format(
                deviceCode=safe_value(device, "deviceCode", "device_code"),
                userDeviceId=safe_value(device, "userDeviceId", "user_device_id", "id"),
                userDeviceTripId=safe_value(device, "userDeviceTripId", "user_device_trip_id"),
                customName=safe_value(device, "customName", "custom_name", "deviceName"),
                battery=safe_value(device, "battery", "batteryPower"),
            )
        )

    print_header("3. tracker.userDevice.get")
    for device in devices:
        user_device_id = safe_value(device, "userDeviceId", "user_device_id", "id", default="")
        if not user_device_id:
            continue
        detail_response = call_raw("tracker.userDevice.get", {"userDeviceId": user_device_id})
        print_code_msg(f"tracker.userDevice.get userDeviceId={user_device_id}", detail_response)
        detail = detail_response.get("data") if isinstance(detail_response.get("data"), dict) else {}
        print(
            "deviceStatus={deviceStatus} inProbeProperty={inProbeProperty} "
            "extProbeProperty={extProbeProperty} userDeviceTripId={userDeviceTripId}".format(
                deviceStatus=safe_value(detail, "deviceStatus", "status"),
                inProbeProperty=safe_value(detail, "inProbeProperty", "in_probe_property"),
                extProbeProperty=safe_value(detail, "extProbeProperty", "ext_probe_property"),
                userDeviceTripId=safe_value(detail, "userDeviceTripId", "user_device_trip_id"),
            )
        )

    print_header("4. tracker.deviceGroup.page / tracker.deviceTrip.page")
    group_page = call_raw("tracker.deviceGroup.page", {"pageNum": 1, "pageSize": args.page_size})
    print_code_msg("tracker.deviceGroup.page", group_page)
    group_total, group_rows, _ = extract_data(group_page)
    print(f"deviceGroup total={group_total}")

    query_begin = now_millis() - args.days * 24 * 3600 * 1000
    query_end = now_millis()
    global_trip_page = call_raw(
        "tracker.deviceTrip.page",
        {
            "pageNum": 1,
            "pageSize": args.page_size,
            "beginTime": query_begin,
            "endTime": query_end,
        },
    )
    print_code_msg("tracker.deviceTrip.page all devices", global_trip_page)
    global_trip_total, _, _ = extract_data(global_trip_page)
    print(f"all devices trip total={global_trip_total}")

    for device in devices:
        device_code = safe_value(device, "deviceCode", "device_code", default="")
        if not device_code:
            continue
        trip_page = call_raw(
            "tracker.deviceTrip.page",
            {
                "pageNum": 1,
                "pageSize": args.page_size,
                "deviceCode": device_code,
                "beginTime": query_begin,
                "endTime": query_end,
            },
        )
        print_code_msg(f"tracker.deviceTrip.page deviceCode={device_code}", trip_page)
        trip_total, trips, _ = extract_data(trip_page)
        print(f"deviceCode={device_code} total={trip_total}")
        if str(trip_page.get("code")) == "0" and not trips:
            conclusions.add("设备无行程")
        for trip in trips:
            trip_id = safe_value(trip, "userDeviceTripId", "user_device_trip_id", "id", default="")
            if trip_id:
                trip_ids.add(str(trip_id))
                trips_by_id[str(trip_id)] = trip
            print(
                "userDeviceTripId={userDeviceTripId} tripStatus={tripStatus} "
                "actualBeginTime={actualBeginTime} actualEndTime={actualEndTime}".format(
                    userDeviceTripId=safe_value(trip, "userDeviceTripId", "user_device_trip_id", "id"),
                    tripStatus=safe_value(trip, "tripStatus", "status"),
                    actualBeginTime=safe_value(trip, "actualBeginTime", "beginTime", "actual_begin_time"),
                    actualEndTime=safe_value(trip, "actualEndTime", "endTime", "actual_end_time"),
                )
            )

    print_header("5. tracker.deviceTrip.get")
    for trip_id in sorted(trip_ids):
        detail_response = call_raw("tracker.deviceTrip.get", {"userDeviceTripId": trip_id})
        print_code_msg(f"tracker.deviceTrip.get userDeviceTripId={trip_id}", detail_response)
        detail = detail_response.get("data") if isinstance(detail_response.get("data"), dict) else {}
        permission = detail.get("trackerPermission") if isinstance(detail.get("trackerPermission"), dict) else {}
        if str(detail_response.get("code")) != "0":
            any_probe_error = True
        if any(str(permission.get(k, "")) in ("0", "False", "false") for k in ("dataListFlag", "dataChartFlag", "csvExportFlag")):
            probe_permission_denied = True
        print(
            "trackerPermission.dataListFlag={dataListFlag} dataChartFlag={dataChartFlag} "
            "csvExportFlag={csvExportFlag} collectInterval={collectInterval} activeTime={activeTime}".format(
                dataListFlag=safe_value(permission, "dataListFlag"),
                dataChartFlag=safe_value(permission, "dataChartFlag"),
                csvExportFlag=safe_value(permission, "csvExportFlag"),
                collectInterval=safe_value(detail, "collectInterval"),
                activeTime=safe_value(detail, "activeTime"),
            )
        )

    print_header("6-7. tracker.tripData.pageProbeData")
    for trip_id in sorted(trip_ids):
        trip = trips_by_id.get(trip_id, {})
        begin_raw = safe_value(trip, "actualBeginTime", "beginTime", "actual_begin_time", default="")
        end_raw = safe_value(trip, "actualEndTime", "endTime", "actual_end_time", default="")
        begin_time = to_millis(begin_raw)
        end_time = to_millis(end_raw, fallback=now_millis())
        if not begin_time:
            conclusions.add("行程未开始")
            print(f"userDeviceTripId={trip_id} actualBeginTime 为空，跳过 pageProbeData。")
            continue
        if end_time <= begin_time:
            end_time = now_millis()
        for probe_type in (0, 1):
            probe_total_calls += 1
            response = call_raw(
                "tracker.tripData.pageProbeData",
                {
                    "userDeviceTripId": trip_id,
                    "probeType": probe_type,
                    "beginTime": begin_time,
                    "endTime": end_time,
                    "pageNum": 1,
                    "pageSize": args.probe_page_size,
                },
            )
            print_code_msg(
                f"tracker.tripData.pageProbeData userDeviceTripId={trip_id} probeType={probe_type}",
                response,
            )
            total, rows, _ = extract_data(response)
            print(f"beginTime(ms)={begin_time} endTime(ms)={end_time} total={total}")
            print_rows_preview(rows)
            code = str(response.get("code"))
            msg = str(response.get("msg") or response.get("message") or response.get("subMsg") or "")
            if code == "0":
                probe_success[probe_type] = True
                if rows:
                    probe_nonempty[probe_type] = True
                    if any(safe_value(row, "humidity", "hum", "h", default=None) is not None for row in rows):
                        humidity_seen = True
            else:
                any_probe_error = True
                if "permission" in msg.lower() or "权限" in msg:
                    probe_permission_denied = True
                if "trip" in msg.lower() or "userDeviceTripId" in msg:
                    conclusions.add("userDeviceTripId 错误")
                if "time" in msg.lower() or "时间" in msg:
                    conclusions.add("时间范围错误")

    print_header("诊断结论")
    if not auth_ok:
        conclusions.add("鉴权失败")
    if user_info_error and auth_ok:
        conclusions.add("tracker.userInfo.get 服务异常，但其他接口已通过鉴权")
    if probe_permission_denied:
        conclusions.add("权限不足")
    if trip_ids and probe_total_calls and not any(probe_success.values()) and any_probe_error:
        conclusions.add("userDeviceTripId 错误")
    if trip_ids and any(probe_success.values()) and not any(probe_nonempty.values()):
        conclusions.add("设备未上传数据")
    if trip_ids and probe_nonempty[0] and not probe_nonempty[1]:
        conclusions.add("探头类型错误")
    if any(probe_nonempty.values()) and not humidity_seen:
        conclusions.add("设备不支持湿度")
    if not conclusions:
        conclusions.add("未发现明显异常：鉴权、设备、行程、探头数据链路至少有一条可用。")

    for item in [
        "鉴权失败",
        "appid 下无设备",
        "设备无行程",
        "行程未开始",
        "userDeviceTripId 错误",
        "时间范围错误",
        "探头类型错误",
        "设备不支持湿度",
        "权限不足",
        "设备未上传数据",
    ]:
        if item in conclusions:
            print(f"- {item}")
    for item in sorted(conclusions - {
        "鉴权失败",
        "appid 下无设备",
        "设备无行程",
        "行程未开始",
        "userDeviceTripId 错误",
        "时间范围错误",
        "探头类型错误",
        "设备不支持湿度",
        "权限不足",
        "设备未上传数据",
    }):
        print(f"- {item}")


def main():
    parser = argparse.ArgumentParser(description="Freshliance API 完整链路诊断")
    parser.add_argument("--days", type=int, default=30, help="查询最近多少天行程，默认 30")
    parser.add_argument("--page-size", type=int, default=50, help="设备/行程分页大小，默认 50")
    parser.add_argument("--probe-page-size", type=int, default=10, help="探头数据分页大小，默认 10")
    args = parser.parse_args()
    diagnose(args)


if __name__ == "__main__":
    main()
