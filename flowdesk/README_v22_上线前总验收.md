# FlowDesk v2.2 — 上线前工程总验收（研发阶段）

## 为什么做这一步

用户希望尽量先完善系统，再开始 60 个未来交易日的正式试验。因此这次 **不启动试验、不自动下单、不购买数据**，只将既有 v2.1 研究工程的上线前审查集中到一个独立命令，给出可复核的失败原因。

**不能靠写代码预先证明一个未来收益规律。** 软件测试和历史回测能证明某些实现行为，却不能证明持续盈利。第一轮未来样本之前，必须固定版本，之后不得边看实验结果边改冻结策略。

## 新增文件

- `prelaunch_audit.py`：只读代码 AST 扫描、必备文件检查、原观察池检查、当前快照哈希/有效期检查、全部归档完整性检查、运行目录登记检查、冻结试验状态检查、固定 SDK 版本与本机 OpenD TCP 探测。
- `Preflight.command`：Mac Terminal 一键运行上面的统一验收，并运行全部回归测试。
- `tests/test_v22_prelaunch.py`：20 项新增失败场景和反证测试。

## Mac 操作

先备份原目录；没有正式冻结试验时才能升级。下载安全补丁并解压后：

```bash
bash /下载后的补丁目录/安装预检.command /Users/quyang/Desktop/FlowDesk
```

升级完成，在 Mac Terminal 运行：

```bash
cd /Users/quyang/Desktop/FlowDesk
bash Preflight.command
```

输出报告：`research/prelaunch/preflight_report.json`。如需完整重验已经录制的原始文件：

```bash
bash Preflight.command --deep-audit
```

大体积连续录制可能需要额外时间读取。此入口不是连接模拟券商下单测试；`--check-opend` 只确认本机 TCP 端口是否有服务响应，不验证 Moomoo 登录与行情授权。`Preflight.command` 默认检查端口，也不会主动修改或下单。

## 怎样理解状态

- `PASS_OFFLINE_TESTS_ONLY`：软件源码和本地回归测试通过，不是盈利证明。
- `NO_GO_OFFLINE_ENGINEERING`：基础结构、配置、自动化测试存在问题，先修复。
- `NO_GO_LOCAL_SDK_OR_OPEN_D`：离线软件测试通过，但当前运行环境缺少已核对的 Moomoo SDK 或 OpenD 端口；不是策略失败。
- `NO_GO_SCAN_COVERAGE`：虽然有扫描报告，但实际覆盖不符合研究门槛。
- `NO_GO_SCAN_TOO_OLD`：本地最新扫描已过期，不能当作实时证据。
- `NO_GO_RUN_INVENTORY_MISMATCH`：有录制目录缺失或未登记。
- `UNVERIFIED_RAW_SEGMENTS_DEEP_AUDIT_REQUIRED`：只检查了登记信息；未审计全部录制字节。
- `UNREGISTERED`：**尚未开启**事前冻结试验，与软件坏掉无关。
- `NO_GO_NO_VERIFIED_ALPHA_OR_BROKER_EXECUTION`：**当前不允许实盘资金**；即使所有软件测试通过，此状态也不会自动变为 GO。

## 本次验收边界

本地 363 项自动化测试（原版 343、新增 20）只说明已测试的代码路径正确；Mac OpenD 真实行情权限、全交易日数据和真实成交还未在本环境验收。该代码没有自动提交股票订单的功能。

软件上的目标是：`PASS_OFFLINE_TESTS_ONLY`、SDK 固定版本、无订单发送接口、脚本可启动、录制/扫描档案可验证、配置可重现、失败行为不伪造成交易利润。达到之后才应考虑**开始采集验证所需的市场数据**；真正策略是否赚钱需要未触碰的未来样本及实盘执行校准。

完整工程放在独立目录；若历史冻结试验已经存在，绝不能向该目录写入新研究 `.py` 文件，因为冻结的代码指纹会变化。
