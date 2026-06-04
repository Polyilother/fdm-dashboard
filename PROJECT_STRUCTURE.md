# FDM Dashboard Project Structure

本文档用于说明 FDM 打印室任务执行电子看板的项目结构、核心文件职责、运行方式和部署流程。

## 1. 项目定位

本项目是一个基于 Streamlit + PostgreSQL 的局域网电子看板系统，用于 FDM 打印室的任务下发、设备状态管理、上机下机记录、权限管理、维保维修日志和报表分析。

当前推荐部署方式：

- 开发电脑在 `G:\FDM_Dashboard_SQLite` 修改代码
- 通过 Git 提交到 GitHub
- Windows 服务器在 `C:\FDM_Dashboard\fdm-dashboard` 执行 `git pull`
- 服务器通过 `deploy.bat` 或 Windows 服务启动看板

## 2. 当前目录结构

```text
G:\FDM_Dashboard_SQLite
├── .git/                         # Git 仓库元数据，不手动修改
├── .venv/                        # 本地 Python 虚拟环境，不提交 Git
├── backups/                      # 数据库备份目录，不提交 Git
├── __pycache__/                  # Python 缓存目录，不提交 Git
├── .gitignore                    # Git 忽略规则
├── deploy.bat                    # Windows 服务器部署与启动脚本
├── fdm_dashboard.py              # Streamlit 主程序，包含 UI、业务逻辑、数据库访问
├── fdm_tasks.db                  # 历史 SQLite 数据文件，当前 PostgreSQL 版本不提交 Git
├── fdm_tasks.json                # 历史 JSON 数据文件，不提交 Git
├── migrate_json_to_sqlite.py     # 历史 JSON 到 SQLite 迁移脚本，保留作追溯
├── README.md                     # 项目简介与基础说明
├── requirements.txt              # Python 依赖清单
├── start_8501.bat                # 旧端口启动脚本，历史保留
└── start_8502.bat                # 本地 8502 端口启动脚本
```

## 3. Git 提交策略

应提交到 Git：

- `fdm_dashboard.py`
- `requirements.txt`
- `deploy.bat`
- `.gitignore`
- `README.md`
- `PROJECT_STRUCTURE.md`
- 必要的脚本或说明文档

不应提交到 Git：

- `.venv/`
- `__pycache__/`
- `backups/`
- `.env`
- 数据库密码文件
- PostgreSQL dump / SQL 备份
- 本地 SQLite / JSON 历史数据文件

当前 `.gitignore` 已包含这些规则。

## 4. 核心文件说明

### 4.1 `fdm_dashboard.py`

项目主程序，当前大部分功能都集中在此文件中。

主要职责：

- Streamlit 页面配置
- 用户登录与会话保持
- 用户权限判断
- PostgreSQL 连接池与数据库初始化
- 测试任务下发
- Gcode / 3MF 文件读取与理论耗时解析
- 打印任务上机、下机、暂停、恢复、提前结束
- 同一任务仅允许一个文件处于打印中
- 设备实时运行状态看板
- 设备快速筛选
- 快速修改设备状态
- 设备维保与故障维修历史日志
- 每日日志 / 注意事项展示
- 报表中心
- 管理员后台
- 数据备份、操作日志、数据库健康状态

关键函数分组：

```text
基础工具
├── parse_gcode_time_fast()
├── get_formatted_time()
├── get_short_log_time()
├── normalize_machine_id()
└── calculate_eta()

PostgreSQL
├── get_pg_password()
├── open_pg_connection()
├── get_pg_pool()
├── ensure_postgres_database()
├── get_conn()
├── init_database()
└── get_database_health()

任务数据
├── normalize_task()
├── load_tasks()
├── save_tasks()
├── update_single_task()
├── upsert_task_row()
├── soft_delete_task()
└── clear_all_tasks()

用户与权限
├── hash_password()
├── verify_password()
├── ensure_default_admin()
├── get_user()
├── create_user()
├── update_user()
├── delete_user()
├── change_password()
├── current_user()
├── user_permissions()
└── can()

登录会话
├── create_login_session()
├── delete_login_session()
├── user_from_session_token()
└── login_gate()

日志与报表
├── log_operation()
├── list_operation_logs()
├── add_daily_log()
├── list_daily_logs()
├── active_attention_tasks()
└── render_reports_section()

任务交互
├── toggle_batch_status()
├── on_exception_submit()
├── on_transfer_notes_submit()
├── update_task_field_log()
└── render_task_card()

后台与工具
├── render_user_management_panel()
├── render_admin_data_tools()
├── create_manual_backup()
├── backup_database_once_daily()
└── write_database_snapshot()
```

### 4.2 `requirements.txt`

当前依赖：

```text
streamlit
pandas
psycopg2-binary
```

### 4.3 `deploy.bat`

Windows 服务器部署脚本。

执行流程：

```text
1. git pull 拉取最新代码
2. 如果没有 .venv，则创建虚拟环境
3. 激活 .venv
4. pip install -r requirements.txt 安装依赖
5. 设置 PostgreSQL 环境变量默认值
6. 启动 Streamlit 8502 端口
```

启动命令：

```bat
python -m streamlit run fdm_dashboard.py --server.address 0.0.0.0 --server.port 8502
```

注意：

- 服务器需要安装 Python。
- 如果服务器只有 `py` 命令，需要先手动创建 `.venv`，或调整脚本兼容 `py`。
- PostgreSQL 密码不要写入 Git 仓库。

### 4.4 `.gitignore`

用于避免提交虚拟环境、缓存、数据库、备份和密码文件。

重点忽略：

```text
.venv/
__pycache__/
*.pyc
logs/
.env
backups/
*.db
*.sqlite
*.sqlite3
fdm_tasks.db
fdm_tasks.json
postgres_password.txt
.streamlit/secrets.toml
*.sql
*.dump
```

## 5. PostgreSQL 数据结构

系统启动时会自动初始化 PostgreSQL 数据库与表。

数据库默认环境变量：

```text
FDM_PGHOST
FDM_PGPORT
FDM_PGDATABASE
FDM_PGUSER
FDM_PGPASSWORD
```

默认数据库名：

```text
fdm_dashboard
```

主要数据表：

```text
tasks
├── id
├── data
├── created_at
└── updated_at

users
├── username
├── password_hash
├── is_admin
├── active
├── permissions
└── created_at

login_sessions
├── token
├── username
├── expires_at
└── created_at

daily_logs
├── log_date
├── device_or_task
├── note
├── author
└── created_at

operation_logs
├── action
├── task_id
├── machine_id
├── operator
├── detail
└── created_at
```

说明：

- `tasks.data` 保存任务主体 JSON 数据。
- 用户权限保存在 `users.permissions`。
- 登录保持通过 `login_sessions` 管理。
- 重要操作写入 `operation_logs`。
- 每日日志写入 `daily_logs`。

## 6. 权限模块

当前权限面向角色控制页面和操作按钮。

常见权限包括：

```text
dispatch_task          # 下发任务
edit_device_status     # 设备状态修改
start_machine          # 上机点击
end_machine            # 下机、暂停、恢复、提前结束相关操作
report_task_flow       # 报表 1：任务流转台账
report_maintenance     # 报表 2：维保维修日志
report_device_flow     # 报表 3：设备流转效率与闲置盲区
report_efficiency      # 报表 4：效率诊断分析
```

管理员账号：

- 可进入后台管理
- 可管理用户账号、密码、权限
- 可查看报表和数据工具

普通账号：

- 根据权限显示对应模块
- 操作人默认使用当前登录账号

## 7. 页面结构

主页面：

```text
电子看板
├── 页面标题与当前时间
├── 每日/任务注意事项日志
├── 设备快速筛选
├── 设备实时运行状态
│   ├── 打印中
│   ├── 暂停中
│   ├── 占用/借用
│   ├── 故障维修
│   ├── 设备维保
│   ├── 材料前期测试
│   └── 长周期测试
├── 打印中任务卡片
│   ├── 任务信息
│   ├── 批次状态按钮
│   ├── 现场记录
│   └── 操作按钮
└── 待上机任务卡片
```

侧边栏：

```text
侧边栏
├── 当前登录用户
├── 退出登录
├── 页面切换
│   ├── 电子看板
│   ├── 后台管理
│   └── 报表中心
├── 测试工程师任务下发
├── 快速修改设备状态
├── 数据安全与日志
└── 清除所有记录
```

后台管理：

```text
后台管理
├── 用户管理
│   ├── 新增用户
│   ├── 修改权限
│   ├── 重置密码
│   └── 删除用户
├── 数据安全
│   ├── 数据库健康状态
│   ├── 手动备份
│   └── 操作日志
└── 报表查看
```

## 8. 本地开发启动

在工作电脑执行：

```powershell
Set-Location G:\FDM_Dashboard_SQLite
.\.venv\Scripts\activate
streamlit run fdm_dashboard.py --server.address 0.0.0.0 --server.port 8502
```

本机访问：

```text
http://127.0.0.1:8502
```

局域网其他电脑访问：

```text
http://工作电脑IP:8502
```

## 9. 服务器首次部署

服务器目录示例：

```text
C:\FDM_Dashboard\fdm-dashboard
```

首次部署步骤：

```powershell
Set-Location C:\FDM_Dashboard
git clone https://github.com/Polyilother/fdm-dashboard.git
Set-Location C:\FDM_Dashboard\fdm-dashboard
```

如果 `python` 可用：

```powershell
.\deploy.bat
```

如果只有 `py` 可用：

```powershell
py -m venv .venv
.\deploy.bat
```

启动成功后访问：

```text
http://服务器IP:8502
```

## 10. 后续更新流程

开发电脑：

```powershell
Set-Location G:\FDM_Dashboard_SQLite
git status
git add .
git commit -m "Update dashboard"
git push
```

服务器：

```powershell
Set-Location C:\FDM_Dashboard\fdm-dashboard
git pull
.\deploy.bat
```

如果已经注册为 Windows 服务，推荐：

```powershell
net stop FDM_Dashboard
Set-Location C:\FDM_Dashboard\fdm-dashboard
git pull
.venv\Scripts\python.exe -m pip install -r requirements.txt
net start FDM_Dashboard
```

## 11. 运行注意事项

- 正式生产使用建议在服务器长期运行，不建议用开发电脑作为正式服务。
- PostgreSQL 数据库必须在服务器上保持运行。
- PostgreSQL 密码建议设置为系统环境变量，不写入代码。
- 服务器防火墙需要放行 TCP 8502。
- 修改业务代码后先在工作电脑测试，再提交 Git。
- 服务器只拉取 Git，不直接改代码。
- 数据备份应定期检查，不只依赖代码仓库。

## 12. 后续可优化方向

当前项目已能满足部署使用。后续如继续扩展，可考虑：

- 将 `fdm_dashboard.py` 拆分为多个模块，例如 `db.py`、`auth.py`、`reports.py`、`ui.py`
- 增加 `.env` 或 Windows 服务环境变量模板
- 增加数据库备份恢复脚本
- 增加管理员操作审计导出
- 增加版本号显示
- 增加服务器健康检查页面

