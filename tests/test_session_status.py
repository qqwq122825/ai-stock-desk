"""Driving dashboard state and restart notification behavior (offline)."""
from types import SimpleNamespace
from unittest.mock import patch

from test_core import Base, SENT, db, market, notify
from core import autopilot, engine


class TestSessionStatus(Base):
    def at(self, hour, minute):
        return market.datetime(2026, 11, 5, hour, minute, tzinfo=market.CN_TZ)

    def record(self, now, ok):
        db.run("INSERT INTO messages(ts,title,content,channels,ok) VALUES(?,?,?,?,?)",
               (now.timestamp(), "盘前外盘：测试", "", "", int(ok)))

    def test_dashboard_distinguishes_lunch_and_unconfirmed_brief(self):
        self.record(self.at(9, 2), False)
        mon = SimpleNamespace(status={"market_open": False})
        with patch.object(market, "now", return_value=self.at(11, 45)):
            st = autopilot.status(mon)
            self.assertEqual(st["steps"][2]["note"], "午休，13:00 继续")
            self.assertEqual(st["steps"][0]["note"], "未记录成功推送")
            self.assertNotEqual(st["steps"][0]["state"], "done")
            self.record(self.at(9, 5), True)
            self.assertEqual(autopilot.status(mon)["steps"][0]["note"], "已推送 09:05")

    def test_session_boundaries_and_holiday(self):
        for hh, mm, opened, trading, note, state in [
            (9, 15, False, True, "9:30 开盘后开始", "wait"),
            (9, 30, True, True, "", "run"),
            (11, 30, True, True, "午休，13:00 继续", "wait"),
            (13, 0, True, True, "", "run"),
            (15, 0, True, True, "今天已收盘", "done"),
            (10, 0, False, False, "今天休市", "off"),
        ]:
            with self.subTest(hh=hh, mm=mm, trading=trading), \
                 patch.object(market, "now", return_value=self.at(hh, mm)), \
                 patch.object(market, "is_trading_day", return_value=trading):
                st = autopilot.status(SimpleNamespace(status={"market_open": opened}))
                self.assertEqual(st["phase_note"], note)
                self.assertEqual(st["steps"][2]["state"], state)

    def test_restart_skips_success_but_not_failed_delivery(self):
        now = self.at(9, 10)
        self.record(self.at(9, 1), False)
        fake_send = notify.send
        def logged_send(title, content, **kw):
            self.record(now, True)
            return fake_send(title, content)
        with patch.object(notify, "send", side_effect=logged_send):
            engine.Monitor().premarket_brief(now)
            engine.Monitor().premarket_brief(now)
        self.assertEqual(len(SENT), 1)
