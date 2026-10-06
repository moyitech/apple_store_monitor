#!/usr/bin/env python3
"""
Apple Store (中国大陆) iPhone 18 系列门店取货库存监控。

用法:
    uv run monitor.py setup       # 交互式配置（地区、门店、型号可多选）
    uv run monitor.py             # 按配置持续监控
    uv run monitor.py --once      # 只检查一次
    uv run monitor.py --list      # 列出可监控的型号和附近门店
    uv run monitor.py config -h   # 用命令行查看/修改单个配置项
"""

import json
import random
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from cli_parser import ChineseArgumentParser, ChineseRawDescriptionHelpFormatter

BASE = "https://www.apple.com.cn"
PICKUP_API = BASE + "/shop/retail/pickup-message"
ADDRESS_API = BASE + "/shop/address-lookup"
BATCH_SIZE = 20  # 接口单次最多返回 20 个型号
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
DEFAULT_CONFIG_PATH = str(Path(__file__).with_name("config.json"))

PRODUCT_RE = re.compile(
    r'\{"sku":"\w+","partNumber":"(?P<part>[\w/]+)",'
    r'"price":\{"fullPrice":(?P<price>[\d.]+)\},'
    r'"category":"\w+","name":"(?P<name>[^"]+)"\}'
)


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def decode_js(s):
    """把页面里的 \\xa0 / \\u00a0 等转义还原成普通空格。"""
    s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return s.replace("\xa0", " ")


class Monitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = self._new_session()
        self.models = {}  # part -> {name, price, page, title}
        self.notified = set()  # 已提醒过的 (store, part)
        self._titles_loaded = False

    # ---------- 网络 ----------
    @staticmethod
    def _new_session():
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        return s

    def discover_models(self, quiet=False):
        for page in self.cfg["product_pages"]:
            url = f"{BASE}/shop/buy-iphone/{page}"
            try:
                html = self.session.get(url, timeout=20).text
            except requests.RequestException as e:
                log(f"获取商品页失败 {page}: {e}")
                continue
            found = 0
            for m in PRODUCT_RE.finditer(html):
                part = m.group("part")
                if part not in self.models:
                    self.models[part] = {
                        "name": decode_js(m.group("name")),
                        "price": float(m.group("price")),
                        "page": page,
                        "title": "",
                    }
                    found += 1
            if not quiet:
                log(f"{page}: 发现 {found} 个型号")
        if not self.models:
            sys.exit("没有发现任何型号，请检查配置里的 product_pages 是否正确。")

    def address_lookup(self, **params):
        """Apple 官网的省/市/区三级联动接口。返回 (下一级字段名, 选项列表) 或直辖市的固定 city。"""
        r = self.session.get(ADDRESS_API, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("body", {})

    def query(self, parts, location):
        """查询某个位置附近门店的取货情况，返回 stores 列表（已合并所有批次）。"""
        stores = {}
        for i in range(0, len(parts), BATCH_SIZE):
            batch = parts[i : i + BATCH_SIZE]
            params = {"pl": "true", "location": location}
            for j, p in enumerate(batch):
                params[f"parts.{j}"] = p
            r = self.session.get(
                PICKUP_API,
                params=params,
                headers={"Referer": f"{BASE}/shop/buy-iphone/{self.cfg['product_pages'][0]}"},
                timeout=20,
            )
            if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                raise RuntimeError(f"接口返回异常 HTTP {r.status_code}（可能被限流）")
            body = r.json().get("body", {})
            if body.get("errorMessage"):
                raise RuntimeError(f"“{location}” 查询失败: {body['errorMessage']}")
            for st in body.get("stores", []):
                key = st["storeNumber"]
                if key not in stores:
                    stores[key] = st
                else:
                    stores[key]["partsAvailability"].update(st["partsAvailability"])
            if i + BATCH_SIZE < len(parts):
                time.sleep(1)
        self._fill_titles(stores.values())
        return sorted(stores.values(), key=lambda s: float(s.get("storedistance") or 0))

    def query_all(self, parts):
        """依次查询配置中的每个位置，按门店编号合并结果。"""
        merged = {}
        for n, loc in enumerate(self.cfg["locations"]):
            if n:
                time.sleep(1)
            for st in self.query(parts, loc):
                key = st["storeNumber"]
                if key not in merged:
                    merged[key] = st
                else:
                    old = merged[key]
                    old["partsAvailability"].update(st["partsAvailability"])
                    old["storedistance"] = min(float(old.get("storedistance") or 0), float(st.get("storedistance") or 0))
        return sorted(merged.values(), key=lambda s: (s.get("city", ""), float(s.get("storedistance") or 0)))

    def _fill_titles(self, stores):
        """用接口返回的中文名补全型号信息（如 “iPhone 18 Pro 256GB 黑色”），便于用中文颜色过滤。"""
        for st in stores:
            for part, info in st["partsAvailability"].items():
                if part in self.models and not self.models[part]["title"]:
                    title = info.get("messageTypes", {}).get("regular", {}).get("storePickupProductTitle", "")
                    self.models[part]["title"] = title.replace("\xa0", " ")
                    self._titles_loaded = True

    # ---------- 过滤 ----------
    def _model_text(self, part):
        m = self.models[part]
        return f"{m['name']} {m['title']} {part}".lower()

    def model_wanted(self, part):
        filters = self.cfg.get("models") or []
        if not filters:
            return True
        text = self._model_text(part)
        # 每条规则里的关键词需全部命中；多条规则之间是“或”
        return any(all(tok.lower() in text for tok in f.split()) for f in filters)

    def store_wanted(self, st):
        wanted = self.cfg.get("stores") or []
        if wanted and not any(w.strip() in (st["storeName"].strip(), st["storeNumber"]) for w in wanted):
            return False
        return True

    def parts_to_query(self):
        # 第一次要查全部型号，拿到中文名后才能准确匹配“黑色”之类的中文关键词
        if not self._titles_loaded:
            return list(self.models)
        return [p for p in self.models if self.model_wanted(p)]

    # ---------- 主逻辑 ----------
    def check(self):
        query_parts = self.parts_to_query()
        if not query_parts:
            log("⚠️  没有符合配置的型号，请运行 `uv run monitor.py setup` 重新选择")
            return
        queried_stores = self.query_all(query_parts)
        stores = [s for s in queried_stores if self.store_wanted(s)]
        parts = [p for p in self.models if self.model_wanted(p)]
        available = {}  # part -> [store names]
        current = set()
        for st in stores:
            for part in parts:
                info = st["partsAvailability"].get(part)
                if info and info.get("pickupDisplay") == "available":
                    available.setdefault(part, []).append(f"{st['storeName']}({st['city']})")
                    current.add((st["storeNumber"], part))

        log(f"检查完成：{len(parts)} 个型号 × {len(stores)} 家监控门店（接口返回 {len(queried_stores)} 家），有货 {len(current)} 项")
        if queried_stores and not stores:
            log(f"⚠️  接口返回的门店中没有已配置的监控门店：{self.cfg.get('stores')}。请运行 `uv run monitor.py setup` 检查门店配置。")
        for part in parts:
            if part in available:
                print(f"    ✅ {self.label(part)}: {', '.join(available[part])}")

        new = current - self.notified
        if new:
            new_parts = sorted({p for _, p in new})
            lines = [f"{self.label(p)} @ {', '.join(available[p])}" for p in new_parts]
            url = f"{BASE}/shop/buy-iphone/{self.models[new_parts[0]]['page']}"
            self.notify("iPhone 有货啦！", "\n".join(lines), url)
        # 只记住当前仍有货的组合：卖光后再次补货会重新提醒
        self.notified = current

    def label(self, part):
        m = self.models[part]
        return f"{m['title'] or m['name']} ¥{m['price']:.0f}"

    # ---------- 通知 ----------
    def notify(self, title, content, url, channels=None, testing=False):
        log(f"🔔 {title}\n{content}\n    购买链接: {url}")
        n = self.cfg.get("notify", {})
        enabled = {
            "macos": n.get("macos", True), "say": n.get("say"),
            "bark": n.get("bark_key"), "serverchan": n.get("serverchan_key"),
        }
        labels = {"macos": "macOS 系统通知", "say": "语音播报", "bark": "Bark", "serverchan": "Server酱"}
        results = {}
        for channel in channels or enabled:
            label = labels[channel]
            if not enabled[channel]:
                results[channel] = "disabled"
                if testing:
                    log(f"{label}：未启用或未配置")
                continue
            if channel in ("macos", "say") and sys.platform != "darwin":
                results[channel] = "unsupported"
                log(f"{label}：当前系统不支持，仅适用于 macOS")
                continue
            try:
                if channel == "macos":
                    script = 'display notification {} with title {} sound name "Glass"'.format(
                        json.dumps(content[:200], ensure_ascii=False), json.dumps(title, ensure_ascii=False)
                    )
                    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=20)
                elif channel == "say":
                    subprocess.run(["say", "库存监控通知测试" if testing else "iPhone 有货了"],
                                   check=True, capture_output=True, text=True, timeout=20)
                else:
                    if channel == "bark":
                        path = "/".join(urllib.parse.quote(x, safe="") for x in (n["bark_key"], title, content))
                        response = requests.get(f"https://api.day.app/{path}",
                                                params={"url": url, "level": "timeSensitive"}, timeout=10)
                        expected_code = 200
                    else:
                        response = requests.post(
                            f"https://sctapi.ftqq.com/{n['serverchan_key']}.send",
                            data={"title": title, "desp": content.replace("\n", "\n\n") + f"\n\n[打开页面]({url})"},
                            timeout=10,
                        )
                        expected_code = 0
                    if not 200 <= response.status_code < 300:
                        raise RuntimeError(f"HTTP {response.status_code}")
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("通知服务返回的数据格式异常")
                    code = payload.get("code", payload.get("errno"))
                    if code != expected_code:
                        raise RuntimeError(f"服务返回失败代码 {code}")
            except (requests.RequestException, OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
                results[channel] = "failed"
                # requests 的异常可能包含带密钥的完整 URL，不直接输出异常内容。
                detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                log(f"{label}：发送失败（{detail}）")
            else:
                results[channel] = "sent"
                if testing:
                    log(f"{label}：发送请求成功，请检查设备是否收到")
        return results

    # ---------- 命令 ----------
    def list_all(self):
        stores = self.query_all(list(self.models))
        print("\n可监控型号：")
        for part, m in sorted(self.models.items(), key=lambda x: (x[1]["page"], x[1]["price"], x[1]["name"])):
            mark = "*" if self.model_wanted(part) else " "
            print(f"  {mark} {part:<11} {m['title'] or m['name']:<32} ¥{m['price']:.0f}")
        print(f"\n{' / '.join(self.cfg['locations'])} 附近门店：")
        for st in stores:
            mark = "*" if self.store_wanted(st) else " "
            print(f"  {mark} {st['storeNumber']}  {st['storeName']:<12} {st['city']}  {st.get('storedistance')} km")
        print("\n标 * 的是按当前配置会被监控的。")

    def run(self):
        interval = max(int(self.cfg.get("interval_seconds", 60)), MIN_INTERVAL)
        log(f"开始监控 {' / '.join(self.cfg['locations'])}，每 {interval} 秒左右检查一次，Ctrl+C 退出")
        fails = 0
        while True:
            try:
                self.check()
                fails = 0
            except Exception as e:  # noqa: BLE001 - 监控循环不应因单次失败退出
                fails += 1
                log(f"⚠️  检查失败（连续 {fails} 次）: {e}")
                self.session = self._new_session()
            # 加随机抖动；连续失败时指数退避，最多 10 分钟
            wait = min(interval * (2 ** min(fails, 4)), 600) if fails else interval
            time.sleep(wait + random.uniform(0, interval * 0.3))


DEFAULT_CONFIG = {
    "locations": ["上海 上海 黄浦区"],
    "stores": [],
    "max_distance_km": 0,
    "product_pages": ["iphone-18-pro", "iphone-duo"],
    "models": [],
    "interval_seconds": 60,
    "notify": {
        "macos": True,
        "say": False,
        "bark_key": "",
        "serverchan_key": "",
    },
}
MIN_INTERVAL = 20
TRUE_WORDS = {"true", "1", "yes", "y", "on", "是", "开"}
FALSE_WORDS = {"false", "0", "no", "n", "off", "否", "关"}


# ---------- 配置读写 ----------
def load_config(path):
    """读取配置并用默认值补全缺失项；文件不存在时直接用默认配置。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    p = Path(path)
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        # 兼容旧版的单个 location
        old = user.pop("location", None)
        if old and not user.get("locations"):
            user["locations"] = [old]
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save_config(path, cfg):
    Path(path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def all_keys(d=DEFAULT_CONFIG, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from all_keys(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


def locate(cfg, key):
    """把 'notify.bark_key' 解析成 (所在的 dict, 末级 key, 默认值)。"""
    defaults = dict(all_keys())
    if key not in defaults:
        sys.exit(f"未知配置项: {key}\n可用配置项: {', '.join(defaults)}")
    *parents, leaf = key.split(".")
    node = cfg
    for p in parents:
        node = node.setdefault(p, {})
    return node, leaf, defaults[key]


def parse_value(key, raw, template):
    if isinstance(template, bool):  # 注意 bool 是 int 的子类，要先判断
        low = raw.strip().lower()
        if low in TRUE_WORDS:
            return True
        if low in FALSE_WORDS:
            return False
        sys.exit(f"{key} 需要布尔值（是/否，也支持 true/false），收到: {raw}")
    if isinstance(template, int):
        try:
            val = int(raw)
        except ValueError:
            sys.exit(f"{key} 需要整数，收到: {raw}")
        if val < 0:
            sys.exit(f"{key} 不能为负数")
        if key == "interval_seconds" and val < MIN_INTERVAL:
            sys.exit(f"interval_seconds 最少 {MIN_INTERVAL} 秒，太频繁会被 Apple 限流")
        return val
    return raw


def cmd_config(args):
    path = args.config
    cfg = load_config(path)
    action = args.action or "show"

    if action == "test-notify":
        available = ("macos", "say", "bark", "serverchan")
        if args.values or (args.key and args.key not in available):
            sys.exit("用法: monitor.py config test-notify [macos|say|bark|serverchan]")
        channels = [args.key] if args.key else None
        results = Monitor(cfg).notify(
            "库存监控通知测试", "这是一条测试通知，用于确认当前通知配置是否正常。",
            BASE, channels=channels, testing=True,
        )
        sent = sum(status == "sent" for status in results.values())
        failed = sum(status in ("failed", "unsupported") for status in results.values())
        print(f"通知测试完成：{sent} 个渠道发送成功，{failed} 个渠道失败或不支持。")
        if failed or not sent:
            sys.exit(1)
        return

    if action == "show":
        print(f"# {Path(path).resolve()}")
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    if action == "keys":
        for k, v in all_keys():
            kind = "列表" if isinstance(v, list) else "布尔" if isinstance(v, bool) else "整数" if isinstance(v, int) else "文本"
            print(f"  {k:<22} {kind}")
        return
    if action == "reset":
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_config(path, cfg)
        print("已恢复默认配置")
        return

    if not args.key:
        sys.exit(f"用法: monitor.py config {action} <配置项> ...")
    node, leaf, template = locate(cfg, args.key)
    values = args.values or []

    if action == "set":
        if isinstance(template, list):
            sys.exit(f"{args.key} 是列表，请用 add / remove / clear 修改")
        if not values:
            sys.exit(f"用法: monitor.py config set {args.key} <值>")
        if isinstance(template, str):
            raw = " ".join(values)  # 允许不加引号: config set notify.bark_key xxx
        elif len(values) == 1:
            raw = values[0]
        else:
            sys.exit(f"{args.key} 只接受一个值")
        node[leaf] = parse_value(args.key, raw, template)
    elif action in ("add", "remove", "clear"):
        if not isinstance(template, list):
            sys.exit(f"{args.key} 不是列表，请用 set 修改")
        items = node.setdefault(leaf, [])
        if action == "clear":
            items.clear()
        elif not values:
            sys.exit(f"用法: monitor.py config {action} {args.key} <值> [<值> ...]")
        elif action == "add":
            items.extend(v for v in values if v not in items)
        else:
            missing = [v for v in values if v not in items]
            if missing:
                sys.exit(f"{args.key} 里没有: {', '.join(missing)}\n当前: {items}")
            node[leaf] = [v for v in items if v not in values]

    save_config(path, cfg)
    print(f"已更新 {args.key} = {json.dumps(node[leaf], ensure_ascii=False)}")
    if args.key in ("locations", "models", "stores", "max_distance_km", "product_pages"):
        print("提示: 运行 `uv run monitor.py --list` 可以确认哪些型号/门店会被监控")


def main():
    ap = ChineseArgumentParser(
        description="Apple Store iPhone 18 系列库存监控",
        epilog="首次使用请运行: monitor.py setup",
    )
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH, metavar="配置文件", help="指定配置文件路径")
    ap.add_argument("--once", action="store_true", help="只检查一次")
    ap.add_argument("--list", action="store_true", help="列出型号和附近门店")
    sub = ap.add_subparsers(dest="command", title="命令", metavar="命令")

    sub.add_parser("setup", help="交互式配置（地区、门店、型号、通知，可多选）")

    cp = sub.add_parser(
        "config",
        help="用命令行查看/修改单个配置项",
        formatter_class=ChineseRawDescriptionHelpFormatter,
        description="查看或修改 config.json",
        epilog="""示例:
  monitor.py config                                  查看当前配置
  monitor.py config keys                             列出所有配置项
  monitor.py config test-notify                      测试所有已启用的通知渠道
  monitor.py config test-notify bark                 只测试 Bark 推送
  monitor.py config add locations "北京 北京 朝阳区"
  monitor.py config set interval_seconds 90
  monitor.py config set notify.bark_key xxxxxx
  monitor.py config set notify.say true
  monitor.py config add models "Pro Max 256GB 银色" MJT74CH/A
  monitor.py config remove models MJT74CH/A
  monitor.py config clear stores
  monitor.py config reset                            恢复默认配置""",
    )
    cp.add_argument("action", nargs="?", metavar="操作", choices=["show", "keys", "set", "add", "remove", "clear", "reset", "test-notify"],
                    help="show 查看配置；keys 列出配置项；set 设置；add 添加；remove 删除；clear 清空；reset 恢复默认；test-notify 测试通知")
    cp.add_argument("key", nargs="?", metavar="配置项", help="配置项，如 locations、models、notify.bark_key；通知测试可指定 macos、say、bark、serverchan")
    cp.add_argument("values", nargs="*", metavar="值", help="要设置、添加或删除的值")
    args = ap.parse_args()

    if args.command == "config":
        cmd_config(args)
        return
    if args.command == "setup" or (not Path(args.config).exists() and sys.stdin.isatty()):
        from wizard import run_wizard  # 延迟导入：只有向导需要 questionary

        if args.command != "setup":
            print("还没有配置文件，先进入配置向导。\n")
        if not run_wizard(args.config) or args.command == "setup":
            return

    cfg = load_config(args.config)
    mon = Monitor(cfg)
    mon.discover_models()
    if args.list:
        mon.list_all()
    elif args.once:
        mon.check()
    else:
        try:
            mon.run()
        except KeyboardInterrupt:
            log("已退出")


if __name__ == "__main__":
    main()
