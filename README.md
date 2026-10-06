# Apple Store iPhone 18 系列库存监控

监控 Apple Store（中国大陆）直营店的 **门店取货** 库存，有货时通过 macOS 通知 / Bark / Server酱（微信）提醒。

## 快速开始

项目用 [uv](https://docs.astral.sh/uv/) 管理 Python 环境和依赖：

```bash
uv run monitor.py setup          # 🌟 交互式配置向导（支持地区/门店/型号多选、通知配置）
uv run monitor.py --list         # 查看当前配置下生效的监控型号和门店
uv run monitor.py --once         # 立即测试检查一次库存
uv run monitor.py                # 启动持续监控（Ctrl+C 退出）
```

## 交互式配置向导 (`setup`)

运行 `uv run monitor.py setup` 进入配置主页，立即查看地区、门店名单、型号、检查间隔、距离筛选和通知状态（不展示通知密钥）。可直接修改某一项，完成后返回主页检查并保存；也可选择“运行完整配置向导”按原有顺序配置。主页还支持测试当前通知设置，退出不保存时不会修改配置文件。

1. **地区管理（支持多选）**：
   - 支持通过 Apple 官网三级联动（省份 ➔ 城市 ➔ 区县）智能选择。
   - 支持添加多个不同的监控地区（例如同时监控上海黄浦区、深圳南山区、北京朝阳区），并自动去重合并附近门店。
   - 支持删除、清空或手动输入地区。
2. **门店筛选（多选）**：
   - 距离上限仅用于配置时筛选候选门店（0 表示不限距离）；选择全部或勾选门店后，都会把具体门店名单保存到 `stores`。
   - 运行监控时按保存的门店名单筛选，不再应用距离上限。重新运行向导可更新门店名单。
   - 自动拉取所选地区周边所有 Apple 直营店，按距离展示，支持方向键和空格直接多选勾选。
3. **型号筛选（多选）**：
   - 自动获取最新可售机型（含中文配色、容量、价格）。
   - 按官网购买流程依次选择机型、颜色和容量；颜色与容量支持多选，并自动组合为实际可售型号。
   - 可继续添加其他机型组合、移除已选组合，并保留已有型号选择。
   - 也支持“监控全部”或“自定义关键词匹配”。
4. **通知与轮询**：
   - 快捷开启/关闭 macOS 系统弹窗、语音播报，并配置 Bark (iOS 推送) 或 Server酱 (微信推送)。

## 命令行快速修改 (`config`)

如果不走交互式向导，也可以用 `config` 命令直接修改单项：

```bash
uv run monitor.py config                                   # 查看当前配置
uv run monitor.py config keys                              # 列出所有配置项及类型
uv run monitor.py config test-notify                       # 测试所有已启用的通知渠道
uv run monitor.py config test-notify bark                  # 只测试 Bark（也可指定 macos、say、serverchan）
uv run monitor.py config set location 北京 北京 朝阳区       # 文本值可以不加引号
uv run monitor.py config set interval_seconds 90
uv run monitor.py config set notify.bark_key xxxxxx
uv run monitor.py config set notify.say true               # 布尔值: true/false、on/off、开/关
uv run monitor.py config add models "Pro Max 256GB 银色" MJT74CH/A
uv run monitor.py config remove models MJT74CH/A
uv run monitor.py config clear models                      # 清空 = 监控全部
uv run monitor.py config reset                             # 恢复默认配置
```

修改时会检查配置项名称和值的类型（比如 `interval_seconds` 不能低于 20）。改完位置、型号或门店后，可以运行 `uv run monitor.py --list` 确认哪些会被监控（标 `*` 的那些）。

## 配置项

| 字段 | 说明 |
| --- | --- |
| `location` | 你的位置，格式 `省 市 区`，如 `北京 北京 朝阳区`、`广东 深圳 南山区` |
| `stores` | 只监控指定门店，填门店名或编号，如 `["浦东", "R390"]`；留空 = 附近所有门店 |
| `max_distance_km` | 只监控这个距离内的门店，`0` 表示不限 |
| `product_pages` | 官网购买页的路径，型号会从这些页面自动抓取。目前有 `iphone-18-pro`（含 Pro Max）和 `iphone-duo` |
| `models` | 型号过滤，留空 = 全部。每条规则里的关键词（空格分隔）需**全部**命中，多条规则之间是“或” |
| `interval_seconds` | 检查间隔，最少 20 秒，建议 ≥ 60，太频繁会被限流 |
| `notify.macos` | macOS 系统通知 + 提示音 |
| `notify.say` | 有货时语音播报 |
| `notify.bark_key` | [Bark](https://apps.apple.com/cn/app/bark/id1403753865) 的 key，推送到 iPhone |
| `notify.serverchan_key` | [Server酱](https://sct.ftqq.com/) 的 SendKey，推送到微信 |

`models` 示例：`["Pro Max 256GB", "18 Pro 512GB 银色", "MJT74CH/A"]`

关键词可以用英文名（`Black`、`Silver`）、中文名（`黑色`、`银色`）或型号编号（part number），不区分大小写。注意 `"18 Pro"` 也会匹配到 Pro Max；如果只要 Pro，可以直接写型号编号。

## 说明

- 有货后只提醒一次；卖光后如果再补货，会再次提醒。
- 只能查 **门店取货**，查不了快递发货时间。
- 用的是 Apple 官网的非公开接口，以后可能会变。连续失败时会自动退避重试。
- 只查询库存，不登录、也不用提供 Apple ID。
