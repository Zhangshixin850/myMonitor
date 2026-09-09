# -*- coding: utf-8 -*-

import itertools
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import sys
import time
import urllib.request

import ccxt
from dotenv import load_dotenv


env_file = os.getenv("MONITOR_ENV_FILE", ".env.monitor")
load_dotenv(env_file if os.path.exists(env_file) else None, override=False)

# 统一日志：终端必打，文件日志按配置启用并自动轮转。
logger = logging.getLogger("perp_price_monitor")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").strip().upper())
logger.handlers.clear()
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

if os.getenv("LOG_TO_FILE", "true").strip().lower() in ("1", "true", "yes", "y", "on"):
    log_file = os.getenv("LOG_FILE", "perp_price_monitor.log").strip().strip("'\"")
    rotate_when = os.getenv("LOG_ROTATE_WHEN", "midnight").strip().strip("'\"")
    rotate_interval = int(os.getenv("LOG_ROTATE_INTERVAL", "1").strip().strip("'\""))
    backup_count = int(os.getenv("LOG_BACKUP_COUNT", "7").strip().strip("'\""))
    file_handler = TimedRotatingFileHandler(
        log_file,
        when=rotate_when,
        interval=rotate_interval,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def clean(value, default=""):
    return str(value if value is not None else default).strip().strip("'\"")


def env_bool(name, default=False):
    value = clean(os.getenv(name, str(default))).lower()
    return value in ("1", "true", "yes", "y", "on")


def to_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


poll_interval = float(clean(os.getenv("POLL_INTERVAL", "4")))
alert_cooldown = float(clean(os.getenv("ALERT_COOLDOWN", "60")))
request_error_limit = int(clean(os.getenv("REQUEST_ERROR_LIMIT", "10")))
price_diff_alert_pct = float(clean(os.getenv("PRICE_DIFF_ALERT_PCT", "10")))
margin_ratio_alert = float(clean(os.getenv("MARGIN_RATIO_ALERT", "0.5")))
enable_margin_monitor = env_bool("ENABLE_MARGIN_MONITOR", True)
notify_webhook = clean(os.getenv("NOTIFY_WEBHOOK"))
disable_webhook = env_bool("DISABLE_WEBHOOK", False)
monitor_exchanges = [x.strip().lower() for x in clean(os.getenv("MONITOR_EXCHANGES", "aster,binance")).split(",") if x.strip()]

if not monitor_exchanges:
    monitor_exchanges = ["aster", "binance"]

last_alert_at = {}
price_errors = {}
margin_errors = {}


def alert(event, message, exchange=None, symbol=None, data=None, dedupe_key=None):
    # 同类报警按冷却时间去重，避免价格持续越线时刷屏。
    key = dedupe_key or f"{event}:{exchange or '*'}:{symbol or '*'}"
    now = time.time()
    if now - last_alert_at.get(key, 0) < alert_cooldown:
        return
    last_alert_at[key] = now
    logger.warning(message)
    if disable_webhook or not notify_webhook:
        return
    try:
        payload = json.dumps(
            {"message": message, "event": event, "exchange": exchange, "symbol": symbol, "data": data or {}},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(notify_webhook, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=5).read()
    except Exception as exc:
        logger.warning("webhook failed: %s", exc)


def build_exchange(exchange_id):
    if not hasattr(ccxt, exchange_id):
        sys.exit(f"ccxt does not support exchange: {exchange_id}")

    # 强制使用线性永续市场，避免误加载现货或币本位市场。
    options = {
        "defaultType": "swap",
        "fetchCurrencies": False,
        "fetchMarkets": {"types": ["linear"]},
    }
    config = {"enableRateLimit": True, "options": options}

    if exchange_id == "aster":
        private_key = clean(os.getenv("ASTER_PRIVATE_KEY") or os.getenv("PRIVATE_KEY"))
        wallet_address = clean(os.getenv("ASTER_WALLET_ADDRESS") or os.getenv("WALLET_ADDRESS"))
        signer_address = clean(os.getenv("ASTER_SIGNER_ADDRESS") or os.getenv("signerAddress"))
        if private_key and wallet_address and signer_address:
            options.update({"signerAddress": signer_address, "builderFee": False})
            config.update({"privateKey": private_key, "walletAddress": wallet_address})
    else:
        prefix = exchange_id.upper()
        api_key = clean(os.getenv(f"{prefix}_API_KEY") or os.getenv("API_KEY"))
        secret = clean(os.getenv(f"{prefix}_SECRET") or os.getenv("SECRET"))
        password = clean(os.getenv(f"{prefix}_PASSWORD") or os.getenv("PASSWORD"))
        if api_key:
            config["apiKey"] = api_key
        if secret:
            config["secret"] = secret
        if password:
            config["password"] = password

    return getattr(ccxt, exchange_id)(config)


def resolve_symbol(exchange, symbol):
    # 启动时校验交易对存在且确实是 perpetual swap。
    candidates = [clean(symbol), clean(symbol).upper()]
    for candidate in candidates:
        if candidate and candidate in exchange.markets:
            market = exchange.market(candidate)
            if market.get("swap"):
                return market["symbol"]
            raise ValueError(f"{candidate} is not a perpetual swap market")
    raise ValueError(f"{symbol} not found in {exchange.id} swap markets")


def fetch_price(exchange, symbol, prefer_mark):
    # 单币种兜底价格源：按当前轮次优先 mark 或买卖一中间价。
    errors = []

    def mark_price():
        funding = exchange.fetch_funding_rate(symbol)
        price = to_float(funding.get("markPrice")) or to_float((funding.get("info") or {}).get("markPrice"))
        if price and price > 0:
            return price, "mark"
        raise RuntimeError("empty mark price")

    def mid_price():
        book = exchange.fetch_order_book(symbol, limit=5)
        bid = to_float(book["bids"][0][0]) if book.get("bids") else None
        ask = to_float(book["asks"][0][0]) if book.get("asks") else None
        if bid and ask and bid > 0 and ask > 0:
            return (bid + ask) / 2, "bid_ask_mid"
        raise RuntimeError("empty bid/ask")

    for getter in ([mark_price, mid_price] if prefer_mark else [mid_price, mark_price]):
        try:
            return getter()
        except Exception as exc:
            errors.append(str(exc))

    ticker = exchange.fetch_ticker(symbol)
    for key in ("mark", "last", "close"):
        price = to_float(ticker.get(key))
        if price and price > 0:
            return price, f"ticker_{key}"
    raise RuntimeError("; ".join(errors) or "empty ticker price")


def price_from_funding(funding):
    price = to_float((funding or {}).get("markPrice")) or to_float(((funding or {}).get("info") or {}).get("markPrice"))
    return price if price and price > 0 else None


def price_from_bid_ask(ticker):
    bid = to_float((ticker or {}).get("bid")) or to_float(((ticker or {}).get("info") or {}).get("bidPrice"))
    ask = to_float((ticker or {}).get("ask")) or to_float(((ticker or {}).get("info") or {}).get("askPrice"))
    return (bid + ask) / 2 if bid and ask and bid > 0 and ask > 0 else None


def fetch_batch_prices(exchange, symbols, prefer_mark):
    # 正常路径每个交易所每轮只请求一次价格接口。
    results = {}
    batch_error = None
    try:
        if prefer_mark:
            fundings = exchange.fetch_funding_rates(symbols)
            for symbol in symbols:
                price = price_from_funding(fundings.get(symbol))
                if price:
                    results[symbol] = (price, "mark")
        else:
            tickers = exchange.fetch_bids_asks(symbols)
            for symbol in symbols:
                price = price_from_bid_ask(tickers.get(symbol))
                if price:
                    results[symbol] = (price, "bid_ask_mid")
    except Exception as exc:
        batch_error = exc

    # 批量结果缺失时才回退单币种请求，兼顾少请求和可用性。
    for symbol in symbols:
        if symbol in results:
            continue
        try:
            results[symbol] = fetch_price(exchange, symbol, prefer_mark)
        except Exception as exc:
            if batch_error:
                raise RuntimeError(f"batch failed: {batch_error}; fallback failed for {symbol}: {exc}") from exc
            raise
    return results


def account_margin_ratio(account, source):
    # 使用账户级维持保证金 / 保证金余额，避免误读仓位级 marginRatio。
    maint = to_float((account or {}).get("totalMaintMargin"))
    margin_balance = to_float((account or {}).get("totalMarginBalance"))
    if maint is not None and margin_balance and margin_balance > 0:
        return maint / margin_balance, source
    raise RuntimeError(f"missing totalMaintMargin/totalMarginBalance in {source}")


def fetch_margin_ratio(exchange):
    # Aster/Binance 优先读 futures account 原始接口，字段更贴近页面风险率。
    errors = []
    if exchange.id == "aster":
        for method_name in ("fapiPrivateGetV4Account", "fapiPrivateGetV3AccountWithJoinMargin", "fapiPrivateGetV3Account"):
            method = getattr(exchange, method_name, None)
            if not method:
                continue
            try:
                return account_margin_ratio(method(), method_name)
            except Exception as exc:
                errors.append(f"{method_name}: {exc}")
    elif exchange.id == "binance":
        for method_name in ("fapiPrivateV3GetAccount", "fapiPrivateV2GetAccount"):
            method = getattr(exchange, method_name, None)
            if not method:
                continue
            try:
                return account_margin_ratio(method(), method_name)
            except Exception as exc:
                errors.append(f"{method_name}: {exc}")

    try:
        balance = exchange.fetch_balance()
        return account_margin_ratio(balance.get("info") or balance, "fetch_balance_info")
    except Exception as exc:
        errors.append(f"fetch_balance: {exc}")
    raise RuntimeError("; ".join(errors) or "cannot derive margin ratio from exchange response")


def main():
    # 读取 5 个固定槽位：只填币种名称和高价报警阈值，默认使用 USDT 永续交易对。
    monitor_symbols = []
    for index in range(1, 6):
        name = clean(os.getenv(f"MONITOR_SYMBOL_{index}")).upper()
        if not name:
            continue
        alert_high = to_float(clean(os.getenv(f"MONITOR_ALERT_HIGH_{index}")))
        if alert_high is None:
            sys.exit(f"Missing or invalid MONITOR_ALERT_HIGH_{index} for {name}")
        monitor_symbols.append({"name": name, "symbols": {}, "alertHigh": alert_high})

    if not monitor_symbols:
        sys.exit("Missing MONITOR_SYMBOL_1..5 in .env.monitor")

    exchanges = {}
    for exchange_id in monitor_exchanges:
        # 启动阶段加载市场，后续可用 ccxt 统一 symbol 访问。
        exchange = build_exchange(exchange_id)
        try:
            exchange.load_markets()
        except Exception as exc:
            alert("exchange_startup_error", f"{exchange_id} load_markets failed: {exc}", exchange_id)
            continue
        exchanges[exchange_id] = exchange
        logger.info("%s connected, swap markets loaded", exchange_id)

    if not exchanges:
        sys.exit("No exchange is available")

    # 市场加载后统一规范化 symbol，避免大小写或别名差异。
    active_symbols = []
    for item in monitor_symbols:
        name = clean(item.get("name")).upper()
        symbols = item.get("symbols") or {}
        alert_high = float(item["alertHigh"])
        resolved = {}
        for exchange_id, exchange in exchanges.items():
            configured_symbol = symbols.get(exchange_id) or f"{name}/USDT:USDT"
            try:
                resolved[exchange_id] = resolve_symbol(exchange, configured_symbol)
            except Exception as exc:
                logger.warning("skip %s on %s: %s", name, exchange_id, exc)
        if resolved:
            active_symbols.append({"name": name, "symbols": resolved, "alertHigh": alert_high})

    if not active_symbols:
        sys.exit("No configured symbol is available on selected exchanges")

    logger.info(
        "monitor started: exchanges=%s, symbols=%s, poll_interval=%ss, margin_monitor=%s",
        ",".join(exchanges),
        ",".join(item["name"] for item in active_symbols),
        poll_interval,
        enable_margin_monitor,
    )

    round_no = 0
    once = "--once" in sys.argv
    while True:
        round_no += 1
        # 奇数轮查标记价格，偶数轮查买卖一中间价。
        prefer_mark = round_no % 2 == 1
        prices = {item["name"]: {} for item in active_symbols}
        logger.info("round=%s price_mode=%s", round_no, "mark" if prefer_mark else "bid_ask_mid")

        for exchange_id, exchange in exchanges.items():
            # 按交易所批量取本轮所有币种价格，减少接口请求次数。
            exchange_items = [item for item in active_symbols if exchange_id in item["symbols"]]
            symbols = [item["symbols"][exchange_id] for item in exchange_items]
            batch_prices = {}
            if symbols:
                try:
                    batch_prices = fetch_batch_prices(exchange, symbols, prefer_mark)
                except Exception as exc:
                    logger.error("%s batch price monitor failed: %s", exchange_id, exc)

            for item in exchange_items:
                symbol = item["symbols"][exchange_id]
                error_key = (exchange_id, item["name"])
                try:
                    if symbol not in batch_prices:
                        raise RuntimeError("price missing from batch response")
                    price, source = batch_prices[symbol]
                    price_errors[error_key] = 0
                    prices[item["name"]][exchange_id] = {"price": price, "symbol": symbol, "source": source}
                    # 每个币种只有高阈值，超过即报警。
                    if price > item["alertHigh"]:
                        alert(
                            "price_high",
                            f"{exchange_id} {symbol} price {price} > alertHigh {item['alertHigh']}",
                            exchange_id,
                            symbol,
                            {"price": price, "priceSource": source, "alertHigh": item["alertHigh"], "coin": item["name"]},
                        )
                except Exception as exc:
                    price_errors[error_key] = price_errors.get(error_key, 0) + 1
                    logger.error("%s %s price monitor failed (%s/%s): %s", exchange_id, item["name"], price_errors[error_key], request_error_limit, exc)
                    if price_errors[error_key] % request_error_limit == 0:
                        alert(
                            "price_monitor_failed",
                            f"{exchange_id} {item['name']} price monitor failed {price_errors[error_key]} times",
                            exchange_id,
                            item["name"],
                            {"errors": price_errors[error_key], "lastError": str(exc)},
                        )

        for item in active_symbols:
            # 终端按币种合并显示，一行包含各交易所价格和最大价差。
            available = prices[item["name"]]
            parts = [item["name"]]
            for exchange_id in exchanges:
                data = available.get(exchange_id)
                if data:
                    parts.append(f"{exchange_id}={data['price']}({data['source']})")

            max_diff_pct = None
            if len(exchanges) >= 2:
                for left_id, right_id in itertools.combinations(available, 2):
                    left = available[left_id]["price"]
                    right = available[right_id]["price"]
                    base = min(left, right)
                    if base <= 0:
                        continue
                    diff_pct = abs(left - right) / base * 100
                    max_diff_pct = diff_pct if max_diff_pct is None else max(max_diff_pct, diff_pct)
                    if diff_pct > price_diff_alert_pct:
                        alert(
                            "price_diff_high",
                            f"{item['name']} price diff {diff_pct:.2f}% > {price_diff_alert_pct}%: {left_id}={left}, {right_id}={right}",
                            None,
                            item["name"],
                            {
                                "leftExchange": left_id,
                                "rightExchange": right_id,
                                "leftPrice": left,
                                "rightPrice": right,
                                "diffPct": diff_pct,
                                "thresholdPct": price_diff_alert_pct,
                            },
                        )
            if max_diff_pct is not None:
                parts.append(f"diff={max_diff_pct:.2f}%")
            if len(parts) > 1:
                logger.info(" | ".join(parts))

        if enable_margin_monitor:
            # 保证金比例按交易所监控，失败连续达到阈值也会报警。
            margin_parts = ["Margin Ratio"]
            for exchange_id, exchange in exchanges.items():
                try:
                    ratio, source = fetch_margin_ratio(exchange)
                    margin_errors[exchange_id] = 0
                    margin_parts.append(f"{exchange_id}={ratio:.4f}")
                    if ratio > margin_ratio_alert:
                        alert(
                            "margin_ratio_high",
                            f"{exchange_id} margin ratio {ratio:.4f} > {margin_ratio_alert}",
                            exchange_id,
                            None,
                            {"marginRatio": ratio, "marginSource": source, "threshold": margin_ratio_alert},
                        )
                except Exception as exc:
                    margin_errors[exchange_id] = margin_errors.get(exchange_id, 0) + 1
                    logger.error("%s margin monitor failed (%s/%s): %s", exchange_id, margin_errors[exchange_id], request_error_limit, exc)
                    if margin_errors[exchange_id] % request_error_limit == 0:
                        alert(
                            "margin_monitor_failed",
                            f"{exchange_id} margin monitor failed {margin_errors[exchange_id]} times",
                            exchange_id,
                            None,
                            {"errors": margin_errors[exchange_id], "lastError": str(exc)},
                        )
            if len(margin_parts) > 1:
                logger.info(" | ".join(margin_parts))

        if once:
            break
        time.sleep(poll_interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("stopped")
