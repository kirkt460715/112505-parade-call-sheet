import json
import tempfile
import unittest
from pathlib import Path

from rehearsal.engine import EngineError
from rehearsal.gateway import OfflineReceiptClient, ReceiptError, load_fixture_bundle
from rehearsal.ledger import Ledger
from rehearsal.server import build_server
from rehearsal.service import RehearsalService
from rehearsal.sync import EventSync

from helpers import FIXTURE_REGISTERS, build_service, error_codes
from http.client import HTTPConnection


class GenerationFenceTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def _valid_change(self, start):
        return [{"op": "move", "booking_id": "b3", "stop": "bridge",
                 "start": start, "end": start + 10}]

    def test_second_plan_cannot_take_effect_with_stale_slots(self):
        first = self.service.create_plan(self._valid_change(60), minute=40)
        second = self.service.create_plan(self._valid_change(62), minute=41)
        sheet = self.service.confirm_plan(first.plan_id, minute=42,
                                          version="board-2")
        self.assertEqual(sheet.generation, 2)
        # 另一套草稿仍带着旧代次的可用时段，确认必须被拒
        with self.assertRaises(EngineError) as ctx:
            self.service.confirm_plan(second.plan_id, minute=43)
        self.assertEqual(ctx.exception.code, "stale_plan")
        self.assertEqual(second.status, "rejected")
        # 基于新版重算后可以再次改排
        retry = self.service.create_plan(self._valid_change(64), minute=44)
        self.assertEqual(retry.base_version, "board-2")
        self.service.confirm_plan(retry.plan_id, minute=45, version="board-3")
        self.assertEqual(self.service.ledger.current_generation, 3)

    def test_invalid_plan_cannot_be_confirmed(self):
        plan = self.service.create_plan(
            [{"op": "move", "booking_id": "b2", "stop": "yard",
              "start": 25, "end": 40}], minute=20)
        self.assertIn("resource_overlap", error_codes(plan))
        with self.assertRaises(EngineError) as ctx:
            self.service.confirm_plan(plan.plan_id, minute=21)
        self.assertEqual(ctx.exception.code, "plan_invalid")
        # 被拒后原排演单仍可执行
        self.assertEqual(self.service.ledger.current_sheet.version, "board-1")

    def test_replan_failure_leaves_executable_sites_running(self):
        # 一个非法改排不会改动任何已确认状态，原站点继续办
        before = self.service.sheet().to_dict()
        plan = self.service.create_plan(
            [{"op": "move", "booking_id": "b2", "stop": "yard",
              "start": 25, "end": 40}], minute=20)
        with self.assertRaises(EngineError):
            self.service.confirm_plan(plan.plan_id, minute=21)
        self.assertEqual(self.service.sheet().to_dict(), before)


class EventIngestTest(unittest.TestCase):
    def test_fixture_event_stream_applies_in_cursor_order(self):
        service = build_service()
        _, events, _ = load_fixture_bundle("fixtures")
        applied = EventSync(service).ingest_bundle(events)
        self.assertEqual([a["cursor"] for a in applied], [1, 2, 3, 4])
        # 场地事件：yard 方向自 30 分钟起仅 outbound
        from rehearsal.engine import segment_direction
        self.assertEqual(
            segment_direction(service.ledger, "plaza-yard", 30), "outbound")
        # 观众事件：trip-19 已在 yard 到位、原地等候
        self.assertEqual(service.ledger.groups["trip-19"].current_stop, "yard")
        # 导演台事件：yard 收到 board-1 叫场（生效）；plaza 收到 board-2（未生效）
        self.assertTrue(service.ledger.delivery("yard", "board-1").in_effect)
        self.assertFalse(service.ledger.delivery("plaza", "board-2").in_effect)
        # 重复摄入同一游标被跳过
        again = EventSync(service).ingest_bundle(events)
        self.assertEqual(again, [])

    def test_stale_cursor_rejected_on_direct_ingest(self):
        service = build_service()
        service.ingest_event(1, "venue", 30,
                             {"site": "yard", "change": "outbound_only"})
        with self.assertRaises(EngineError) as ctx:
            service.ingest_event(1, "venue", 30,
                                 {"site": "yard", "change": "outbound_only"})
        self.assertEqual(ctx.exception.code, "stale_cursor")


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        _, events, _ = load_fixture_bundle("fixtures")
        EventSync(self.service).ingest_bundle(events)

    def test_stop_authority_and_guide_gating(self):
        recon = self.service.reconciliation()
        by_stop = {row["stop"]: row for row in recon["stops"]}
        # board-2 只在 plaza 被收到且未生效；yard 还停在 board-1
        self.assertFalse(by_stop["plaza"]["may_follow"])
        self.assertEqual(by_stop["plaza"]["authority"],
                         "received_not_effective")
        self.assertEqual(by_stop["yard"]["effective_version"], "board-1")

        # 导演台确认 board-2，但 yard 尚未确认生效：导游不得按新版方向带队
        plan = self.service.create_plan(
            [{"op": "move", "booking_id": "b3", "stop": "bridge",
              "start": 60, "end": 70}], minute=40)
        self.service.confirm_plan(plan.plan_id, 41, version="board-2")
        decision = self.service.guide_route("trip-19", "bridge", 42)
        self.assertFalse(decision["authorized"])
        self.assertEqual(decision["code"], "version_not_effective")

        # yard 确认新版生效后，导游请求才进入通路/等候判断
        self.service.note_delivery("yard", "board-2", True, True, 43)
        decision = self.service.guide_route("trip-19", "bridge", 44)
        self.assertFalse(decision["authorized"])
        self.assertEqual(decision["code"], "group_waiting")

    def test_distribution_recovers_after_lost_reply(self):
        # 先确认出 board-2，再向各站派发
        plan = self.service.create_plan(
            [{"op": "move", "booking_id": "b3", "stop": "bridge",
              "start": 60, "end": 70}], minute=40)
        self.service.confirm_plan(plan.plan_id, 41, version="board-2")
        client = OfflineReceiptClient("fixtures")
        # yard 的回执首次丢回复
        with self.assertRaises(ReceiptError) as ctx:
            self.service.distribute("yard", "board-2", 40, client)
        self.assertTrue(ctx.exception.retryable)
        # 同一 reference 重试：对端已记录，幂等返回同一回执（in_effect=false）
        result = self.service.distribute("yard", "board-2", 41, client)
        self.assertFalse(result["in_effect"])
        # plaza 正常送达
        plaza = self.service.distribute("plaza", "board-2", 40,
                                        OfflineReceiptClient("fixtures"))
        self.assertTrue(plaza["delivered"])

    def test_reference_reuse_conflicts(self):
        client = OfflineReceiptClient("fixtures")
        client.post_receipt("ref-1", "post-call-sheet", "plaza")
        with self.assertRaises(ReceiptError) as ctx:
            client.post_receipt("ref-1", "post-call-sheet", "yard")
        self.assertEqual(ctx.exception.code, "reference_reused")


class SnapshotTest(unittest.TestCase):
    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "state.json"
            ledger = Ledger(snapshot)
            ledger.load_registers(FIXTURE_REGISTERS)
            service = RehearsalService(ledger)
            plan = service.create_plan(
                [{"op": "move", "booking_id": "b3", "stop": "bridge",
                  "start": 60, "end": 70}], minute=40)
            service.confirm_plan(plan.plan_id, 45, version="board-9")
            service.record_progress("team_start", 46, booking_id="b1",
                                    team="drum-a", stop="plaza",
                                    version="board-9")

            revived = RehearsalService(Ledger(snapshot))
            self.assertEqual(revived.ledger.current_sheet.version, "board-9")
            self.assertIn("b1", revived.ledger.executed_booking_ids())
            # 新改排单编号不与持久化的冲突
            another = revived.create_plan(
                [{"op": "move", "booking_id": "b3", "stop": "bridge",
                  "start": 72, "end": 80}], minute=50)
            self.assertNotEqual(another.plan_id, plan.plan_id)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = build_server(0, "fixtures",
                                  receipt_client=OfflineReceiptClient("fixtures"))
        import threading
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

    def test_full_replan_flow_over_http(self):
        status, _ = self.call("GET", "/v1/health")
        self.assertEqual(status, 200)
        # 当前版本与资源占用
        status, view = self.call("GET", "/v1/resources")
        self.assertEqual(view["version"], "board-1")
        self.assertIn("drum-a", view["resources"])
        # 提交合法改排：bridge-parade 顺延，满足 drum-a 缓冲，不影响游客承诺
        status, plan = self.call("POST", "/v1/plans", {
            "minute": 40,
            "changes": [{"op": "move", "booking_id": "b3",
                         "stop": "bridge", "start": 60, "end": 70}]})
        self.assertEqual(status, 200)
        self.assertTrue(plan["status"] == "proposed")
        self.assertEqual(
            {f["code"] for f in plan["findings"] if f["level"] == "error"},
            set())
        # 确认生成 board-2
        status, sheet = self.call("POST",
                                  f"/v1/plans/{plan['plan_id']}/confirm",
                                  {"minute": 42, "version": "board-2"})
        self.assertEqual(status, 200)
        self.assertEqual(sheet["generation"], 2)
        # 现场记录
        status, _ = self.call("POST", "/v1/progress", {
            "kind": "pickup", "minute": 44, "booking_id": "b2",
            "stop": "yard", "groups": ["trip-19"], "version": "board-2"})
        self.assertEqual(status, 200)
        status, recon = self.call("GET", "/v1/reconciliation")
        self.assertEqual(status, 200)
        self.assertIn("b2", recon["executed_booking_ids"])
        # 已执行部分不可再改
        status, bad = self.call("POST", "/v1/plans", {
            "minute": 46,
            "changes": [{"op": "move", "booking_id": "b2",
                         "stop": "plaza", "start": 60, "end": 70}]})
        self.assertEqual(status, 200)
        self.assertIn("executed_immutable",
                      {f["code"] for f in bad["findings"]
                       if f["level"] == "error"})
        # 派发：plaza 正常，yard 首次断线重试
        status, _ = self.call("POST", "/v1/distribute", {
            "stop": "plaza", "version": "board-2", "minute": 47})
        self.assertEqual(status, 200)
        status, lost = self.call("POST", "/v1/distribute", {
            "stop": "yard", "version": "board-2", "minute": 47})
        self.assertEqual(status, 503)
        self.assertTrue(lost["retryable"])
        status, retry = self.call("POST", "/v1/distribute", {
            "stop": "yard", "version": "board-2", "minute": 48})
        self.assertEqual(status, 200)
        self.assertFalse(retry["in_effect"])
        # 围栏裂脑时导游被拦
        self.call("POST", "/v1/fences",
                  {"stop": "plaza", "version": "board-2",
                   "action": "released", "minute": 49})
        self.call("POST", "/v1/fences",
                  {"stop": "yard", "version": "board-1",
                   "action": "blocked", "minute": 49})
        status, decision = self.call("POST", "/v1/guide/route", {
            "group": "trip-19", "to": "plaza", "minute": 49})
        self.assertFalse(decision["authorized"])


if __name__ == "__main__":
    unittest.main()
