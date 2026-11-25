"""本地启动入口：python -m rehearsal --port 8080 --fixtures fixtures"""

from __future__ import annotations

import argparse

from .gateway import HttpReceiptClient, OfflineReceiptClient
from .server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="巡游改排导演台后端")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--fixtures", default="fixtures")
    parser.add_argument("--snapshot", default=None,
                        help="台账快照路径，提供后进程重启可恢复全部状态")
    parser.add_argument("--receipt-base-url", default=None,
                        help="合作方回执 HTTP 入口；缺省使用 fixture 离线桩")
    options = parser.parse_args()

    if options.receipt_base_url:
        client = HttpReceiptClient(options.receipt_base_url)
    else:
        client = OfflineReceiptClient(options.fixtures)

    server = build_server(options.port, options.fixtures,
                          snapshot_path=options.snapshot,
                          receipt_client=client)
    print(f"导演台后端 http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
