"""合作方回执客户端与离线 fixture 加载。

线上模式走对端 HTTP（urllib，标准库）；离线模式直接读 fixture 文件，
使断线/丢回复等联调场景无需真实网络即可复现。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


class ReceiptError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class HttpReceiptClient:
    """对 `fixture_gateway.py` 协议的真实 HTTP 封装，POST 带幂等 reference。"""

    def __init__(self, base_url: str, timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post_receipt(self, reference: str, operation: str,
                     recipient: str) -> dict:
        body = json.dumps({"reference": reference, "operation": operation,
                           "recipient": recipient}).encode("utf-8")
        req = urlrequest.Request(
            self.base_url + "/v1/receipts", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read())["receipt"]
        except HTTPError as exc:
            payload = json.loads(exc.read() or b"{}")
            if exc.code == 409:
                raise ReceiptError("reference_reused",
                                   payload.get("code", "reference_reused"))
            if exc.code == 422:
                raise ReceiptError("no_counterparty_example",
                                   payload.get("code", "no_counterparty_example"))
            raise ReceiptError("counterparty_error", str(exc), retryable=True)
        except URLError as exc:
            # 连接断开：调用方可用同一 reference 安全重试
            raise ReceiptError("connection_lost", str(exc), retryable=True)


class OfflineReceiptClient:
    """按 fixtures/receipts.json 的约定回放回执，模拟 applied/lost_reply。"""

    def __init__(self, fixtures_folder: str | Path):
        folder = Path(fixtures_folder)
        examples = json.loads((folder / "receipts.json").read_text("utf-8"))
        self._replies = {(e["operation"], e["recipient"]): e for e in examples}
        self._seen: dict[str, tuple[str, str]] = {}

    def post_receipt(self, reference: str, operation: str,
                     recipient: str) -> dict:
        signature = (operation, recipient)
        previous = self._seen.get(reference)
        if previous is not None:
            if previous != signature:
                raise ReceiptError("reference_reused", "编号被另一个操作使用")
            return self._replies[signature]["receipt"]
        example = self._replies.get(signature)
        if example is None:
            raise ReceiptError("no_counterparty_example",
                               "对端没有该操作的回执示例")
        self._seen[reference] = signature
        if example["delivery"] == "lost_reply":
            # 首次调用已被对端记录，但回复丢失：重试即拿到回执
            raise ReceiptError("connection_lost", "对端已记录但回复丢失",
                               retryable=True)
        return example["receipt"]


def load_fixture_bundle(fixtures_folder: str | Path) -> tuple[dict, list[dict], list[dict]]:
    folder = Path(fixtures_folder)
    registers = json.loads((folder / "registers.json").read_text("utf-8"))
    events = [json.loads(line)
              for line in (folder / "events.ndjson").read_text("utf-8").splitlines()
              if line.strip()]
    receipts = json.loads((folder / "receipts.json").read_text("utf-8"))
    return registers, events, receipts
