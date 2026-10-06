"""
交互式配置向导 (wizard.py)
使用 questionary 提供友好的终端交互界面，支持：
1. 地区多选（通过 Apple 官网地址接口联动查询省/市/区，或手动输入，支持添加多个地区）
2. 门店多选（基于所选地区在线获取附近直营店列表供勾选）
3. 型号多选（在线抓取官网当前所有可售型号，按机型/颜色/容量清晰展示，支持搜索过滤和多选）
4. 监控间隔与通知渠道（macOS 提示、语音播报、Bark、Server酱）
"""

import copy
import re
from pathlib import Path
from cli_prompts import checkbox, confirm, select, text
from questionary import Choice, Separator

from monitor import MIN_INTERVAL, Monitor, load_config, save_config


def choose_location_interactive(mon):
    """
    通过 Apple 官网地址接口交互式选择一个地区（省 -> 市 -> 区）。
    """
    print("\n正在获取省份列表...")
    try:
        body = mon.address_lookup()
        state_data = body.get("state", {}).get("data", [])
        if not state_data:
            raise ValueError("未能获取省份数据")
    except Exception as e:
        print(f"获取省份列表失败 ({e})，切换为手动输入。")
        val = text("请输入地区 (格式: 省 市 区，例如 '上海 上海 黄浦区'):").ask()
        return val.strip() if val else None

    state_choices = [Choice(title=s["label"], value=s["value"]) for s in state_data]
    selected_state = select(
        "请选择省份/直辖市 (可输入地区名称过滤):",
        choices=state_choices,
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()

    if not selected_state:
        return None

    # 获取城市
    print("正在获取城市列表...")
    try:
        body = mon.address_lookup(state=selected_state)
    except Exception as e:
        print(f"获取城市失败: {e}")
        return selected_state

    city_obj = body.get("city")
    if isinstance(city_obj, str):
        # 直辖市直接返回固定城市名，比如 "北京"
        selected_city = city_obj
    elif isinstance(city_obj, dict):
        city_data = city_obj.get("data", [])
        city_choices = [Choice(title=c["label"], value=c["value"]) for c in city_data]
        selected_city = select(
            f"请选择【{selected_state}】下的城市:",
            choices=city_choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).ask()
        if not selected_city:
            return None
    else:
        selected_city = selected_state

    # 获取区县
    print("正在获取区县列表...")
    try:
        body = mon.address_lookup(state=selected_state, city=selected_city)
        dist_data = body.get("district", {}).get("data", [])
    except Exception:
        dist_data = []

    if dist_data:
        dist_choices = [Choice(title=d["label"], value=d["value"]) for d in dist_data]
        selected_district = select(
            f"请选择【{selected_city}】下的区县:",
            choices=dist_choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).ask()
        if selected_district:
            return f"{selected_state} {selected_city} {selected_district}"

    return f"{selected_state} {selected_city}"


def configure_locations(mon, current_locations):
    """
    配置监控地区（支持添加多个地区、多选管理）
    """
    locations = list(current_locations)
    while True:
        print("\n当前已配置的监控地区：")
        if not locations:
            print("  （暂无配置）")
        else:
            for idx, loc in enumerate(locations, 1):
                print(f"  {idx}. {loc}")

        choices = [
            Choice("添加新地区（通过省市区联动选择）", "add_interactive"),
            Choice("添加新地区（手动输入）", "add_manual"),
        ]
        if locations:
            choices.extend([
                Choice("删除已有地区", "remove"),
                Choice("清空所有地区", "clear"),
            ])
        choices.extend([
            Separator(),
            Choice("完成地区配置，下一步", "done"),
        ])
        action = select(
            "地区配置操作:",
            choices=choices,
        ).ask()

        if action is None or action == "done":
            if not locations:
                print("请至少保留一个监控地区！")
                continue
            break
        elif action == "add_interactive":
            new_loc = choose_location_interactive(mon)
            if new_loc and new_loc not in locations:
                locations.append(new_loc)
                print(f"已添加: {new_loc}")
        elif action == "add_manual":
            manual = text(
                "请输入位置 (格式: 省 市 区，如 '广东 深圳 南山区'):"
            ).ask()
            if manual and manual.strip():
                clean = manual.strip()
                if clean not in locations:
                    locations.append(clean)
                    print(f"已添加: {clean}")
        elif action == "remove":
            to_remove = checkbox(
                "请勾选要删除的地区 (空格选择，回车确认):",
                choices=[Choice(l, l) for l in locations],
            ).ask()
            if to_remove:
                locations = [l for l in locations if l not in to_remove]
                print(f"已移除: {', '.join(to_remove)}")
        elif action == "clear":
            if confirm("确定要清空所有地区吗？").ask():
                locations = []

    return locations


def configure_models(mon, current_models):
    """
    配置关注型号（多选）
    """
    print("\n正在获取最新商品列表与中文描述...")
    if not mon.models:
        mon.discover_models(quiet=True)
    try:
        # 查询一次获取中文名称，以便更清晰展示
        stores = mon.query_all(list(mon.models))
        mon._fill_titles(stores)
    except Exception as e:
        print(f"获取型号中文详情提示失败 ({e})，将使用原始英文名称。")

    print("\n【型号筛选设置】")
    mode = select(
        "如何筛选监控型号？",
        choices=[
            Choice("监控全部型号 (不进行过滤)", "all"),
            Choice("按机型 → 颜色 → 容量选择组合", "combinations"),
            Choice("使用自定义关键词匹配 (高级模式)", "keywords"),
        ],
    ).ask()

    if mode == "all":
        return []

    if mode == "keywords":
        print("\n提示：多个关键词用空格隔开（需全部满足），多个规则用逗号隔开。")
        print("例如：'Pro Max 256GB 银色, 18 Pro 黑色'")
        kw_input = text("请输入关键词规则:").ask()
        if not kw_input or not kw_input.strip():
            return []
        return [k.strip() for k in kw_input.split(",") if k.strip()]

    if mode != "combinations":
        return current_models
    return choose_model_combinations(mon, current_models)


def model_dimensions(model):
    """从官网名称提取机型、容量和配色，优先使用接口补全的中文名称。"""
    pattern = r"^(.*?)\s+(\d+(?:\.\d+)?\s*(?:GB|TB))\s+(.+)$"
    for text in (model.get("title"), model.get("name")):
        if not text:
            continue
        match = re.match(pattern, text.replace("\xa0", " ").strip(), re.I)
        if match:
            device, capacity, color = match.groups()
            return device.strip(), color.strip(), capacity.replace(" ", "").upper()
    return None


def choose_model_combinations(mon, current_models):
    catalog = {}
    for part, model in mon.models.items():
        dimensions = model_dimensions(model)
        if dimensions:
            catalog[part] = dimensions
    if not catalog:
        print("无法解析商品的机型、颜色和容量，保留原有型号配置。可使用自定义关键词匹配。")
        return current_models

    selected = {
        part for part in mon.models
        if any(all(token.lower() in mon._model_text(part) for token in rule.split())
               for rule in current_models)
    }
    if len(catalog) < len(mon.models):
        print("部分商品名称无法解析，组合选择仅显示已识别商品。")

    while True:
        print("\n当前选择的监控组合：")
        for part in sorted(selected):
            print(f"  {mon.label(part)}")
        if not selected:
            print("  （暂无选择）")
        actions = [Choice("添加机型、颜色和容量组合", "add")]
        if selected:
            actions.append(Choice("移除已选组合", "remove"))
        actions.append(Choice("完成型号选择", "done"))
        action = select("型号配置操作:", choices=actions).ask()
        if action is None:
            return current_models
        if action == "done":
            if selected:
                return sorted(selected)
            print("请至少选择一个组合；如需监控全部，请在型号筛选方式中选择监控全部。")
            continue
        if action == "remove":
            removed = checkbox(
                "请选择要移除的组合:",
                choices=[Choice(mon.label(part), part) for part in sorted(selected)],
            ).ask()
            selected.difference_update(removed or [])
            continue

        device = select(
            "请选择机型:", choices=sorted({d[0] for d in catalog.values()}),
        ).ask()
        if device is None:
            continue
        device_parts = {p: d for p, d in catalog.items() if d[0] == device}
        colors = checkbox(
            f"请选择【{device}】的颜色（可多选）:",
            choices=sorted({d[1] for d in device_parts.values()}),
        ).ask()
        if not colors:
            continue
        capacities = {d[2] for d in device_parts.values() if d[1] in colors}
        capacities = sorted(capacities, key=lambda c: float(c[:-2]) * (1024 if c.endswith("TB") else 1))
        chosen_capacities = checkbox(
            "请选择容量（可多选，将添加所选颜色与容量的可售组合）:",
            choices=capacities,
        ).ask()
        if not chosen_capacities:
            continue
        added = {p for p, d in device_parts.items() if d[1] in colors and d[2] in chosen_capacities}
        count = len(added - selected)
        selected.update(added)
        print(f"已添加 {count} 个组合，共选择 {len(selected)} 个组合。")


def configure_stores(mon, locations, current_stores):
    """
    根据配置的地区，拉取所有周边门店并支持多选
    """
    print(f"\n正在查询附近门店列表...")
    try:
        # 取货接口必须携带商品编号，先加载商品再查询门店。
        if not mon.models:
            mon.discover_models(quiet=True)
        mon.cfg["locations"] = list(locations)
        stores = mon.query_all(list(mon.models)[:2])  # 仅需查 1-2 个型号探测门店
    except Exception as e:
        print(f"获取门店失败 ({e})，跳过门店过滤设置。")
        return current_stores

    if not stores:
        print("Apple 取货接口未返回附近门店，请检查所选地区或稍后重试。保留原有门店筛选。")
        return current_stores

    print(f"\n查询到 {len(stores)} 家附近直营店：")
    max_km = mon.cfg.get("max_distance_km") or 0
    print(f"配置时的距离筛选：{str(max_km) + ' 公里以内' if max_km else '不限距离'}（运行监控时按保存的门店名单，不再按距离筛选）")
    eligible_stores = []
    for idx, store in enumerate(stores, 1):
        distance = store.get("storedistance")
        distance_text = f"{distance} km" if distance is not None and distance != "" else "未知"
        excluded = bool(max_km and float(distance or 0) > max_km)
        note = "（超出配置筛选范围）" if excluded else ""
        print(f"  {idx}. {store['storeName'].strip()} ({store.get('city') or '城市未知'}) - 距离: {distance_text}{note}")
        if not excluded:
            eligible_stores.append(store)

    if not eligible_stores:
        print("没有符合距离限制的门店。请增大距离上限或设为 0（不限距离），原有门店筛选将保留。")
        return current_stores
    stores = eligible_stores

    store_mode = select(
        "门店筛选方式:",
        choices=[
            Choice(f"监控符合距离限制的所有门店 ({len(stores)} 家直营店)", "all"),
            Choice("勾选指定的 Apple Store 门店", "select"),
        ],
    ).ask()

    if store_mode == "all":
        return [store["storeName"].strip() for store in stores]

    if store_mode is None:
        return current_stores

    choices = []
    for st in stores:
        name = st["storeName"].strip()
        city = st.get("city")
        dist = st.get("storedistance")
        title = f"{name} ({city}) - 距离: {dist}km [编号: {st['storeNumber']}]"
        checked = name in current_stores or st["storeNumber"] in current_stores
        choices.append(Choice(title=title, value=name, checked=checked))

    while True:
        selected = checkbox(
            "请勾选想要监控的门店 (空格勾选，回车确认):",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).ask()
        if selected is None:
            return current_stores
        if selected:
            return selected
        print("请至少选择一家门店。")


def configure_notifications(current_notify):
    """编辑通知设置，取消时保留原有配置。"""
    notify = dict(current_notify)
    print("\n【通知推送设置】")
    for key, message, default in (
        ("macos", "开启 macOS 系统弹窗与提示音？", True),
        ("say", "开启 macOS 语音朗读播报？", False),
    ):
        answer = confirm(message, default=bool(notify.get(key, default))).ask()
        if answer is None:
            return current_notify
        notify[key] = answer
    for key, message, prompt in (
        ("bark_key", "配置 Bark 推送（推送至 iPhone/iPad）？", "请输入 Bark 设备密钥（可在 Bark 应用中查看）:"),
        ("serverchan_key", "配置 Server酱推送（推送至微信）？", "请输入 Server酱推送密钥（SendKey）:"),
    ):
        answer = confirm(message, default=bool(notify.get(key))).ask()
        if answer is None:
            return current_notify
        if answer:
            value = text(prompt, default=notify.get(key) or "").ask()
            if value is None:
                return current_notify
            notify[key] = value.strip()
        else:
            notify[key] = ""
    return notify


def configure_distance(cfg):
    value = text(
        "配置时筛选门店的距离上限（公里，0 表示不限距离；保存后按门店名单监控）:",
        default=str(cfg.get("max_distance_km", 0)),
        validate=lambda val: val.isdigit() or "请输入不小于 0 的整数，0 表示不限距离",
    ).ask()
    if value is not None:
        cfg["max_distance_km"] = int(value)


def configure_interval(cfg):
    value = text(
        f"监控检查间隔（秒，最少 {MIN_INTERVAL} 秒，建议不小于 60 秒）:",
        default=str(cfg.get("interval_seconds", 60)),
        validate=lambda val: val.isdigit() and int(val) >= MIN_INTERVAL or f"请输入不小于 {MIN_INTERVAL} 的正整数",
    ).ask()
    if value is not None:
        cfg["interval_seconds"] = int(value)


def show_configuration(cfg, mon, dirty=False):
    print("\n" + "=" * 55)
    print("  Apple Store 库存监控 · 配置主页")
    print("  修改尚未保存" if dirty else "  当前已保存的配置")
    print("=" * 55)
    print("监控地区：")
    for location in cfg["locations"]:
        print(f"  · {location}")
    print("监控门店：")
    for store in cfg.get("stores") or ["未指定名单，兼容旧配置：监控接口返回的全部门店"]:
        print(f"  · {store}")
    print("监控型号：")
    for model in cfg.get("models") or ["全部型号"]:
        label = mon.label(model) if model in mon.models else model
        print(f"  · {label}")
    print(f"检查间隔：{cfg['interval_seconds']} 秒（每轮有随机抖动）")
    distance = cfg.get("max_distance_km") or 0
    print(f"配置时的距离筛选：{str(distance) + ' 公里' if distance else '不限距离'}（不影响已保存门店的监控）")
    print("商品系列：" + "、".join(cfg["product_pages"]))
    notify = cfg.get("notify", {})
    print("通知设置：")
    for label, enabled in (
        ("macOS 系统通知", notify.get("macos", True)),
        ("语音播报", notify.get("say")),
        ("Bark 推送", notify.get("bark_key")),
        ("Server酱推送", notify.get("serverchan_key")),
    ):
        print(f"  · {label}：{'已启用' if enabled else '未启用'}")
    print("=" * 55)


def configure_pipeline(mon, cfg):
    """保留原有的地区 → 门店 → 型号 → 时间 → 通知完整流程。"""
    cfg["locations"] = configure_locations(mon, cfg["locations"])
    configure_distance(cfg)
    cfg["stores"] = configure_stores(mon, cfg["locations"], cfg.get("stores", []))
    cfg["models"] = configure_models(mon, cfg.get("models", []))
    configure_interval(cfg)
    cfg["notify"] = configure_notifications(cfg.get("notify", {}))


def run_wizard(config_path):
    """进入配置主页，可单独修改设置或运行完整向导。"""
    cfg = load_config(config_path)
    saved_cfg = copy.deepcopy(cfg)
    mon = Monitor(cfg)
    while True:
        show_configuration(cfg, mon, dirty=cfg != saved_cfg)
        action = select(
            "请选择配置操作:",
            choices=[
                Choice("修改监控地区", "locations"),
                Choice("修改监控门店", "stores"),
                Choice("修改监控型号", "models"),
                Choice("修改检查间隔", "interval"),
                Choice("修改通知设置", "notify"),
                Choice("修改配置时的距离筛选", "distance"),
                Separator(),
                Choice("运行完整配置向导（按步骤配置）", "pipeline"),
                Choice("测试当前通知设置", "test_notify"),
                Separator(),
                Choice("保存配置并退出", "save"),
                Choice("退出，不保存修改", "exit"),
            ],
        ).ask()
        if action is None or action == "exit":
            print("已退出配置主页，修改未保存。" if cfg != saved_cfg else "已退出配置主页。")
            return False
        if action == "save":
            save_config(config_path, cfg)
            print(f"配置已保存至：{Path(config_path).resolve()}")
            print("运行 `uv run monitor.py` 开始监控；已运行的监控进程需重启以加载修改。")
            return True
        if action == "locations":
            cfg["locations"] = configure_locations(mon, cfg["locations"])
            print("地区已更新。如需更换监控门店，请进入“修改监控门店”；当前门店名单保持不变。")
        elif action == "stores":
            configure_distance(cfg)
            cfg["stores"] = configure_stores(mon, cfg["locations"], cfg.get("stores", []))
        elif action == "models":
            cfg["models"] = configure_models(mon, cfg.get("models", []))
        elif action == "interval":
            configure_interval(cfg)
        elif action == "notify":
            cfg["notify"] = configure_notifications(cfg.get("notify", {}))
        elif action == "distance":
            configure_distance(cfg)
            print("距离筛选已更新。重新进入“修改监控门店”选择并保存门店后生效。")
        elif action == "pipeline":
            configure_pipeline(mon, cfg)
        elif action == "test_notify":
            mon.notify("库存监控通知测试", "这是一条测试通知，用于确认当前通知配置是否正常。",
                       "https://www.apple.com.cn", testing=True)
