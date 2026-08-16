# SQDCP Dashboard

从飞书多维表格读取指标配置与每日数据，渲染 SQDCP 运营看板。看板按安全（Safety）、质量（Quality）、交付（Delivery）、成本（Cost）、人员（People）五个维度展示每日红绿灯、月度均值、达标率、指标对比与异常清单。

## 主要功能

- 从飞书多维表格自动拉取指标配置和每日记录
- 可生成内置数据的静态 `dashboard.html`
- 可启动本地服务，通过 `/api/data` 提供最新数据
- 页面首次加载使用快照，随后轮询最新接口，接口失败时自动沿用缓存数据
- 月份下拉切换、KPI 卡片、SQDCP 字母日历、指标对比表、雷达图、未达标清单

## 项目结构

```text
sqdcp/
├── sqdcp.py              # 单一入口：serve / build / data
├── sqdcp.html            # 页面模板，包含渲染逻辑和数据注入点
├── dashboard.html        # build 生成的静态看板，可单独打开
└── TUTORIAL.md           # 详细教程
```

## 环境准备

需要 Python 3.9+ 和 `requests`。

```bash
cd SQDCP-DASHBOARD

python3 -m venv .venv
source .venv/bin/activate
pip install requests
```

## 配置飞书

程序通过环境变量读取飞书应用凭证：

```bash
export FEISHU_APP_ID="你的 App ID"
export FEISHU_APP_SECRET="你的 App Secret"
```

没有设置环境变量时会使用 `sqdcp.py` 内的示例凭证。正式使用请通过环境变量注入，不要将 App Secret 提交到公开仓库。

如需使用自己的多维表格，还需要修改 `sqdcp.py` 中的 `APP_TOKEN`，并确认：

- 飞书应用已开通多维表格读取权限
- 飞书应用已被添加为目标多维表格的协作者
- 多维表格中包含 `指标配置表` 和 `每日数据表` 两张表

## 快速开始

### 生成静态看板

```bash
python3 sqdcp.py build          # 包含全部年份
python3 sqdcp.py build 2026     # 只保留 2026 年数据
```

脚本会依次获取飞书访问凭证、读取指标配置、分页拉取每日记录，然后把最新 JSON 注入 `sqdcp.html` 并写入 `dashboard.html`。生成后可直接打开 `dashboard.html`。

### 启动本地服务

```bash
python3 sqdcp.py serve
```

也可以直接运行：

```bash
python3 sqdcp.py
```

浏览器打开：

```text
http://localhost:7700/
```

服务默认监听 `0.0.0.0:7700`，提供以下路由：

| 路由 | 说明 |
| --- | --- |
| `GET /` | 返回 `sqdcp.html`，同源页面 |
| `GET /api/data` | 返回 `{metrics, daily, months}` JSON |

`/api/data` 结果会缓存 5 分钟，避免页面刷新时频繁请求飞书接口。

## 命令行

```bash
python3 sqdcp.py --help
python3 sqdcp.py serve
python3 sqdcp.py build [year]
python3 sqdcp.py data [year]
```

- `serve`：启动本地服务，默认命令
- `build`：拉取数据并生成 `dashboard.html`
- `data`：拉取数据并输出 JSON，便于调试

## 数据源结构

程序依赖飞书多维表格中的两张表。

### 指标配置表

| 字段 | 说明 | 示例 |
| --- | --- | --- |
| `指标编号` | 指标唯一编码 | `D01` |
| `指标名称` | 指标中文名 | `出库准时率` |
| `SQDCP分类` | `Safety`、`Quality`、`Delivery`、`Cost`、`People` | `Delivery` |
| `目标值` | 目标值，百分比建议存小数 | `0.98` |
| `目标方向` | `越低越好`、`越高越好`、`不低于阈值` | `越高越好` |
| `单位` | `%`、`次`、`人`、`件`、`LPN`、`小时`、`元` | `%` |

### 每日数据表

| 字段 | 说明 | 示例 |
| --- | --- | --- |
| `指标` | 关联到指标配置表记录 | 指向 `D01` |
| `日期` | 日期字段 | `2026-02-08` |
| `数值` | 当日实际值，百分比存小数 | `0.996` |
| `维度` | 同一指标的不同口径 | `实际值` |

同一指标同一天可以有多行不同维度，例如 `D02` 同时存在 `准时出库量` 和 `未准时量`。

## 页面模块

- 顶部：月份下拉框、实时或缓存状态
- KPI 卡片：出库准时率、入库准时率、质量逃逸、技能合格率、出勤率、加班工时
- 字母日历：以 S、Q、D、C、P 字母轮廓展示当月每日红绿灯
- 指标对比表：按主维度计算本月均值并与目标值比较
- 雷达图：五个类别当月达标天数占比
- 未达标清单：展示未达标日期、指标、实际值和目标值

## 红绿灯规则

| 类别 | 规则 |
| --- | --- |
| Safety / Quality | 当天任一指标数值大于 0 即红，全部为 0 才绿 |
| Delivery | `D01` 出库准时率和 `D03` 入库准时率都达标才绿 |
| Cost / People | 当天所有带目标值的指标都达标才绿 |

指标是否达标由目标方向决定：

- `越低越好`：实际值 <= 目标值
- `越高越好`：实际值 >= 目标值
- `不低于阈值`：实际值 >= 目标值

## 切换数据接口

前端默认请求 `http://localhost:7700/api/data`，也可以用 `api` 参数覆盖：

```text
http://localhost:7700/?api=https://your-worker.example.com/api/data
```

任何返回 `{metrics, daily, months}` 的服务都可以作为数据源。

## 更多文档

详细的飞书配置、表结构说明、常见问题见 [TUTORIAL.md](TUTORIAL.md)。
