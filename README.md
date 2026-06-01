# FDM 打印室任务执行电子看板

基于 Streamlit + PostgreSQL 的 FDM 打印室任务看板，用于任务下发、设备状态流转、上机/下机记录、用户权限管理和报表查看。

## 运行环境

- Python 3.10 或更高版本
- PostgreSQL
- Windows / Server 均可运行

## 安装依赖

```powershell
pip install -r requirements.txt
```

## PostgreSQL 配置

推荐使用环境变量提供数据库连接信息，不要把密码提交到 GitHub。

```powershell
$env:FDM_PGHOST="localhost"
$env:FDM_PGPORT="5432"
$env:FDM_PGDATABASE="fdm_dashboard"
$env:FDM_PGUSER="postgres"
$env:FDM_PGPASSWORD="你的数据库密码"
```

可选环境变量：

```powershell
$env:FDM_PGSSLMODE="prefer"
```

## 启动

```powershell
python -m streamlit run fdm_dashboard.py --server.address 0.0.0.0 --server.port 8502
```

也可以直接运行项目内的：

```powershell
.\start_8502.bat
```

## 访问

本机访问：

```text
http://127.0.0.1:8502
```

局域网其他电脑访问：

```text
http://服务器IP:8502
```

## 账号和权限

- 管理员可进入后台管理页面，维护用户、权限和报表。
- 普通用户根据账号权限显示对应功能。
- 技术员建议每人独立账号，上机、下机等操作会默认记录当前登录账号。

## 不要提交到 GitHub 的内容

以下内容已经在 `.gitignore` 中排除：

- PostgreSQL 密码文件
- `.env`
- 本地 SQLite 数据库
- 旧 JSON 数据
- 备份目录
- 数据库 dump / SQL 备份
- Python 缓存和临时文件

请保持 GitHub 仓库为私有仓库。

