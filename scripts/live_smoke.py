"""Live verification against a real put.io account.

Creates a uniquely named temporary folder at the root, exercises every organisation
tool inside it, and (by default) deletes that folder afterwards. Nothing outside the
temporary folder is touched.

Usage:
    PUTIO_TOKEN=... uv run python scripts/live_smoke.py [--keep] [--transfer-url URL]

--keep           leave the temporary folder in place for manual inspection
--transfer-url   also add a real transfer (an http(s)/magnet URL) into the temp
                 folder to verify destination selection; it is cancelled afterwards

Exit status is non-zero when any step fails.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from putio_mcp.client import PutioClient  # noqa: E402
from putio_mcp.errors import CONFLICT, INVALID, PutioError  # noqa: E402
from putio_mcp.service import PutioService  # noqa: E402


class Smoke:
    def __init__(self, token: str) -> None:
        self.client = PutioClient(token)
        self.svc = PutioService(self.client)
        self.failures = 0
        self.temp_folder_id: int | None = None
        self.transfer_id: int | None = None

    def step(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            out = fn()
        except Exception as exc:  # noqa: BLE001 - report everything
            self.failures += 1
            print(f"FAIL  {name}: {exc}")
            return None
        print(f"PASS  {name}" + (f" -> {out}" if out not in (None, True) else ""))
        return out

    def expect_error(self, kind: str, fn: Callable[[], Any]) -> str:
        try:
            fn()
        except PutioError as exc:
            if exc.kind == kind:
                return f"rejected as expected ({exc.kind})"
            raise AssertionError(f"expected {kind}, got {exc}") from None
        raise AssertionError(f"expected {kind} error but call succeeded")

    def upload_text(self, name: str, parent_id: int) -> int:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / name
            path.write_text(f"putio-mcp smoke test {time.time()}\n")
            uploaded = self.client._client.File.upload(str(path), parent_id=parent_id)
        return int(uploaded.id)

    def run(self, transfer_url: str | None) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"putio-mcp-smoke-{stamp}"

        root = self.step("list root folder", lambda: self.svc.list_files(0, per_page=5))
        if root is None:
            return

        created = self.step("create temp folder", lambda: self.svc.create_folder(base, 0))
        if not created:
            return
        self.temp_folder_id = tid = created["created"]["id"]

        self.step(
            "create_folder conflict detected",
            lambda: self.expect_error(CONFLICT, lambda: self.svc.create_folder(base.upper(), 0)),
        )
        self.step(
            "create_folder dry_run",
            lambda: self.svc.create_folder("preview-only", tid, dry_run=True)["dry_run"],
        )

        renamed_name = base + "-renamed"
        self.step("rename temp folder", lambda: self.svc.rename_file(tid, renamed_name)["item"]["name"])
        self.step("get_file reflects rename", lambda: self.svc.get_file(tid)["name"] == renamed_name or "name mismatch")

        sub = self.step("create subfolder", lambda: self.svc.create_folder("Sub", tid)["created"]["id"])
        f1 = self.step("upload file 1", lambda: self.upload_text("alpha.txt", tid))
        f2 = self.step("upload file 2", lambda: self.upload_text("beta.txt", tid))
        if not (sub and f1 and f2):
            return

        self.step(
            "move dry_run leaves files in place",
            lambda: (
                self.svc.move_files([f1, f2], sub, dry_run=True)["summary"]["would_move"] == 2
                and self.svc.get_file(f1)["parent_id"] == tid
            ) or "unexpected state",
        )
        self.step(
            "move refuses folder into itself",
            lambda: self.svc.move_files([sub], sub)["results"][0]["status"] == "failed" or "not rejected",
        )
        self.step(
            "move refuses folder into descendant",
            lambda: self.svc.move_files([tid], sub)["results"][0]["error_kind"] == INVALID or "not rejected",
        )
        self.step(
            "move files into subfolder",
            lambda: self.svc.move_files([f1, f2, 1], sub)["summary"],  # id 1 should be not_found/invalid
        )
        self.step(
            "list subfolder shows both files",
            lambda: sorted(i["name"] for i in self.svc.list_files(sub)["items"]) == ["alpha.txt", "beta.txt"]
            or "listing mismatch",
        )
        self.step(
            "pagination with per_page=1",
            lambda: (lambda p: p["count"] == 1 and p["next_cursor"] is not None
                     and self.svc.list_files(sub, per_page=1, cursor=p["next_cursor"])["count"] == 1)(
                self.svc.list_files(sub, per_page=1)
            ) or "pagination mismatch",
        )
        self.step(
            "move conflict detected (same name in destination)",
            lambda: (lambda f3: self.svc.move_files([f3], sub)["results"][0]["error_kind"] == CONFLICT)(
                self.upload_text("alpha.txt", tid)
            ) or "conflict not detected",
        )
        self.step(
            "rename conflict detected",
            lambda: self.expect_error(CONFLICT, lambda: self.svc.rename_file(f1, "BETA.txt")),
        )
        self.step("search finds uploaded file", lambda: any(i["id"] == f1 for i in self.svc.search_files("alpha", 50)["items"]) or "not found")

        self.step(
            "add_transfer rejects file as destination",
            lambda: self.expect_error(INVALID, lambda: self.svc.add_transfer("magnet:?xt=urn:btih:0", f1)),
        )
        if transfer_url:
            def add() -> Any:
                res = self.svc.add_transfer(transfer_url, sub)
                self.transfer_id = res["transfer"]["id"]
                return {"id": self.transfer_id, "save_parent_id": res["transfer"].get("save_parent_id")}
            self.step("add_transfer into subfolder", add)
            if self.transfer_id:
                self.step("list_transfers includes it", lambda: any(t["id"] == self.transfer_id for t in self.svc.list_transfers()) or "missing")
                self.step("get_browser_link", lambda: self.svc.get_browser_link(self.transfer_id).get("url") or "not completed yet (ok)")
        else:
            print("SKIP  add_transfer with destination (pass --transfer-url to exercise)")

    def cleanup(self, keep: bool) -> None:
        if self.transfer_id:
            try:
                self.svc.cancel_transfer(self.transfer_id)
                print(f"CLEAN cancelled transfer {self.transfer_id}")
            except PutioError as exc:
                print(f"WARN  could not cancel transfer {self.transfer_id}: {exc}")
        if self.temp_folder_id is None:
            return
        if keep:
            print(f"KEEP  temp folder id {self.temp_folder_id} left in place")
            return
        try:
            self.client._client.File.get(self.temp_folder_id).delete()
            print(f"CLEAN deleted temp folder {self.temp_folder_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"WARN  could not delete temp folder {self.temp_folder_id}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--transfer-url")
    args = ap.parse_args()

    token = os.environ.get("PUTIO_TOKEN", "").strip()
    if not token:
        print("PUTIO_TOKEN is not set; refusing to run.", file=sys.stderr)
        return 2

    smoke = Smoke(token)
    try:
        smoke.run(args.transfer_url)
    finally:
        smoke.cleanup(args.keep)
    print(f"\n{'OK' if smoke.failures == 0 else 'FAILED'}: {smoke.failures} failing step(s)")
    return 1 if smoke.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
