"""Tests for feedback submission hardening, gateway error visibility, and CN error-feedback routing."""

from __future__ import annotations

import gzip
import hashlib
import inspect
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError


REPO_ROOT = Path(__file__).resolve().parents[1]
PACER_BACKEND = REPO_ROOT / "pacer" / "backend"
MAIN_BACKEND = REPO_ROOT / "backend"


def _with_backend(pkg_root: Path):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            saved_modules = sys.modules.copy()
            saved_path = list(sys.path)
            for mod_name in list(sys.modules):
                if mod_name == "core" or mod_name.startswith("core.") or mod_name == "desktop" or mod_name.startswith("desktop."):
                    sys.modules.pop(mod_name, None)
            sys.path.insert(0, str(pkg_root))
            try:
                return fn(*args, **kwargs)
            finally:
                for mod_name in list(sys.modules):
                    if mod_name not in saved_modules:
                        sys.modules.pop(mod_name, None)
                sys.modules.update(saved_modules)
                sys.path[:] = saved_path

        return wrapper

    return decorator


def _write_draft(root: Path, draft_id: str, raw_payload: bytes, *, packed_override: bytes | None = None, multipart_required: bool = False) -> Path:
    folder = root / "exports" / "feedback" / "drafts" / draft_id
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "feedback.json.gz"
    if packed_override is not None:
        archive.write_bytes(packed_override)
    else:
        with archive.open("wb") as stream:
            with gzip.GzipFile(fileobj=stream, mode="wb", compresslevel=6, mtime=0) as gz:
                gz.write(raw_payload)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (folder / "draft.json").write_text(
        json.dumps(
            {
                "schema_version": "uma.feedback_draft.v1",
                "draft_id": draft_id,
                "sha256": digest,
                "multipart_required": multipart_required,
                "submitted": None,
            }
        ),
        encoding="utf-8",
    )
    return folder


class _FakeHttpResponse:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode("utf-8")

    def read(self, n: int = -1) -> bytes:
        return self._raw[:n] if n >= 0 else self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class TestFeedbackSubmissionHardening(unittest.TestCase):
    @_with_backend(PACER_BACKEND)
    def test_pacer_storage_lock_wait_default_is_10_seconds(self) -> None:
        from core.auto_trainer import pacer_storage

        sig = inspect.signature(pacer_storage.read_access_for_run)
        self.assertEqual(sig.parameters["max_wait_seconds"].default, 10.0)

    @_with_backend(PACER_BACKEND)
    def test_messages_and_transient_gateway_failover(self) -> None:
        from desktop import cut_support_client as csc
        from desktop import service_transport as transport

        expected_codes = (
            "gateway_request_too_large",
            "gateway_invalid",
            "gateway_route_denied",
            "gateway_unavailable",
            "gateway_client_ip_unavailable",
            "feedback_upload_denied",
            "feedback_storage_busy",
            "feedback_run_unavailable",
            "feedback_prepare_failed",
        )
        for code in expected_codes:
            self.assertIn(code, csc.MESSAGES, code)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {"base_url": transport.APPROVED_CN_ORIGIN, "channel": "preview", "api_transport": "cn"}
            for code in ("gateway_unavailable", "gateway_client_ip_unavailable"):
                body = json.dumps({"ok": False, "code": code}).encode("utf-8")
                err = HTTPError(cfg["base_url"] + "/v1/feedback", 503, "Service Unavailable", {}, io.BytesIO(body))
                with mock.patch.object(csc, "_send_keepalive_origin", side_effect=err):
                    with self.assertRaises(csc.SupportError) as ctx:
                        csc._wire_origin(root, cfg, "/v1/feedback")
                self.assertEqual(ctx.exception.code, code)
                self.assertTrue(getattr(ctx.exception, "maauma_transport_retryable", False), code)

    @_with_backend(PACER_BACKEND)
    def test_prepare_feedback_maps_local_errors_to_specific_codes(self) -> None:
        from desktop import cut_support_client as csc

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("core.auto_trainer.career_review_export.checked_review_run", side_effect=FileNotFoundError("gone")):
                with self.assertRaises(csc.SupportError) as ctx:
                    csc.prepare_feedback(root, "run_1", "desc")
                self.assertEqual(ctx.exception.code, "feedback_run_unavailable")

            with (
                mock.patch("core.auto_trainer.career_review_export.checked_review_run", return_value=root / "run_1"),
                mock.patch("core.auto_trainer.pacer_storage.read_access_for_run", side_effect=RuntimeError("pacer_run_maintenance_busy")),
            ):
                with self.assertRaises(csc.SupportError) as ctx:
                    csc.prepare_feedback(root, "run_1", "desc")
                self.assertEqual(ctx.exception.code, "feedback_storage_busy")

            with (
                mock.patch("core.auto_trainer.career_review_export.checked_review_run", return_value=root / "run_1"),
                mock.patch("core.auto_trainer.pacer_storage.read_access_for_run", side_effect=OSError("disk error")),
            ):
                with self.assertRaises(csc.SupportError) as ctx:
                    csc.prepare_feedback(root, "run_1", "desc")
                self.assertEqual(ctx.exception.code, "feedback_prepare_failed")

    @_with_backend(PACER_BACKEND)
    def test_submit_feedback_prefers_multipart_above_350kb_or_3mb_raw_and_keeps_legacy_fallback(self) -> None:
        from desktop import cut_support_client as csc
        from desktop import feedback_multipart

        fid = "MF-12345678-1234-1234-1234-1234567890ab"
        draft_small = "a" * 32
        draft_large_raw = "b" * 32
        draft_fallback = "c" * 32
        draft_unsupported = "d" * 32

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1. Small payload -> inline directly without /v1/info
            _write_draft(root, draft_small, b'{"ok":true}')
            calls = []
            with mock.patch.object(csc, "request", side_effect=lambda r, p, b=None, **kw: (calls.append(p), {"feedback_id": fid, "status": "open"})[1]):
                res = csc.submit_feedback(root, draft_small)
            self.assertEqual(res["feedback_id"], fid)
            self.assertEqual(calls, ["/v1/feedback"])

            # 2. Raw > 3 MB even with tiny gzip -> prefers multipart
            compressible = b'{"pad":"' + (b"x" * (csc.INLINE_MAX_RAW + 1024)) + b'"}'
            _write_draft(root, draft_large_raw, compressible)
            info_calls = []

            def fake_req(r, p, b=None, **kw):
                info_calls.append(p)
                if p == "/v1/info":
                    return {"feedback_upload": "maauma.feedback_upload.v1"}
                return {"feedback_id": fid, "status": "open"}

            with (
                mock.patch.object(csc, "request", side_effect=fake_req),
                mock.patch.object(feedback_multipart, "upload_gzip", return_value={"feedback_id": fid, "status": "open"}) as up_mock,
            ):
                res2 = csc.submit_feedback(root, draft_large_raw)
            self.assertEqual(res2["feedback_id"], fid)
            self.assertEqual(info_calls, ["/v1/info"])
            self.assertEqual(up_mock.call_count, 1)

            # 3. Packed between 350KB and 1.5MB with server lacking multipart -> legacy inline fallback
            incompressible = gzip.compress(bytes((i * 73 + 17) % 256 for i in range(420_000)), compresslevel=0)
            # Build valid gzip > INLINE_MAX_PACKED and <= MAX_PACKED with small raw_size <= INLINE_MAX_RAW
            self.assertGreater(len(incompressible), csc.INLINE_MAX_PACKED)
            self.assertLessEqual(len(incompressible), csc.MAX_PACKED)
            _write_draft(root, draft_fallback, b"", packed_override=incompressible)
            legacy_calls = []

            def fake_legacy_req(r, p, b=None, **kw):
                legacy_calls.append(p)
                if p == "/v1/info":
                    return {"ok": True}
                return {"feedback_id": fid, "status": "open"}

            with mock.patch.object(csc, "request", side_effect=fake_legacy_req):
                res3 = csc.submit_feedback(root, draft_fallback)
            self.assertEqual(res3["feedback_id"], fid)
            self.assertEqual(legacy_calls, ["/v1/info", "/v1/feedback"])

            # 4. > MAX_PACKED with server lacking multipart -> feedback_upload_unsupported
            huge_incompressible = gzip.compress(bytes((i * 97 + 31) % 256 for i in range(csc.MAX_PACKED + 20_000)), compresslevel=0)
            _write_draft(root, draft_unsupported, b"", packed_override=huge_incompressible)
            with mock.patch.object(csc, "request", return_value={"ok": True}):
                with self.assertRaises(csc.SupportError) as ctx:
                    csc.submit_feedback(root, draft_unsupported)
                self.assertEqual(ctx.exception.code, "feedback_upload_unsupported")

    @_with_backend(MAIN_BACKEND)
    def test_error_feedback_defaults_to_cn_direct_with_legacy_failover_only_on_transient(self) -> None:
        from urllib.request import ProxyHandler
        from desktop import maauma_error_feedback as mef

        self.assertEqual(mef.DEFAULT_ENDPOINT, "https://api.maauma.top/v1/bug-feedback")
        self.assertEqual(mef.LEGACY_ENDPOINT, "https://maauma-private.2234198378.workers.dev/v1/bug-feedback")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1. Failover from api.maauma.top (with ProxyHandler({})) to workers.dev on 503
            prep = mef.prepare(root, {"app_version": "0.6.24", "exception_type": "RuntimeError", "message": "boom"})
            rid = prep["report_id"]
            seen = []
            ack_id = "BF-12345678-1234-1234-1234-1234567890ab"

            def opener_failover(*handlers):
                has_empty_proxy = any(isinstance(h, ProxyHandler) and h.proxies == {} for h in handlers)

                class _Opener:
                    def open(self, req, timeout=20):
                        seen.append((req.full_url, has_empty_proxy))
                        if len(seen) == 1:
                            raise HTTPError(req.full_url, 503, "Unavailable", {}, io.BytesIO(b"{}"))
                        return _FakeHttpResponse({"ok": True, "feedback_id": ack_id, "status": "open"})

                return _Opener()

            with mock.patch.object(mef, "build_opener", side_effect=opener_failover):
                res = mef.submit(root, rid)
            self.assertEqual(res["feedback_id"], ack_id)
            self.assertEqual(seen, [(mef.DEFAULT_ENDPOINT, True), (mef.LEGACY_ENDPOINT, False)])

            # 2. No failover on HTTP 400
            rid2 = mef.prepare(root, {"app_version": "0.6.24", "exception_type": "ValueError", "message": "bad"})["report_id"]
            seen_400 = []

            def opener_400(*handlers):
                class _Opener:
                    def open(self, req, timeout=20):
                        seen_400.append(req.full_url)
                        raise HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))

                return _Opener()

            with mock.patch.object(mef, "build_opener", side_effect=opener_400):
                with self.assertRaises(mef.FeedbackError) as ctx:
                    mef.submit(root, rid2)
            self.assertEqual(str(ctx.exception), "error_feedback_rejected")
            self.assertEqual(seen_400, [mef.DEFAULT_ENDPOINT])

            # 3. No failover on HTTP 429
            rid3 = mef.prepare(root, {"app_version": "0.6.24", "exception_type": "ValueError", "message": "limit"})["report_id"]
            seen_429 = []

            def opener_429(*handlers):
                class _Opener:
                    def open(self, req, timeout=20):
                        seen_429.append(req.full_url)
                        raise HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))

                return _Opener()

            with mock.patch.object(mef, "build_opener", side_effect=opener_429):
                with self.assertRaises(mef.FeedbackError) as ctx2:
                    mef.submit(root, rid3)
            self.assertEqual(str(ctx2.exception), "error_feedback_rate_limited")
            self.assertEqual(seen_429, [mef.DEFAULT_ENDPOINT])

            # 4. Custom endpoint does not fail over to legacy
            rid4 = mef.prepare(root, {"app_version": "0.6.24", "exception_type": "OSError", "message": "net"})["report_id"]
            seen_custom = []

            def opener_custom(*handlers):
                class _Opener:
                    def open(self, req, timeout=20):
                        seen_custom.append((req.full_url, any(isinstance(h, ProxyHandler) for h in handlers)))
                        raise URLError("down")

                return _Opener()

            with mock.patch.object(mef, "build_opener", side_effect=opener_custom):
                with self.assertRaises(mef.FeedbackError) as ctx3:
                    mef.submit(root, rid4, endpoint="https://127.0.0.1:9999/v1/bug-feedback")
            self.assertEqual(str(ctx3.exception), "error_feedback_unavailable")
            self.assertEqual(seen_custom, [("https://127.0.0.1:9999/v1/bug-feedback", False)])


if __name__ == "__main__":
    unittest.main()
