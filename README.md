# Telegram 代币价差监控

只监听指定 Telegram 群里配置的一位成员，按数字用户 ID 匹配。读取 Bitget（BG）、Binance（BN）、OKX、Aster、Gate 五家的 USDT 本位永续盘口，输出价差方向和 Astro 卡片预览。

> **预览与真实建卡**
>
> `listen-bot` 只生成预览。生产入口会创建已启用的真实 FF 卡，允许 Astro 按阈值开仓和平仓。每张卡限额 1,000 USDT，杠杆 2 倍，开仓阈值 4%，清仓阈值 0.5%。
>
> 安装后，可以把本地项目目录交给自己使用的 AI 编程助手，按需修改设置，例如 Telegram 群与发送者、价差阈值或测试卡参数。修改后可先运行下方的离线演示核对输出。

## 先跑离线演示

在仓库根目录运行，Python 3.11 或更新版本即可，无需安装依赖、连接网络或登录 Telegram。

```bash
python3 -m telegram_spread_monitor demo
```

演示用同一条消息触发 PEPE 和 BTC。币安的 PEPE 买一以 1,000 枚为报价单位，`101.5 ÷ 1000 = 0.1015`。Bitget 卖一是 `0.1000`，所以最佳方向为 BG 多 / BN 空，开仓毛价差为 `(0.1015 - 0.1000) / 0.1000 × 100 = 1.50%`。预计看到以下日志，省略了时间及详细盘口。

```text
INFO tokens_identified message_id=42 tokens=PEPE,BTC
WARNING quote_skipped token=PEPE exchange=Aster reason=timeout
WARNING quote_skipped token=PEPE exchange=Gate reason=stale_quote age_ms=9000 ...
INFO spread_ranked token=PEPE BG 多 / BN 空 +1.5000% | ...
INFO card_preview token=PEPE direction=BG 多 / BN 空 gross_pct=1.50% ...
INFO token_skipped token=BTC reason=insufficient_quotes valid=1
INFO duplicate_message message_id=42
```

`card_preview` 后面的 JSON 包含标准代币代码、两腿的场所和原生合约代码、原始及标准化买卖一、报价时间、毛价差和请求状态 `enabled`。备注显示「开仓毛价差，未扣手续费和滑点」。

## 安装和查询实时报价

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m telegram_spread_monitor quotes 'BTC, $PEPE'
```

`quotes` 使用真实公开盘口，无需 Telegram 或交易所凭据。五家请求并行发出，每个代币使用独立的盘口批次。毛价差必须严格高于 `MIN_SPREAD_PCT` 才会输出预览，模板阈值为 **0.10%**。例如 `MIN_SPREAD_PCT=4` 表示实时毛价差必须大于 4%。结果未超过阈值时，可在 `spread_ranked` 核对双向排序。没有有效路径时会记录原因。

Telegram 暂不能登录时，可以把消息原文直接传给 `quotes`，例如 `python -m telegram_spread_monitor quotes '🔔 价差通知【SOPH】 Binance-F → Bybit-F'`。程序忽略其中的交易所标签，用当前盘口重新计算。此方式可测试内容识别，不验证群消息接收和发送者过滤。

## 用 Bot API 监听群消息

此方式使用专用机器人。先在 BotFather 创建机器人，把 token 填到本机 `.env` 的 `TG_BOT_TOKEN`。关闭群隐私模式后，由有权限的人把机器人加入目标群，使它能收到群里的普通消息。 [Telegram 官方说明](https://core.telegram.org/bots/features#privacy-mode)

在仓库根目录进入已安装依赖的 Python 环境后运行：

```bash
python -m telegram_spread_monitor bot-discover --group '你的群名' --sender '目标成员显示名'
```

让 目标成员 发一条新消息，终端应出现 `bot_discovery_received`。把打印的群 ID 与他的用户 ID 分别填入 `TG_CHAT_ID`、`TG_SENDER_ID`。旧配置若同时有两个发送者字段，新版优先用 `TG_TICKER_SENDER_ID`。可以把该值迁入 `TG_SENDER_ID` 后删除旧字段。

```bash
python -m telegram_spread_monitor listen-bot
```

`bot_listener_started` 表示机器人凭据验证通过，`bot_message_received` 才表示收到了配置的群和发送者的消息。被接受的消息走和 `quotes` 一样的盘口计算及阈值。这里仍然只输出预览。不要同时运行 `bot-discover` 和 `listen-bot`。

## 用个人账号首次获取 Telegram ID

1. 用已经加入目标群的 Telegram 个人账号在 [Telegram API 开发页面](https://my.telegram.org/apps) 创建应用，取得 `api_id` 和 `api_hash`。程序使用本地 Telethon 会话。首次运行时在终端输入手机号和验证码，若开启二步验证，还会提示输入密码。
2. 复制本地配置，填入 `TG_API_ID` 和 `TG_API_HASH`。

   ```bash
   cp .env.example .env
   chmod 600 .env
   python -m telegram_spread_monitor discover --group '你的群名' --sender '目标成员显示名'
   ```

3. 程序按群名定位群，再检查最近 200 条消息的实际发送者资料。若找不到目标，请让目标成员在群里发一句话后重试。若群重名，把 `--group` 换成 `--group-id=-100...`，使用输出的群 ID 重试。若成员重名，通过 Telegram 资料和命令输出的用户名核对后选择。匿名管理员或频道身份发出的消息没有对应的个人发送者 ID，请让目标用个人身份发一条。
4. 把输出的两个数字填回 `.env`。保留群 ID 前面的负号。下面是示例 ID。

   ```dotenv
   TG_CHAT_ID=-1001234567890
   TG_SENDER_ID=123456789
   ```

会话默认存放在 `.local/telegram.session`，与 `.env` 一样只保存在本机。

## 运行监听

```bash
python -m telegram_spread_monitor listen
```

启动后日志显示 `listener_started mode=preview`。请用指定账号在指定群发送 `BTC、$PEPE、ETH/USDT` 检查 `tokens_identified` 和盘口日志。运行时按数字群 ID 和个人 ID 匹配，显示名变动无需改配置。程序处理运行期间收到的消息，并忽略超过 `MAX_MESSAGE_AGE_SECONDS` 的事件。按 Ctrl+C 停止。

默认使用 `.env`。可用 `python -m telegram_spread_monitor --env /绝对路径/.env listen` 指定其他配置。环境变量优先于文件。状态库默认是 `.local/monitor.sqlite3`。用同一状态库重启，已经处理的消息会跳过。即使第一次处理在一条多代币消息中途中断，已写入的预览也不会再次生成。预览按「群 ID + 消息 ID + 标准代币」唯一存储。可用 `sqlite3` 读取记录。

```bash
sqlite3 .local/monitor.sqlite3 \
  'SELECT message_id, token, payload FROM previews ORDER BY message_id DESC LIMIT 10;'
```

## 识别和报价口径

- 支持 `BTC`、`btc`、`$BTC`、`BTCUSDT`、`BTC/USDT`。一条消息可写多枚代币，普通正文里的小写代码需带 `$` 或标出 USDT 交易对。只接受能在至少一家的有效合约列表中核实的代码，并跳过链接、邮箱和用户名。
- `1000PEPE`、`1000SATS` 等已核对的报价单位按 `core.py` 中的 `BUNDLES` 显式转换。比如 `1000CHEEMS` 的价格除以 1,000，`1MCHEEMS` 除以 1,000,000。未知的数字前缀按原代码保留。Gate 的 `quanto_multiplier` 和 OKX 的 `ctVal` 表示每张合约的数量，盘口价格不按这两个字段缩放。
- 合约列表默认缓存 300 秒。盘口每次重新查询。交易所未上线、合约暂停、超时、响应异常、盘口为空或过期都会在日志里说明，其他有效报价继续参与。Gate 使用订单簿最后变化的时间 `update`，其余使用盘口接口返回的报价时间。
- 默认报价最长 3 秒、两腿报价最多相差 2 秒，本机时间需要同步。每条路线用多腿卖一和空腿买一按用户给定的公式比较。仅对两家不同交易所的有效报价计算双向价差，按未四舍五入的结果排序。至少有两家有效报价且第一名为正、严格高于 `MIN_SPREAD_PCT`，该代币才生成一张预览。这个报价只覆盖盘口第一档。
- 盘口来源：[Bitget](https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market)、[Binance USDⓈ-M](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)、[OKX V5](https://www.okx.com/docs-v5/en/)、[Aster Futures V3](https://asterdex.github.io/aster-api-website/futures-v3/market-data/)、[Gate API V4](https://www.gate.com/docs/developers/apiv4/en/futures/)。

**已知限制。** 目前程序不会自动识别 Pre-IPO 合约的股数基准或拆分比例。同一标的在不同所的报价单位可能不同，例如 OKX 对 OPENAI 的 10 倍拆分。这会让直接比较得出的价差失真，相关预览需先按合约规格复核。[OKX 拆分公告](https://www.okx.com/en-sg/help/okx-to-execute-rebase-on-openaiusdt-and-anthropicusdt-pre-market-perpetual)

## 真实 Astro 建卡

`astro_live` 是单次只读复核入口。它同时读取五家永续盘口和交易所指数价，按未四舍五入的毛价差排序，筛选严格大于 4% 且空腿指数价较高的 FF 方向。常规复核支持 BTC、ETH，以及 Binance 和 Gate 上的 Quant 代币 QNT。其他场所的 QNT 可能代表不同资产。OPENAI 的同名合约也使用不同计价单位，常规自动建卡不包含 OPENAI。

```bash
python -m telegram_spread_monitor.astro_live --token BTC
```

### 在已有 Astro 套利面板的服务器上运行

生产入口依赖现有面板提供的 Python 模块 `app.astro_sdk`、`app.astro_spread_scanner` 及其依赖。先把面板 backend 目录加入 `PYTHONPATH`，并使用能导入这两个模块的 Python 环境。生产入口连接同机的 `https://127.0.0.1:8443`。

通过服务管理器向进程注入 `TG_BOT_TOKEN`、`TG_CHAT_ID`、`TG_SENDER_ID` 和 `STATE_DB_PATH`（可写且持久的 SQLite 文件路径），以及面板 SDK 的 `ASTRO_SDK_ADMIN_PREFIX`、`ASTRO_SDK_API_KEY` 和相应的 TLS 配置。生产入口直接读取环境变量。同一个 Telegram 机器人只运行一个监听进程。

```bash
python -m telegram_spread_monitor.production
```

启动时会读取真实卡片列表，成功后记录 `astro_connected`。收到指定成员的币名后，先核对普通市场盘口和指数，再用多腿 1,000 USDT 和空腿相同数量复核深度。可成交毛价差须严格超过 4%，且空腿指数价高于多腿。每条腿独立选通道。Bitget（BG）使用普通通道，Binance、OKX、Gate 和 Aster 各自使用 GC。例如，Binance 多 / Bitget 空保存为 `gc-binance / bitget`。

新卡默认启用。Astro 的开仓和清仓阈值分别写入 `openPosition=0.04`、`closePosition=0.005`。程序回读卡号、两腿、启用状态、开平仓开关、阈值、额度和杠杆，全部匹配才记录 `saved_enabled`。同标的同方向已有卡时跳过。提交回执不明时保留本地防重记录。Astro 官方说明新增卡片会导致 Core 重启。[Astro SDK 文档](https://github.com/astro-btc/Astro/blob/main/SDK-API.md#62-新增-pair)

## 验收命令

```bash
python -m unittest discover -s tests -v
python -m telegram_spread_monitor demo
python -m telegram_spread_monitor quotes 'BTC, $PEPE'
```
