# 给接手本文件夹的 AI

这是一个 A 股 AI 自动决策系统（波段 1~4 周）。你通常是被定时器或异动唤醒来**做交易决策**的。

1. 先读 [AGENT_PROMPT.md](AGENT_PROMPT.md)（总纲：命令、机制、交易原则、决策格式、硬性约束）
2. 再读唤醒你的那个场景提示词，在 [prompts/](prompts/README.md)：
   - 每 10 分钟 → `prompts/01_定时决策.md`
   - 异动唤醒 → `prompts/02_异动调研.md`
   - 盘前 → `prompts/03_盘前准备.md`，收盘 → `prompts/04_收盘复盘.md`，周末 → `prompts/05_策略研究.md`
   - 要买股票 → 先过 `prompts/06_投研委员会.md`（系统强制）
   - 维护/改代码 → `prompts/07_系统维护升级.md`（安全规则、git、测试、CHANGELOG、待办 docs/BACKLOG.md）
3. 如果没人告诉你是哪个场景：交易时段按 01，收盘后按 04，周末按 05；被要求"维护/升级/修bug"按 07。

技术说明：
- 零依赖，Python 3.9+ 标准库。`python3 agent_cli.py --help` 看全部命令。
- 网页 + 实时监控（`python3 app.py`）在用户电脑上常驻；你不需要启动它，`agent_cli.py` 直接读写 `data/stock.db`。
- 改代码（仅当用户要求）：`core/` 核心，`strategies/` 指标策略，`web/index.html` 网页，`prompts/` 提示词；
  改完跑 `python3 -m unittest discover tests`。
- 不要改 `data/config.json` 的 `ai.accounts`（实盘授权）、`risk`、`committee`，除非用户明确要求。不要删除文件。
