# SIMtrade 实训工作台

在一台 Windows 电脑上连接 1–3 个 SIMtrade 学生账号，查看出口商、进口商和工厂的余额与待办，按三方预算试算报价，并接续本机保存的合同。可以用浏览器界面、终端菜单或内置 Agent 操作。

![工作台登录界面，采用深红侧栏、黄色导航和白色表单](docs/workbench.png)

## 下载和启动

1. [下载 Windows x64 压缩包](https://github.com/smy19801388613-afk/simtrade-workbench/releases/latest/download/SIMtrade-workbench-Agent-Windows-x64.zip)，完整解压到可写目录，双击 `SIMtrade工作台.exe`。浏览器会自动打开，使用期间请保持终端窗口开启。
2. 连上校园网或学校 VPN，输入本次使用的学生账号和密码，点击「连接账号」。工作台已预填 `http://80.80.80.23/simtrade/`。使用其他学校的平台时，可在「学校平台地址」中修改。
3. 在「新建交易」选择商品、箱数、贸易术语和结算方式，核对各角色分工和预计利润后再确认办理。FOB、CFR、CIF 可分别搭配 T/T、D/P、D/A、L/C，也可以逐笔指定。

「英文目的港」留空时，工作台按每笔订单的进口商所在国自动选港。一批里同时有美国和印度尼西亚的进口商时，可以一起报价。若指定 `LOS ANGELES`，美国订单走洛杉矶，印度尼西亚订单仍按当地港口报价。

## Agent 助手

在界面右上角打开「Agent 助手」。可以输入“试算一笔床单，三箱，FOB＋D/P”，再说“改成 CIF＋L/C，其余不变”。也可以让它运行终端自检，或按合同号核对实际收支。正式开单和续办都会显示确认单。

常用操作由本地程序完成。需要开放式问答时，可在「连接设置」配置兼容 Chat Completions 的模型服务。合同和回执保存在本机的 `工作台数据` 目录。有关数据范围、旧版存档和模型设置，请参阅[使用说明](使用说明.md)。

## 从源码运行

需要 Python 3.10 或更高版本。程序使用 Python 标准库。在项目目录运行 `python desktop.py` 打开浏览器界面，或运行 `python workbench.py` 进入终端菜单。离线自检命令为 `python workbench.py --self-check`。
