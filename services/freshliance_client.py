import base64
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


API_URL = os.getenv("FRESHLIANCE_API_URL", "https://api.freshliance.com/api")
APP_ID_ENV = "FRESHLIANCE_APP_ID"
PRIVATE_KEY_ENV = "FRESHLIANCE_PRIVATE_KEY"

logger = logging.getLogger(__name__)


class FreshlianceAPIError(RuntimeError):
    pass


def load_local_env():
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.exists():
        return
    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as exc:
        logger.warning("本地 .env 读取失败：%s", exc)


def _clean_params_for_sign(params):
    return {
        key: value
        for key, value in params.items()
        if key != "sign" and value is not None and value != ""
    }


def _sign_source(params):
    clean_params = _clean_params_for_sign(params)
    parts = []
    for key in sorted(clean_params.keys()):
        value = clean_params[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{key}={value}")
    return "&".join(parts)


def _load_private_key(private_key):
    key_text = private_key.strip().replace("\\n", "\n")
    if "BEGIN" not in key_text:
        try:
            return serialization.load_der_private_key(base64.b64decode(key_text), password=None)
        except ValueError as exc:
            raise FreshlianceAPIError("Freshliance RSA 私钥格式无效，请确认平台私钥未被截断。") from exc
    try:
        return serialization.load_pem_private_key(key_text.encode("utf-8"), password=None)
    except ValueError as exc:
        raise FreshlianceAPIError("Freshliance RSA 私钥格式无效，请确认环境变量为 PEM 私钥。") from exc


def build_sign(params, private_key):
    signer = _load_private_key(private_key)
    source = _sign_source(params).encode("utf-8")
    signature = signer.sign(source, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")


def _get_credentials():
    load_local_env()
    app_id = os.getenv(APP_ID_ENV, "").strip()
    private_key = os.getenv(PRIVATE_KEY_ENV, "").strip()
    if not app_id:
        raise FreshlianceAPIError(f"缺少环境变量 {APP_ID_ENV}")
    if not private_key:
        raise FreshlianceAPIError(f"缺少环境变量 {PRIVATE_KEY_ENV}")
    return app_id, private_key


def _sanitize_payload(payload):
    sanitized = dict(payload)
    if "sign" in sanitized:
        sanitized["sign"] = "***"
    return sanitized


def _post_with_retry(method, payload):
    last_exc = None
    for attempt in range(1, 4):
        try:
            return requests.post(API_URL, json=payload, timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 3:
                logger.warning("Freshliance API network retry %s/3: method=%s", attempt, method)
                time.sleep(2 * attempt)
                continue
            raise last_exc


def call_api(method, biz_content):
    app_id, private_key = _get_credentials()
    payload = {
        "appId": app_id,
        "method": method,
        "format": "JSON",
        "charset": "UTF-8",
        "signType": "RSA2",
        "timestamp": str(int(datetime.now().timestamp() * 1000)),
        "version": "1.0",
        "bizContent": biz_content or {},
    }
    payload["sign"] = build_sign(payload, private_key)

    try:
        response = _post_with_retry(method, payload)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        logger.exception("Freshliance API 网络请求失败: method=%s payload=%s", method, _sanitize_payload(payload))
        raise FreshlianceAPIError(f"Freshliance API 网络请求失败：{exc}") from exc
    except ValueError as exc:
        logger.exception("Freshliance API 返回非 JSON: method=%s", method)
        raise FreshlianceAPIError("Freshliance API 返回内容不是有效 JSON。") from exc

    code = str(result.get("code", ""))
    if code != "0":
        message = result.get("msg") or result.get("message") or result.get("subMsg") or "未知错误"
        logger.warning("Freshliance API 返回错误: method=%s code=%s message=%s", method, code, message)
        raise FreshlianceAPIError(f"Freshliance API 调用失败：{method} code={code} message={message}")

    return result


def get_user_devices(page_num=1, page_size=50):
    return call_api("tracker.userDevice.page", {"pageNum": page_num, "pageSize": page_size})


def get_device_detail(user_device_id):
    return call_api("tracker.userDevice.get", {"userDeviceId": user_device_id})


def get_device_trips(page_num=1, page_size=50, device_code=None, begin_time=None, end_time=None):
    biz_content = {"pageNum": page_num, "pageSize": page_size}
    if device_code:
        biz_content["deviceCode"] = device_code
    if begin_time:
        biz_content["beginTime"] = begin_time
    if end_time:
        biz_content["endTime"] = end_time
    return call_api("tracker.deviceTrip.page", biz_content)


def get_probe_data(user_device_trip_id, begin_time, end_time, probe_type=0, page_num=1, page_size=50):
    return call_api(
        "tracker.tripData.pageProbeData",
        {
            "userDeviceTripId": user_device_trip_id,
            "beginTime": begin_time,
            "endTime": end_time,
            "probeType": probe_type,
            "pageNum": page_num,
            "pageSize": page_size,
        },
    )


def get_gateway_devices(page_num=1, page_size=50):
    return call_api("gw.deviceInfo.page", {"pageNo": page_num, "pageSize": page_size})


def get_gateway_records(device_id=None, device_sn=None, page_num=1, page_size=50):
    biz_content = {"pageNum": page_num, "pageSize": page_size}
    if device_id:
        biz_content["deviceId"] = device_id
    if device_sn:
        biz_content["deviceSn"] = device_sn
    return call_api("gw.deviceInfo.recordPage", biz_content)


def get_gateway_data(record_id, probe_type=0, begin_time=None, end_time=None, page_num=1, page_size=50):
    biz_content = {
        "recordId": record_id,
        "probeType": probe_type,
        "pageNum": page_num,
        "pageSize": page_size,
    }
    if begin_time is not None and end_time is not None:
        biz_content["dataTime"] = [int(begin_time), int(end_time)]
    return call_api("gw.deviceData.page", biz_content)
