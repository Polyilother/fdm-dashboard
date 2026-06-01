# FDM 打印室任务执行电子看板

## 项目简介

本项目用于 FDM 打印室的任务下发、设备状态管理、上机/下机记录、用户权限管理、设备维保维修日志和生产效率报表统计。

系统基于 Streamlit + PostgreSQL 构建，支持局域网多用户访问，适用于测试工程师、白夜班技术员和管理员协同使用。

## 功能模块

- 测试工程师任务下发
- Gcode / 3MF 文件上传与切片理论耗时读取
- 技术员上机、下机、暂停、恢复、提前结束任务
- 设备实时运行状态看板
- 设备快速筛选
- 设备状态管理
- 用户账号与权限管理
- 设备维保与故障维修历史日志
- 任务流转台账
- 设备流转效率与闲置盲区分析
- 生产测试效率与流转偏差分析

## 技术栈

- Python
- Streamlit
- PostgreSQL
- pandas
- psycopg2

## 运行环境

建议环境：

- Windows Server / Windows 10+
- Python 3.10 或更高版本
- PostgreSQL
- 局域网固定 IP

## 安装依赖

```powershell
pip install -r requirements.txt