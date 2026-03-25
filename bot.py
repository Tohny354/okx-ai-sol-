import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from indicators import (
    amplitude_pct_recent,
    compute_indicator_bundle,
    daily_ma30_strategy_context,
    newest_first_to_oldest_first,
    parse_okx_candle_rows,
    pullback_metrics_vs_48h,
    range_48h_from_1h,
    round_floats,
    ema_last,
)

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# OpenAI 客户端缓存（避免每次循环新建连接）
_OAI_CLIENT_CACHE: Optional[tuple] = None

OKX_BASE_URL = "https://www.okx.com"
# 始终从脚本所在目录加载 .env（不依赖当前工作目录）
_PROJECT_DIR = Path(__file__).resolve().parent
_ENV_FILE = _PROJECT_DIR / ".env"


def utc_now_iso() -> str:
    """与欧易文档一致：2020-12-08T09:08:57.715Z（UTC，毫秒三位）。"""
    dt = datetime.now(timezone.utc)
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def _clean_secret_str(v: str) -> str:
    """去掉首尾空白、UTF-8 BOM；避免 .env 里误带空格导致 Invalid Sign。"""
    if not v:
        return ""
    return v.strip().strip("\ufeff")


def str_to_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass
class BotConfig:
    api_key: str
    api_secret: str
    passphrase: str
    http_proxy: Optional[str]
    https_proxy: Optional[str]
    simulated: bool
    inst_id: str
    td_mode: str
    lever: str
    order_size: str
    check_interval_sec: int
    dry_run: bool
    take_profit_pct: float
    stop_loss_pct: float
    use_ai: bool
    ai_model: str
    openai_api_key: str
    openai_base_url: Optional[str]
    ai_user_rule: str
    # 经代理访问 OKX 时，偶发 TLS 被掐断（UNEXPECTED_EOF），可加重试与超时
    okx_timeout: float
    okx_retries: int
    # K 线：公开接口 GET /api/v5/market/candles，不足时用 history-candles 向前扩展
    candle_bar: str
    candle_limit: int
    # 多周期：配合 AI_USER_RULE 里「日线/4H/15m/48h」等
    daily_candle_limit: int
    h4_candle_limit: int
    h1_candle_limit: int


# 仅对这些网络层错误重试（不重试 4xx/5xx 业务错误）
_RETRYABLE_EXC = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ProxyError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.Timeout,
)


class OKXClient:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.session = requests.Session()
        proxies: Dict[str, str] = {}
        if cfg.https_proxy:
            proxies["https"] = cfg.https_proxy
        if cfg.http_proxy:
            proxies["http"] = cfg.http_proxy
        if proxies:
            self.session.proxies.update(proxies)
            # 避免与系统环境变量混用导致难排查：显式以 .env 为准
            self.session.trust_env = False

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        msg = f"{timestamp}{method}{request_path}{body}"
        mac = hmac.new(
            self.cfg.api_secret.encode("utf-8"),
            msg.encode("utf-8"),
            digestmod=hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, method: str, request_path: str, body: str) -> Dict[str, str]:
        ts = utc_now_iso()
        return {
            "OK-ACCESS-KEY": self.cfg.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.cfg.passphrase,
            "Content-Type": "application/json",
            **({"x-simulated-trading": "1"} if self.cfg.simulated else {}),
        }

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, payload: Optional[Dict[str, Any]] = None):
        params = params or {}
        payload = payload or {}

        if params:
            # 查询串按 key 排序，与常见签名规范一致，避免多参数时签名校验失败
            items = sorted(params.items(), key=lambda kv: kv[0])
            query = "&".join([f"{k}={v}" for k, v in items])
            request_path = f"{path}?{query}"
        else:
            request_path = path

        if method == "GET":
            body = ""
        else:
            # 键排序，保证与参与签名的 JSON 字符串一致
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        url = OKX_BASE_URL + request_path
        headers = self._headers(method, request_path, body)

        timeout = self.cfg.okx_timeout
        retries = max(1, self.cfg.okx_retries)
        resp = None
        last_exc: Optional[BaseException] = None

        for attempt in range(retries):
            try:
                if method == "GET":
                    resp = self.session.get(url, headers=headers, timeout=timeout)
                elif method == "POST":
                    resp = self.session.post(url, headers=headers, data=body, timeout=timeout)
                else:
                    raise ValueError(f"Unsupported method: {method}")
                break
            except _RETRYABLE_EXC as e:
                last_exc = e
                if attempt + 1 >= retries:
                    raise RuntimeError(
                        f"OKX 网络请求失败（已重试 {retries} 次）。经 HTTP 代理访问时，"
                        f"若节点不稳定常出现 SSL EOF。请：换 Clash 节点、或暂时关闭代理（直连若可用）、"
                        f"或增大 .env 中 OKX_TIMEOUT / OKX_RETRIES。\n原始错误：{e!r}"
                    ) from e
                time.sleep(0.5 * (attempt + 1))

        if resp is None:
            raise RuntimeError(f"OKX 请求无响应：{last_exc!r}") from last_exc

        if not resp.ok:
            detail = resp.text[:800]
            try:
                j = resp.json()
                detail = json.dumps(j, ensure_ascii=False)[:800]
            except Exception:
                pass
            hint = ""
            if resp.status_code == 401:
                hint = (
                    "（401 常见原因：API Key/Secret/Passphrase 填错；"
                    "模拟盘需在欧易创建「模拟交易」用 API；"
                    "若 API 绑定了 IP 白名单，走代理时出口 IP 变化也会导致 401）"
                )
                try:
                    j401 = resp.json()
                    if j401.get("code") == "50113" or "Invalid Sign" in str(j401.get("msg", "")):
                        hint += (
                            " code=50113 Invalid Sign：请重新从欧易复制 Secret（勿多空格/换行），"
                            "确认 Passphrase 为创建 API 时自设且大小写一致；"
                            "实盘 Key 不要配模拟盘开关、反之亦然。"
                        )
                except Exception:
                    pass
            raise RuntimeError(f"HTTP {resp.status_code} {hint}\n{detail}")

        data = resp.json()
        if data.get("code") != "0":
            raise RuntimeError(f"OKX API error: {data}")
        return data.get("data", [])

    def get_ticker(self, inst_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/api/v5/market/ticker", params={"instId": inst_id})
        if not data:
            raise RuntimeError("No ticker data returned.")
        return data[0]

    def get_positions(self, inst_id: str) -> Dict[str, Any]:
        data = self._request("GET", "/api/v5/account/positions", params={"instId": inst_id})
        if not data:
            return {}
        return data[0]

    def set_leverage(self, inst_id: str, lever: str, mgn_mode: str):
        payload_long = {"instId": inst_id, "lever": lever, "mgnMode": mgn_mode, "posSide": "long"}
        resp_long = self._request("POST", "/api/v5/account/set-leverage", payload=payload_long)
        print(f"[DEBUG] set_leverage long response: {resp_long}")  # 添加
        payload_short = {"instId": inst_id, "lever": lever, "mgnMode": mgn_mode, "posSide": "short"}
        resp_short = self._request("POST", "/api/v5/account/set-leverage", payload=payload_short)
        print(f"[DEBUG] set_leverage short response: {resp_short}")  # 添加

    def place_market_order(self, inst_id: str, td_mode: str, side: str, pos_side: str, size: str):
        payload = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "market",
            "sz": size,
            "posSide": pos_side,
        }
        print(f"[DEBUG] place_market_order with size={size}")
        return self._request("POST", "/api/v5/trade/order", payload=payload)

    def get_usdt_equity_snapshot(self) -> Dict[str, Any]:
        """账户权益（私有）：用于资金管理与当日亏损规则。GET /api/v5/account/balance"""
        data = self._request("GET", "/api/v5/account/balance", params={})
        if not data:
            return {"error": "empty_balance_response"}
        row = data[0]
        out: Dict[str, Any] = {
            "adjEq": row.get("adjEq"),
            "isoEq": row.get("isoEq"),
            "totalEq": row.get("totalEq"),
            "mgnRatio": row.get("mgnRatio"),
        }
        for d in row.get("details", []) or []:
            if d.get("ccy") == "USDT":
                out["usdt_eq"] = d.get("eq")
                out["usdt_availEq"] = d.get("availEq")
                out["usdt_availBal"] = d.get("availBal")
                out["usdt_frozenBal"] = d.get("frozenBal")
                break
        return out

    def _public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Any]:
        """公开行情接口（无需签名）：用于 K 线等。"""
        params = params or {}
        items = sorted(params.items(), key=lambda kv: kv[0])
        query = "&".join([f"{k}={v}" for k, v in items])
        request_path = f"{path}?{query}" if params else path
        url = OKX_BASE_URL + request_path
        timeout = self.cfg.okx_timeout
        retries = max(1, self.cfg.okx_retries)
        resp = None
        last_exc: Optional[BaseException] = None
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=timeout)
                break
            except _RETRYABLE_EXC as e:
                last_exc = e
                if attempt + 1 >= retries:
                    raise RuntimeError(
                        f"OKX 公开接口网络失败（已重试 {retries} 次）：{e!r}"
                    ) from e
                time.sleep(0.5 * (attempt + 1))
        if resp is None:
            raise RuntimeError(f"OKX 公开接口无响应：{last_exc!r}") from last_exc
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:600]}")
        data = resp.json()
        if data.get("code") != "0":
            raise RuntimeError(f"OKX public API error: {data}")
        return data.get("data", []) or []

    def get_candles_for_analysis(self, inst_id: str, bar: str, limit: int) -> List[Dict[str, Any]]:
        """
        获取历史 K 线（时间升序：最旧→最新）。
        先 GET /api/v5/market/candles；若根数不足，再 GET /api/v5/market/history-candles 用 before 向前补。
        """
        limit = max(1, min(limit, 500))
        first = self._public_get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": bar, "limit": str(min(300, limit))},
        )
        rows = parse_okx_candle_rows(first)
        chrono = newest_first_to_oldest_first(rows)
        if not chrono:
            return []

        def _dedupe_sort(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            seen = set()
            out: List[Dict[str, Any]] = []
            for c in sorted(candles, key=lambda x: x["ts"]):
                if c["ts"] not in seen:
                    seen.add(c["ts"])
                    out.append(c)
            return out

        chrono = _dedupe_sort(chrono)
        while len(chrono) < limit:
            oldest_ts = chrono[0]["ts"]
            need = min(100, limit - len(chrono))
            more = self._public_get(
                "/api/v5/market/history-candles",
                {
                    "instId": inst_id,
                    "bar": bar,
                    "before": str(oldest_ts),
                    "limit": str(need),
                },
            )
            if not more:
                break
            older = newest_first_to_oldest_first(parse_okx_candle_rows(more))
            if not older:
                break
            chrono = _dedupe_sort(older + chrono)
            if len(more) < need:
                break

        if len(chrono) > limit:
            chrono = chrono[-limit:]
        return chrono


def parse_position(pos: Dict[str, Any]) -> Dict[str, Any]:
    if not pos:
        return {"exists": False}

    size = float(pos.get("pos", "0"))
    avg_px = float(pos.get("avgPx", "0") or 0)
    pos_side = pos.get("posSide", "net")
    return {
        "exists": abs(size) > 0,
        "size": size,
        "avg_px": avg_px,
        "pos_side": pos_side,
        "margin": pos.get("margin"),
        "notionalUsd": pos.get("notionalUsd"),
        "lever": pos.get("lever"),
        "upl": pos.get("upl"),
        "raw": pos,
    }


def _get_openai_client(cfg: BotConfig):
    """与 OKX 相同代理访问 AI（OpenAI 兼容接口），避免直连被地区拦截。"""
    global _OAI_CLIENT_CACHE
    if OpenAI is None:
        raise RuntimeError("openai package unavailable. Please install requirements.txt")

    sig = (
        cfg.openai_api_key,
        cfg.openai_base_url or "",
        cfg.https_proxy or "",
        cfg.http_proxy or "",
    )
    if _OAI_CLIENT_CACHE and _OAI_CLIENT_CACHE[0] == sig:
        return _OAI_CLIENT_CACHE[1]

    import httpx

    kwargs: Dict[str, Any] = {"api_key": cfg.openai_api_key}
    if cfg.openai_base_url:
        kwargs["base_url"] = cfg.openai_base_url.rstrip("/")
    proxy = cfg.https_proxy or cfg.http_proxy
    if proxy:
        kwargs["http_client"] = httpx.Client(proxy=proxy, timeout=60.0)
    client = OpenAI(**kwargs)
    _OAI_CLIENT_CACHE = (sig, client)
    return client


def _daily_risk_context(current_eq: Optional[float]) -> Dict[str, Any]:
    """
    记录 UTC 自然日开盘基准权益，计算当日盈亏%，用于「当日亏损超 5% 禁止开仓」。
    状态文件：equity_day_state.json（与 bot.py 同目录）
    """
    if current_eq is None or current_eq <= 0:
        return {
            "error": "no_equity",
            "block_new_orders_if_daily_loss_over_5pct": False,
        }
    path = _PROJECT_DIR / "equity_day_state.json"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state: Dict[str, Any] = {}
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    if state.get("date_utc") != today:
        state = {"date_utc": today, "eq_day_start": current_eq}
    elif "eq_day_start" not in state:
        state["eq_day_start"] = current_eq
    try:
        eq0 = float(state["eq_day_start"])
    except (TypeError, ValueError):
        eq0 = current_eq
        state["eq_day_start"] = current_eq
    daily_pnl_pct = (current_eq - eq0) / eq0 * 100.0 if eq0 else 0.0
    state["last_eq"] = current_eq
    state["last_daily_pnl_pct"] = daily_pnl_pct
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {
        "date_utc": today,
        "eq_day_start": eq0,
        "current_eq": current_eq,
        "daily_pnl_pct_vs_day_start": daily_pnl_pct,
        "block_new_orders_if_daily_loss_over_5pct": daily_pnl_pct <= -5.0,
    }


def build_strategy_inputs(
    client: OKXClient,
    cfg: BotConfig,
    last_price: float,
    position: Dict[str, Any],
) -> Dict[str, Any]:
    """
    拉取规则所需多周期 K 线 + 账户权益，供 AI 与 AI_USER_RULE 对齐使用。
    """
    inst = cfg.inst_id

    candles_1d = client.get_candles_for_analysis(inst, "1D", cfg.daily_candle_limit)
    closes_1d = [c["close"] for c in candles_1d]
    daily_ctx = daily_ma30_strategy_context(closes_1d)

    candles_4h = client.get_candles_for_analysis(inst, "4H", cfg.h4_candle_limit)
    ind_4h = compute_indicator_bundle(candles_4h)

    candles_15m = client.get_candles_for_analysis(inst, cfg.candle_bar, cfg.candle_limit)
    ind_primary = compute_indicator_bundle(candles_15m)

    candles_1h = client.get_candles_for_analysis(inst, "1H", cfg.h1_candle_limit)
    h48 = range_48h_from_1h(candles_1h)
    pullback = pullback_metrics_vs_48h(last_price, h48)

    amp_15m = amplitude_pct_recent(candles_15m, 20)
    amp_4h = amplitude_pct_recent(candles_4h, min(24, len(candles_4h))) if candles_4h else None
    # 从主周期（15m）K线提取收盘价序列，计算 EMA21/55
    closes_primary = [c["close"] for c in candles_15m] if candles_15m else []
    ema21_primary = ema_last(closes_primary, 21) if closes_primary else None
    ema55_primary = ema_last(closes_primary, 55) if closes_primary else None
    # 用“主周期最后一根K线收盘价”对齐回踩/反弹判断
    primary_last_close = closes_primary[-1] if closes_primary else None
    price_vs_ema21_pct = (
        ((primary_last_close - ema21_primary) / ema21_primary * 100.0)
        if ema21_primary and primary_last_close is not None
        else None
    )

    # 从1H K线提取收盘价序列，计算 EMA21/55
    closes_1h = [c["close"] for c in candles_1h] if candles_1h else []
    ema21_1h = ema_last(closes_1h, 21) if closes_1h else None
    ema55_1h = ema_last(closes_1h, 55) if closes_1h else None

    last_candle: Dict[str, Any] = {}
    if candles_15m:
        lc = candles_15m[-1]
        # AI 规则里需要 open/close（回踩/收阳收阴）
        last_candle = {
            "ts": lc.get("ts"),
            "open": lc.get("open"),
            "high": lc.get("high"),
            "low": lc.get("low"),
            "close": lc.get("close"),
        }

    eq_snap: Dict[str, Any] = {}
    try:
        eq_snap = client.get_usdt_equity_snapshot()
    except Exception as e:
        eq_snap = {"error": str(e)}

    current_eq: Optional[float] = None
    for key in ("usdt_eq", "adjEq", "totalEq"):
        v = eq_snap.get(key)
        if v is not None and v != "":
            try:
                current_eq = float(v)
                break
            except (TypeError, ValueError):
                continue

    daily_risk = _daily_risk_context(current_eq)

    notional = position.get("notionalUsd")
    try:
        nu = float(notional) if notional is not None else None
    except (TypeError, ValueError):
        nu = None
    exposure_hint = None
    if nu is not None and current_eq and current_eq > 0:
        exposure_hint = nu / current_eq

    return {
        "daily_1D": daily_ctx,
        "timeframe_4H": {"bar": "4H", **ind_4h},
        "timeframe_primary": {"bar": cfg.candle_bar, "indicators": ind_primary},
        "ema_primary": {
            "ema21": ema21_primary,
            "ema55": ema55_primary,
            "price_vs_ema21_pct": price_vs_ema21_pct,
        },
        "ema_1h": {
            "ema21": ema21_1h,
            "ema55": ema55_1h,
        },
        "last_candle": last_candle,
        "h48_from_1H": h48,
        "pullback_vs_48h": pullback,
        "sideways_amplitude": {
            "pct_recent_primary_20bars": amp_15m,
            "pct_recent_4h_up_to_24bars": amp_4h,
            "note": "横盘「振幅<5%」可用 pct 与日线 trend_class 对照",
        },
        "account_equity": eq_snap,
        "daily_pnl_rule": daily_risk,
        "position_for_risk_rules": position,
        "position_exposure_vs_equity": {
            "notional_usd": nu,
            "notional_div_equity": exposure_hint,
            "note": "用户规则：单次约 20% 权益开仓、总持仓约 50% 权益上限；以下为参考数值",
        },
    }


def simple_rule_decision(
    last_price: float,
    strategy_inputs: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    # 占位规则：演示用途。实际可结合 strategy_inputs 写规则。
    _ = (last_price, strategy_inputs)
    return {"action": "hold", "reason": "default-safe-rule"}


def ai_decision(
    cfg: BotConfig,
    last_price: float,
    position: Dict[str, Any],
    strategy_inputs: Dict[str, Any],
) -> Dict[str, str]:
    if not cfg.use_ai:
        return simple_rule_decision(last_price, strategy_inputs)
    if not cfg.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is empty but USE_AI=true")

    client = _get_openai_client(cfg)
    system_prompt = (
        "你是合约交易执行器，只能输出JSON，不要任何解释。"
        "允许动作: open_long, open_short, close_long, close_short, hold。"
        "输出格式: {\"action\":\"...\",\"reason\":\"...\"}\n"
        "用户提供的数据中包含：\n"
        "- 日线趋势（daily_1D）\n"
        "- 4H 指标（timeframe_4H）\n"
        "- 主周期（如15m）指标（timeframe_primary）包含 SMA、RSI、MACD 等\n"
        "- EMA 数据（ema_primary: ema21, ema55, price_vs_ema21_pct）\n"
        "- 1H 周期 EMA（ema_1h）\n"
        "- 最后一根 K 线数据（last_candle: open, close）\n"
        "- 账户权益与风控信息（daily_pnl_rule）\n"
        "请严格依据这些数值判断是否符合用户规则，不要编造未给出的数值。"
    )
    user_prompt = {
        "inst_id": cfg.inst_id,
        "last_price": last_price,
        "position": position,
        "strategy_inputs": strategy_inputs,
        "risk_rules": {
            "take_profit_pct": cfg.take_profit_pct,
            "stop_loss_pct": cfg.stop_loss_pct,
            "user_rule": cfg.ai_user_rule or "",
        },
        "instruction": (
            "请根据上述 strategy_inputs 中的技术指标，结合 risk_rules.user_rule 中的交易规则，做出决策。\n"
            "注意：\n"
            "- 做多条件需要：价格在 EMA21 和 EMA55 之上（多头排列），RSI(14) 在 40-60 之间向上拐头，且 K 线回踩 EMA21 企稳收阳。\n"
            "- 做空条件需要：价格在 EMA21 和 EMA55 之下（空头排列），RSI(14) 从高位跌破 70 或向下，K 线反弹 EMA21 受阻收阴。\n"
            "- 判断“回踩”可参考 price_vs_ema21_pct 的绝对值小于 0.5%，并结合 K 线形态。\n"
            "- 所有数值均在 strategy_inputs 中提供，请直接使用。\n"
            "- reason 必须是中文短句，简述“符合/不符合”的关键指标点（例如：EMA21&EMA55多头、RSI拐头区间、触及EMA21并收阳/收阴等）。"
        ),
    }

    try:
        resp = client.chat.completions.create(
            model=cfg.ai_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
            ],
        )
    except Exception as e:
        err = str(e)
        if "unsupported_country" in err or "Country, region, or territory not supported" in err:
            raise RuntimeError(
                "AI 接口返回「当前地区不支持」(403)。处理方式：\n"
                "  1) 在 .env 里已配置 HTTPS_PROXY 时，脚本会为 AI 请求走同一代理；若仍 403，请换可用节点。\n"
                "  2) 换用你所在地区可用的兼容接口：设置 OPENAI_BASE_URL（如国内可用的中转或 DeepSeek 等）。\n"
                "  3) 暂时关闭 AI：USE_AI=false，仅用内置规则。\n"
                f"原始错误：{err}"
            ) from e
        raise

    content = resp.choices[0].message.content or "{}"
    obj = json.loads(content)
    action = obj.get("action", "hold")
    reason = obj.get("reason", "")
    return {"action": action, "reason": reason}


def format_decision_output(
    action: str,
    last_price: float,
    cfg: BotConfig,
    reason: str,
) -> str:
    if action in ("open_long", "close_long"):
        signal_state = "买入做多"
    elif action in ("open_short", "close_short"):
        signal_state = "卖出做空"
    else:
        signal_state = "观望"

    # 即使是观望，也给出“以当前价为开仓价”的假设区间，便于你快速对照
    entry_price = last_price
    if action in ("open_long", "close_long"):
        sl = entry_price * (1.0 - cfg.stop_loss_pct)
        tp = entry_price * (1.0 + cfg.take_profit_pct)
    elif action in ("open_short", "close_short"):
        sl = entry_price * (1.0 + cfg.stop_loss_pct)
        tp = entry_price * (1.0 - cfg.take_profit_pct)
    else:
        # 观望：默认按“做多”计算一组假设区间
        sl = entry_price * (1.0 - cfg.stop_loss_pct)
        tp = entry_price * (1.0 + cfg.take_profit_pct)

    # 避免 AI reason 里带换行导致控制台展示破版
    reason_one_line = (reason or "").replace("\n", " ").replace("\r", " ").strip()

    return (
        f"【信号状态】: {signal_state}\n"
        f"【开仓价格】: ${entry_price:.2f}\n"
        f"【止损价格】: ${sl:.2f}\n"
        f"【止盈价格】: ${tp:.2f}\n"
        f"【操作理由】: {reason_one_line}"
    )


def risk_override(position: Dict[str, Any], last_price: float, tp: float, sl: float) -> Optional[Dict[str, str]]:
    if not position.get("exists"):
        return None

    avg_px = position["avg_px"]
    pos_side = position["pos_side"]
    if avg_px <= 0:
        return None

    if pos_side == "long":
        pnl_pct = (last_price - avg_px) / avg_px
        if pnl_pct >= tp:
            return {"action": "close_long", "reason": "take-profit"}
        if pnl_pct <= -sl:
            return {"action": "close_long", "reason": "stop-loss"}
    elif pos_side == "short":
        pnl_pct = (avg_px - last_price) / avg_px
        if pnl_pct >= tp:
            return {"action": "close_short", "reason": "take-profit"}
        if pnl_pct <= -sl:
            return {"action": "close_short", "reason": "stop-loss"}
    return None


# ========== 新增：动态计算开仓张数 ==========
def _calc_lot_size(client: OKXClient, cfg: BotConfig, percent: float) -> Optional[str]:
    try:
        equity_data = client.get_usdt_equity_snapshot()
        equity = float(equity_data.get('totalEq') or equity_data.get('usdt_eq', 0))
        if equity <= 0:
            print(f"[ERROR] 账户权益为 {equity}，无法计算开仓数量")
            return None

        ticker = client.get_ticker(cfg.inst_id)
        price = float(ticker['last'])
        if price <= 0:
            print(f"[ERROR] 当前价格无效 {price}")
            return None

        lever = int(cfg.lever)

        # 根据交易对设置正确的合约面值
        if cfg.inst_id == "BTC-USDT-SWAP":
            contract_val = 0.001  # 0.001 BTC/张
        elif cfg.inst_id == "SOL-USDT-SWAP":
            contract_val = 1  # 1 SOL/张
        else:
            # 默认兜底：用 1 USDT/张（适用于多数 USDT 合约）
            contract_val = 1

        margin = equity * percent
        position_value = margin * lever
        lots = position_value / (price * contract_val)

        # BTC 强制整数，SOL 支持小数
        if cfg.inst_id == "BTC-USDT-SWAP":
            lots_final = max(1, int(lots))  # 最小 1 张
            return str(lots_final)
        else:
            lots_rounded = round(lots * 100) / 100
            if lots_rounded < 0.01:
                print(f"[WARN] 计算张数 {lots_rounded} 小于最小开仓 0.01 张")
                return None
            return f"{lots_rounded:.2f}"
    except Exception as e:
        print(f"[ERROR] 计算开仓张数失败: {e}")
        return None
def execute_action(client: OKXClient, cfg: BotConfig, action: str, position: Dict[str, Any]):
    """执行交易动作（开仓/平仓/hold）"""
    if action == "open_long":
        order_size_raw = cfg.order_size.strip()
        if order_size_raw.endswith('%'):
            percent = float(order_size_raw[:-1]) / 100.0
            size = _calc_lot_size(client, cfg, percent)
        else:
            size = order_size_raw  # 固定张数，直接使用
        if size is None:
            print("[WARN] 无法计算开仓数量，跳过开多")
            return
        if cfg.dry_run:
            print(f"[DRY_RUN] open long size={size}")
        else:
            client.place_market_order(cfg.inst_id, cfg.td_mode, side="buy", pos_side="long", size=size)

    elif action == "open_short":
        order_size_raw = cfg.order_size.strip()
        if order_size_raw.endswith('%'):
            percent = float(order_size_raw[:-1]) / 100.0
            size = _calc_lot_size(client, cfg, percent)
        else:
            size = order_size_raw
        if size is None:
            print("[WARN] 无法计算开仓数量，跳过开空")
            return
        if cfg.dry_run:
            print(f"[DRY_RUN] open short size={size}")
        else:
            client.place_market_order(cfg.inst_id, cfg.td_mode, side="sell", pos_side="short", size=size)

    elif action == "close_long":
        pos_size = abs(position.get('size', 0))
        if pos_size <= 0:
            print("[WARN] 无持仓，无法平多")
            return
        size = str(pos_size)
        if cfg.dry_run:
            print(f"[DRY_RUN] close long size={size}")
        else:
            client.place_market_order(cfg.inst_id, cfg.td_mode, side="sell", pos_side="long", size=size)

    elif action == "close_short":
        pos_size = abs(position.get('size', 0))
        if pos_size <= 0:
            print("[WARN] 无持仓，无法平空")
            return
        size = str(pos_size)
        if cfg.dry_run:
            print(f"[DRY_RUN] close short size={size}")
        else:
            client.place_market_order(cfg.inst_id, cfg.td_mode, side="buy", pos_side="short", size=size)

    elif action == "hold":
        print("[INFO] hold")
    else:
        print(f"[WARN] unknown action: {action}")


def load_config() -> BotConfig:
    load_dotenv(dotenv_path=_ENV_FILE, override=False)
    http_proxy = (os.getenv("HTTP_PROXY") or "").strip() or None
    https_proxy = (os.getenv("HTTPS_PROXY") or "").strip() or None
    # 只配 HTTPS 代理时，HTTP 也走同一出口（部分环境需要）
    if https_proxy and not http_proxy:
        http_proxy = https_proxy
    cfg = BotConfig(
        api_key=_clean_secret_str(os.getenv("OKX_API_KEY", "")),
        api_secret=_clean_secret_str(os.getenv("OKX_API_SECRET", "")),
        passphrase=_clean_secret_str(os.getenv("OKX_PASSPHRASE", "")),
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        simulated=str_to_bool(os.getenv("OKX_SIMULATED", "true"), True),
        inst_id=os.getenv("INST_ID", "BTC-USDT-SWAP"),
        td_mode=os.getenv("TD_MODE", "isolated"),
        lever=os.getenv("LEVER", "5"),
        order_size=os.getenv("ORDER_SIZE", "0.01"),
        check_interval_sec=int(os.getenv("CHECK_INTERVAL_SEC", "30")),
        dry_run=str_to_bool(os.getenv("DRY_RUN", "true"), True),
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.015")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.008")),
        use_ai=str_to_bool(os.getenv("USE_AI", "false"), False),
        ai_model=os.getenv("AI_MODEL", "gpt-4o-mini"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=(os.getenv("OPENAI_BASE_URL") or "").strip() or None,
        ai_user_rule=os.getenv("AI_USER_RULE", ""),
        okx_timeout=float(os.getenv("OKX_TIMEOUT", "30")),
        okx_retries=int(os.getenv("OKX_RETRIES", "4")),
        candle_bar=(os.getenv("CANDLE_BAR") or "15m").strip(),
        candle_limit=int(os.getenv("CANDLE_LIMIT", "200")),
        daily_candle_limit=int(os.getenv("DAILY_CANDLE_LIMIT", "45")),
        h4_candle_limit=int(os.getenv("H4_CANDLE_LIMIT", "120")),
        h1_candle_limit=int(os.getenv("H1_CANDLE_LIMIT", "60")),
    )
    return cfg


def check_required(cfg: BotConfig):
    missing = []
    if not cfg.api_key:
        missing.append("OKX_API_KEY")
    if not cfg.api_secret:
        missing.append("OKX_API_SECRET")
    if not cfg.passphrase:
        missing.append("OKX_PASSPHRASE")
    if missing:
        hint = (
            f"\n\n请在下面路径创建或编辑 .env 文件，并填入欧易 API：\n"
            f"  {_ENV_FILE}\n\n"
            f"在 PowerShell 中进入项目目录后执行：copy .env.example .env\n"
            f"注意：文件名必须是 .env（不要变成 .env.txt）\n"
            f"缺少变量：{', '.join(missing)}"
        )
        raise RuntimeError(hint)


def main():
    cfg = load_config()
    check_required(cfg)
    client = OKXClient(cfg)

    proxy_hint = ""
    if cfg.https_proxy or cfg.http_proxy:
        proxy_hint = f" proxy=https={cfg.https_proxy!r} http={cfg.http_proxy!r}"
    else:
        proxy_hint = " proxy=none"

    print(
        f"[BOOT] inst={cfg.inst_id} simulated={cfg.simulated} dry_run={cfg.dry_run} "
        f"ai={cfg.use_ai} interval={cfg.check_interval_sec}s "
        f"tf_primary={cfg.candle_bar}x{cfg.candle_limit} "
        f"1D={cfg.daily_candle_limit} 4H={cfg.h4_candle_limit} 1H={cfg.h1_candle_limit}{proxy_hint}"
    )
    if not cfg.dry_run:
        client.set_leverage(cfg.inst_id, cfg.lever, cfg.td_mode)
        print(f"[BOOT] leverage set to {cfg.lever}x")

    while True:
        try:
            ticker = client.get_ticker(cfg.inst_id)
            last_price = float(ticker["last"])
            position_raw = client.get_positions(cfg.inst_id)
            position = parse_position(position_raw)

            strategy_inputs = round_floats(
                build_strategy_inputs(client, cfg, last_price, position)
            )

            forced = risk_override(position, last_price, cfg.take_profit_pct, cfg.stop_loss_pct)
            if forced:
                decision = forced
            else:
                decision = ai_decision(cfg, last_price, position, strategy_inputs)

            action = decision.get("action", "hold")
            reason = decision.get("reason", "")
            print(format_decision_output(action, last_price, cfg, reason))
            # 传递 position 给 execute_action
            execute_action(client, cfg, action, position)
        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(cfg.check_interval_sec)


if __name__ == "__main__":
    main()