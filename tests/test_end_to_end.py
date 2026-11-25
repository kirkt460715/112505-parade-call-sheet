"""端到端叙事联调：巷口单向通行后的整套改线流程。

覆盖用户陈述的关键约束：
1. 场地改向后不能只平移街道，必须按可达性重新改排；
2. 同一资源不能许给两队（代次栅栏）；
3. 已执行部分保留，未开始部分可改；
4. 围栏裂脑时两边都不能自行推断开放，导游不得带队；
5. 断线后凭幂等回执恢复，对账找回各站执行的版本；
6. 替补只有在观众能实际参加时才算承诺兑现；
7. 改排失败时原站点继续可执行。
"""

import json
import threading
import unittest
from http.client import HTTPConnection

from rehearsal.gateway import OfflineReceiptClient
from rehearsal.server import build_server


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = build_server(0, "fixtures",
                                  receipt_client=OfflineReceiptClient("fixtures"))
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, payload=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            body = json.dumps(payload) if payload is not None else None
            conn.request(method, path, body,
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_full_story(self):
        # ---- 1. 事件流已摄入：plaza-yard 第 30 分钟起只出不进 ----
        status, segments = self.call("GET", "/v1/segments?minute=31")
        self.assertEqual(status, 200)
        target = next(s for s in segments["segments"]
                      if s["segment"] == "plaza-yard")
        self.assertEqual(target["direction"], "outbound")
        # yard 的游客团不能反向走出巷子
        status, route = self.call("GET",
                                  "/v1/route?from=yard&to=plaza&minute=31")
        self.assertFalse(route["ok"])

        # ---- 2. 两套改排同时借用 drum-a：先确认一套，另一套必须失效 ----
        _, plan_a = self.call("POST", "/v1/plans", {
            "minute": 40,
            "changes": [{"op": "move", "booking_id": "b3",
                         "stop": "bridge", "start": 60, "end": 70}]})
        _, plan_b = self.call("POST", "/v1/plans", {
            "minute": 41,
            "changes": [{"op": "move", "booking_id": "b3",
                         "stop": "bridge", "start": 62, "end": 72}]})
        status, sheet = self.call(
            "POST", f"/v1/plans/{plan_a['plan_id']}/confirm",
            {"minute": 42, "version": "board-2"})
        self.assertEqual(status, 200)
        status, rejected = self.call(
            "POST", f"/v1/plans/{plan_b['plan_id']}/confirm",
            {"minute": 43})
        self.assertEqual(status, 409)
        self.assertEqual(rejected["code"], "stale_plan")
        # 资源视图只反映已确认版本，drum-a 只有一套占用
        _, view = self.call("GET", "/v1/resources")
        drum = [r for r in view["resources"]["drum-a"]
                if r["program"] == "bridge-parade"]
        self.assertEqual(len(drum), 1)
        self.assertEqual(drum[0]["start"], 60)

        # ---- 3. 派发排演单：plaza 正常，yard 首次断线后幂等重试 ----
        status, _ = self.call("POST", "/v1/distribute", {
            "stop": "plaza", "version": "board-2", "minute": 44})
        self.assertEqual(status, 200)
        status, lost = self.call("POST", "/v1/distribute", {
            "stop": "yard", "version": "board-2", "minute": 44})
        self.assertEqual(status, 503)
        self.assertTrue(lost["retryable"])
        status, retry = self.call("POST", "/v1/distribute", {
            "stop": "yard", "version": "board-2", "minute": 45})
        self.assertEqual(status, 200)

        # ---- 4. 现场已执行 b2 的接人：之后改排不能再动它 ----
        status, _ = self.call("POST", "/v1/progress", {
            "kind": "pickup", "minute": 46, "booking_id": "b2",
            "stop": "yard", "groups": ["trip-19"], "version": "board-2"})
        self.assertEqual(status, 200)
        _, bad = self.call("POST", "/v1/plans", {
            "minute": 47,
            "changes": [{"op": "move", "booking_id": "b2",
                         "stop": "plaza", "start": 60, "end": 70}]})
        self.assertIn("executed_immutable",
                      {f["code"] for f in bad["findings"]
                       if f["level"] == "error"})

        # ---- 5. 围栏裂脑：plaza 按新版放行，yard 仍按旧版拦截 ----
        self.call("POST", "/v1/fences",
                  {"stop": "plaza", "version": "board-2",
                   "action": "released", "minute": 48})
        self.call("POST", "/v1/fences",
                  {"stop": "yard", "version": "board-1",
                   "action": "blocked", "minute": 48})
        status, split = self.call("GET", "/v1/segments?minute=48")
        row = next(s for s in split["segments"]
                   if s["segment"] == "plaza-yard")
        self.assertTrue(row["fence"]["split_brain"])
        # 导游在裂脑未对齐前一律不能带队
        self.service_note_yard_effective()
        _, decision = self.call("POST", "/v1/guide/route", {
            "group": "trip-19", "to": "bridge", "minute": 49})
        self.assertFalse(decision["authorized"])

        # ---- 6. 围栏对齐后，等候中的游客仍只能原地，可带走的才放行 ----
        self.call("POST", "/v1/fences",
                  {"stop": "yard", "version": "board-2",
                   "action": "released", "minute": 50})
        _, still_waiting = self.call("POST", "/v1/guide/route", {
            "group": "trip-19", "to": "bridge", "minute": 50})
        self.assertEqual(still_waiting["code"], "group_waiting")
        self.call("POST", "/v1/audience/arrivals", {
            "group": "trip-19", "stop": "yard", "minute": 50,
            "disposition": "movable"})
        _, movable = self.call("POST", "/v1/guide/route", {
            "group": "trip-19", "to": "bridge", "minute": 50})
        self.assertTrue(movable["authorized"])

        # ---- 7. 对账：找回各站实际收到/执行的版本与承诺状态 ----
        status, recon = self.call("GET", "/v1/reconciliation")
        self.assertEqual(status, 200)
        self.assertEqual(recon["current_version"], "board-2")
        self.assertIn("b2", recon["executed_booking_ids"])
        promise = next(p for p in recon["promises"]
                       if p["group"] == "trip-19")
        self.assertEqual(promise["status"], "fulfilled")

    def service_note_yard_effective(self):
        # yard 在测试前置中只有 board-1 生效；补登 board-2 生效以隔离围栏变量
        self.server.service.note_delivery("yard", "board-2", True, True, 48)


if __name__ == "__main__":
    unittest.main()
