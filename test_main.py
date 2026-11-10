"""Isolated in-memory scheduling and receipt tests; no real jobs or credentials."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import patch


def load_draft():
    modules = {name: types.ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
        "astrbot.core", "astrbot.core.star", "astrbot.core.star.star")}
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = NS(llm_tool=lambda **kw: lambda fn: fn)
    class Star:
        def __init__(self, context):
            pass
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].register = lambda *args: lambda cls: cls
    modules["astrbot.core.star.star"].star_registry = []
    path = Path(__file__).resolve().parent / "main.py"
    spec = importlib.util.spec_from_file_location("reminder_draft_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module, modules


main, STUBS = load_draft()
REGISTRY = STUBS["astrbot.core.star.star"].star_registry
DUE = datetime(2099, 7, 12, 14, 23, tzinfo=main.TZ)
DAILY = dict(run_once=False, cron_expression="23 14 * * *")


class Event:
    def __init__(self, session="session-A", sender="alice", admin=True):
        self.unified_msg_origin, self.sender, self.admin = session, sender, admin
    def is_admin(self):
        return self.admin
    def get_sender_id(self):
        return self.sender


class Manager:
    def __init__(self):
        self.jobs = {}
        self.db = self
        self.adds = 0
        self.deleted = []
        self.delete_error = False
    async def add_active_job(self, **kw):
        self.adds += 1
        jid = f"job-{self.adds}"
        job = NS(job_id=jid, next_run_time=kw.pop("run_at") or DUE, **kw)
        self.jobs[jid] = job
        return job
    async def list_jobs(self):
        return list(self.jobs.values())
    async def get_cron_job(self, jid):
        return self.jobs.get(jid)
    def get_next_run_time(self, jid):
        return self.jobs[jid].next_run_time if jid in self.jobs else None
    async def delete_job(self, jid):
        self.deleted.append(jid)
        if self.delete_error:
            raise RuntimeError("delete failed")
        self.jobs.pop(jid, None)


class Delivery:
    def __init__(self, mgr, rows=None):
        self.mgr = mgr
        self.rows = {} if rows is None else rows
        self.fail = ""
        self.cancel_error = False
        self.registrations = 0
        self.cancellations = []
        self.occurrences = {}
    async def register(self, job):
        self.registrations += 1
        if self.fail == "before":
            raise RuntimeError("register failed")
        p = job.payload
        row = dict(id=job.job_id, session=p["session"], sender=p["sender_id"],
                   note=p["note"], run_once=job.run_once)
        if job.run_once:
            due = datetime.fromisoformat(p["run_at"]).timestamp()
            row.update(due=due, status="pending", attempts=0, message_id="", error="",
                       last_attempt=None, finished=None, next_attempt=due)
        else:
            due = self.mgr.get_next_run_time(job.job_id).timestamp()
            row.update(due=due, next_due=due, status="active", cron_expression=job.cron_expression,
                       timezone=job.timezone, latest_delivery=None)
        self.rows[job.job_id] = row
        if self.fail == "after":
            raise RuntimeError("update failed after durable insert")
        if self.fail == "cancelled":
            raise asyncio.CancelledError()
        job.payload = dict(p, note="只调用 shrink_deliver_reminder", delivery_id=job.job_id)
        return self.get(job.job_id)
    def get(self, jid):
        return self.rows.get(jid)
    def listing(self):
        return list(self.rows.values())
    async def cancel(self, jid, session, sender):
        self.cancellations.append((jid, session, sender))
        if self.cancel_error:
            raise RuntimeError("cancel failed")
        row = self.rows[jid]
        if row["session"] != session or row["sender"] != sender:
            raise ValueError("owner mismatch")
        if row["status"] not in {"sent", "missed"}:
            row["status"] = "cancelled"
        for key, occurrence in self.occurrences.items():
            if key.startswith(jid + ":") and occurrence["status"] not in {"sent", "missed"}:
                occurrence["status"] = "cancelled"
        await self.mgr.delete_job(jid)
        return dict(id=jid, status=row["status"])


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patcher = patch.dict(sys.modules, STUBS)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.mgr = Manager()
        self.service = Delivery(self.mgr)
        self.activate(self.service)
        self.plugin = main.ReminderExtension(NS(cron_manager=self.mgr))
        self.plugin._service = self.service
        self.plugin._service = self.service
        self.event = Event()

    def activate(self, service, activated=True):
        self.service = service if activated else None
        if hasattr(self, 'plugin'):
            self.plugin._service = self.service

    async def call(self, action="create", event=None, **kw):
        if action == "create":
            kw.setdefault("content", "测试事项")
            kw.setdefault("time_str", DUE.isoformat())
        return json.loads(await self.plugin.reminder(event or self.event, action, **kw))

    async def test_create_registers_and_returns_pending_not_sent(self):
        result = await self.call()
        self.assertTrue(result["ok"])
        self.assertEqual(result["reminder"]["status"], "pending")
        self.assertEqual(result["reminder"]["message_id"], "")
        self.assertTrue(result["reminder"]["run_once"])
        self.assertTrue(result["reminder"]["delivery_registered"])
        self.assertEqual(result["reminder"]["note"], "测试事项")
        self.assertEqual(self.mgr.jobs[result["job_id"]].payload["delivery_id"], result["job_id"])

    async def test_daily_registers_and_deduplicates_after_payload_rewrite(self):
        results = await asyncio.gather(*(self.call(**DAILY) for _ in range(3)))
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(self.mgr.adds, 1)
        self.assertEqual(self.service.registrations, 1)
        self.assertEqual(sum(r["deduplicated"] for r in results), 2)
        item = results[0]["reminder"]
        self.assertFalse(item["run_once"])
        self.assertTrue(item["delivery_registered"])
        self.assertEqual(item["status"], "active")
        self.assertEqual(item["cron_expression"], DAILY["cron_expression"])
        self.assertEqual(item["next_due"], DUE.timestamp())
        self.assertIsNone(item["latest_delivery"])
        self.assertNotIn("run_at", item)
        self.assertNotIn("run_at", self.mgr.jobs[item["job_id"]].payload)

    async def test_concurrent_one_shot_dedup_after_core_rewrites_note(self):
        results = await asyncio.gather(self.call(), self.call(), self.call())
        self.assertEqual(self.mgr.adds, 1)
        self.assertEqual(self.service.registrations, 1)
        self.assertEqual(sum(r["deduplicated"] for r in results), 2)

    async def test_daily_and_once_dedup_are_isolated_in_both_orders(self):
        for daily_first in (True, False):
            with self.subTest(daily_first=daily_first):
                self.mgr.jobs.clear()
                self.service.rows.clear()
                first = await self.call(**(DAILY if daily_first else {}))
                second = await self.call(**({} if daily_first else DAILY))
                self.assertTrue(first["ok"] and second["ok"])
                self.assertNotEqual(first["job_id"], second["job_id"])
                for options, expected in ((DAILY, first if daily_first else second),
                                           ({}, second if daily_first else first)):
                    result = await self.call(**options)
                    self.assertTrue(result["deduplicated"])
                    self.assertEqual(result["job_id"], expected["job_id"])

    async def test_daily_listing_latest_receipt_and_cancel_preserves_history(self):
        jid = (await self.call(**DAILY))["job_id"]
        latest = dict(id=jid + ":1", status="sent", message_id="receipt-id", due=1, attempts=1)
        self.service.rows[jid]["latest_delivery"] = latest
        self.service.occurrences = {jid + ":1": latest, jid + ":2": {"status": "missed"},
                                    jid + ":3": {"status": "pending"}}
        listing = await self.call("list")
        self.assertEqual(listing["count"], 1)
        item = listing["reminders"][0]
        self.assertEqual(item["status"], "active")
        self.assertEqual(item["latest_delivery"], latest)
        self.assertFalse(item["run_once"])
        self.assertEqual(item["next_due"], DUE.timestamp())
        self.assertEqual(item["cron_expression"], DAILY["cron_expression"])
        result = await self.call("delete", job_id=jid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reminder"]["status"], "cancelled")
        self.assertEqual(result["reminder"]["latest_delivery"]["message_id"], "receipt-id")
        self.assertEqual([r["status"] for r in self.service.occurrences.values()],
                         ["sent", "missed", "cancelled"])
        self.assertNotIn(jid, self.mgr.jobs)
        self.assertEqual((await self.call("list"))["reminders"][0]["status"], "cancelled")

    async def test_registration_failures_compensate_for_both_kinds(self):
        for options in ({}, DAILY):
            for failure in ("before", "after"):
                with self.subTest(options=options, failure=failure):
                    self.service.fail = failure
                    result = await self.call(**options)
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["compensated"])
                    self.assertFalse(self.mgr.jobs)
                    row = self.service.get(result["job_id"])
                    if failure == "after":
                        self.assertEqual(row["status"], "cancelled")
                    else:
                        self.assertIsNone(row)

    async def test_cleanup_failure_is_not_reported_as_success(self):
        for options in ({}, DAILY):
            self.service.fail = "before"
            self.mgr.delete_error = True
            result = await self.call(**options)
            self.assertFalse(result["ok"])
            self.assertFalse(result["compensated"])
            self.assertIn("原生任务清理失败", result["cleanup_errors"][0])

    async def test_cancel_failure_still_attempts_native_cleanup(self):
        for options in ({}, DAILY):
            self.service.fail = "after"
            self.service.cancel_error = True
            result = await self.call(**options)
            self.assertFalse(result["compensated"])
            self.assertFalse(self.mgr.jobs)
            self.assertIn("回执取消失败", result["cleanup_errors"][0])

    async def test_task_cancellation_also_compensates(self):
        for options in ({}, DAILY):
            self.service.fail = "cancelled"
            with self.assertRaises(asyncio.CancelledError):
                await self.call(**options)
            self.assertFalse(self.mgr.jobs)
            self.assertTrue(all(r["status"] == "cancelled" for r in self.service.rows.values()))

    async def test_non_admin_all_actions_rejected(self):
        for action in ("create", "list", "delete"):
            result = await self.call(action, event=Event(admin=False))
            self.assertFalse(result["ok"])
        self.assertEqual(self.mgr.adds, 0)

    async def test_cross_sender_and_cross_session_isolation_for_both_kinds(self):
        for options in ({}, DAILY):
            created = await self.call(**options)
            jid = created["job_id"]
            for event in (Event(sender="bob"), Event(session="session-B")):
                self.assertEqual((await self.call("list", event=event))["count"], 0)
                self.assertFalse((await self.call("delete", event=event, job_id=jid))["ok"])
                own = await self.call(event=event, **options)
                self.assertTrue(own["ok"])
                self.assertFalse(own["deduplicated"])
                await self.call("delete", event=event, job_id=own["job_id"])
                # Remove fake durable history for the next isolation assertion.
                self.service.rows.pop(own["job_id"])
            self.assertIn(jid, self.mgr.jobs)
            self.assertNotEqual(self.service.get(jid)["status"], "cancelled")
        self.assertFalse((await self.call("delete", job_id="missing"))["ok"])

    async def test_restart_reads_all_persisted_one_shot_status_facts(self):
        jid = (await self.call())["job_id"]
        self.mgr.jobs.clear()
        persisted = self.service.rows
        self.activate(Delivery(self.mgr, rows=persisted))
        self.plugin = main.ReminderExtension(NS(cron_manager=self.mgr))
        self.plugin._service = self.service
        for status in ("pending", "retry", "unknown", "sending", "sent", "cancelled", "missed"):
            persisted[jid].update(status=status, message_id="real-id" if status == "sent" else "",
                                  attempts=2, error="transport uncertain" if status == "unknown" else "")
            item = (await self.call("list"))["reminders"][0]
            self.assertEqual(item["status"], status)
            self.assertEqual(item["message_id"], persisted[jid]["message_id"])
            self.assertEqual(item["error"], persisted[jid]["error"])
            self.assertEqual(item["attempts"], 2)

    async def test_daily_restart_without_native_job_reads_series_and_occurrence_status(self):
        jid = (await self.call(**DAILY))["job_id"]
        self.mgr.jobs.clear()
        self.activate(Delivery(self.mgr, rows=self.service.rows))
        self.plugin = main.ReminderExtension(NS(cron_manager=self.mgr))
        self.plugin._service = self.service
        for status in ("pending", "retry", "unknown", "sent", "missed"):
            self.service.rows[jid]["latest_delivery"] = dict(id=jid + ":1", status=status)
            item = (await self.call("list"))["reminders"][0]
            self.assertEqual(item["status"], "active")
            self.assertEqual(item["latest_delivery"]["status"], status)
        self.assertTrue((await self.call("delete", job_id=jid))["ok"])

    async def test_delete_one_shot_without_native_job_preserves_sent(self):
        jid = (await self.call())["job_id"]
        self.mgr.jobs.clear()
        self.assertEqual((await self.call("delete", job_id=jid))["status"], "cancelled")
        self.service.rows[jid].update(status="sent", message_id="confirmed-id")
        result = await self.call("delete", job_id=jid)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["reminder"]["message_id"], "confirmed-id")

    async def test_resolve_current_core_and_fail_closed_for_both_kinds(self):
        await self.call()
        replacement = Delivery(self.mgr)
        self.activate(replacement)
        await self.call(content="新事项")
        self.assertEqual(replacement.registrations, 1)
        self.assertEqual(self.service.registrations, 1)
        self.activate(replacement, activated=False)
        count = self.mgr.adds
        for options in ({}, DAILY):
            self.assertFalse((await self.call(content="不得创建", **options))["ok"])
        self.assertEqual(self.mgr.adds, count)
        self.assertFalse((await self.call("list"))["ok"])

    async def test_legacy_untracked_and_foreign_origin(self):
        for options in ({}, DAILY):
            jid = (await self.call(**options))["job_id"]
            self.service.rows.clear()
            item = (await self.call("list"))["reminders"][0]
            self.assertEqual(item["status"], "untracked")
            self.assertFalse(item["delivery_registered"])
            self.mgr.jobs[jid].payload["origin"] = "unrelated_plugin"
            self.assertEqual((await self.call("list"))["count"], 0)
            self.assertFalse((await self.call("delete", job_id=jid))["ok"])

    async def test_validation_no_side_effects(self):
        for kw in (dict(content=""), dict(time_str="2000-01-01T14:23:00+08:00"),
                   dict(time_str="25:99"), dict(time_str="nonsense"),
                   dict(run_once=False, cron_expression="")):
            self.assertFalse((await self.call(**kw))["ok"])
        self.assertFalse((await self.call("delete"))["ok"])
        self.assertFalse((await self.call("invalid"))["ok"])
        self.assertEqual(self.mgr.adds, 0)

    def test_parser_boundaries(self):
        now = datetime(2030, 4, 2, 15, 0, tzinfo=main.TZ)
        target, err = self.plugin._parse_target_datetime("14:23", now)
        self.assertIsNone(err)
        self.assertEqual(target.day, 3)
        target, err = self.plugin._parse_target_datetime("明天下午2点半", now)
        self.assertIsNone(err)
        self.assertEqual((target.day, target.hour, target.minute), (3, 14, 30))
        self.assertIsNotNone(self.plugin._parse_target_datetime("今天14:23", now)[1])
        self.assertIsNotNone(self.plugin._parse_target_datetime("2030-02-30 14:23", now)[1])


if __name__ == "__main__":
    unittest.main()
