from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from pipeline_integrations import oss_transfer, platform_qa
from pipeline_db.task_update import TaskStageUpdater


class PlatformQaTests(unittest.TestCase):
    def test_claim_sends_user_id_and_count_with_required_headers(self) -> None:
        captured = {}

        def sender(url, headers, body, timeout, retries):
            captured.update({
                "url": url,
                "headers": headers,
                "body": body,
                "timeout": timeout,
                "retries": retries,
            })
            return {
                "code": 200,
                "data": {
                    "claimed_count": 1,
                    "requested_count": 2,
                    "tasks": [{"task_id": "TASK-ONE"}],
                },
            }

        result, rc = platform_qa.run_operation(
            "claim",
            {"user_id": "user-1", "task_count": 2, "task_ids": []},
            {
                "TRAJ_REPAIR_BASE_URL": "https://platform.example",
                "TRAJ_REPAIR_API_KEY": "api-key",
            },
            30,
            2,
            sender=sender,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["task_ids"], ["TASK-ONE"])
        self.assertEqual(captured["url"], "https://platform.example/api/traj-repair/api/qa/claim")
        self.assertEqual(captured["body"], {"qa_user_id": "user-1", "count": 2})
        self.assertEqual(captured["retries"], 0)
        self.assertEqual(captured["headers"]["X-API-Key"], "api-key")
        self.assertNotIn("Authorization", captured["headers"])

    def test_claim_requires_positive_count_without_explicit_task_ids(self) -> None:
        for value in (None, 0, -1, True, "2"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "positive task_count"):
                    platform_qa.claim_body({"user_id": "user-1", "task_ids": [], "task_count": value})

    def test_dispatch_write_api_key_is_accepted_as_platform_key_alias(self) -> None:
        headers = platform_qa.build_headers({"DISPATCH_WRITE_API_KEY": "dispatch-key"})
        self.assertEqual(headers["X-API-Key"], "dispatch-key")

    def test_platform_key_is_required_under_either_supported_name(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "DISPATCH_WRITE_API_KEY"):
            platform_qa.build_headers({})

    def test_status_update_reports_partial_failure(self) -> None:
        def sender(_url, _headers, body, _timeout, _retries):
            return {
                "code": 200,
                "data": {
                    "requested": 2,
                    "updated": 1,
                    "updated_tasks": [{"task_id": body["task_ids"][0]}],
                    "failed": [{"task_id": body["task_ids"][1], "reason": "wrong state"}],
                },
            }

        result, rc = platform_qa.run_operation(
            "mark-in-progress",
            {"user_id": "user-1", "task_ids": ["TASK-ONE", "TASK-TWO"]},
            {
                "TRAJ_REPAIR_BASE_URL": "https://platform.example",
                "TRAJ_REPAIR_API_KEY": "api-key",
            },
            30,
            0,
            sender=sender,
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["qa_status"], "质检中")
        self.assertEqual(len(result["failed"]), 1)

    def test_status_machine_payloads_are_explicit(self) -> None:
        context = {"user_id": "user-1", "task_ids": ["TASK-ONE"]}
        self.assertEqual(
            platform_qa.update_body(context, "质检中"),
            {"qa_user_id": "user-1", "task_ids": ["TASK-ONE"], "qa_status": "质检中"},
        )
        self.assertEqual(
            platform_qa.update_body(context, "质检完成")["qa_status"],
            "质检完成",
        )


class OssTransferTests(unittest.TestCase):
    def test_normalize_full_data_task_preserves_sessions_only_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "TASK-ONE"
            sessions = task / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "1.json").write_text("{}\n", encoding="utf-8")

            result = oss_transfer.normalize_full_data_task(task)

            self.assertEqual(result["status"], "sessions_layout_preserved")
            self.assertTrue((sessions / "1.json").is_file())
            for directory in ("original_sessions", "convert-sessions", "clean_sessions"):
                self.assertFalse((task / directory).exists())

    def test_normalize_full_data_task_preserves_partial_compatibility_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "TASK-ONE"
            sessions = task / "sessions"
            original = task / "original_sessions"
            sessions.mkdir(parents=True)
            original.mkdir(parents=True)
            (sessions / "1.json").write_text("session\n", encoding="utf-8")
            (original / "raw.jsonl").write_text("raw\n", encoding="utf-8")

            result = oss_transfer.normalize_full_data_task(task)

            self.assertEqual(result["status"], "sessions_layout_preserved")
            self.assertEqual(result["preserved_directories"], ["original_sessions"])
            self.assertEqual((original / "raw.jsonl").read_text(encoding="utf-8"), "raw\n")
            self.assertFalse((task / "convert-sessions").exists())
            self.assertFalse((task / "clean_sessions").exists())

    def test_build_after_task_preserves_full_tree_and_replaces_only_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            full_data = base / "full" / "TASK-ONE"
            source_sessions = full_data / "sessions"
            source_sessions.mkdir(parents=True)
            (source_sessions / "source.json").write_text("source\n", encoding="utf-8")
            for directory, filename in (
                ("original_sessions", "original.jsonl"),
                ("convert-sessions", "converted.json"),
                ("clean_sessions", "clean.json"),
                ("report", "pipeline_report_summary.md"),
                (".chain_logs", "pipeline.log"),
                ("future_artifacts", "unknown.bin"),
            ):
                target = full_data / directory
                target.mkdir(parents=True)
                (target / filename).write_text(f"{directory}\n", encoding="utf-8")
            (full_data / "skills" / "full-skill").mkdir(parents=True)
            (full_data / "skills" / "full-skill" / "SKILL.md").write_text("full skill\n", encoding="utf-8")
            (full_data / "root-report.md").write_text("root report\n", encoding="utf-8")
            (full_data / ".hidden-state").write_text("hidden\n", encoding="utf-8")
            aggregate = base / "aggregate" / "TASK-ONE"
            (aggregate / "sessions").mkdir(parents=True)
            (aggregate / "sessions" / "repaired.json").write_text("repaired\n", encoding="utf-8")
            (aggregate / "repo").mkdir()
            (aggregate / "repo" / "README.md").write_text("repo\n", encoding="utf-8")
            (aggregate / "docs").mkdir()
            (aggregate / "docs" / "guide.md").write_text("docs\n", encoding="utf-8")
            (aggregate / "metadata.json").write_text('{"task": "TASK-ONE"}\n', encoding="utf-8")
            after = base / "after" / "TASK-ONE"

            assembled = oss_transfer.build_after_task(full_data, aggregate, after)
            completion = oss_transfer.complete_after_task_sessions(full_data, after)

            self.assertEqual(assembled["aggregate_session_count"], 1)
            self.assertEqual(
                assembled["supplemented_support_files"],
                ["repo", "docs", "metadata.json"],
            )
            self.assertEqual(completion["mode"], "complete_missing_clean_sessions")
            self.assertEqual(completion["added"], 1)
            self.assertEqual(completion["final_sessions"], 2)
            self.assertTrue((after / "sessions" / "repaired.json").is_file())
            self.assertTrue((after / "sessions" / "clean.json").is_file())
            self.assertFalse((after / "sessions" / "source.json").exists())
            self.assertTrue((source_sessions / "source.json").is_file())
            self.assertTrue((after / "repo" / "README.md").is_file())
            self.assertTrue((after / "docs" / "guide.md").is_file())
            self.assertTrue((after / "metadata.json").is_file())
            self.assertTrue((after / "skills" / "full-skill" / "SKILL.md").is_file())
            self.assertEqual((after / "root-report.md").read_text(encoding="utf-8"), "root report\n")
            self.assertEqual((after / ".hidden-state").read_text(encoding="utf-8"), "hidden\n")
            for directory, filename in (
                ("original_sessions", "original.jsonl"),
                ("convert-sessions", "converted.json"),
                ("clean_sessions", "clean.json"),
                ("report", "pipeline_report_summary.md"),
                (".chain_logs", "pipeline.log"),
                ("future_artifacts", "unknown.bin"),
            ):
                self.assertTrue((after / directory / filename).is_file())

    def test_task_support_supplement_does_not_overwrite_full_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            full_data = base / "full"
            aggregate = base / "aggregate"
            after = base / "after"
            for root, value in ((full_data, "full"), (aggregate, "aggregate")):
                (root / "repo").mkdir(parents=True)
                (root / "repo" / "value.txt").write_text(value, encoding="utf-8")
                (root / "docs").mkdir()
                (root / "docs" / "value.txt").write_text(value, encoding="utf-8")
                (root / "metadata.json").write_text(value, encoding="utf-8")
            after.mkdir()
            for name in ("repo", "docs"):
                shutil.copytree(full_data / name, after / name)
            shutil.copy2(full_data / "metadata.json", after / "metadata.json")

            supplemented = oss_transfer.supplement_task_support_files(
                full_data,
                aggregate,
                after,
            )

            self.assertEqual(supplemented, [])
            self.assertEqual((after / "repo" / "value.txt").read_text(encoding="utf-8"), "full")
            self.assertEqual((after / "docs" / "value.txt").read_text(encoding="utf-8"), "full")
            self.assertEqual((after / "metadata.json").read_text(encoding="utf-8"), "full")

    def test_download_refuses_incomplete_existing_destination_without_overwrite(self) -> None:
        class UnusedBucket:
            def get_object_to_file(self, _key, _path):
                raise AssertionError("OSS must not be contacted")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "tasks"
            incomplete = root / "TASK-ONE"
            incomplete.mkdir(parents=True)
            (incomplete / "keep.txt").write_text("keep\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                oss_transfer.download_one(
                    UnusedBucket(),
                    "TASK-ONE",
                    root,
                    base / "work",
                    oss_transfer.DEFAULT_PREFIX,
                    overwrite=False,
                    keep_zip=False,
                )
            self.assertTrue((incomplete / "keep.txt").is_file())

    def test_safe_extract_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = base / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../escape.txt", "bad")
            with self.assertRaises(RuntimeError):
                oss_transfer.safe_extract(archive, base / "out")
            self.assertFalse((base / "escape.txt").exists())

    def test_zip_task_keeps_task_root_and_preserves_pipeline_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task = base / "TASK-ONE"
            (task / "sessions").mkdir(parents=True)
            (task / "sessions" / "one.json").write_text("{}\n", encoding="utf-8")
            (task / "pipeline_runs").mkdir()
            (task / "pipeline_runs" / "internal.json").write_text("{}\n", encoding="utf-8")
            archive = base / "out" / "TASK-ONE.zip"
            count = oss_transfer.zip_task(task, archive)
            self.assertEqual(count, 2)
            with zipfile.ZipFile(archive) as handle:
                self.assertEqual(
                    handle.namelist(),
                    ["TASK-ONE/pipeline_runs/internal.json", "TASK-ONE/sessions/one.json"],
                )

    def test_fake_oss_upload_download_round_trip(self) -> None:
        class MemoryBucket:
            def __init__(self) -> None:
                self.objects: dict[str, bytes] = {}

            def object_exists(self, key: str) -> bool:
                return key in self.objects

            def put_object_from_file(self, key: str, path: str) -> None:
                self.objects[key] = Path(path).read_bytes()

            def get_object_to_file(self, key: str, path: str) -> None:
                destination = Path(path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(self.objects[key])

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_root = base / "source"
            task = source_root / "TASK-ONE"
            (task / "sessions").mkdir(parents=True)
            (task / "sessions" / "one.json").write_text('{"messages": []}\n', encoding="utf-8")
            (task / "pipeline_runs").mkdir()
            (task / "pipeline_runs" / "internal.json").write_text("internal\n", encoding="utf-8")

            bucket = MemoryBucket()
            upload = oss_transfer.upload_one(
                bucket,
                "TASK-ONE",
                task,
                base / "upload-work",
                "repo/",
                overwrite=False,
                keep_zip=False,
            )
            self.assertEqual(upload["status"], "uploaded")
            self.assertEqual(list(bucket.objects), ["repo/TASK-ONE.zip"])

            destination_root = base / "downloaded"
            download = oss_transfer.download_one(
                bucket,
                "TASK-ONE",
                destination_root,
                base / "download-work",
                "repo/",
                overwrite=False,
                keep_zip=False,
            )
            self.assertEqual(download["status"], "downloaded")
            downloaded_task = destination_root / "TASK-ONE"
            self.assertEqual(
                (downloaded_task / "sessions" / "one.json").read_text(encoding="utf-8"),
                '{"messages": []}\n',
            )
            # Uploads are verbatim: pipeline_runs round-trips too.
            self.assertEqual(
                (downloaded_task / "pipeline_runs" / "internal.json").read_text(encoding="utf-8"),
                "internal\n",
            )

    def test_prepare_pipeline_keeps_before_sources_and_completes_after_sessions(self) -> None:
        class MemoryBucket:
            def __init__(self) -> None:
                self.objects: dict[str, bytes] = {}

            def object_exists(self, key: str) -> bool:
                return key in self.objects

            def put_object_from_file(self, key: str, path: str) -> None:
                self.objects[key] = Path(path).read_bytes()

            def get_object_to_file(self, key: str, path: str) -> None:
                destination = Path(path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(self.objects[key])

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            full_source = base / "full-source" / "TASK-ONE"
            for name in ("original_sessions", "convert-sessions", "clean_sessions"):
                (full_source / name).mkdir(parents=True)
            (full_source / "original_sessions" / "original.json").write_text("original\n", encoding="utf-8")
            (full_source / "convert-sessions" / "convert.json").write_text("convert\n", encoding="utf-8")
            (full_source / "clean_sessions" / "one.json").write_text("clean-one\n", encoding="utf-8")
            (full_source / "clean_sessions" / "two.json").write_text("clean-two\n", encoding="utf-8")

            aggregate_source = base / "aggregate-source" / "TASK-ONE"
            (aggregate_source / "sessions").mkdir(parents=True)
            (aggregate_source / "sessions" / "one.json").write_text("aggregate-one\n", encoding="utf-8")

            bucket = MemoryBucket()
            oss_transfer.upload_one(bucket, "TASK-ONE", full_source, base / "full-work", "full-clean/", False, False)
            oss_transfer.upload_one(bucket, "TASK-ONE", aggregate_source, base / "aggregate-work", "aggregate/", False, False)

            before_root = base / "batch_before_tasks"
            after_root = base / "batch_after_tasks"
            prepared = oss_transfer.prepare_pipeline_one(
                bucket,
                "TASK-ONE",
                before_root,
                after_root,
                base / "prepare-work",
                "full-clean/",
                "aggregate/",
                False,
                False,
            )
            self.assertEqual(prepared["status"], "prepared")
            self.assertEqual(
                (Path(prepared["after_task_path"]) / "sessions" / "one.json").read_text(encoding="utf-8"),
                "aggregate-one\n",
            )
            self.assertEqual(
                (before_root / "TASK-ONE" / "full_clean" / "clean_sessions" / "two.json").read_text(encoding="utf-8"),
                "clean-two\n",
            )
            (Path(prepared["after_task_path"]) / "sessions" / "one.json").write_text("repaired-one\n", encoding="utf-8")
            completion = oss_transfer.complete_after_task_sessions(
                Path(prepared["full_clean_task_path"]),
                Path(prepared["after_task_path"]),
            )
            self.assertEqual(completion["added"], 1)
            self.assertEqual(
                (Path(prepared["after_task_path"]) / "sessions" / "one.json").read_text(encoding="utf-8"),
                "repaired-one\n",
            )
            self.assertEqual(
                (Path(prepared["after_task_path"]) / "sessions" / "two.json").read_text(encoding="utf-8"),
                "clean-two\n",
            )
            self.assertEqual(
                (before_root / "TASK-ONE" / "aggregate" / "sessions" / "one.json").read_text(encoding="utf-8"),
                "aggregate-one\n",
            )
            with self.assertRaisesRegex(RuntimeError, "never overwrites batch_before_tasks"):
                oss_transfer.prepare_pipeline_one(
                    bucket,
                    "TASK-ONE",
                    before_root,
                    after_root,
                    base / "prepare-work-2",
                    "full-clean/",
                    "aggregate/",
                    True,
                    False,
                )

    def test_upload_task_outputs_splits_full_and_sessions_archives(self) -> None:
        class MemoryBucket:
            def __init__(self) -> None:
                self.objects: dict[str, bytes] = {}

            def object_exists(self, key: str) -> bool:
                return key in self.objects

            def put_object_from_file(self, key: str, path: str) -> None:
                self.objects[key] = Path(path).read_bytes()

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task = base / "TASK-ONE"
            (task / "original_sessions").mkdir(parents=True)
            (task / "convert-sessions").mkdir(parents=True)
            (task / "sessions").mkdir(parents=True)
            (task / "original_sessions" / "original.json").write_text("original\n", encoding="utf-8")
            (task / "convert-sessions" / "converted.json").write_text("converted\n", encoding="utf-8")
            (task / "sessions" / "one.json").write_text("one\n", encoding="utf-8")
            (task / "report").mkdir()
            (task / "report" / "summary.md").write_text("report\n", encoding="utf-8")
            (task / ".chain_logs").mkdir()
            (task / ".chain_logs" / "pipeline.log").write_text("log\n", encoding="utf-8")
            (task / "empty-directory").mkdir()
            (task / "root-report.txt").write_text("root\n", encoding="utf-8")
            (task / "repo").mkdir()
            (task / "repo" / "README.md").write_text("repo\n", encoding="utf-8")
            (task / "repo" / ".git").mkdir()
            (task / "repo" / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (task / "repo" / "__pycache__").mkdir()
            (task / "repo" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"pyc\n")
            (task / "docs").mkdir()
            (task / "docs" / "guide.md").write_text("docs\n", encoding="utf-8")
            (task / "metadata.json").write_text('{"task": "TASK-ONE"}\n', encoding="utf-8")
            bucket = MemoryBucket()
            result = oss_transfer.upload_task_outputs(
                bucket,
                "TASK-ONE",
                task,
                base / "upload-work",
                "full-out/",
                "sessions-out/",
                True,
                False,
            )
            self.assertEqual(result["status"], "uploaded")
            self.assertEqual(set(bucket.objects), {"full-out/TASK-ONE.zip", "sessions-out/TASK-ONE.zip"})
            self.assertEqual(result["sessions"]["support_files"], ["repo", "docs", "metadata.json"])
            with zipfile.ZipFile(io.BytesIO(bucket.objects["full-out/TASK-ONE.zip"])) as archive:
                # The full archive is a recursive mirror of every TASK entry.
                self.assertEqual(
                    archive.namelist(),
                    [
                        "TASK-ONE/.chain_logs/pipeline.log",
                        "TASK-ONE/convert-sessions/converted.json",
                        "TASK-ONE/docs/guide.md",
                        "TASK-ONE/empty-directory/",
                        "TASK-ONE/metadata.json",
                        "TASK-ONE/original_sessions/original.json",
                        "TASK-ONE/repo/.git/HEAD",
                        "TASK-ONE/repo/__pycache__/mod.cpython-312.pyc",
                        "TASK-ONE/repo/README.md",
                        "TASK-ONE/report/summary.md",
                        "TASK-ONE/root-report.txt",
                        "TASK-ONE/sessions/one.json",
                    ],
                )
            with zipfile.ZipFile(io.BytesIO(bucket.objects["sessions-out/TASK-ONE.zip"])) as archive:
                # The sessions archive intentionally remains sessions plus
                # repo/docs/metadata.json; it is not the full TASK archive.
                self.assertEqual(
                    archive.namelist(),
                    [
                        "TASK-ONE/sessions/one.json",
                        "TASK-ONE/repo/.git/HEAD",
                        "TASK-ONE/repo/__pycache__/mod.cpython-312.pyc",
                        "TASK-ONE/repo/README.md",
                        "TASK-ONE/docs/guide.md",
                        "TASK-ONE/metadata.json",
                    ],
                )

    def test_build_after_task_flags_unaligned_when_aggregate_has_no_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            full_clean = base / "full_clean"
            for name in ("original_sessions", "convert-sessions", "clean_sessions"):
                (full_clean / name).mkdir(parents=True)
            (full_clean / "clean_sessions" / "one.json").write_text("clean\n", encoding="utf-8")
            aggregate = base / "aggregate"
            aggregate.mkdir()  # no sessions directory -> cannot align
            with self.assertRaises(oss_transfer.TaskAlignmentError):
                oss_transfer.build_after_task(full_clean, aggregate, base / "after")

    def test_prepare_pipeline_one_flags_unaligned_when_aggregate_object_missing(self) -> None:
        class FakeNoSuchKey(Exception):
            status = 404

        class MemoryBucket:
            def __init__(self) -> None:
                self.objects: dict[str, bytes] = {}

            def object_exists(self, key: str) -> bool:
                return key in self.objects

            def put_object_from_file(self, key: str, path: str) -> None:
                self.objects[key] = Path(path).read_bytes()

            def get_object_to_file(self, key: str, path: str) -> None:
                if key not in self.objects:
                    raise FakeNoSuchKey(key)
                destination = Path(path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(self.objects[key])

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            full_source = base / "full-source" / "TASK-ONE"
            for name in ("original_sessions", "convert-sessions", "clean_sessions"):
                (full_source / name).mkdir(parents=True)
            (full_source / "original_sessions" / "original.json").write_text("original\n", encoding="utf-8")
            (full_source / "convert-sessions" / "convert.json").write_text("convert\n", encoding="utf-8")
            (full_source / "clean_sessions" / "one.json").write_text("clean\n", encoding="utf-8")

            bucket = MemoryBucket()
            # Only 全量清洗成功 is uploaded; 聚合任务 is absent for this TASK.
            oss_transfer.upload_one(bucket, "TASK-ONE", full_source, base / "full-work", "full-clean/", False, False)

            with self.assertRaises(oss_transfer.TaskAlignmentError):
                oss_transfer.prepare_pipeline_one(
                    bucket,
                    "TASK-ONE",
                    base / "batch_before_tasks",
                    base / "batch_after_tasks",
                    base / "prepare-work",
                    "full-clean/",
                    "aggregate/",
                    False,
                    False,
                )

    def test_main_prepare_records_unaligned_and_keeps_processing_batch(self) -> None:
        class FakeNoSuchKey(Exception):
            status = 404

        class MemoryBucket:
            def __init__(self) -> None:
                self.objects: dict[str, bytes] = {}

            def object_exists(self, key: str) -> bool:
                return key in self.objects

            def put_object_from_file(self, key: str, path: str) -> None:
                self.objects[key] = Path(path).read_bytes()

            def get_object_to_file(self, key: str, path: str) -> None:
                if key not in self.objects:
                    raise FakeNoSuchKey(key)
                destination = Path(path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(self.objects[key])

        def seed_full(bucket: MemoryBucket, base: Path, task_id: str) -> None:
            source = base / "seed" / "full" / task_id
            for name in ("original_sessions", "convert-sessions", "clean_sessions"):
                (source / name).mkdir(parents=True)
            (source / "original_sessions" / "original.json").write_text("original\n", encoding="utf-8")
            (source / "convert-sessions" / "convert.json").write_text("convert\n", encoding="utf-8")
            (source / "clean_sessions" / "one.json").write_text("clean\n", encoding="utf-8")
            oss_transfer.upload_one(bucket, task_id, source, base / f"fw-{task_id}", "full-clean/", False, False)

        def seed_aggregate(bucket: MemoryBucket, base: Path, task_id: str) -> None:
            source = base / "seed" / "aggregate" / task_id
            (source / "sessions").mkdir(parents=True)
            (source / "sessions" / "one.json").write_text("aggregate\n", encoding="utf-8")
            oss_transfer.upload_one(bucket, task_id, source, base / f"aw-{task_id}", "aggregate/", False, False)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bucket = MemoryBucket()
            # TASK-OK has both folders; TASK-BAD is missing 聚合任务 -> unaligned.
            seed_full(bucket, base, "TASK-OK")
            seed_aggregate(bucket, base, "TASK-OK")
            seed_full(bucket, base, "TASK-BAD")

            run_dir = base / "run"
            context_path = base / "context.json"
            context_path.write_text(json.dumps({
                "task_ids": ["TASK-OK", "TASK-BAD"],
                "root": str(base),
                "run_dir": str(run_dir),
                "before_root": str(base / "batch_before_tasks"),
                "after_root": str(base / "batch_after_tasks"),
            }), encoding="utf-8")
            result_path = base / "result.json"
            env_path = base / "oss.env"
            env_path.write_text(
                "OSS_ENDPOINT=oss-cn-test.aliyuncs.com\n"
                "OSS_ACCESS_KEY_ID=id\n"
                "OSS_ACCESS_KEY_SECRET=secret\n"
                "OSS_BUCKET=bucket\n"
                "OSS_FULL_DATA_PREFIX=full-clean/\n"
                "OSS_AGGREGATE_DATA_PREFIX=aggregate/\n",
                encoding="utf-8",
            )

            original_build_bucket = oss_transfer.build_bucket
            oss_transfer.build_bucket = lambda config: bucket
            try:
                code = oss_transfer.main([
                    "prepare",
                    "--context", str(context_path),
                    "--result", str(result_path),
                    "--env-file", str(env_path),
                    "--workers", "1",
                ])
            finally:
                oss_transfer.build_bucket = original_build_bucket

            # Unaligned TASKs are not hard failures, so the batch exits cleanly.
            self.assertEqual(code, 0)
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            statuses = {row["task_id"]: row["status"] for row in payload["results"]}
            self.assertEqual(statuses["TASK-OK"], "prepared")
            self.assertEqual(statuses["TASK-BAD"], "unaligned")
            self.assertEqual([row["task_id"] for row in payload["unaligned"]], ["TASK-BAD"])
            self.assertEqual(payload["failed"], [])
            # The healthy TASK was still assembled despite the unaligned sibling.
            self.assertTrue((base / "batch_after_tasks" / "TASK-OK" / "sessions").is_dir())

    def test_stage_updater_marks_unaligned_sub_status(self) -> None:
        class RecordingDatabase:
            def __init__(self) -> None:
                self.calls: list[tuple[list[str], str, str]] = []

            def set_task_sub_status(self, task_ids, status, status_name) -> None:
                self.calls.append((list(task_ids), status, status_name))

        database = RecordingDatabase()
        updater = TaskStageUpdater(database, dry_run=False)
        updater.mark_unaligned(["TASK-BAD", "", "  "])
        self.assertEqual(database.calls, [(["TASK-BAD"], "无法对齐", "聚合任务与全量清洗成功无法对齐")])

        database.calls.clear()
        TaskStageUpdater(database, dry_run=True).mark_unaligned(["TASK-BAD"])
        self.assertEqual(database.calls, [])

    def test_stage_updater_split_preserves_failed_task_status(self) -> None:
        class RecordingDatabase:
            def __init__(self) -> None:
                self.calls: list[tuple[list[str], str, str]] = []

            def set_task_sub_status(self, task_ids, status, status_name) -> None:
                self.calls.append((list(task_ids), status, status_name))

        database = RecordingDatabase()
        updater = TaskStageUpdater(database, dry_run=False)
        stage = SimpleNamespace(
            name="split_success_fail_tasks",
            items=[
                {"item": "TASK-GOOD", "status": "completed", "bucket": "success_task"},
                {"item": "TASK-FAILED", "status": "completed", "bucket": "fail_task"},
                {"item": "TASK-COPY-ERROR", "status": "failed", "bucket": "fail_task"},
            ],
        )

        updater.mark_split_items(
            stage,
            {
                "TASK-GOOD": object(),
                "TASK-FAILED": object(),
                "TASK-COPY-ERROR": object(),
            },
        )

        self.assertEqual(
            database.calls,
            [
                (
                    ["TASK-GOOD"],
                    "split_success_fail_tasks-success",
                    "拆分成功和失败任务-success",
                ),
                (
                    ["TASK-COPY-ERROR"],
                    "split_success_fail_tasks-error",
                    "拆分成功和失败任务-error",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
