# MT5 Auto Trading Bot Dashboard - local bridge + engine + web UI
# Requires: python -m pip install MetaTrader5
# Terminal MetaTrader 5 (broker apapun, demo/live) harus jalan & sudah login.
import json
import logging
import os
import threading
import time

import numpy as np
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import MetaTrader5 as mt5
    MT5_OK = True
except ImportError:
    mt5 = None
    MT5_OK = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8940
MAGIC = 972025
LOCK = threading.RLock()
LOG = logging.getLogger("mt5bot")

DEFAULT_CONFIG = {
    "symbol": "XAUUSD",
    "direction": "AUTO",       # AUTO (ikut trend EMA H1) | BUY | SELL
    "initial_lot": 0.01,
    "lot_multiplier": 1.0,     # 1.0 = flat, 1.5 = martingale ringan
    "max_layers": 50,          # input user #1
    "basket_target": 5.0,      # input user #2, USD, default $5
    "max_basket_loss": 0.0,    # safety statis: close kalau floating <= -nilai ini (0 = mati)
    "spike_drop": 50.0,        # safety spike: close kalau floating turun secepat ini (USD) ...
    "spike_window_sec": 10.0,  # ... dalam jendela detik ini
    "layer_step_points": 300,  # jarak tambah layer (points)
    "point_value_fix": None,   # optional override
}

state = {
    "mt5_available": MT5_OK,
    "connected": False,
    "error": "" if MT5_OK else "Paket MetaTrader5 belum terinstall (python -m pip install MetaTrader5)",
    "running": False,
    "trend": None,
    "account": {},
    "positions": [],
    "basket": {"count": 0, "volume": 0.0, "pnl": 0.0, "layers": 0, "last_entry": None, "next_layer_price": None},
    "cycles": 0,
    "history": [],
    "pnl_hist": [],
    "log": [],
    "server_time": time.time(),
}

config = dict(DEFAULT_CONFIG)


def load_config():
    global config
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k in DEFAULT_CONFIG:
                if k in saved:
                    config[k] = saved[k]
        except Exception as e:
            LOG.warning("config.json rusak, pakai default: %s", e)


def save_config():
    with LOCK:
        path = os.path.join(BASE_DIR, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)


def log(msg):
    with LOCK:
        state["log"].append({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": str(msg)})
        if len(state["log"]) > 200:
            state["log"] = state["log"][-200:]
    LOG.info(msg)


def mt5_connect():
    with LOCK:
        if not MT5_OK:
            state["connected"] = False
            return False
        if not mt5.initialize():
            state["connected"] = False
            state["error"] = (
                "Terminal MT5 tidak terdeteksi. Pastikan terminal jalan & sudah login "
                "(error %s)" % mt5.last_error()
            )
            return False
        info = mt5.account_info()
        if info is None:
            state["connected"] = False
            state["error"] = "Terminal belum login ke akun manapun. Login dulu di terminal."
            return False
        state["connected"] = True
        state["error"] = ""
        state["account"] = {
            "login": info.login,
            "server": info.server,
            "currency": info.currency,
            "balance": round(info.balance, 2),
            "equity": round(info.equity, 2),
            "margin": round(info.margin, 2),
            "free_margin": round(info.margin_free, 2),
            "is_demo": {"DEMO": True, "CONTEST": True}.get(info.trade_mode_name(), False)
            if hasattr(info, "trade_mode_name") else (info.trade_mode == 0),
        }
        return True


def send_order(symbol, direction, volume, price_point):
    """Market order with filling-mode fallback. Returns (ok, message)."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False, "Tidak ada tick untuk %s" % symbol
    is_buy = direction == "BUY"
    price = tick.ask if is_buy else tick.bid
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "deviation": 20,
        "magic": MAGIC,
        "comment": "layerbot",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    for fill in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
        req["type_filling"] = fill
        r = mt5.order_send(req)
        if r is None:
            continue
        if r.retcode == mt5.TRADE_RETCODE_DONE:
            return True, "OK ticket=%s fill=%s" % (r.order, fill)
        if r.retcode != 10030:  # bukan masalah filling mode
            return False, "retcode=%s %s" % (r.retcode, r.comment)
    return False, "order_send gagal (semua filling mode)"


def close_position(pos, symbol):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False, "no tick"
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": pos.ticket,
        "price": tick.bid if is_buy else tick.ask,
        "deviation": 20,
        "magic": MAGIC,
        "comment": "closebasket",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    for fill in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
        req["type_filling"] = fill
        r = mt5.order_send(req)
        if r is None:
            continue
        if r.retcode == mt5.TRADE_RETCODE_DONE:
            return True, "closed=%s" % pos.ticket
        if r.retcode != 10030:
            return False, "retcode=%s %s" % (r.retcode, r.comment)
    return False, "close gagal (semua filling mode)"


def ema_final(values, period):
    a = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = a * v + (1 - a) * e
    return e


def detect_trend(symbol):
    """Trend dari EMA14 vs EMA50 di H1: fast > slow = BUY, else SELL."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 80)
    if rates is None or len(rates) < 60:
        return None, None, None
    closes = np.array([r["close"] for r in rates], dtype=float)
    fast = ema_final(closes[-60:], 14)
    slow = ema_final(closes[-60:], 50)
    return ("BUY" if fast > slow else "SELL"), fast, slow


def normalize_volume(symbol, volume):
    si = mt5.symbol_info(symbol)
    if si is None:
        return round(volume, 2)
    v = max(si.volume_min, min(si.volume_max, volume))
    step = si.volume_step or 0.01
    v = round(round(v / step) * step, 2)
    return max(si.volume_min, v)


def record_history(eff, poss, pnl, note=""):
    """Catat satu basket yang selesai di-close ke riwayat."""
    with LOCK:
        state["history"].insert(0, {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": config["symbol"],
            "dir": eff,
            "layers": len(poss),
            "volume": round(sum(p.volume for p in poss), 2),
            "pnl": round(pnl, 2),
            "note": note,
        })
        if len(state["history"]) > 50:
            state["history"] = state["history"][:50]


def get_positions(symbol):
    if not MT5_OK:
        return []
    poss = mt5.positions_get(symbol=symbol) or []
    return [p for p in poss if p.magic == MAGIC]


def close_basket(poss, symbol, eff, pnl, note):
    """Close semua posisi basket, catat riwayat. Return True kalau semua sukses."""
    ok_all = True
    for p in list(poss):
        ok, msg = close_position(p, symbol)
        if ok:
            log("Close %s" % msg)
        else:
            ok_all = False
            log("FAIL close %s: %s" % (p.ticket, msg))
    record_history(eff, poss, pnl, note=note)
    return ok_all


def engine_tick():
    """One engine step. Runs every ~0.5s while running."""
    with LOCK:
        run = state["running"]
        cfg = dict(config)
    if not run:
        return
    symbol = cfg["symbol"]

    si = mt5.symbol_info(symbol)
    if si is None:
        if not mt5.symbol_select(symbol, True):
            log("WARN: symbol %s tidak ditemukan di Market Watch" % symbol)
            return
        si = mt5.symbol_info(symbol)
        if si is None:
            log("WARN: symbol %s gagal diambil" % symbol)
            return
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    point = si.point or 0.00001
    poss = get_positions(symbol)

    # --- tentukan arah siklus: manual, atau AUTO ikut trend EMA H1 ---
    if cfg["direction"] in ("BUY", "SELL"):
        eff = cfg["direction"]
        trend = {"mode": "manual", "dir": eff, "fast": None, "slow": None}
    elif poss:
        # arah siklus AUTO terkunci mengikuti posisi yang sudah ada
        eff = "BUY" if poss[0].type == mt5.POSITION_TYPE_BUY else "SELL"
        prev = state.get("trend") or {}
        trend = {"mode": "auto (terkunci)", "dir": eff,
                 "fast": prev.get("fast"), "slow": prev.get("slow")}
    else:
        eff, fast, slow = detect_trend(symbol)
        if eff is None:
            log("WARN: data H1 kurang untuk deteksi trend, entry ditunda")
            return
        trend = {"mode": "auto", "dir": eff,
                 "fast": round(fast, si.digits), "slow": round(slow, si.digits)}
    is_buy = eff == "BUY"
    price = tick.bid if is_buy else tick.ask  # harga yang relevan utk layer arah melawan
    with LOCK:
        state["trend"] = trend

    # --- Exit: basket PnL >= target -> close all ---
    if poss:
        pnl = sum(p.profit + p.swap for p in poss)  # float PnL basket
        now = time.time()
        with LOCK:
            state["pnl_hist"].append((now, pnl))
            win = max(5.0, float(cfg.get("spike_window_sec") or 10))
            state["pnl_hist"] = [x for x in state["pnl_hist"] if now - x[0] <= max(win, 30)]
            recent = [v for t, v in state["pnl_hist"] if now - t <= win]

        if cfg["basket_target"] > 0 and pnl >= cfg["basket_target"]:
            log("TARGET tercapai: basket PnL $%.2f >= $%.2f -> close all %d posisi"
                % (pnl, cfg["basket_target"], len(poss)))
            close_basket(poss, symbol, eff, pnl, note="")
            with LOCK:
                state["cycles"] += 1
                state["pnl_hist"] = []
            time.sleep(1)
            return  # siklus baru mulai di tick berikutnya

        # --- Safety statis (opsional): floating <= -limit -> close + stop ---
        loss_limit = abs(cfg.get("max_basket_loss") or 0)
        if loss_limit > 0 and pnl <= -loss_limit:
            log("SAFETY STOP: floating $%.2f <= -$%.2f -> close all %d posisi, BOT DIHENTIKAN"
                % (pnl, loss_limit, len(poss)))
            close_basket(poss, symbol, eff, pnl, note="stop-loss")
            with LOCK:
                state["running"] = False
                state["pnl_hist"] = []
            time.sleep(1)
            return

        # --- Safety spike: floating jatuh cepat dalam jendela singkat ---
        spike_drop = abs(cfg.get("spike_drop") or 0)
        if spike_drop > 0 and recent and (max(recent) - pnl) >= spike_drop:
            log("SPIKE terdeteksi: floating turun $%.2f dalam <=%.0fs (peak $%.2f -> $%.2f) -> close all + STOP"
                % (max(recent) - pnl, win, max(recent), pnl))
            close_basket(poss, symbol, eff, pnl, note="spike")
            with LOCK:
                state["running"] = False
                state["pnl_hist"] = []
            time.sleep(1)
            return

        # --- Entry layering ---
        # Entry terakhir = entry TERBARU (paling jauh melawan harga):
        # BUY ladder turun -> ambil min; SELL ladder naik -> ambil max.
        entries = [p.price_open for p in poss]
        last_entry = min(entries) if is_buy else max(entries)
        step = cfg["layer_step_points"] * point
        layers_left = cfg["max_layers"] - len(poss)
        trigger = (price <= last_entry - step) if is_buy else (price >= last_entry + step)
        with LOCK:
            b = state["basket"]
            b["layers"] = len(poss)
            b["last_entry"] = last_entry
        if layers_left > 0 and trigger:
            new_layer = len(poss) + 1
            vol = normalize_volume(symbol, cfg["initial_lot"] * (cfg["lot_multiplier"] ** (new_layer - 1)))
            ok, msg = send_order(symbol, eff, vol, point)
            if ok:
                log("LAYER %d/%d open %s %.2f lot %s" % (new_layer, cfg["max_layers"], eff, vol, symbol))
            else:
                log("FAIL layer %d: %s" % (new_layer, msg))
        elif layers_left > 0:
            nxt = (last_entry - step) if is_buy else (last_entry + step)
            with LOCK:
                state["basket"]["next_layer_price"] = round(nxt, si.digits)
    else:
        # --- Auto-entry: bot nyala & kosong -> buka posisi pertama ---
        with LOCK:
            state["pnl_hist"] = []
        vol = normalize_volume(symbol, cfg["initial_lot"])
        if trend["mode"].startswith("auto"):
            log("TREND: %s (EMA14 %s vs EMA50 %s, H1)" % (trend["dir"], trend["fast"], trend["slow"]))
        ok, msg = send_order(symbol, eff, vol, point)
        if ok:
            log("ENTRY pertama %s %.2f lot %s" % (eff, vol, symbol))
            with LOCK:
                state["basket"]["next_layer_price"] = None
        else:
            log("FAIL entry pertama: %s" % msg)
            time.sleep(2)


def refresh_state():
    with LOCK:
        symbol = config["symbol"]
    if not MT5_OK:
        return
    account = mt5.account_info()
    if account is not None:
        with LOCK:
            is_demo = account.trade_mode == 0  # 0 = demo
            state["account"] = {
                "login": account.login,
                "server": account.server,
                "currency": account.currency,
                "balance": round(account.balance, 2),
                "equity": round(account.equity, 2),
                "margin": round(account.margin, 2),
                "free_margin": round(account.margin_free, 2),
                "is_demo": is_demo,
            }
    poss = get_positions(symbol)
    pos_view = []
    for p in sorted(poss, key=lambda x: x.ticket):
        pos_view.append({
            "ticket": p.ticket,
            "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
            "volume": p.volume,
            "entry": round(p.price_open, 5),
            "current": round(p.price_current, 5),
            "profit": round(p.profit + p.swap, 2),
        })
    with LOCK:
        state["positions"] = pos_view
        basket_pnl = sum(v["profit"] for v in pos_view)
        b = state["basket"]
        b["count"] = len(pos_view)
        b["volume"] = round(sum(v["volume"] for v in pos_view), 2)
        b["pnl"] = round(basket_pnl, 2)


def engine_loop():
    while True:
        try:
            if not mt5_connect():
                time.sleep(3)
                continue
            refresh_state()
            engine_tick()
        except Exception as e:
            log("ERROR engine: %s" % e)
            time.sleep(3)
        time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # senyapkan access log
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            try:
                with open(os.path.join(BASE_DIR, "dashboard.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, b"dashboard.html hilang", "text/plain")
        elif path == "/api/state":
            with LOCK:
                snap = dict(state)
                snap["config"] = dict(config)
                snap["server_time"] = time.time()
            self._send(200, snap)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "json tidak valid"})
        path = self.path.split("?")[0]
        if path == "/api/config":
            with LOCK:
                for k in DEFAULT_CONFIG:
                    if k in body:
                        try:
                            config[k] = type(DEFAULT_CONFIG[k])(body[k]) if body[k] is not None else config[k]
                        except (TypeError, ValueError):
                            pass
                cfg = dict(config)
            save_config()
            log("Config diubah: symbol=%s %s lot0=%.2f x%.2f max=%d target=$%.2f step=%d"
                % (cfg["symbol"], cfg["direction"], cfg["initial_lot"], cfg["lot_multiplier"],
                   cfg["max_layers"], cfg["basket_target"], cfg["layer_step_points"]))
            return self._send(200, {"ok": True, "config": cfg})
        if path == "/api/action":
            act = body.get("action")
            if act == "start":
                with LOCK:
                    state["running"] = True
                log("BOT START")
                return self._send(200, {"ok": True})
            if act == "stop":
                with LOCK:
                    state["running"] = False
                log("BOT STOP (posisi dibiarkan jalan)")
                return self._send(200, {"ok": True})
            if act == "panic":
                with LOCK:
                    state["running"] = False
                    symbol = config["symbol"]
                poss = get_positions(symbol)
                for p in poss:
                    ok, msg = close_position(p, symbol)
                    log("PANIC %s %s" % ("OK" if ok else "FAIL", msg))
                if poss:
                    pnl = sum(p.profit + p.swap for p in poss)  # float PnL basket
                    eff0 = "BUY" if poss[0].type == mt5.POSITION_TYPE_BUY else "SELL"
                    record_history(eff0, poss, pnl, note="panic")
                log("PANIC CLOSE ALL (%d posisi)" % len(poss))
                return self._send(200, {"ok": True})
            return self._send(400, {"error": "action tidak dikenal"})
        return self._send(404, {"error": "not found"})


def main():
    load_config()
    t = threading.Thread(target=engine_loop, daemon=True)
    t.start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log("Dashboard: http://localhost:%s" % PORT)
    print("MT5 Bot Dashboard -> http://localhost:%s  (Ctrl+C untuk stop)" % PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
