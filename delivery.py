"""Durable delivery receipts and retries driven by AstrBot's native scheduler."""
import asyncio
import json
import time
from datetime import datetime, timezone

from .prompts import render, text as prompt_text


class ReminderDelivery:
    def __init__(self, context, database, compose):
        self.context, self.db, self.compose = context, database, compose
        self.lock = asyncio.Lock()
        self.ticker = None
        self.db.connection.executescript("""
        CREATE TABLE IF NOT EXISTS reminder_deliveries(
          id TEXT PRIMARY KEY,session TEXT NOT NULL,sender TEXT NOT NULL,note TEXT NOT NULL,
          due REAL NOT NULL,next_attempt REAL NOT NULL,status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,text TEXT NOT NULL DEFAULT '',
          message_id TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
          last_attempt REAL,finished REAL);
        CREATE TABLE IF NOT EXISTS reminder_series(
          id TEXT PRIMARY KEY,session TEXT NOT NULL,sender TEXT NOT NULL,note TEXT NOT NULL,
          cron TEXT NOT NULL,timezone TEXT NOT NULL,next_due REAL NOT NULL,status TEXT NOT NULL);
        """)
        with self.db.transaction() as con:
            con.execute("UPDATE reminder_deliveries SET status='unknown',error='进程在发送期间中断' "
                        "WHERE status='sending'")

    async def start(self):
        manager = self.context.cron_manager
        for job in await manager.list_jobs():
            if job.name == "shirnk_delivery_recovery" and job.job_type == "basic":
                await manager.delete_job(job.job_id)
        job = await manager.add_basic_job(
            name="shirnk_delivery_recovery", cron_expression="* * * * *",
            handler=self.tick, description="石英提醒发送恢复", persistent=False)
        self.ticker = job.job_id

    async def register(self, job):
        """Called by authored reminder plugins after native add_active_job."""
        payload = job.payload or {}
        required = ("session", "sender_id", "note", "run_at") if job.run_once else ("session", "sender_id", "note")
        if not all(payload.get(k) for k in required):
            raise ValueError("提醒缺少会话、创建者、内容或时间")
        if not job.run_once:
            due = self.context.cron_manager.get_next_run_time(job.job_id)
            if due is None or due.tzinfo is None or not job.cron_expression:
                raise ValueError("周期提醒缺少有效的下一次执行时间")
            with self.db.transaction() as con:
                con.execute("INSERT OR IGNORE INTO reminder_series VALUES(?,?,?,?,?,?,?,'active')",
                            (job.job_id, payload["session"], str(payload["sender_id"]), payload["note"],
                             job.cron_expression, job.timezone or "Asia/Shanghai", due.timestamp()))
            await self._route(job.job_id, payload)
            return self.get(job.job_id)
        due = datetime.fromisoformat(payload["run_at"])
        if due.tzinfo is None:
            raise ValueError("提醒必须包含时区")
        with self.db.transaction() as con:
            con.execute("INSERT OR IGNORE INTO reminder_deliveries "
                        "(id,session,sender,note,due,next_attempt,status) VALUES(?,?,?,?,?,?,'pending')",
                        (job.job_id, payload["session"], str(payload["sender_id"]),
                         payload["note"], due.timestamp(), due.timestamp()))
        await self._route(job.job_id, payload)
        return self.get(job.job_id)

    async def _route(self, job_id, payload):
        payload = {**payload, "delivery_id": job_id,
                   "note": render("reminders.route", delivery_id=job_id)}
        await self.context.cron_manager.update_job(job_id, payload=payload)

    def get(self, delivery_id):
        series = self.db.rows("SELECT * FROM reminder_series WHERE id=?", (delivery_id,))
        if series:
            row = series[0]
            receipts = self.db.rows(
                "SELECT * FROM reminder_deliveries WHERE id LIKE ? ORDER BY due DESC LIMIT 1",
                (delivery_id + ":%",))
            return {**row, "due": row["next_due"], "run_once": False,
                    "cron_expression": row["cron"], "latest_delivery": receipts[0] if receipts else None}
        rows = self.db.rows("SELECT * FROM reminder_deliveries WHERE id=?", (delivery_id,))
        return {**rows[0], "run_once": True} if rows else None

    def listing(self):
        return [self.get(r["id"]) for r in self.db.rows("SELECT id FROM reminder_series")] + [
            {**r, "run_once": True} for r in self.db.rows(
                "SELECT * FROM reminder_deliveries WHERE instr(id,':')=0 ORDER BY due DESC LIMIT 100")]

    def prepare_occurrences(self, now):
        from apscheduler.triggers.cron import CronTrigger
        from astrbot.core.cron.manager import _normalize_crontab_day_of_week
        for series in self.db.rows("SELECT * FROM reminder_series WHERE status='active' AND next_due<=?", (now,)):
            fields = series["cron"].split()
            fields[4] = _normalize_crontab_day_of_week(fields[4])
            trigger = CronTrigger.from_crontab(" ".join(fields), timezone=series["timezone"])
            due = series["next_due"]
            for _ in range(1441):
                if due > now:
                    break
                overdue = now - due > 86400
                with self.db.transaction() as con:
                    con.execute("INSERT OR IGNORE INTO reminder_deliveries "
                                "(id,session,sender,note,due,next_attempt,status,error) VALUES(?,?,?,?,?,?,?,?)",
                                (f"{series['id']}:{int(due)}", series["session"], series["sender"],
                                 series["note"], due, due, "missed" if overdue else "pending",
                                 "已超过24小时补发窗口" if overdue else ""))
                current = datetime.fromtimestamp(due, timezone.utc)
                # Skip expired history after retaining the first missed occurrence.
                cursor = datetime.fromtimestamp(max(due, now - 86400), timezone.utc)
                following = trigger.get_next_fire_time(None if overdue else current, cursor)
                if following is None:
                    with self.db.transaction() as con:
                        con.execute("UPDATE reminder_series SET status='ended' WHERE id=?", (series["id"],))
                    break
                due = following.timestamp()
                with self.db.transaction() as con:
                    con.execute("UPDATE reminder_series SET next_due=? WHERE id=?", (due, series["id"]))

    async def cancel(self, delivery_id, session, sender):
        row = self.get(delivery_id)
        if not row or row["session"] != session or row["sender"] != str(sender):
            raise ValueError("提醒不存在或不属于当前会话创建者")
        async with self.lock:
            with self.db.transaction() as con:
                con.execute("UPDATE reminder_series SET status='cancelled' WHERE id=?", (delivery_id,))
                con.execute("UPDATE reminder_deliveries SET status='cancelled',finished=? "
                            "WHERE id LIKE ? AND status NOT IN ('sent','missed')",
                            (time.time(), delivery_id + ":%"))
                con.execute("UPDATE reminder_deliveries SET status='cancelled',finished=? "
                            "WHERE id=? AND status NOT IN ('sent','missed')", (time.time(), delivery_id))
            await self.context.cron_manager.delete_job(delivery_id)
        return {"id": delivery_id, "status": self.get(delivery_id)["status"]}

    def client(self, session):
        from astrbot.core.platform.message_session import MessageSession
        parsed = MessageSession.from_str(session)
        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().id == parsed.platform_id and hasattr(platform, "get_client"):
                return platform.get_client(), parsed
        raise ValueError("QQ平台连接不可用")

    async def reconcile(self, row, client, session):
        group = "group" in str(session.message_type).lower()
        api = "get_group_msg_history" if group else "get_friend_msg_history"
        target = {"group_id" if group else "user_id": int(session.session_id.split("_")[-1]),
                  "count": 100}
        history = await asyncio.wait_for(client.call_action(api, **target), 15)
        info = await asyncio.wait_for(client.call_action("get_login_info"), 10)
        messages = history.get("messages", [])
        for message in messages:
            sender = str((message.get("sender") or {}).get("user_id", ""))
            text = "".join(str(c.get("data", {}).get("text", "")) for c in message.get("message", [])
                           if isinstance(c, dict) and c.get("type") == "text")
            if sender == str(info.get("user_id")) and text == row["text"] and \
                    float(message.get("time", 0)) >= (row["last_attempt"] or row["due"]) - 2:
                return str(message.get("message_id") or "")
        # Absence from a limited history page cannot establish non-delivery.
        return ""

    async def deliver(self, delivery_id, session=None, sender=None):
        async with self.lock:
            row = self.get(delivery_id)
            if not row:
                raise ValueError("发送记录不存在")
            if session is not None and (row["session"] != session or row["sender"] != str(sender)):
                raise ValueError("不能发送其他创建者的提醒")
            if not row["run_once"]:
                if row["status"] != "active":
                    return row
                self.prepare_occurrences(time.time())
                occurrences = self.db.rows(
                    "SELECT id FROM reminder_deliveries WHERE id LIKE ? AND due<=? ORDER BY due DESC LIMIT 1",
                    (delivery_id + ":%", time.time()))
                if not occurrences:
                    return {"status": "not_due", "due": row["due"]}
                delivery_id = occurrences[0]["id"]
                row = self.get(delivery_id)
            if row["status"] in {"sent", "cancelled", "missed"}:
                return row
            now = time.time()
            if now < row["due"]:
                return {"status": "not_due", "due": row["due"]}
            if now - row["due"] > 86400:
                self._finish(delivery_id, "missed", "已超过24小时补发窗口")
                return self.get(delivery_id)
            if row["status"] in {"retry", "unknown"} and now < row["next_attempt"]:
                return row
            try:
                client, parsed = self.client(row["session"])
                if row["status"] == "unknown":
                    receipt = await self.reconcile(row, client, parsed)
                    if receipt:
                        self._finish(delivery_id, "sent", "", receipt)
                    else:
                        self._retry(row, "unknown", "历史记录尚未确认发送结果，暂不重复发送")
                    return self.get(delivery_id)
                text = row["text"]
                if not text:
                    text = await asyncio.wait_for(self.compose(
                        render("reminders.compose", note=row['note'],
                               due=datetime.fromtimestamp(row['due'], timezone.utc).isoformat(),
                               delay=int(now - row['due'])),
                        prompt_text("reminders.compose.system")), 40)
                    if not text.strip():
                        raise ValueError("模型未生成提醒内容")
                with self.db.transaction() as con:
                    con.execute("UPDATE reminder_deliveries SET status='sending',text=?,"
                                "attempts=attempts+1,last_attempt=? WHERE id=?",
                                (text, time.time(), delivery_id))
                group = "group" in str(parsed.message_type).lower()
                payload = {"group_id" if group else "user_id": int(parsed.session_id.split("_")[-1]),
                           "message": [{"type": "text", "data": {"text": text}}]}
                try:
                    receipt = await asyncio.wait_for(
                        client.call_action("send_group_msg" if group else "send_private_msg", **payload), 30)
                    message_id = str(receipt.get("message_id") or "")
                    if not message_id:
                        raise TimeoutError("接口未返回消息ID")
                except Exception as exc:
                    # The request may have reached QQ before a transport error.
                    self._retry(self.get(delivery_id), "unknown", f"{type(exc).__name__}: {exc}")
                    return self.get(delivery_id)
                self._finish(delivery_id, "sent", "", message_id)
            except Exception as exc:
                self._retry(row, "unknown" if row["status"] == "unknown" else "retry",
                            f"{type(exc).__name__}: {exc}")
            return self.get(delivery_id)

    def _retry(self, row, status, error):
        attempts = row["attempts"] + (0 if row["status"] == "sending" else 1)
        delays = (60, 300, 900)
        delay = delays[min(attempts - 1, 2)] if attempts <= 3 else 3600
        with self.db.transaction() as con:
            con.execute("UPDATE reminder_deliveries SET status=?,attempts=?,next_attempt=?,error=? WHERE id=?",
                        (status, attempts, time.time() + delay, error[:1000], row["id"]))

    def _finish(self, delivery_id, status, error="", message_id=""):
        with self.db.transaction() as con:
            con.execute("UPDATE reminder_deliveries SET status=?,error=?,message_id=?,finished=? WHERE id=?",
                        (status, error, message_id, time.time(), delivery_id))

    async def tick(self):
        self.prepare_occurrences(time.time())
        for row in self.db.rows(
            "SELECT id FROM reminder_deliveries WHERE status IN ('pending','retry','unknown') "
            "AND next_attempt<=? ORDER BY next_attempt LIMIT 8", (time.time() - 15,)):
            await self.deliver(row["id"])

    async def close(self):
        if self.ticker:
            await self.context.cron_manager.delete_job(self.ticker)
