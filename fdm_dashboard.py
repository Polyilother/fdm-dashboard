import streamlit as st
import pandas as pd
import json
import hashlib
import secrets
import os
import re
import zipfile
import io
from datetime import datetime, timedelta
from pathlib import Path

try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    pool = None
    RealDictCursor = None


# ==================== 配置 ====================
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(APP_DIR, "fdm_tasks.db")
LEGACY_JSON_FILE = os.path.join(APP_DIR, "fdm_tasks.json")
BACKUP_DIR = os.path.join(APP_DIR, "backups")
st.set_page_config(layout="wide", page_title="FDM打印室任务看板", page_icon="🖨️")

# ==================== 刷新策略 ====================
# PostgreSQL 版多人使用时，5 秒全页自动刷新会造成页面发白闪烁。
# 先关闭全页自动刷新，用户刷新浏览器即可拉取最新数据。

# ==================== 常量定义 ====================
ENGINEERS = ["孙义", "萧浩林", "张瑞", "乔鑫辉","蔡炜光", "丁柏瑞", "吴怀栋", "孙小辉", "其他（手动填写）"]
TECHNICIANS = ["刘相冰", "刘东昊"]
TASK_TYPES = ["简易测试", "完整测试", "中试测试", "其他"]
SPECIAL_STATUSES = ["故障维修", "设备维保", "外部借用", "材料前期测试", "长周期测试"]
MAINTAIN_TYPES = ["1个月小保养", "2个月大保养"]

# ==================== 🛠️ 穿透：.3mf压缩包内存级闪读算法（终极精准校准版） ====================
def parse_gcode_time_fast(file_bytes, filename):
    try:
        if not zipfile.is_zipfile(io.BytesIO(file_bytes)):
            tail_data = file_bytes[-65536:].decode('utf-8', errors='ignore')
            head_data = file_bytes[:65536].decode('utf-8', errors='ignore')
            combined = head_data + "\n" + tail_data
            
            bambu_matches = re.findall(r';\s*total estimated time:\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?', combined)
            if bambu_matches:
                final_match = bambu_matches[-1]
                d = int(final_match[0]) if final_match[0] else 0
                h = int(final_match[1]) if final_match[1] else 0
                m = int(final_match[2]) if final_match[2] else 0
                s = int(final_match[3]) if final_match[3] else 0
                calc_hours = round((d * 24) + h + (m / 60.0) + (s / 3600.0), 1)
                if calc_hours > 0: return calc_hours
                
            cura_match = re.search(r';TIME:\s*(\d+)', combined)
            if cura_match:
                return round(int(cura_match.group(1)) / 3600.0, 1)
            return 0.6

        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            gcode_files = [name for name in z.namelist() if name.endswith('.gcode')]
            if gcode_files:
                target_gcode = gcode_files[0]
                with z.open(target_gcode) as f:
                    content_raw = f.read()
                    head_part = content_raw[:65536].decode('utf-8', errors='ignore')
                    tail_part = content_raw[-65536:].decode('utf-8', errors='ignore')
                    gcode_text = head_part + "\n" + tail_part
                    
                    bambu_matches = re.findall(r';\s*total estimated time:\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?', gcode_text)
                    if bambu_matches:
                        final_match = bambu_matches[-1]
                        d = int(final_match[0]) if final_match[0] else 0
                        h = int(final_match[1]) if final_match[1] else 0
                        m = int(final_match[2]) if final_match[2] else 0
                        s = int(final_match[3]) if final_match[3] else 0
                        calc_hours = round((d * 24) + h + (m / 60.0) + (s / 3600.0), 1)
                        if calc_hours > 0: return calc_hours
                    
                    cura_match = re.search(r';TIME:\s*(\d+)', gcode_text)
                    if cura_match:
                        return round(int(cura_match.group(1)) / 3600.0, 1)
    except Exception as e:
        pass
    return 0.6  

# ==================== 时间格式化工具 ====================
def get_formatted_time():
    now = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return now.strftime(f"%Y-%m-%d {weekdays[now.weekday()]} %H:%M")

def get_short_log_time():
    return datetime.now().strftime("%m.%d %H:%M")

def normalize_machine_id(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("＃", "#").replace("号", "#")
    normalized = re.sub(r"\s+", "", normalized)
    match = re.fullmatch(r"(\d+)#?", normalized)
    if match:
        return f"{int(match.group(1))}#"
    return raw

def calculate_eta(start_time_str, total_hours):
    try:
        parts = start_time_str.split(" ")
        dt = datetime.strptime(f"{parts[0]} {parts[2]}", "%Y-%m-%d %H:%M")
        eta_dt = dt + timedelta(minutes=int(float(total_hours) * 60))
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        return eta_dt.strftime(f"%Y-%m-%d {weekdays[eta_dt.weekday()]} %H:%M")
    except:
        return "-"

# ==================== PostgreSQL 数据与权限 ====================
PGHOST = os.getenv("FDM_PGHOST", "localhost")
PGPORT = int(os.getenv("FDM_PGPORT", "5432"))
PGDATABASE = os.getenv("FDM_PGDATABASE", "fdm_dashboard")
PGUSER = os.getenv("FDM_PGUSER", "postgres")
PGSSLMODE = os.getenv("FDM_PGSSLMODE", "prefer")
DB_LABEL = f"postgresql://{PGUSER}@{PGHOST}:{PGPORT}/{PGDATABASE}"

def get_pg_password():
    password = os.getenv("FDM_PGPASSWORD", "")
    if password:
        return password
    password_file = os.path.join(APP_DIR, "postgres_password.txt")
    if os.path.exists(password_file):
        try:
            return open(password_file, "r", encoding="utf-8").read().strip()
        except Exception:
            return ""
    return ""

PGPASSWORD = get_pg_password()

PERMISSIONS = {
    "dispatch_task": "下发任务",
    "edit_device_status": "设备状态修改",
    "start_machine": "上机点击",
    "end_machine": "下机点击",
    "report_task_flow": "报表1-任务流转台账",
    "report_maintenance": "报表2-维保维修日志",
    "report_oee": "报表3-设备流转效率与闲置盲区",
    "report_efficiency": "报表4-效率诊断分析",
}

DBIntegrityError = Exception
if psycopg2 is not None:
    DBIntegrityError = psycopg2.IntegrityError

class PgAppConnection:
    def __init__(self):
        self.pool = get_pg_pool()
        self.conn = self.pool.getconn()
        self.conn.autocommit = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.pool.putconn(self.conn)

    def execute(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query.replace("?", "%s"), params or ())
        return cur

def open_pg_connection(database=None, autocommit=False):
    if psycopg2 is None:
        st.error("缺少 PostgreSQL Python 驱动，请先安装：pip install psycopg2-binary")
        st.stop()
    kwargs = {
        "host": PGHOST,
        "port": PGPORT,
        "dbname": database or PGDATABASE,
        "user": PGUSER,
        "password": PGPASSWORD,
        "sslmode": PGSSLMODE,
        "cursor_factory": RealDictCursor,
    }
    conn = psycopg2.connect(**kwargs)
    conn.autocommit = autocommit
    return conn

@st.cache_resource(show_spinner=False)
def get_pg_pool():
    if psycopg2 is None:
        st.error("缺少 PostgreSQL Python 驱动，请先安装：pip install psycopg2-binary")
        st.stop()
    return pool.SimpleConnectionPool(
        1,
        8,
        host=PGHOST,
        port=PGPORT,
        dbname=PGDATABASE,
        user=PGUSER,
        password=PGPASSWORD,
        sslmode=PGSSLMODE,
        cursor_factory=RealDictCursor,
    )

def ensure_postgres_database():
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", PGDATABASE):
        st.error("FDM_PGDATABASE 只能使用字母、数字和下划线，且不能以数字开头。")
        st.stop()
    try:
        admin_conn = open_pg_connection(database="postgres", autocommit=True)
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PGDATABASE,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{PGDATABASE}"')
        admin_conn.close()
    except Exception as exc:
        st.error(f"无法连接或初始化 PostgreSQL：{exc}")
        st.stop()

def get_conn():
    return PgAppConnection()

def normalize_task(t):
    if "machine_id" in t:
        t["machine_id"] = normalize_machine_id(t.get("machine_id"))
    t.setdefault("total_batches", 1)
    t.setdefault("gcode_names", [])
    t.setdefault("finished_batch_timestamps", [])
    t.setdefault("batch_statuses", ["待打印"] * t["total_batches"])
    t.setdefault("batch_start_times", ["-"] * t["total_batches"])
    t.setdefault("batch_end_times", ["-"] * t["total_batches"])
    try:
        total_batches = int(t.get("total_batches") or 1)
    except (TypeError, ValueError):
        total_batches = 1
    t["total_batches"] = max(total_batches, 1)
    if not isinstance(t.get("batch_statuses"), list):
        t["batch_statuses"] = []
    if not isinstance(t.get("batch_start_times"), list):
        t["batch_start_times"] = []
    if not isinstance(t.get("batch_end_times"), list):
        t["batch_end_times"] = []
    if len(t["batch_statuses"]) < t["total_batches"]:
        t["batch_statuses"].extend(["待打印"] * (t["total_batches"] - len(t["batch_statuses"])))
    if len(t["batch_start_times"]) < t["total_batches"]:
        t["batch_start_times"].extend(["-"] * (t["total_batches"] - len(t["batch_start_times"])))
    if len(t["batch_end_times"]) < t["total_batches"]:
        t["batch_end_times"].extend(["-"] * (t["total_batches"] - len(t["batch_end_times"])))
    t.setdefault("material", "未知")
    t.setdefault("special_notes", "无")
    t.setdefault("exception_log", "-")
    t.setdefault("transfer_notes", "-")
    t.setdefault("operator", "-")
    t.setdefault("end_operator", "-")
    t.setdefault("start_time", "-")
    t.setdefault("end_time", "-")
    t.setdefault("is_paused", False)
    t.setdefault("pause_reason", "-")
    t.setdefault("pause_start_time", "-")
    t.setdefault("test_task_type", "未定义")
    t.setdefault("theory_total_hours", None)
    t.setdefault("eta_time", "-")
    t.setdefault("created_at", get_formatted_time())
    return t

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"

def verify_password(password, stored):
    try:
        salt, digest = stored.split("$", 1)
        return hash_password(password, salt).split("$", 1)[1] == digest
    except Exception:
        return False

def render_reports_section(all_tasks, expanded=True):
    # ==================== 历史报表与统计大区 ====================
    st.divider()
    with st.expander("📊 查看历史任务与统计报表", expanded=expanded):
        if all_tasks:
            report_permissions = [
                can("report_task_flow"),
                can("report_maintenance"),
                can("report_oee"),
                can("report_efficiency"),
            ]
            if any(report_permissions):
                st.info("当前账号按权限显示已授权报表。")
            else:
                st.warning("当前账号暂无报表查看权限，请联系管理员分配。")
            st.divider()

            clean_df_tasks = [t for t in all_tasks if "已完成" not in str(t.get("status")) and t.get("status") not in SPECIAL_STATUSES]
            if can("report_task_flow") and clean_df_tasks:
                st.markdown("#### 📋 测试任务流转台账")
                df = pd.DataFrame(clean_df_tasks)

                # 🛠️ 🛠️ 🛠️ 终极数据联动：让台账中的流转时间链完美跟随提取的清洗盘名 🛠️ 🛠️ 🛠️
                def format_batch_details(row):
                    if "batch_statuses" in row and isinstance(row["batch_statuses"], list):
                        details = []
                        g_names = row.get("gcode_names", [])
                        for i, s in enumerate(row["batch_statuses"]):
                            # 获取对应的干净文件名进行数据绑定展示
                            if i < len(g_names):
                                f_clean = re.sub(r'(\.gcode)?\.3mf$', '', g_names[i])
                                f_clean = re.sub(r'\.gcode$', '', f_clean)
                            else:
                                f_clean = f"样件_{i+1}"

                            st_t = row["batch_start_times"][i] if i < len(row["batch_start_times"]) else "-"
                            ed_t = row["batch_end_times"][i] if i < len(row["batch_end_times"]) else "-"
                            details.append(f"盘{i+1}[{f_clean}] - {s}(上机:{st_t} | 完工:{ed_t})")
                        return " ｜ ".join(details)
                    return "-"

                df['各盘精密时间链'] = df.apply(format_batch_details, axis=1)

                mapping = {
                    "created_at": "派单时间", "machine_id": "设备编号", "test_task_type": "任务类型",
                    "engineer": "白班工程师(派单人)", "material": "线材批次", "status": "最终状态", 
                    "operator": "执行技术员", "end_operator": "下机技术员", "start_time": "实际上机时间", 
                        "end_time": "实际下机时间", "special_notes": "注意事项", "exception_log": "异常记录",
                    "transfer_notes": "班次交接备注" 
                }
                df_show = df.rename(columns=mapping)
                cols_to_show = [col for col in mapping.values() if col in df_show.columns]
                st.dataframe(df_show[cols_to_show + ["各盘精密时间链"]], use_container_width=True)

            st.markdown("<br/>", unsafe_allow_html=True)

            maintenance_statuses = ["故障维修", "设备维保"]
            history_logs = [
                t for t in all_tasks
                if t.get("status") in maintenance_statuses
                or any(str(t.get("status")) == f"已完成-{s}" for s in maintenance_statuses)
            ]
            if can("report_maintenance") and history_logs:
                st.markdown("#### 🛠️ 设备维保与维修历史日志")
                df_log = pd.DataFrame(history_logs)
                df_log['display_status'] = df_log['status'].apply(lambda x: str(x).replace("已完成-", "已完成恢复空闲") if "已完成" in str(x) else f"{x}(进行中)")
                df_log['maintenance_kind'] = df_log.apply(
                    lambda row: "设备维保" if "设备维保" in str(row.get("status", "")) or "设备维保" in str(row.get("test_task_type", "")) else "故障维修",
                    axis=1,
                )
                df_log['machine_sort_no'] = df_log['machine_id'].apply(
                    lambda value: int(re.search(r"\d+", str(value or "999999")).group()) if re.search(r"\d+", str(value or "")) else 999999
                )

                mapping_log = {
                    "machine_id": "设备编号", "test_task_type": "维保/维修项目", 
                    "display_status": "当前状态", "operator": "操作技术员", 
                    "start_time": "开始时间/登记时间", "end_time": "解除时间/恢复空闲时间",
                    "exception_log": "维护详情(具体做了什么)"
                }
                maintain_df = df_log[df_log["maintenance_kind"] == "设备维保"].sort_values(
                    by=["machine_sort_no", "start_time"],
                    ascending=[True, False],
                )
                repair_df = df_log[df_log["maintenance_kind"] == "故障维修"].sort_values(
                    by=["start_time", "machine_sort_no"],
                    ascending=[False, True],
                )
                maint_tab, repair_tab = st.tabs([f"设备维保 ({len(maintain_df)})", f"故障维修 ({len(repair_df)})"])
                with maint_tab:
                    if maintain_df.empty:
                        st.caption("暂无设备维保记录。")
                    else:
                        df_log_show = maintain_df.rename(columns=mapping_log)
                        cols_log_to_show = [col for col in mapping_log.values() if col in df_log_show.columns]
                        st.dataframe(df_log_show[cols_log_to_show], use_container_width=True, hide_index=True)
                with repair_tab:
                    if repair_df.empty:
                        st.caption("暂无故障维修记录。")
                    else:
                        df_log_show = repair_df.rename(columns=mapping_log)
                        cols_log_to_show = [col for col in mapping_log.values() if col in df_log_show.columns]
                        st.dataframe(df_log_show[cols_log_to_show], use_container_width=True, hide_index=True)

            st.markdown("<hr style='border: 1px dashed #DDD; margin: 25px 0;'/>", unsafe_allow_html=True)

            if can("report_oee"):
                st.markdown("#### 📊 设备流转效率与闲置盲区分析")
                oee_raw_data = []
                def is_real_test_flow_task(t):
                    status = str(t.get("status", ""))
                    task_type = str(t.get("test_task_type", ""))
                    material = str(t.get("material", ""))
                    if t.get("machine_id") == "待定" or t.get("start_time") == "-" or t.get("end_time") == "-":
                        return False
                    if status in SPECIAL_STATUSES or any(status == f"已完成-{s}" for s in SPECIAL_STATUSES):
                        return False
                    if task_type in SPECIAL_STATUSES or material in SPECIAL_STATUSES:
                        return False
                    return True
                valid_history = [t for t in all_tasks if is_real_test_flow_task(t)]

                if valid_history:
                    device_timeline = {}
                    for t in valid_history:
                        mc = t.get("machine_id")
                        st_str_raw = t.get("start_time")
                        ed_str_raw = t.get("end_time")

                        if " " not in str(st_str_raw) or " " not in str(ed_str_raw):
                            continue
                        try:
                            st_p = st_str_raw.split(" ")
                            ed_p = ed_str_raw.split(" ")
                            st_dt = datetime.strptime(f"{st_p[0]} {st_p[2]}", "%Y-%m-%d %H:%M")
                            ed_dt = datetime.strptime(f"{ed_p[0]} {ed_p[2]}", "%Y-%m-%d %H:%M")
                            device_timeline.setdefault(mc, []).append({
                                "task": t, "start_dt": st_dt, "end_dt": ed_dt, "start_str": st_str_raw, "end_str": ed_str_raw
                            })
                        except: pass

                    for mc, logs in device_timeline.items():
                        logs.sort(key=lambda x: x["start_dt"])
                        for i in range(len(logs)):
                            current_log = logs[i]
                            task_obj = current_log["task"]

                            idle_hours = "-"
                            last_end_str = "-"

                            if i > 0:
                                prev_log = logs[i-1]
                                last_end_str = prev_log["end_str"]
                                try:
                                    diff_seconds = (current_log["start_dt"] - prev_log["end_dt"]).total_seconds()
                                    idle_hours = round(max(diff_seconds, 0.0) / 3600.0, 1)
                                except: pass

                            try:
                                run_seconds = (current_log["end_dt"] - current_log["start_dt"]).total_seconds()
                                run_hours = round(run_seconds / 3600.0, 1)
                            except: run_hours = 0.0

                            oee_raw_data.append({
                                "设备编号": mc,
                                "样品牌号": task_obj.get("material"),
                                "当前任务实际占机(h)": run_hours,
                                "上一单完工技术下机时间": last_end_str,
                                "当前单技术确认上机时间": current_log["start_str"],
                                "设备流转闲置盲区(h)": idle_hours,
                                "执行技术员": task_obj.get("operator") if task_obj.get("operator") != "-" else task_obj.get("end_operator"),
                                "最终状态": task_obj.get("status")
                            })

                    if oee_raw_data:
                        df_oee = pd.DataFrame(oee_raw_data)
                        st.dataframe(df_oee, use_container_width=True)
                    else:
                        st.info("💡 暂无有效测试任务流转记录用于分析设备流转效率。")
                else:
                    st.info("💡 暂无测试任务流转完工记录，请在技术员上机流转产生数据后查看设备流转效率。")

            st.markdown("<hr style='border: 1px dashed #DDD; margin: 25px 0;'/>", unsafe_allow_html=True)

            if can("report_efficiency"):
                st.markdown("### 🤖 测试效率智能诊断与切片偏差分析")

                now_dt = datetime.now()
                yesterday_dt = now_dt - timedelta(days=1)
                recent_tasks = []
                deviation_log_data = [] 

                for t in all_tasks:
                    status_now = t.get("status")
                    st_str = t.get("start_time", "-")
                    ed_str = t.get("end_time", "-")

                    if st_str != "-" and ed_str != "-":
                        try:
                            st_parts = st_str.split(" ")
                            ed_parts = ed_str.split(" ")
                            st_dt = datetime.strptime(f"{st_parts[0]} {st_parts[2]}", "%Y-%m-%d %H:%M")
                            ed_dt = datetime.strptime(f"{ed_parts[0]} {ed_parts[2]}", "%Y-%m-%d %H:%M")

                            real_total_hours = round((ed_dt - st_dt).seconds / 3600 + (ed_dt - st_dt).days * 24, 1)
                            raw_th = t.get("theory_total_hours")
                            theory_total_hours = float(raw_th) if raw_th is not None else 2.0
                            deviation = round(real_total_hours - theory_total_hours, 1) 

                            if deviation <= 1.5: rating = "🟢 极速响应流转"
                            elif deviation <= 3.0: rating = "💛 正常多台管控延迟"
                            else: rating = "🔴 严重闲置/超期卡顿"
                            if status_now == "异常中止": rating = "❌ 测试异常中止"

                            deviation_log_data.append({
                                "设备编号": t.get("machine_id"), "样品牌号": t.get("material"), "总盘数": t.get("total_batches"),
                                "切片理论总时(h)": theory_total_hours if raw_th is not None else "无", 
                                "设备真实占时(h)": real_total_hours, "流转偏差时间(h)": deviation if raw_th is not None else "无法计算",
                                "精益效率评级": rating, "实际上机时间": st_str, "实际下机时间": ed_str, "执行技术员": t.get("operator")
                            })
                            if st_dt >= yesterday_dt:
                                recent_tasks.append({"deviation": deviation if raw_th is not None else 0, "status": status_now})
                        except: pass

                total_recent = len(recent_tasks)
                aborted_recent = len([t for t in recent_tasks if t["status"] == "异常中止"])
                high_delay_cnt = len([t for t in recent_tasks if t["deviation"] > 3.0])

                metric_c1, metric_c2, metric_c3 = st.columns(3)
                with metric_c1: st.metric("近24H 生产测试中止率", f"{(aborted_recent/max(total_recent,1))*100:.1f}%")
                with metric_c2: st.metric("下机流转闲置严重机台", f"{high_delay_cnt} 单")
                with metric_c3: st.metric("当前故障/维保锁定机台", f"{len([t for t in all_tasks if t.get('status') in ['故障维修', '设备维保']])} 台")

                st.markdown("<br/>", unsafe_allow_html=True)
                if deviation_log_data:
                    st.markdown("#### ⏱️ 设备生产测试效率与流转偏差分析明细")
                    st.dataframe(pd.DataFrame(deviation_log_data), use_container_width=True)

                ai_prompt_text = f"""你是一个资深的 FDM 3D 打印车间精益管理大模型专家。请根据以下我提供的实验室今日“真实总耗时”与“切片预估总时间”的流转数据，输出 150 字内的【设备流转效率与闲置盲区分析日报】：
        1. 数据总览：近24小时共完成/流转测试任务 {total_recent} 项，出现下机严重闲置超期（理论偏差超过3小时）的任务有 {high_delay_cnt} 笔，异常中止 {aborted_recent} 批。
        2. 硬件锁定：当前车间因故障维修、设备维保等锁定不可用的设备共有 {len([t for t in all_tasks if t.get('status') in ['故障维修', '设备维保']])} 台。
        请评估今日车间设备流转效率与盲区周转是否达标，并为工程师和白/夜班技术员分别提供一条减少设备“印完空等”的现场管理动作建议。"""

                with st.popover("🤖 一键生成今日 AI 生产效率诊断报告 Prompt", use_container_width=True):
                    st.info("💡 复制下方自动打包好的生产流转数据，直接发给任何大语言模型，即可获得精炼的生产效率日报推送！")
                    st.text_area("AI 诊断源数据栏（点击右上角一键复制）", value=ai_prompt_text, height=180)

def init_database():
    ensure_postgres_database()
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                permissions TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_logs (
                id SERIAL PRIMARY KEY,
                log_date TEXT NOT NULL,
                device_or_task TEXT NOT NULL,
                note TEXT NOT NULL,
                author TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operation_logs (
                id SERIAL PRIMARY KEY,
                action TEXT NOT NULL,
                task_id TEXT,
                machine_id TEXT,
                operator TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            )
        """)

def get_database_snapshot():
    tables = ["tasks", "users", "login_sessions", "daily_logs", "operation_logs"]
    order_by_map = {
        "tasks": "created_at, id",
        "users": "id",
        "login_sessions": "created_at, token",
        "daily_logs": "id",
        "operation_logs": "id",
    }
    snapshot = {
        "created_at": get_formatted_time(),
        "database": DB_LABEL,
        "tables": {},
    }
    with get_conn() as conn:
        for table in tables:
            order_by = order_by_map[table]
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
            snapshot["tables"][table] = [dict(row) for row in rows]
    return json.dumps(snapshot, ensure_ascii=False, indent=2)

def write_database_snapshot(path):
    Path(path).write_text(get_database_snapshot(), encoding="utf-8")

def backup_database_once_daily():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_file = os.path.join(BACKUP_DIR, f"fdm_postgresql_{datetime.now().strftime('%Y%m%d')}.json")
    if not os.path.exists(backup_file):
        write_database_snapshot(backup_file)

def create_manual_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_file = os.path.join(BACKUP_DIR, f"fdm_postgresql_manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    write_database_snapshot(backup_file)
    log_operation("手动备份数据库", detail=f"备份文件:{os.path.basename(backup_file)}")
    return backup_file

def get_latest_backup_info():
    if not os.path.exists(BACKUP_DIR):
        return None
    backups = [
        os.path.join(BACKUP_DIR, name)
        for name in os.listdir(BACKUP_DIR)
        if name.lower().endswith(".json") and name.startswith("fdm_postgresql")
    ]
    if not backups:
        return None
    latest = max(backups, key=os.path.getmtime)
    return {
        "path": latest,
        "name": os.path.basename(latest),
        "time": datetime.fromtimestamp(os.path.getmtime(latest)).strftime("%Y-%m-%d %H:%M:%S"),
        "size_mb": round(os.path.getsize(latest) / 1024 / 1024, 2),
    }

def log_operation(action, task=None, detail="", operator=None):
    task = task or {}
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO operation_logs (action, task_id, machine_id, operator, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                action,
                str(task.get("id", "")) if task else "",
                str(task.get("machine_id", "")) if task else "",
                operator or st.session_state.get("auth_user", ""),
                detail,
                get_formatted_time(),
            ),
        )

def list_operation_logs(limit=100, action_filter="全部"):
    with get_conn() as conn:
        if action_filter and action_filter != "全部":
            return conn.execute(
                "SELECT action, task_id, machine_id, operator, detail, created_at FROM operation_logs WHERE action = ? ORDER BY id DESC LIMIT ?",
                (action_filter, int(limit)),
            ).fetchall()
        return conn.execute(
            "SELECT action, task_id, machine_id, operator, detail, created_at FROM operation_logs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()

def get_database_health():
    latest_backup = get_latest_backup_info()
    with get_conn() as conn:
        task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE COALESCE((data::jsonb ->> 'deleted')::boolean, false) = false").fetchone()["c"]
        deleted_count = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE COALESCE((data::jsonb ->> 'deleted')::boolean, false) = true").fetchone()["c"]
        log_count = conn.execute("SELECT COUNT(*) AS c FROM operation_logs").fetchone()["c"]
        last_log = conn.execute("SELECT action, operator, created_at FROM operation_logs ORDER BY id DESC LIMIT 1").fetchone()
        db_size = conn.execute("SELECT pg_database_size(current_database()) AS size_bytes").fetchone()["size_bytes"]
    today_key = datetime.now().strftime("%Y%m%d")
    today_backup_ok = os.path.exists(os.path.join(BACKUP_DIR, f"fdm_postgresql_{today_key}.json"))
    return {
        "db_path": DB_LABEL,
        "backup_dir": BACKUP_DIR,
        "db_size_mb": round(int(db_size or 0) / 1024 / 1024, 2),
        "task_count": task_count,
        "deleted_count": deleted_count,
        "log_count": log_count,
        "latest_backup": latest_backup,
        "today_backup_ok": today_backup_ok,
        "last_log": dict(last_log) if last_log else None,
    }

def ensure_default_admin():
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        if row["c"] == 0:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, active, permissions, created_at) VALUES (?, ?, 1, 1, ?, ?)",
                ("admin", hash_password("admin123"), json.dumps(list(PERMISSIONS.keys()), ensure_ascii=False), get_formatted_time()),
            )

def upsert_task_row(conn, task_id, data, created_at, updated_at):
    conn.execute(
        """
        INSERT INTO tasks (id, data, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            data = EXCLUDED.data,
            created_at = EXCLUDED.created_at,
            updated_at = EXCLUDED.updated_at
        """,
        (task_id, data, created_at, updated_at),
    )

def migrate_legacy_json_once():
    if not os.path.exists(LEGACY_JSON_FILE):
        return
    with get_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        if existing > 0:
            return
        try:
            with open(LEGACY_JSON_FILE, "r", encoding="utf-8") as f:
                tasks = json.load(f)
        except Exception as exc:
            st.warning(f"旧 JSON 数据读取失败，未执行自动迁移：{exc}")
            return
        now = get_formatted_time()
        for idx, task in enumerate(tasks):
            task = normalize_task(task)
            task_id = str(task.get("id") or f"legacy_{idx}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}")
            task["id"] = task_id
            upsert_task_row(conn, task_id, json.dumps(task, ensure_ascii=False), str(task.get("created_at", "")), now)

def load_tasks():
    with get_conn() as conn:
        rows = conn.execute("SELECT data FROM tasks ORDER BY created_at, id").fetchall()
    tasks = []
    for row in rows:
        try:
            task = normalize_task(json.loads(row["data"]))
            if not task.get("deleted", False):
                tasks.append(task)
        except Exception:
            pass
    return tasks

def save_tasks(tasks):
    now = get_formatted_time()
    with get_conn() as conn:
        for idx, task in enumerate(tasks):
            task = normalize_task(task)
            task_id = str(task.get("id") or f"task_{idx}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}")
            task["id"] = task_id
            upsert_task_row(conn, task_id, json.dumps(task, ensure_ascii=False), str(task.get("created_at", "")), now)

def clear_all_tasks():
    with get_conn() as conn:
        conn.execute("DELETE FROM tasks")

def soft_delete_task(task_id, operator=None):
    with get_conn() as conn:
        row = conn.execute("SELECT data FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if not row:
            return
        task = normalize_task(json.loads(row["data"]))
        task["deleted"] = True
        task["deleted_at"] = get_formatted_time()
        task["deleted_by"] = operator or st.session_state.get("auth_user", "")
        conn.execute(
            "UPDATE tasks SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(task, ensure_ascii=False), get_formatted_time(), str(task_id)),
        )
    log_operation("软删除任务", task, "任务移除后保留在PostgreSQL中", operator)

def update_single_task(task):
    task = normalize_task(task)
    now = get_formatted_time()
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(task, ensure_ascii=False), now, str(task.get("id"))),
        )

def get_today_key():
    return datetime.now().strftime("%Y-%m-%d")

def add_daily_log(device_or_task, note, author):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_logs (log_date, device_or_task, note, author, created_at) VALUES (?, ?, ?, ?, ?)",
            (get_today_key(), device_or_task, note, author, get_formatted_time()),
        )

def list_daily_logs(log_date=None):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM daily_logs WHERE log_date = ? ORDER BY id DESC",
            (log_date or get_today_key(),),
        ).fetchall()

def active_attention_tasks(tasks):
    active_statuses = {"待上机", "打印中"}
    items = []
    for task in tasks:
        note = str(task.get("special_notes", "") or "").strip()
        if task.get("status") in active_statuses and note and note not in ["无", "-", "空白"]:
            items.append(task)
    return items

def get_user(username):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

def create_login_session(username):
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO login_sessions (token, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token, username, expires_at, get_formatted_time()),
        )
    return token

def delete_login_session(token):
    if not token:
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM login_sessions WHERE token = ?", (token,))

def user_from_session_token(token):
    if not token:
        return None
    with get_conn() as conn:
        conn.execute("DELETE FROM login_sessions WHERE expires_at < ?", (datetime.now().isoformat(timespec="seconds"),))
        row = conn.execute("SELECT username FROM login_sessions WHERE token = ?", (token,)).fetchone()
    return get_user(row["username"]) if row else None

def get_session_token_from_url():
    token = st.query_params.get("session")
    if isinstance(token, list):
        return token[0] if token else None
    return token

def list_users():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users ORDER BY is_admin DESC, username").fetchall()

def create_user(username, password, permissions):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, active, permissions, created_at) VALUES (?, ?, 0, 1, ?, ?)",
            (username, hash_password(password), json.dumps(permissions, ensure_ascii=False), get_formatted_time()),
        )

def update_user(username, permissions, active, new_password=None):
    with get_conn() as conn:
        if new_password:
            conn.execute(
                "UPDATE users SET permissions = ?, active = ?, password_hash = ? WHERE username = ? AND is_admin = 0",
                (json.dumps(permissions, ensure_ascii=False), int(active), hash_password(new_password), username),
            )
        else:
            conn.execute(
                "UPDATE users SET permissions = ?, active = ? WHERE username = ? AND is_admin = 0",
                (json.dumps(permissions, ensure_ascii=False), int(active), username),
            )

def change_password(username, old_password, new_password):
    row = get_user(username)
    if not row or not verify_password(old_password, row["password_hash"]):
        return False
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hash_password(new_password), username))
    return True

def delete_user(username):
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE username = ? AND is_admin = 0", (username,))

def current_user():
    username = st.session_state.get("auth_user")
    if username:
        cached_user = st.session_state.get("_current_user_row")
        if cached_user and cached_user.get("username") == username:
            return cached_user
        user = get_user(username)
        if user:
            st.session_state["_current_user_row"] = dict(user)
        return user
    token = get_session_token_from_url()
    user = user_from_session_token(token)
    if user and user["active"]:
        st.session_state["auth_user"] = user["username"]
        st.session_state["_current_user_row"] = dict(user)
        return st.session_state["_current_user_row"]
    return None

def user_permissions(user):
    if not user:
        return set()
    if user["is_admin"]:
        return set(PERMISSIONS.keys())
    try:
        return set(json.loads(user["permissions"] or "[]"))
    except Exception:
        return set()

def can(permission):
    return permission in user_permissions(current_user())

def request_view_refresh():
    st.rerun()

def login_gate():
    user = current_user()
    if user and user["active"]:
        return user
    st.title("FDM 打印室任务看板")
    st.markdown("### 用户登录")
    username = st.text_input("用户名")
    password = st.text_input("密码", type="password")
    if st.button("登录", type="primary"):
        row = get_user(username.strip())
        if row and row["active"] and verify_password(password, row["password_hash"]):
            st.session_state["auth_user"] = row["username"]
            st.query_params["session"] = create_login_session(row["username"])
            request_view_refresh()
        else:
            st.error("用户名或密码错误，或账号已停用。")
    st.info("首次默认管理员：admin / admin123。上线前请新增正式账号，并妥善保管管理员密码。")
    st.stop()

def render_user_management_panel():
    user = current_user()
    if not user or not user["is_admin"]:
        return
    with st.container(border=True):
        st.markdown("### 👤 用户管理")
        tab_add, tab_manage, tab_password = st.tabs(["新增用户", "维护账号", "管理员密码"])

        with tab_add:
            new_username = st.text_input("新用户名", key="panel_new_user_name").strip()
            new_password = st.text_input("新用户密码", type="password", key="panel_new_user_pwd")
            perm_cols = st.columns(2)
            new_perms = []
            for idx, (code, label) in enumerate(PERMISSIONS.items()):
                with perm_cols[idx % 2]:
                    if st.checkbox(label, key=f"panel_new_perm_{code}"):
                        new_perms.append(code)
            if st.button("新增用户", use_container_width=True, key="panel_btn_create_user"):
                if not new_username or not new_password:
                    st.error("请填写用户名和密码。")
                else:
                    try:
                        create_user(new_username, new_password, new_perms)
                        log_operation("新增用户", detail=f"新增用户:{new_username}; 权限:{','.join(new_perms)}")
                        st.success("用户已新增。")
                        request_view_refresh()
                    except DBIntegrityError:
                        st.error("用户名已存在。")

        with tab_manage:
            normal_users = [u for u in list_users() if not u["is_admin"]]
            if normal_users:
                user_rows = []
                for u in normal_users:
                    perms = user_permissions(u)
                    user_rows.append({
                        "用户名": u["username"],
                        "状态": "启用" if u["active"] else "停用",
                        "权限数": len(perms),
                    })
                st.dataframe(pd.DataFrame(user_rows), use_container_width=True, hide_index=True, height=220)

                selected = st.selectbox("选择要维护的用户", [u["username"] for u in normal_users], key="panel_manage_user")
                row = get_user(selected)
                existing = user_permissions(row)
                active = st.checkbox("启用账号", value=bool(row["active"]), key=f"panel_active_{selected}")
                edit_cols = st.columns(2)
                edit_perms = []
                for idx, (code, label) in enumerate(PERMISSIONS.items()):
                    with edit_cols[idx % 2]:
                        if st.checkbox(label, value=code in existing, key=f"panel_edit_perm_{selected}_{code}"):
                            edit_perms.append(code)
                new_pwd = st.text_input("重置密码（留空不修改）", type="password", key=f"panel_reset_pwd_{selected}")
                save_col, delete_col = st.columns(2)
                with save_col:
                    if st.button("保存账号", use_container_width=True, type="primary", key=f"panel_save_user_{selected}"):
                        update_user(selected, edit_perms, active, new_pwd or None)
                        log_operation("保存用户权限", detail=f"用户:{selected}; 启用:{active}; 权限:{','.join(edit_perms)}; 重置密码:{bool(new_pwd)}")
                        st.success("用户权限已保存。")
                        request_view_refresh()
                with delete_col:
                    if st.button("删除用户", use_container_width=True, key=f"panel_delete_user_{selected}"):
                        delete_user(selected)
                        log_operation("删除用户", detail=f"删除用户:{selected}")
                        st.warning("用户已删除。")
                        request_view_refresh()
            else:
                st.caption("暂无普通用户。")

        with tab_password:
            st.caption(f"当前管理员：{user['username']}")
            old_pwd = st.text_input("当前密码", type="password", key="panel_admin_old_pwd")
            new_self_pwd = st.text_input("新密码", type="password", key="panel_admin_new_pwd")
            if st.button("修改管理员密码", use_container_width=True, key="panel_btn_change_self_pwd"):
                if old_pwd and new_self_pwd and change_password(user["username"], old_pwd, new_self_pwd):
                    st.success("密码已修改。")
                    log_operation("修改密码", detail=f"用户 {user['username']} 修改了自己的密码")
                else:
                    st.error("当前密码错误或新密码为空。")

def render_admin_data_tools():
    user = current_user()
    if not user or not user["is_admin"]:
        return
    with st.sidebar.expander("🛡️ 数据安全与日志", expanded=False):
        health = get_database_health()
        st.markdown("**数据库健康状态**")
        st.caption(f"数据库：{health['db_path']}")
        st.caption(f"备份目录：{health['backup_dir']}")
        c1, c2 = st.columns(2)
        c1.metric("任务数", health["task_count"])
        c2.metric("库大小", f"{health['db_size_mb']} MB")
        c3, c4 = st.columns(2)
        c3.metric("已移除", health["deleted_count"])
        c4.metric("日志数", health["log_count"])
        if health["latest_backup"]:
            status_text = "今日已备份" if health["today_backup_ok"] else "今日未检测到备份"
            st.info(f"{status_text}｜最近备份：{health['latest_backup']['name']}｜{health['latest_backup']['time']}")
        else:
            st.warning("暂未发现数据库备份。")
        if health["last_log"]:
            last = health["last_log"]
            st.caption(f"最后操作：{last.get('created_at')}｜{last.get('operator', '-')}｜{last.get('action')}")
        st.divider()

        if st.button("立即备份数据库", use_container_width=True, key="btn_manual_db_backup"):
            backup_file = create_manual_backup()
            if backup_file:
                st.success(f"已备份：{os.path.basename(backup_file)}")
            else:
                st.error("数据库文件不存在，备份失败。")
        snapshot_bytes = get_database_snapshot().encode("utf-8")
        st.download_button(
            "下载当前数据库快照",
            data=snapshot_bytes,
            file_name=f"fdm_postgresql_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json",
            use_container_width=True,
            key="download_current_db",
        )

        st.divider()
        st.markdown("**操作日志查询**")
        actions = ["全部"] + sorted({row["action"] for row in list_operation_logs(limit=500)})
        action_filter = st.selectbox("操作类型", actions, key="op_log_action_filter")
        log_limit = st.selectbox("显示条数", [50, 100, 200, 500], index=1, key="op_log_limit")
        logs = list_operation_logs(limit=log_limit, action_filter=action_filter)
        if logs:
            df_logs = pd.DataFrame([dict(row) for row in logs]).rename(columns={
                "action": "操作",
                "task_id": "任务ID",
                "machine_id": "设备",
                "operator": "操作人",
                "detail": "详情",
                "created_at": "时间",
            })
            st.dataframe(df_logs, use_container_width=True, hide_index=True)
        else:
            st.caption("暂无操作日志。")

def on_exception_submit(tid):
    val = st.session_state.get(f"ex_{tid}")
    if val:
        update_task_field_log(tid, "exception_log", val, "异常记录")
        st.session_state[f"ex_{tid}"] = "" 

def on_transfer_notes_submit(tid):
    val = st.session_state.get(f"note_{tid}")
    if val:
        update_task_field_log(tid, "transfer_notes", val, "班次交接记录")
        st.session_state[f"note_{tid}"] = ""

def update_task_field_log(tid, field, value, action):
    with get_conn() as conn:
        row = conn.execute("SELECT data FROM tasks WHERE id = ?", (str(tid),)).fetchone()
        if not row:
            return
        task = normalize_task(json.loads(row["data"]))
        old = task.get(field, "-")
        time_prefix = get_short_log_time()
        task[field] = f"[{time_prefix}]{value}" if old == "-" else f"{old} | [{time_prefix}]{value}"
        conn.execute(
            "UPDATE tasks SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(task, ensure_ascii=False), get_formatted_time(), str(tid)),
        )
        conn.execute(
            "INSERT INTO operation_logs (action, task_id, machine_id, operator, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (action, str(tid), str(task.get("machine_id", "")), st.session_state.get("auth_user", ""), value, get_formatted_time()),
        )

def toggle_batch_status(tid, idx, raw_file_name):
    with get_conn() as conn:
        row = conn.execute("SELECT data FROM tasks WHERE id = ? FOR UPDATE", (str(tid),)).fetchone()
        if not row:
            return
        task = normalize_task(json.loads(row["data"]))
        if idx < 0 or idx >= len(task["batch_statuses"]):
            return
        now = get_formatted_time()
        action = ""
        alert_key = f"alert_err_{tid}"
        if task["batch_statuses"][idx] == "待打印":
            running_indices = [i for i, status in enumerate(task["batch_statuses"]) if status == "打印中" and i != idx]
            if running_indices:
                st.session_state[alert_key] = f"当前任务已有第 {running_indices[0] + 1} 盘正在打印，请先完成该盘后再启动新的文件。"
                return
            task["batch_statuses"][idx] = "打印中"
            task["batch_start_times"][idx] = now
            action = "批次上机"
        elif task["batch_statuses"][idx] == "打印中":
            task["batch_statuses"][idx] = "已完成"
            task["batch_end_times"][idx] = now
            task.setdefault("finished_batch_timestamps", []).append(f"盘{idx+1}:{now}")
            action = "批次完工"
        elif task["batch_statuses"][idx] == "已完成":
            task["batch_statuses"][idx] = "待打印"
            task["batch_start_times"][idx] = "-"
            task["batch_end_times"][idx] = "-"
            action = "批次重置"
        else:
            return
        st.session_state.pop(alert_key, None)
        conn.execute(
            "UPDATE tasks SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(task, ensure_ascii=False), now, str(tid)),
        )
        conn.execute(
            "INSERT INTO operation_logs (action, task_id, machine_id, operator, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (action, str(tid), str(task.get("machine_id", "")), st.session_state.get("auth_user", ""), f"盘{idx+1}:{raw_file_name}", now),
        )

def check_maintenance_expiry(tasks):
    expiry_alerts = []
    maintain_records = [t for t in tasks if "已完成-设备维保" in str(t.get("status")) and t.get("end_time") != "-"]
    if not maintain_records: return expiry_alerts
    device_logs = {}
    for r in maintain_records:
        mc = r.get("machine_id")
        m_type = r.get("test_task_type")
        try:
            end_dt = datetime.strptime(r.get("end_time").split(" ")[0], "%Y-%m-%d")
            device_logs.setdefault(mc, {})
            if m_type not in device_logs[mc] or end_dt > device_logs[mc][m_type]:
                device_logs[mc][m_type] = end_dt
        except: pass
    now_dt = datetime.now()
    for mc, types in device_logs.items():
        for m_type, last_dt in types.items():
            days_passed = (now_dt - last_dt).days
            if "1个月小保养" in m_type and days_passed > 30:
                expiry_alerts.append(f"⚠️ 设备 {mc} [小保养] 已超期 {days_passed - 30} 天！")
            elif "2个月大保养" in m_type and days_passed > 60:
                expiry_alerts.append(f"🚨 设备 {mc} [大保养] 已超期 {days_passed - 60} 天！")
    return expiry_alerts

init_database()
ensure_default_admin()
backup_database_once_daily()
migrate_legacy_json_once()
login_gate()
all_tasks = load_tasks()

# ==================== 侧边栏 ====================
with st.sidebar:
    auth_user = current_user()
    st.markdown(f"### 👋 {auth_user['username']}")
    if st.button("退出登录", use_container_width=True):
        delete_login_session(get_session_token_from_url())
        st.session_state.pop("auth_user", None)
        st.session_state.pop("_current_user_row", None)
        st.query_params.clear()
        request_view_refresh()
    page_options = ["电子看板"]
    if auth_user['is_admin']:
        page_options.append("后台管理")
    elif any(can(p) for p in ['report_task_flow', 'report_maintenance', 'report_oee', 'report_efficiency']):
        page_options.append("报表中心")
    app_page = st.radio("页面", page_options, key="app_page", horizontal=False)
    st.divider()
    if app_page == "电子看板":
        render_admin_data_tools()
        st.divider()
    
        expiry_messages = check_maintenance_expiry(all_tasks)
        if expiry_messages:
            st.markdown("### ⏰ 维保超期警报")
            for msg in expiry_messages[:4]: st.warning(msg)
            st.divider()

        if "form_version" not in st.session_state:
            st.session_state["form_version"] = 0
        v = st.session_state["form_version"]
    
        if can("dispatch_task"):
            with st.expander("📝 测试工程师任务下发", expanded=False):
                with st.container(border=True):
                    login_engineer = current_user()["username"]
                    st.text_input("测试工程师 *", value=login_engineer, disabled=True, key=f"eng_login_{v}")
                    sel_task_type = st.selectbox("测试任务类型 *", options=TASK_TYPES, key=f"type_select_{v}")
                    custom_task_type = st.text_input("✍️ 自定义任务类型 *", key=f"cust_type_{v}") if sel_task_type == "其他" else ""
            
                    machine_id = st.text_input("设备编号 *", placeholder="请输入机台号", key=f"form_mc_id_{v}")
                    machine_id_clean = normalize_machine_id(machine_id)
                    is_bound_machine = "#" in machine_id_clean
                    occupied_task = None
                    if is_bound_machine:
                        occupied_task = next(
                            (
                                t for t in all_tasks
                                if normalize_machine_id(t.get("machine_id", "")) == machine_id_clean
                                and t.get("status") in (["打印中"] + SPECIAL_STATUSES)
                            ),
                            None,
                        )
                    if occupied_task:
                        st.error(
                            f"设备 {machine_id_clean} 当前占用中：{occupied_task.get('status')} / {occupied_task.get('test_task_type', '-')}"
                        )
                    elif machine_id_clean and not is_bound_machine:
                        st.caption("未输入 # 的设备编号将按不指定/占坑任务处理，后续可在任务卡片中绑定具体设备。")
                    material = st.text_input("样品牌号 *", key=f"form_mat_{v}")
            
                    uploaded_gcodes = st.file_uploader(
                        "📂 拖入该任务的所有 Gcode 文件 *", 
                        type=["gcode", "3mf"], 
                        accept_multiple_files=True,
                        key=f"gcode_uploader_sidebar_{v}" 
                    )
                    preview_total_hours = 0.0
                    if uploaded_gcodes:
                        preview_rows = []
                        for gfile in uploaded_gcodes:
                            bytes_data = gfile.read()
                            file_hours = parse_gcode_time_fast(bytes_data, gfile.name)
                            preview_total_hours += file_hours
                            preview_rows.append(f"{gfile.name}: {file_hours} 小时")
                            gfile.seek(0)
                        st.info(f"⏱️ 切片预览总耗时：{round(preview_total_hours, 1)} 小时 ｜ 共 {len(uploaded_gcodes)} 盘")
                        with st.expander("查看各文件预估耗时", expanded=False):
                            for row in preview_rows:
                                st.caption(row)
            
                    special_notes = st.text_area("注意事项", key=f"form_notes_{v}")
            
                    if st.button("🚀 发送任务", use_container_width=True, type="primary", key=f"submit_btn_{v}", disabled=bool(occupied_task)):
                        final_eng = login_engineer
                        final_type = custom_task_type.strip() if sel_task_type == "其他" else sel_task_type
                
                        if occupied_task:
                            st.error(f"设备 {machine_id_clean} 当前不属于空闲状态，不能绑定下发。")
                        elif not final_eng or not final_type or not material.strip() or not machine_id_clean or not uploaded_gcodes:
                            st.error("⚠️ 请完整填写信息并上传对应的 Gcode 文件！")
                        else:
                            computed_batches = len(uploaded_gcodes)
                            accumulated_hours = 0.0
                            gcode_names = []  
                    
                            for gfile in uploaded_gcodes:
                                gcode_names.append(gfile.name)  
                                bytes_data = gfile.read()
                                file_hours = parse_gcode_time_fast(bytes_data, gfile.name)
                                accumulated_hours += file_hours
                    
                            final_total_hours = round(accumulated_hours, 1)
                            current_time = get_formatted_time()
                    
                            all_tasks.append({
                                "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
                                "engineer": final_eng, 
                                "test_task_type": final_type, 
                                "machine_id": machine_id_clean,
                                "material": material.strip(), 
                                "total_batches": computed_batches,
                                "gcode_names": gcode_names,  
                                "theory_total_hours": final_total_hours, 
                                "finished_batch_timestamps": [], 
                                "batch_statuses": ["待打印"] * computed_batches,
                                "batch_start_times": ["-"] * computed_batches,
                                "batch_end_times": ["-"] * computed_batches,
                                "status": "待上机",
                                "start_time": "-", "operator": "-", "end_operator": "-", "end_time": "-",
                                "eta_time": "-",
                                "special_notes": special_notes,
                                "exception_log": "-", "transfer_notes": "-",
                                "created_at": current_time
                            })
                            log_operation("下发任务", all_tasks[-1], f"任务类型:{final_type}; 样品牌号:{material.strip()}; 盘数:{computed_batches}", final_eng)
                            save_tasks(all_tasks)
                    
                            st.session_state["form_version"] += 1
                            request_view_refresh()
        if not can("dispatch_task"):
            st.info("当前账号无下发任务权限。")
        st.divider()
        if can("edit_device_status"):
            with st.expander("🛠️ 快速修改设备状态", expanded=False):
                with st.container(border=True):
                    m_id = normalize_machine_id(st.text_input("设备编号 *", placeholder="例如: 5#", key="m_id_speed"))
                    current_special_task = next(
                        (
                            t for t in all_tasks
                            if normalize_machine_id(t.get("machine_id")) == m_id
                            and t.get("status") in SPECIAL_STATUSES
                        ),
                        None,
                    )
                    current_special_status = current_special_task.get("status") if current_special_task else ""
                    m_status = st.selectbox("变更状态为 *", options=["正常空闲", "故障维修", "设备维保", "外部借用", "材料前期测试", "长周期测试"])
                    m_sub_type = st.radio("维保类型 *", options=MAINTAIN_TYPES, horizontal=True) if m_status == "设备维保" else ""
                    m_op = current_user()["username"]
                    st.text_input("操作人 *", value=m_op, disabled=True, key="m_op_speed")
                    need_restore_detail = m_status == "正常空闲" and current_special_status in ["故障维修", "设备维保"]
                    if m_status == "正常空闲" and current_special_status:
                        st.caption(f"当前设备状态：{current_special_status}")
                    m_detail = st.text_input("维护详情 *", placeholder="具体维护内容", key="m_detail_speed").strip() if need_restore_detail else ""
        
                    if st.button("💾 确认变更状态", use_container_width=True, key="btn_speed_status"):
                        if m_id and m_op:
                            if need_restore_detail and not m_detail:
                                st.error("该设备上次状态为故障维修或设备维保，恢复空闲前请填写维护详情。")
                                st.stop()
                            for t in all_tasks:
                                if normalize_machine_id(t.get('machine_id')) == m_id and t.get('status') in SPECIAL_STATUSES and m_status == "正常空闲":
                                    t['status'] = f"已完成-{t['status']}" 
                                    t['end_time'] = get_formatted_time()
                                    if m_detail: t['exception_log'] = m_detail  
                            if m_status != "正常空闲":
                                all_tasks = [t for t in all_tasks if not (normalize_machine_id(t.get('machine_id')) == m_id and t.get('status') in SPECIAL_STATUSES)]
                                log_type_display = f"{m_status}({m_sub_type})" if m_status == "设备维保" else m_status
                                all_tasks.append({
                                    "id": datetime.now().strftime("%Y%m%d%H%M%S%f"), "engineer": m_op, "test_task_type": log_type_display, 
                                    "machine_id": m_id, "material": log_type_display, "total_batches": 1, "finished_batch_timestamps": [], 
                                    "batch_statuses": ["已完成"], "batch_start_times": [get_formatted_time()], "batch_end_times": [get_formatted_time()],
                                    "status": m_status, "start_time": get_formatted_time(), "end_time": "-", "operator": m_op, "end_operator": "-",
                                    "special_notes": "快速锁定状态", "exception_log": "-", "transfer_notes": "-", "created_at": get_formatted_time()
                                })
                            log_operation("修改设备状态", {"machine_id": m_id}, f"状态变更为:{m_status}; 操作人:{m_op}; 详情:{m_detail}")
                            save_tasks(all_tasks)
                            request_view_refresh()

        if not can("edit_device_status"):
            st.info("当前账号无设备状态修改权限。")
        st.divider()
    
        if current_user()["is_admin"]:
            with st.popover("🚨 清除所有记录", use_container_width=True):
                pwd = st.text_input("管理员密码", type="password")
                if st.button("确认清空"):
                    if pwd and pwd.lower() == "kexcelled": clear_all_tasks(); log_operation("清空所有记录", detail="管理员清空任务表"); request_view_refresh()
                    else: st.error("❌ 密码错误")

# ==================== 看板数据流转逻辑与绝对精确检索 ====================
st.markdown("""
    <style>
    .block-container {
        padding-top: 1.35rem !important;
        padding-bottom: 1.5rem !important;
    }
    div[data-testid="stStatusWidget"],
    div[data-testid="stStatusWidget"] *,
    [data-testid="stAppRunningIcon"],
    div[data-testid="stSpinner"],
    div[data-testid="stSpinner"] * {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
    }
    header [data-testid="stToolbar"],
    [data-testid="stHeaderActionElements"],
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"] {
        display: flex !important;
        visibility: visible !important;
        opacity: 1 !important;
        pointer-events: auto !important;
    }
        div[data-testid="stAppViewContainer"],
        div[data-testid="stAppViewBlockContainer"],
        section[data-testid="stSidebar"] {
            opacity: 1 !important;
            filter: none !important;
        }
    [class*="stale"],
    [class*="Stale"],
    [class*="stale"] *,
    [class*="Stale"] *,
    div[data-testid*="stale"],
    div[data-testid*="Stale"],
    div[data-testid*="stale"] *,
    div[data-testid*="Stale"] *,
    div[data-testid="stElementContainer"],
    div[data-testid="stElementContainer"] *,
    div[data-testid="stVerticalBlock"],
    div[data-testid="stVerticalBlock"] *,
    div[data-testid="stHorizontalBlock"],
    div[data-testid="stHorizontalBlock"] *,
    div[data-testid="stMarkdownContainer"],
    div[data-testid="stMarkdownContainer"] *,
    div[data-testid="stButton"],
    div[data-testid="stButton"] *,
    div[data-testid="stProgress"],
    div[data-testid="stProgress"] *,
    div[data-testid="stDataFrame"],
    div[data-testid="stDataFrame"] * {
            opacity: 1 !important;
            filter: none !important;
            backdrop-filter: none !important;
        }
    div[data-testid="stAppViewContainer"]::before,
    div[data-testid="stAppViewContainer"]::after,
    section.main::before,
    section.main::after,
    .stApp::before,
    .stApp::after {
        opacity: 0 !important;
        display: none !important;
        background: transparent !important;
    }
    [aria-busy="true"],
    [aria-busy="true"] * {
        opacity: 1 !important;
        filter: none !important;
    }
    button:disabled,
    button[disabled] {
        opacity: 0.48 !important;
        cursor: not-allowed !important;
        filter: grayscale(0.25) !important;
    }
    html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stAppViewBlockContainer"] {
        transition: none !important;
        animation: none !important;
        background: #ffffff !important;
    }
    * {
        transition-property: background-color, border-color, color, box-shadow !important;
    }
    h1 {
        margin-top: 0 !important;
        margin-bottom: 0.35rem !important;
        padding-top: 0 !important;
        line-height: 1.16 !important;
        overflow: visible !important;
    }
    div[data-testid="stMarkdownContainer"],
    div[data-testid="stMarkdownContainer"] h1 {
        overflow: visible !important;
        line-height: 1.16 !important;
    }
    div[data-testid="stElementContainer"]:has(.app-title),
    div[data-testid="stMarkdownContainer"]:has(.app-title) {
        min-height: 116px !important;
        overflow: visible !important;
    }
    h1 a, h2 a, h3 a, h4 a,
    div[data-testid="stMarkdownContainer"] a[href^="#"] {
        display: none !important;
        visibility: hidden !important;
    }
    .app-title {
        display: flex;
        align-items: flex-start;
        gap: 14px;
        margin: 0 0 0.25rem 0;
        padding: 20px 0 18px 0;
        min-height: 108px;
        color: #2B2D3A;
        overflow: visible !important;
    }
    .app-title-icon {
        flex: 0 0 auto;
        display: inline-flex;
        align-items: center;
        height: 68px;
        font-size: 38px;
        line-height: 1;
        overflow: visible;
    }
    .app-title-text {
        display: inline-block;
        font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "SimHei", sans-serif;
        font-size: 36px;
        font-weight: 700;
        line-height: 1.8;
        letter-spacing: 0;
        padding: 0 0 12px 0;
        overflow: visible;
        white-space: nowrap;
        text-rendering: geometricPrecision;
        -webkit-font-smoothing: antialiased;
    }
    .task-section-title {
        margin: 0.35rem 0 0.85rem 0;
        line-height: 1.15;
        font-size: 28px;
        font-weight: 900;
        color: #2B2D3A;
    }
    h3 {
        margin-top: 0.35rem !important;
        margin-bottom: 0.35rem !important;
    }
    div[data-testid="stTextInput"] {
        margin-bottom: 0 !important;
    }
    div[data-testid="stTextInput"] > div {
        margin-top: 0 !important;
    }
    div[data-testid="stTextInput"] input {
        min-height: 34px !important;
        height: 34px !important;
        padding-top: 4px !important;
        padding-bottom: 4px !important;
    }
    .dashboard-time {
        color: #4B5563;
        font-size: 15px;
        font-weight: 600;
        margin-top: 0;
        margin-bottom: 0.45rem;
    }
    div[data-testid="stHorizontalBlock"] {
        gap: 0.55rem !important;
        align-items: flex-start !important;
    }
    div[data-testid="stHorizontalBlock"] div.stButton > button {
        padding-top: 6px !important;
        padding-bottom: 6px !important;
        margin-top: 1px !important;
        margin-bottom: 1px !important;
        min-height: 40px !important;
        line-height: 1.2 !important;
        font-size: 16px !important;
    }
    span.batch-done + div button,
    div[data-testid="stElementContainer"]:has(> div span.batch-done) + div[data-testid="stElementContainer"] button {
        background: #ECFDF5 !important;
        border-color: #34D399 !important;
        color: #065F46 !important;
        font-weight: 800 !important;
    }
    span.batch-wait + div button,
    div[data-testid="stElementContainer"]:has(> div span.batch-wait) + div[data-testid="stElementContainer"] button {
        background: #FFFBEB !important;
        border-color: #F59E0B !important;
        color: #92400E !important;
        font-weight: 800 !important;
    }
    span.batch-running + div button,
    div[data-testid="stElementContainer"]:has(> div span.batch-running) + div[data-testid="stElementContainer"] button {
        background: #FF4B4B !important;
        border-color: #FF4B4B !important;
        color: #FFFFFF !important;
        font-weight: 900 !important;
    }
    </style>
""", unsafe_allow_html=True)
if app_page == "后台管理":
    st.markdown(
        "<div class='app-title'><span class='app-title-icon'>🛠️</span><span class='app-title-text'>FDM 后台管理</span></div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"<div class='dashboard-time'>当前时间：{get_formatted_time()}</div>", unsafe_allow_html=True)
    render_user_management_panel()
    render_reports_section(all_tasks, expanded=True)
    st.stop()

if app_page == "报表中心":
    st.markdown(
        "<div class='app-title'><span class='app-title-icon'>📊</span><span class='app-title-text'>FDM 报表中心</span></div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"<div class='dashboard-time'>当前时间：{get_formatted_time()}</div>", unsafe_allow_html=True)
    render_reports_section(all_tasks, expanded=True)
    st.stop()

st.markdown(
    "<div class='app-title'><span class='app-title-icon'>🖨️</span><span class='app-title-text'>FDM 打印室任务执行电子看板</span></div>",
    unsafe_allow_html=True,
)
st.markdown(f"<div class='dashboard-time'>当前时间：{get_formatted_time()}</div>", unsafe_allow_html=True)

status_printing_tasks = [t for t in all_tasks if t.get("status") == "打印中"]
status_special_tasks = [t for t in all_tasks if t.get("status") in SPECIAL_STATUSES]
status_active_devices = status_printing_tasks + status_special_tasks

with st.expander(f"🗒️ {get_today_key()} 日志", expanded=False):
    attention_tasks = active_attention_tasks(all_tasks)
    if attention_tasks:
        for task in attention_tasks:
            st.markdown(f"""
            <div style="border-left:4px solid #EF4444; background:#FEF2F2; padding:8px 10px; margin-bottom:6px; border-radius:4px;">
                <div style="font-size:12px; color:#374151;">
                    <b>设备:</b> {task.get('machine_id', '-')} ｜ 
                    <b>测试牌号:</b> {task.get('material', '-')} ｜ 
                    <b>测试工程师:</b> {task.get('engineer', '-')} ｜ 
                    <b>状态:</b> {task.get('status', '-')}
                </div>
                <div style="font-size:14px; font-weight:800; color:#991B1B; margin-top:3px;">{task.get('special_notes')}</div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.caption("当前没有未下机任务的注意事项。")

st.markdown("### 🔍 设备快速筛选")
search_query = normalize_machine_id(st.text_input(
    "搜索设备编号",
    key="search_input",
    placeholder="输入纯数字(如: 5)按回车",
    label_visibility="collapsed",
))

# ==================== 顶部设备运行实况 ====================
if not search_query:
    st.markdown("### 🖥️ 设备实时运行状态")
    if not status_active_devices:
        st.info("🟢 所有设备空闲中")
    else:
        def status_group_key(task):
            status_now = task.get("status")
            if task.get("is_paused", False):
                return "paused"
            if status_now == "打印中":
                return "green"
            if status_now == "故障维修":
                return "red"
            if status_now == "设备维保":
                return "blue"
            if status_now == "长周期测试":
                return "purple"
            if status_now == "材料前期测试":
                return "cyan"
            return "yellow"

        grouped_devices = {"green": [], "paused": [], "yellow": [], "cyan": [], "red": [], "purple": [], "blue": []}
        for task in status_active_devices:
            grouped_devices.setdefault(status_group_key(task), []).append(task)

        group_labels = {
            "green": ("打印中", "#ECFDF5", "#10B981", "#065F46"),
            "paused": ("暂停中", "#F3F4F6", "#6B7280", "#111827"),
            "yellow": ("占用/借用", "#FFFBEB", "#F59E0B", "#92400E"),
            "cyan": ("材料前期测试", "#CCFBF1", "#14B8A6", "#115E59"),
            "red": ("故障维修", "#FEE2E2", "#EF4444", "#991B1B"),
            "purple": ("长周期测试", "#F3E8FF", "#A855F7", "#6B21A8"),
            "blue": ("设备维保", "#DBEAFE", "#3B82F6", "#1E40AF"),
        }

        for group_key in ["green", "paused", "yellow", "cyan", "red", "purple", "blue"]:
            group_tasks = grouped_devices.get(group_key, [])
            for row_start in range(0, len(group_tasks), 6):
                row_tasks = group_tasks[row_start:row_start + 6]
                group_count = len(group_tasks)
                label, label_bg, label_border, label_color = group_labels.get(
                    group_key, ("其他状态", "#F9FAFB", "#D1D5DB", "#374151")
                )
                label_col, cards_col = st.columns([0.42, 5.58])
                with label_col:
                    if row_start == 0:
                        st.markdown(
                            f"""
                            <div style="
                                min-height: 82px;
                                display:flex;
                                align-items:center;
                                justify-content:center;
                                text-align:center;
                                padding: 6px 4px;
                                border-left: 4px solid {label_border};
                                background: {label_bg};
                                color: {label_color};
                                border-radius: 6px;
                                font-size: 13px;
                                font-weight: 900;
                                line-height: 1.25;
                                margin-bottom: 5px;
                                flex-direction: column;
                            ">
                                <div>{label}</div>
                                <div style="font-size:12px; font-weight:800; margin-top:6px; opacity:0.86;">{group_count} 台</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            """
                            <div style="
                                min-height: 82px;
                                margin-bottom: 5px;
                            "></div>
                            """,
                            unsafe_allow_html=True,
                        )
                with cards_col:
                    cols = st.columns(6)
                for idx, t in enumerate(row_tasks):
                    status_now = t.get("status")
                
                    if "batch_statuses" in t:
                        count = len([s for s in t["batch_statuses"] if s == "已完成"])
                    else:
                        count = len(t.get("finished_batch_timestamps", []))
                    
                    total = t.get("total_batches", 1)
                    progress = min(count / max(total, 1), 1.0)
                
                    with cols[idx]:
                        if t.get("is_paused", False):
                            bg_color, border_color, text_color = "#F3F4F6", "#6B7280", "#111827"
                            st.markdown(f"""
                            <div style="border: 1px solid {border_color}; padding: 6px; border-radius: 6px; background: {bg_color}; font-size: 11px; margin-bottom: 5px;">
                                <div style="font-weight: 900; font-size: 18px; color: {text_color}; border-bottom: 1px solid {border_color}; margin-bottom: 4px; padding-bottom: 2px;">设备: {t.get('machine_id')}</div>
                                <div style="font-weight: bold; color: {text_color};">⏸️ 状态: 暂停中</div>
                                <div style="color: #374151;">原因: {t.get('pause_reason', '-')}</div>
                                <div style="color: #4B5563; font-size: 10px;">暂停: {t.get('pause_start_time', '-')}</div>
                            </div>
                            """, unsafe_allow_html=True)
                        elif status_now in SPECIAL_STATUSES:
                            if status_now == "故障维修": bg_color, border_color, text_color = "#FEE2E2", "#FCA5A5", "#991B1B" 
                            elif status_now == "设备维保": bg_color, border_color, text_color = "#DBEAFE", "#93C5FD", "#1E40AF" 
                            elif status_now == "长周期测试": bg_color, border_color, text_color = "#F3E8FF", "#C084FC", "#6B21A8" 
                            elif status_now == "材料前期测试": bg_color, border_color, text_color = "#CCFBF1", "#5EEAD4", "#115E59" 
                            else: bg_color, border_color, text_color = "#FFFBEB", "#FCD34D", "#92400E" 
                            st.markdown(f"""
                            <div style="border: 1px solid {border_color}; padding: 6px; border-radius: 6px; background: {bg_color}; font-size: 11px; margin-bottom: 5px;">
                                <div style="font-weight: 900; font-size: 18px; color: {text_color}; border-bottom: 1px solid {border_color}; margin-bottom: 4px; padding-bottom: 2px;">设备: {t.get('machine_id')}</div>
                                <div style="font-weight: bold; color: {text_color};">⚠️ 状态: {t.get('test_task_type')}</div>
                                <div style="color: #4B5563;">操作人: {t.get('operator')}</div>
                                <div style="color: #4B5563; font-size: 10px;">时间: {t.get('start_time')}</div>
                            </div>
                            """, unsafe_allow_html=True)
                        else:
                            st.markdown(f"""
                            <div style="border: 1px solid #6EE7B7; padding: 6px; border-radius: 6px; background: #ECFDF5; font-size: 11px; margin-bottom: 5px;">
                                <div style="font-weight: 900; font-size: 18px; color: #065F46; border-bottom: 1px solid #A7F3D0; margin-bottom: 4px; padding-bottom: 2px;">设备: {t.get('machine_id')}</div>
                                <div style="margin-bottom: 1px; color:#1F2937;">工程师: {t.get('engineer','-')}</div>
                                <div style="margin-bottom: 1px; color:#1F2937;">牌号: {t.get('material','-')}</div>
                                <div style="color: #4B5563; font-size: 10px;">派单: {t.get('created_at','-')}</div>
                                <div style="color: #4B5563; font-size: 10px;">上机: {t.get('start_time','-')}</div>
                                <div style="color: #7C3AED; font-weight: bold; font-size: 11px; margin-top: 2px;">🔮 预计下机: {t.get('eta_time', '-')}</div>
                                <div style="font-weight: bold; color: #047857; margin-top: 2px;">进度: {count}/{total} 盘</div>
                            </div>
                            """, unsafe_allow_html=True)
                            st.progress(progress)
    st.divider()

display_tasks = all_tasks.copy()
if search_query:
    display_tasks = [t for t in display_tasks if normalize_machine_id(t.get("machine_id", "")) == search_query]

printing_tasks = [t for t in display_tasks if t.get("status") == "打印中"]
special_status_tasks = [t for t in display_tasks if t.get("status") in SPECIAL_STATUSES]
pending_tasks = [t for t in display_tasks if t.get("status") == "待上机"]
if search_query:
    st.info(f"精确搜索设备：{search_query} ｜ 打印中 {len(printing_tasks)} 项 ｜ 待上机 {len(pending_tasks)} 项")
    st.divider()

# ==================== 主看板卡片渲染 ====================
col1, col2 = st.columns(2, vertical_alignment="top")

def render_task_card(task, is_printing):
    global all_tasks 
    border = "#10B981" if is_printing else "#3B82F6"
    tid = task['id']

    with st.container(border=True):
        curr_mc = normalize_machine_id(task.get('machine_id', '待定'))
        is_occupied_anywhere = any(normalize_machine_id(t.get('machine_id')) == curr_mc and t.get("status") in (["打印中"] + SPECIAL_STATUSES) for t in all_tasks)
    
        is_unbound_mc = "#" not in curr_mc
    
        if not is_printing and is_unbound_mc and can("edit_device_status"):
            title_col, edit_col = st.columns([5, 3])
            with title_col:
                st.markdown(f"### 设备编号: {curr_mc}")
            with edit_col:
                with st.popover("🔧 指定具体设备", use_container_width=True):
                    new_mc = normalize_machine_id(st.text_input("请输入确定的机台号:", value="", placeholder="例如: 5#", key=f"inp_mc_{tid}"))
                    if st.button("💾 确认绑定", key=f"btn_mc_{tid}", type="primary", use_container_width=True):
                        if new_mc:
                            is_target_busy = any(normalize_machine_id(t.get('machine_id')) == new_mc and t.get('status') in (["打印中"] + SPECIAL_STATUSES) for t in all_tasks)
                            if is_target_busy:
                                st.error(f"⚠️ 机台 {new_mc} 目前正在使用中或已被挂牌占用，无法重复绑定！")
                            else:
                                for t in all_tasks:
                                    if t['id'] == tid: t['machine_id'] = new_mc
                                log_operation("绑定设备", task, f"绑定设备为:{new_mc}")
                                save_tasks(all_tasks)
                                request_view_refresh()
        else:
            st.markdown(f"### 设备编号: {curr_mc}")
        
        st.write(f"测试工程师: {task.get('engineer')} | 任务类型: **{task.get('test_task_type')}**")
        st.write(f"样品牌号: {task.get('material')} | 📦 文件总盘数: **{task.get('total_batches')} 盘**")
    
        theory_hours = task.get('theory_total_hours')
        if theory_hours is None:
            st.markdown("⏱️ 切片理论总耗时: <span style='color:#6B7280; font-weight:bold;'>无 (历史老任务)</span>", unsafe_allow_html=True)
        else:
            st.markdown(f"⏱️ 切片理论总耗时: <span style='color:#1E40AF; font-weight:bold;'>{theory_hours} 小时</span>", unsafe_allow_html=True)
    
        notes = task.get('special_notes', '无')
        clean_notes = notes.strip() if notes else "无"
    
        if is_printing:
            st.markdown(f"🏁 实际上机时间: {task.get('start_time')}")
            st.markdown(f"🔮 <span style='font-size:16px; color:#7C3AED; font-weight:bold;'>预计下机时间: {task.get('eta_time', '-')}</span>", unsafe_allow_html=True)
            is_paused = bool(task.get("is_paused", False))
            if is_paused:
                st.markdown(
                    f"<div style='background:#FFF7ED; border:1px solid #FDBA74; color:#9A3412; padding:8px; border-radius:4px; font-weight:700;'>⏸️ 当前任务已暂停 ｜ {task.get('pause_start_time', '-')} ｜ 原因：{task.get('pause_reason', '-')}</div>",
                    unsafe_allow_html=True,
                )
        
            if not clean_notes or clean_notes in ["无", "-", "空白"]:
                st.markdown(f"📝 注意事项: <span style='color:#1F2937;'>无</span>", unsafe_allow_html=True)
            else:
                st.markdown(f"📝 注意事项: <span style='color:#EF4444; font-weight:bold;'>{clean_notes}</span>", unsafe_allow_html=True)
        
            task.setdefault("batch_statuses", ["待打印"] * task["total_batches"])
            task.setdefault("batch_start_times", ["-"] * task["total_batches"])
            task.setdefault("batch_end_times", ["-"] * task["total_batches"])
        
            count = len([s for s in task["batch_statuses"] if s == "已完成"])
            total = task.get("total_batches", 1)
            running_batch_indices = [i for i, s in enumerate(task["batch_statuses"]) if s == "打印中"]
            st.progress(min(count / max(total, 1), 1.0))
        
            ex_log = task.get("exception_log", "-")
            if ex_log and ex_log != "-":
                st.markdown(f"**已记录异常:** <span style='color:#D97706; font-weight:bold;'>{ex_log}</span>", unsafe_allow_html=True)
            
            tr_notes = task.get("transfer_notes", "-")
            if tr_notes and tr_notes != "-":
                st.markdown(f"**班次交接记录:** <span style='color:#2563EB; font-weight:bold;'>{tr_notes}</span>", unsafe_allow_html=True)
        
            alert_err_key = f"alert_err_{tid}"
            if alert_err_key in st.session_state:
                st.error(st.session_state[alert_err_key])
        
            st.markdown("<div style='margin-top:12px;'></div>", unsafe_allow_html=True)
            gcode_list = task.get("gcode_names", [])
            st.markdown("<span style='font-size:12px; font-weight:bold; color:#4B5563;'>📋 各盘任务流转清单 (点选转换状态):</span>", unsafe_allow_html=True)

            gcode_area_col, _ = st.columns([5, 3])
            with gcode_area_col:
                grid_cols = st.columns(2)
                for idx in range(total):
                    if idx < len(gcode_list):
                        raw_file_name = gcode_list[idx]
                    else:
                        raw_file_name = f"未命名测试样件_{idx+1} (历史老任务)"
                
                    clean_name = re.sub(r'(\.gcode)?\.3mf$', '', raw_file_name)
                    clean_name = re.sub(r'\.gcode$', '', clean_name)
                    short_name = clean_name if len(clean_name) <= 15 else f"{clean_name[:10]}..."
                
                    current_batch_status = task["batch_statuses"][idx]
                
                    if current_batch_status == "已完成":
                        btn_text = f"✅ {short_name}"
                        btn_type = "secondary"
                        btn_key = f"done_id_{tid}_{idx}"
                        btn_class = "batch-done"
                        help_tip = f"📁 完整文件名:\n{raw_file_name}\n\n🟢 上机: {task['batch_start_times'][idx]}\n🏁 完工: {task['batch_end_times'][idx]}\n\n💡 提示：单点此键可将其复位重打。"
                    elif current_batch_status == "打印中":
                        btn_text = f"▶️ {short_name} (打印中)"
                        btn_type = "primary"  
                        btn_key = f"run_id_{tid}_{idx}"
                        btn_class = "batch-running"
                        help_tip = f"📁 完整文件名:\n{raw_file_name}\n\n⚡ 启动时间: {task['batch_start_times'][idx]}\n\n💡 提示：再次点击确认该盘打印结束。"
                    else:
                        btn_text = f"⏳ {short_name}"
                        btn_type = "secondary"  
                        btn_key = f"wait_id_{tid}_{idx}"
                        btn_class = "batch-wait"
                        help_tip = f"📁 完整文件名:\n{raw_file_name}\n\n⚪ 状态: 空闲等待中\n\n💡 提示：点击立刻切入上机状态。"

                    with grid_cols[idx % 2]:
                        st.markdown(f"<span class='{btn_class}'></span>", unsafe_allow_html=True)
                        has_other_running_batch = current_batch_status == "待打印" and any(i != idx for i in running_batch_indices)
                        batch_disabled = is_paused or has_other_running_batch or (current_batch_status == "待打印" and not can("start_machine")) or (current_batch_status == "打印中" and not can("end_machine")) or (current_batch_status == "已完成" and not (can("start_machine") or can("end_machine")))
                        if has_other_running_batch:
                            help_tip = f"{help_tip}\n\n⚠️ 当前任务已有其他文件正在打印，请先完成当前打印中的文件。"
                        st.button(
                            btn_text,
                            key=btn_key,
                            type=btn_type,
                            use_container_width=True,
                            help=help_tip,
                            disabled=batch_disabled,
                            on_click=toggle_batch_status,
                            args=(tid, idx, raw_file_name),
                        )

            st.markdown("<div style='margin-top:5px;'></div>", unsafe_allow_html=True)
            record_col, operation_col = st.columns([5, 3])
            with record_col:
                with st.container(border=True):
                    st.markdown("<span style='font-size:13px; font-weight:800; color:#374151;'>现场记录</span>", unsafe_allow_html=True)
                    st.text_input("异常记录", key=f"ex_{tid}", placeholder="输入异常内容后按回车保存", on_change=on_exception_submit, args=(tid,))
                    st.text_input("班次交接记录", key=f"note_{tid}", placeholder="输入交接信息后按回车保存", on_change=on_transfer_notes_submit, args=(tid,))
            
                    if count >= total:
                        st.markdown("<div style='background-color:#ECFDF5; padding:8px; border-radius:4px; border:1px solid #A7F3D0; font-weight:bold; color:#065F46; text-align:center; margin: 8px 0;'>🎉 全盘打印完毕，请确认技术员流转下机</div>", unsafe_allow_html=True)
                        end_op = current_user()["username"]
                        st.text_input("负责下机技术员", value=end_op, disabled=True, key=f"end_op_{tid}")
                        if st.button("🏁 确认完工下机", key=f"btn_end_{tid}", type="primary", use_container_width=True, disabled=not can("end_machine")):
                            now_str = get_formatted_time()
                            eta_str = task.get('eta_time', '-')
                            try:
                                n_p = now_str.split(" ")
                                e_p = eta_str.split(" ")
                                now_dt = datetime.strptime(f"{n_p[0]} {n_p[2]}", "%Y-%m-%d %H:%M")
                                eta_dt = datetime.strptime(f"{e_p[0]} {e_p[2]}", "%Y-%m-%d %H:%M")
                            
                                if now_dt < eta_dt:
                                    st.session_state[alert_err_key] = f"❌ 拦截：当前时间早于预计下机时间！任务尚未真正完工。若发生断料或故障请走右侧 [❌ 提前结束] 流程登记下机原因！"
                                else:
                                    if alert_err_key in st.session_state: del st.session_state[alert_err_key]
                                    for t in all_tasks:
                                        if t['id'] == tid:
                                            t.update({"status": "已完工", "end_operator": end_op, "end_time": now_str})
                                            log_operation("确认下机", t, f"下机技术员:{end_op}")
                                            update_single_task(t)
                                    request_view_refresh()
                            except:
                                if alert_err_key in st.session_state: del st.session_state[alert_err_key]
                                for t in all_tasks:
                                    if t['id'] == tid:
                                        t.update({"status": "已完工", "end_operator": end_op, "end_time": now_str})
                                        log_operation("确认下机", t, f"下机技术员:{end_op}")
                                        update_single_task(t)
                                request_view_refresh()

            with operation_col:
                abort_key = f"abort_{tid}"
                with st.container(border=True):
                    st.markdown("<span style='font-size:13px; font-weight:800; color:#374151;'>操作按钮</span>", unsafe_allow_html=True)
                    st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
                    if st.session_state.get(abort_key, False):
                        abort_reason = st.text_input("下机原因 *", key=f"reason_input_{tid}", placeholder="请输入提前结束的具体原因").strip()
                        c_y, c_n = st.columns(2)
                        with c_y:
                            if st.button("✔️ 确认", key=f"ay_{tid}", type="primary", use_container_width=True):
                                if abort_reason:
                                    if alert_err_key in st.session_state: del st.session_state[alert_err_key]
                                    for t in all_tasks:
                                        if t['id'] == tid:
                                            t['exception_log'] = f"提前下机原因: {abort_reason}"
                                            t.update({"status": "异常中止", "end_time": get_formatted_time()})
                                            log_operation("提前结束任务", t, f"原因:{abort_reason}")
                                            update_single_task(t)
                                    st.session_state[abort_key] = False; request_view_refresh()
                        with c_n:
                            if st.button("❌ 取消", key=f"an_{tid}", use_container_width=True): 
                                st.session_state[abort_key] = False; request_view_refresh()
                    else:
                        if st.button("❌ 提前结束任务", key=f"init_a_{tid}", use_container_width=True, disabled=not can("end_machine")): 
                            if alert_err_key in st.session_state: del st.session_state[alert_err_key]
                            st.session_state[abort_key] = True; request_view_refresh()

                    st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)
                    pause_key = f"pause_{tid}"
                    resume_key = f"resume_{tid}"
                    if task.get("is_paused", False):
                        if st.button("▶️ 恢复此项任务", key=resume_key, type="primary", use_container_width=True, disabled=not can("start_machine")):
                            resume_time = get_formatted_time()
                            for t in all_tasks:
                                if t["id"] == tid:
                                    old = t.get("transfer_notes", "-")
                                    reason = t.get("pause_reason", "-")
                                    resume_log = f"[{resume_time}]恢复任务，暂停原因:{reason}"
                                    t["transfer_notes"] = resume_log if old == "-" else f"{old} | {resume_log}"
                                    t["is_paused"] = False
                                    t["pause_reason"] = "-"
                                    t["pause_start_time"] = "-"
                                    log_operation("恢复任务", t, f"暂停原因:{reason}")
                                    update_single_task(t)
                            request_view_refresh()
                    elif st.session_state.get(pause_key, False):
                        pause_reason = st.text_input("暂停原因 *", key=f"pause_reason_{tid}", placeholder="请输入暂停原因").strip()
                        p_y, p_n = st.columns(2)
                        with p_y:
                            if st.button("⏸️ 确认暂停", key=f"py_{tid}", type="primary", use_container_width=True):
                                if pause_reason:
                                    pause_time = get_formatted_time()
                                    for t in all_tasks:
                                        if t["id"] == tid:
                                            old = t.get("transfer_notes", "-")
                                            pause_log = f"[{pause_time}]暂停任务:{pause_reason}"
                                            t["transfer_notes"] = pause_log if old == "-" else f"{old} | {pause_log}"
                                            t["is_paused"] = True
                                            t["pause_reason"] = pause_reason
                                            t["pause_start_time"] = pause_time
                                            log_operation("暂停任务", t, f"原因:{pause_reason}")
                                            update_single_task(t)
                                    st.session_state[pause_key] = False
                                    request_view_refresh()
                        with p_n:
                            if st.button("取消", key=f"pn_{tid}", use_container_width=True):
                                st.session_state[pause_key] = False
                                request_view_refresh()
                    else:
                        if st.button("⏸️ 暂停此项任务", key=f"init_p_{tid}", use_container_width=True, disabled=not can("end_machine")):
                            st.session_state[pause_key] = True
                            request_view_refresh()
                    st.markdown("<div style='height:22px;'></div>", unsafe_allow_html=True)
                      
        else:
            if not clean_notes or clean_notes in ["无", "-", "空白"]:
                st.markdown(f"📝 注意事项: <span style='color:#1F2937;'>无</span>", unsafe_allow_html=True)
            else:
                st.markdown(f"📝 注意事项: <span style='color:#EF4444; font-weight:bold;'>{clean_notes}</span>", unsafe_allow_html=True)
            
            st.write(f"派单时间: {task.get('created_at', '-')}")
            op_name = current_user()["username"]
            st.text_input("负责技术员", value=op_name, disabled=True, key=f"op_{tid}")
            bc1, bc2 = st.columns([5, 2])
            with bc1:
                if is_unbound_mc: 
                    st.button("▶️ 请先指定具体设备", use_container_width=True, disabled=True, key=f"btn_lockout_{tid}", help="该测试任务目前为占坑/模糊状态，请先在上方修改并绑定确切的设备编号！")
                elif is_occupied_anywhere: 
                    st.button("⚠️ 设备占/突中", use_container_width=True, disabled=True, key=f"btn_busy_{tid}")
                else:
                    if st.button("▶️ 确认上机", key=f"btn_start_{tid}", type="primary", use_container_width=True, disabled=not can("start_machine")):
                        for t in all_tasks:
                            if t['id'] == tid:
                                start_time_now = get_formatted_time()
                                raw_th = t.get('theory_total_hours')
                                computed_th = float(raw_th) if raw_th is not None else 2.0
                                eta_calculated = calculate_eta(start_time_now, computed_th)
                                t.update({"status": "打印中", "operator": op_name, "start_time": start_time_now, "eta_time": eta_calculated})
                                log_operation("任务上机", t, f"负责技术员:{op_name}")
                                update_single_task(t)
                        request_view_refresh() 
            with bc2:
                del_key = f"del_{tid}"
                if st.session_state.get(del_key, False):
                    if st.button("✔️", key=f"dy_{tid}", type="primary", use_container_width=True):
                        soft_delete_task(tid); all_tasks = [t for t in all_tasks if t['id'] != tid]; request_view_refresh()
                else:
                    if st.button("🗑️ 移除", key=f"init_d_{tid}", use_container_width=True, disabled=not can("dispatch_task")): st.session_state[del_key] = True; request_view_refresh()

with col1:
    st.markdown(f"<div class='task-section-title'>🖨️ 打印中 ({len(printing_tasks)})</div>", unsafe_allow_html=True)
    for t in printing_tasks: render_task_card(t, True)
with col2:
    st.markdown(f"<div class='task-section-title'>⏳ 待上机 ({len(pending_tasks)})</div>", unsafe_allow_html=True)
    for t in pending_tasks: render_task_card(t, False)
