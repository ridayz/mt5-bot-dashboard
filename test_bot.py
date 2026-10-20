# Unit-test logika engine MT5 bot pakai mock MetaTrader5 (tanpa terminal MT5).
# Menyiapkan: exit target basket (lock profit), NO SL/NO CL, layering, aksi tp_basket.
import io
import json, os, sys, threading, time, types, importlib, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---- Mock modul MetaTrader5 ----
class Pos:
    def __init__(self, ticket, ptype, vol, price_open, profit):
        self.ticket=ticket; self.type=ptype; self.volume=vol
        self.price_open=price_open; self.price_current=price_open; self.profit=profit; self.swap=0.0
        self.magic = 972025

class Tick:
    def __init__(s, bid, ask): s.bid=bid; s.ask=ask

def make_mock(price=2000.0, target_profit=999.0):
    closed = []
    positions = [Pos(1, 0, 0.01, price, target_profit), Pos(2, 0, 0.01, price, target_profit/2)]
    m = types.ModuleType("MetaTrader5")
    m.POSITION_TYPE_BUY=0; m.POSITION_TYPE_SELL=1
    m.ORDER_TYPE_BUY=0; m.ORDER_TYPE_SELL=1
    m.ORDER_FILLING_FOK=0; m.ORDER_FILLING_IOC=1; m.ORDER_FILLING_RETURN=2
    m.TIMEFRAME_H1=1; m.ORDER_TIME_GTC=0; m.TRADE_ACTION_DEAL=1
    m.TRADE_RETCODE_DONE=10009
    class R: retcode=10009; order=5; comment="done"
    def initialize(): return True
    def last_error(): return (0,"")
    def account_info():
        class I: pass
        i=I(); i.login=12345; i.server="Demo-Svr"; i.currency="USD"
        i.balance=10000; i.equity=10000+target_profit; i.margin=0; i.margin_free=10000
        return i
    def symbol_info(sym):
        class S: pass
        s=S(); s.digits=2; s.point=0.01; s.volume_min=0.01; s.volume_max=100; s.volume_step=0.01
        return s
    def symbol_select(sym, v): return True
    def symbol_info_tick(sym): return Tick(price, price+0.2)
    def positions_get(**kw):
        if "ticket" in kw:
            return next((p for p in positions if p.ticket==kw["ticket"]), None)
        return list(positions)
    def copy_rates_from_pos(sym, tf, start, n):
        return [ {"close": price} for _ in range(n) ]
    def order_send(req):
        if req.get("type") in (m.ORDER_TYPE_BUY, m.ORDER_TYPE_SELL) and "position" not in req:
            new = len(positions)+1
            positions.append(Pos(new, req["type"], req["volume"], Tick(price,price+0.2).ask, 0.0))
            return R()
        # close
        for p in list(positions):
            if p.ticket==req.get("position"):
                positions.remove(p); closed.append(p.ticket)
        return R()
    m.initialize=initialize; m.last_error=last_error; m.account_info=account_info
    m.symbol_info=symbol_info; m.symbol_select=symbol_select; m.symbol_info_tick=symbol_info_tick
    m.positions_get=positions_get; m.copy_rates_from_pos=copy_rates_from_pos; m.order_send=order_send
    m.__dict__ .update({"initialize":initialize,"last_error":last_error,"account_info":account_info,
        "symbol_info":symbol_info,"symbol_select":symbol_select,"symbol_info_tick":symbol_info_tick,
        "positions_get":positions_get,"copy_rates_from_pos":copy_rates_from_pos,"order_send":order_send})
    return m, positions, closed

import io
def test():
    # install mock before import bot
    m, positions, closed = make_mock()
    sys.modules["MetaTrader5"] = m
    if "bot" in sys.modules: del sys.modules["bot"]
    bot = importlib.import_module("bot")
    bot.load_config()

    print("== TEST 1: exit target basket (lock profit) ==")
    bot.state["running"] = True
    bot.config["basket_target"] = 10.0
    bot.engine_tick()   # tick pertama: poss ada, pnl >= target -> close semua
    print("closed tickets:", closed, "| cycles:", bot.state["cycles"])
    assert closed, "harus close semua posisi saat target tercapai (NO SL NO CL)"
    assert bot.state["cycles"] >= 1, "siklus harus bertambah"

    import io
    print("== TEST 2: aksi tp_basket (lock profit manual) ==")
    importlib.reload(bot)  # reset state
    bot.load_config()
    m2, pos2, closed2 = make_mock()
    sys.modules["MetaTrader5"] = m2
    importlib.reload(bot)
    bot.load_config()
    # panggil handler langsung untuk aksi tp_basket
    class H(bot.Handler):
        def log_message(self, *a): pass
    h = H.__new__(H)
    h.path = "/api/action"
    h.rfile = io.BytesIO(json.dumps({"action":"tp_basket"}).encode())
    h.wfile = io.BytesIO()
    h.headers = {"Content-Length": len(json.dumps({"action":"tp_basket"}))}
    h.command = "POST"
    h.requestline = "POST /api/action HTTP/1.1"
    h.request_version = "HTTP/1.1"
    h.response_version = "HTTP/1.1"
    def _send_stub(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.wfile.write(data)
    bot.Handler._send = _send_stub
    h.do_POST()
    out = json.loads(h.wfile.getvalue().decode())
    print("tp_basket resp:", out)
    assert out.get("ok") is True and out.get("closed") == 2, "tp_basket harus close semua posisi"

    print("== TEST 3: layer bebas input (max_layers & step) ==")
    bot.config["max_layers"] = 5
    bot.config["basket_target"] = 0   # matikan exit target
    bot.config["layer_step_points"] = 100
    bot.engine_tick()
    print("layer sekarang:", bot.state["basket"]["layers"], "next:", bot.state["basket"]["next_layer_price"])

    print("\nSEMUA TEST LULUS")

if __name__ == "__main__":
    test()
