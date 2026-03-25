# OKX 合约 AI 自动执行脚本（Python）

这个脚本用于在欧易（OKX）上执行你定义的交易决策。  
默认是**模拟盘 + DRY_RUN**，不会真实下单，先验证逻辑再放开。

## 功能

- 获取合约行情（`instId`）
- 调用 OKX **公开**接口拉取历史 K 线：`GET /api/v5/market/candles`，根数不足时用 `GET /api/v5/market/history-candles` 向前补
- 本地计算**指标摘要**（SMA5/10/20/50/100/200、RSI14、MACD、近 20/50 根高低点等）并传给 AI
- **多周期 `strategy_inputs`**：日线（30 日均线/斜率/趋势分类）、4H 与主周期指标、**1H 汇总的 48h 高低**与回撤比例、**账户 USDT 权益**与**当日盈亏%**（见 `equity_day_state.json`）、持仓名义价值/权益占比（资金规则参考）
- 读取当前持仓
- 支持两种决策：
  - 内置规则（默认）
  - AI 决策（可选）
- 风控覆盖：止盈 / 止损优先于 AI
- 市价开平仓

## 1) 安装依赖

```bash
pip install -r requirements.txt
```

## 2) API 放在哪里？

API **不需要写进 `bot.py` 代码**，统一放在项目根目录的 `.env` 文件里。  
脚本会通过 `load_dotenv()` 自动读取。

先复制模板：

```bash
copy .env.example .env
```

然后编辑 `.env`，把下面三项改成你的真实值：

```env
OKX_API_KEY=你的欧易API_KEY
OKX_API_SECRET=你的欧易API_SECRET
OKX_PASSPHRASE=你创建API时设置的密码
```

> 位置说明：`.env` 和 `bot.py` 在同一层目录（当前就是 `e:\holiday`）。

## 3) 其他配置项（也在 `.env`）

关键开关：

- `OKX_SIMULATED=true`：模拟盘（建议先一直开）
- `DRY_RUN=true`：只打印动作，不下单（建议先开）
- `USE_AI=false`：先关闭 AI，先验证下单链路
- `INST_ID=BTC-USDT-SWAP`：交易标的
- `LEVER=5`：杠杆倍数
- `ORDER_SIZE=0.01`：每次下单数量
- `CANDLE_BAR` / `CANDLE_LIMIT`：K 线周期与根数（如 `15m`、`200`）
- `HTTP_PROXY` / `HTTPS_PROXY`：本机 **DNS 解析不了 `www.okx.com`** 时，填本地代理地址，例如 `http://127.0.0.1:7890`（与 Clash 等软件的端口一致）

### 网络自检（推荐）

```bash
python check_network.py
```

若第 1 步域名解析失败，先改 DNS、或打开代理并在 `.env` 填写 `HTTPS_PROXY`，直到第 2 步返回 HTTP 200。

## 4) 运行

```bash
python bot.py
```

## 5) 切换到 AI 决策

在 `.env` 中：

- `USE_AI=true`
- `OPENAI_API_KEY=...`
- `AI_MODEL=gpt-4o-mini`（可改）
- `AI_USER_RULE=你的交易偏好`

## 6) 上实盘前务必检查

1. 先用模拟盘跑一段时间（至少几天）
2. `DRY_RUN=false` 前确认仓位、杠杆、下单量都正确
3. 将 `ORDER_SIZE` 设为非常小
4. 观察日志，确认止盈止损逻辑符合预期

## 常见问题

### Q1：我能把 API 直接写在 `bot.py` 里吗？
可以，但不建议。放在 `.env` 更安全，也更方便切换账号。

### Q2：为什么填了 API 还是报错 Missing env vars？
通常是以下原因：

- `.env` 文件名写错（例如 `.env.txt`）
- `.env` 不在项目根目录（应与 `bot.py` 同级）
- 变量名拼写不一致（必须是 `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_PASSPHRASE`）

### Q3：报错 Failed to resolve 'www.okx.com' / getaddrinfo failed？
说明 **DNS 无法解析欧易域名**。请运行 `python check_network.py` 排查；多数情况下需要在 `.env` 配置 `HTTPS_PROXY`（本机已开代理时），或更换可用 DNS / 网络环境。

### Q4：AI 报错 `unsupported_country_region_territory` / 403
这是 **OpenAI 官方接口** 根据 IP 限制地区，不是欧易。处理方式：

1. 确保 `.env` 里 **HTTPS_PROXY** 与能翻墙的端口一致（脚本会让 AI 请求走同一代理；若仍 403，请换代理节点）。  
2. 若使用 **DeepSeek** 等兼容接口：设置 `OPENAI_BASE_URL`（见 `.env.example`）和对应 `AI_MODEL`、密钥。  
3. 暂时不用 AI：设 `USE_AI=false`。

### Q5：`SSL: UNEXPECTED_EOF_WHILE_READING` / 经代理连 OKX 失败
多为 **HTTP 代理节点不稳定**，在 HTTPS（CONNECT）阶段被掐断。可：**换代理节点**、在 `.env` 增大 `OKX_TIMEOUT` 与 `OKX_RETRIES`、若本机直连欧易可用则**暂时去掉代理**再试。

### Q6：`50113` / `Invalid Sign`
表示 **签名与欧易服务器计算不一致**。请核对：`.env` 里 **API Key / Secret / Passphrase** 是否与后台一致（**Secret 勿多空格、勿换行**；**Passphrase 区分大小写**）；**实盘 Key 与 `OKX_SIMULATED` 要匹配**。更新 `bot.py` 后已自动去除 BOM、并对 JSON/查询串做规范排序。

### Q7：401 Unauthorized（账户/持仓等需鉴权的接口）
表示 **API 密钥校验失败**，与行情接口是否通无关。请逐项检查：

1. **Key / Secret / Passphrase** 是否与欧易后台一致（Passphrase 区分大小写，创建 API 时自设）。  
2. **模拟盘**：`OKX_SIMULATED=true` 时，应使用在欧易 **模拟交易** 里创建的 API（或带模拟交易权限的密钥），不要用仅实盘权限的密钥硬配模拟头。  
3. **IP 白名单**：若 API 限制了 IP，走代理后出口 IP 会变；需关闭白名单、或把当前出口 IP 加入白名单。

### 安全提醒
**不要把真实 API Key 写进 `.env.example` 或发给他人。** 若密钥曾泄露，请立即在欧易后台 **删除并重建 API**。

## 重要风险提示

自动化合约交易有高风险，可能快速亏损。  
请只使用你可承受损失的资金，并先在模拟盘验证策略。
