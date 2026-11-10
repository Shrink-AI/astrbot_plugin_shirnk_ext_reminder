import asyncio
import json
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

TZ = ZoneInfo("Asia/Shanghai")
ORIGIN = "shirnk_reminder"


@register("astrbot_plugin_shirnk_ext_reminder", "Shirnk", "石英未来提醒插件", "0.1.0")
class ReminderExtension(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self._create_lock = asyncio.Lock()
        self._service = None
        self._database = None

    async def initialize(self):
        from astrbot.api.star import StarTools
        from .storage import Database
        from .delivery import ReminderDelivery
        from .prompts import load
        root = StarTools.get_data_dir('astrbot_plugin_shirnk_ext_reminder')
        load(root)
        self._database = Database(root)
        self._service = ReminderDelivery(self.context, self._database, self._compose)
        await self._service.start()

    async def terminate(self):
        if self._service:
            await self._service.close()
            self._service = None
        if self._database:
            self._database.close()
            self._database = None

    async def _compose(self, facts, instruction):
        from .plugin_protocol import CapabilityClient
        bus = CapabilityClient()
        if any(c['name'] == 'message.compose' and c['version'] == 1 for c in bus.catalog()):
            return (await bus.call('message.compose', 'compose',
                    {'facts': facts, 'instruction': instruction}))['text']
        # Delivery remains operational without any persona plugin.
        provider = self.context.get_using_provider()
        if provider is None:
            raise RuntimeError('没有可用的对话模型')
        response = await provider.text_chat(prompt=facts, system_prompt=instruction)
        return response.completion_text

    def plugin_capabilities(self):
        return {'protocol': 1, 'capabilities': [{
            'name': 'reminders.manage', 'version': 1, 'description': '一次性与周期提醒，发送回执及取消',
            'actions': {
                'list': {'description': '当前会话创建者的提醒和回执', 'readonly': True, 'parameters': {'type': 'object'}},
                'create': {'description': '按用户明确要求创建提醒', 'readonly': False, 'parameters': {
                    'type': 'object', 'required': ['content', 'time_str'], 'properties': {
                        'content': {'type': 'string'}, 'time_str': {'type': 'string'},
                        'run_once': {'type': 'boolean'}, 'cron_expression': {'type': 'string'}}}},
                'delete': {'description': '取消当前创建者指定提醒', 'readonly': False, 'parameters': {
                    'type': 'object', 'required': ['job_id'], 'properties': {'job_id': {'type': 'string'}}}},
            }}]}

    async def invoke_capability(self, request):
        from .plugin_protocol import validate, success
        from types import SimpleNamespace
        validate(request, 'reminders.manage', {'list', 'create', 'delete'})
        scope = request.get('scope') or {}
        if not scope.get('admin') or not scope.get('session') or not scope.get('sender'):
            raise ValueError('提醒操作需要管理员及原会话身份')
        event = SimpleNamespace(is_admin=lambda: True, unified_msg_origin=scope['session'],
                                get_sender_id=lambda: scope['sender'])
        args = request['arguments']
        allowed = {'content', 'time_str', 'run_once', 'cron_expression', 'job_id'}
        if set(args) - allowed:
            raise ValueError('不支持的提醒参数')
        return success(json.loads(await self.reminder(event, action=request['action'], **args)))

    def _delivery(self):
        if self._service is None:
            raise RuntimeError('提醒插件尚未初始化')
        return self._service

    @filter.llm_tool(name='shrink_deliver_reminder')
    async def deliver_reminder(self, event: AstrMessageEvent, delivery_id: str):
        """发送到期提醒并返回真实回执；兼容旧任务工具名，业务完全由提醒插件管理。

        Args:
            delivery_id(string): 到期任务携带的提醒 ID。
        """
        if not event.is_admin():
            return json.dumps({'ok': False, 'error': '仅管理员可发送提醒'})
        service = self._delivery()
        row = service.get(delivery_id)
        if not row or row['session'] != event.unified_msg_origin:
            return json.dumps({'ok': False, 'error': '提醒不属于当前会话'})
        return json.dumps(await service.deliver(delivery_id), ensure_ascii=False)

    def _belongs_to_caller(self, job, session, sender_id):
        p = getattr(job, "payload", None)
        return (isinstance(p, dict) and p.get("origin") == ORIGIN
                and str(p.get("session") or "") == session
                and str(p.get("sender_id") or "") == sender_id)

    def _owns_receipt(self, row, session, sender):
        return bool(row and row["session"] == session and str(row["sender"]) == sender)

    def _parse_target_datetime(self, time_str, now):
        now = now.astimezone(TZ)
        raw = (time_str or "").strip()
        if not raw:
            return None, "未提供提醒时间"
        try:
            target = datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            target = None
        if target is not None:
            target = target.replace(tzinfo=TZ) if target.tzinfo is None else target.astimezone(TZ)
            return (None, "指定的时间已经是过去的时间，已拒绝创建") if target <= now else (target, None)
        explicit_date = None
        rest = raw
        match = re.match(r"(?:(\d{4})[-年/])?(\d{1,2})[-月/](\d{1,2})[日号]?\s*", rest)
        if match:
            try:
                explicit_date = datetime(int(match[1] or now.year), int(match[2]), int(match[3]), tzinfo=TZ).date()
            except ValueError:
                return None, f"无效的日期格式: {raw}"
            rest = rest[match.end():].strip()
        day_offset = None
        if explicit_date is None:
            for words, offset in (("大后天", 3), ("后天", 2), ("明天|明日", 1), ("今天|今日", 0)):
                match = re.match(words, rest)
                if match:
                    day_offset = offset
                    rest = rest[match.end():].strip()
                    break
            if re.search(r"昨天|前天|过去", rest):
                return None, "指定的是过去的时间，已拒绝创建"
        period = None
        match = re.match(r"凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|今晚|明早", rest)
        if match:
            period = match[0]
            rest = rest[match.end():].strip()
            if day_offset is None and explicit_date is None:
                if period == "今晚":
                    day_offset = 0
                elif period == "明早":
                    day_offset = 1
        hm = re.fullmatch(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", rest)
        cn = re.fullmatch(r"(\d{1,2})\s*[点时](?:(半)|(\d{1,2})\s*分?)?", rest)
        if hm:
            hour, minute, second = int(hm[1]), int(hm[2]), int(hm[3] or 0)
        elif cn:
            hour, minute, second = int(cn[1]), 30 if cn[2] else int(cn[3] or 0), 0
        else:
            return None, f"无法解析提醒时间: {raw}"
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None, f"时间数值超出范围: {raw}"
        if period in ("下午", "傍晚", "晚上", "夜里", "今晚") and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period in ("凌晨", "早上", "早晨", "上午", "明早") and hour == 12:
            hour = 0
        date = explicit_date or (now + timedelta(days=day_offset or 0)).date()
        target = datetime(date.year, date.month, date.day, hour, minute, second, tzinfo=TZ)
        if target <= now:
            if explicit_date is not None or day_offset is not None:
                return None, "指定的时间已经是过去的时间，已拒绝创建"
            target += timedelta(days=1)
        return target, None

    def _format_receipt(self, row):
        # Series status and latest occurrence status are independent facts.
        once = row.get("run_once", True)
        result = dict(job_id=row["id"], note=row["note"], run_once=once,
                      status=row["status"], timezone=row.get("timezone") or "Asia/Shanghai",
                      delivery_registered=True)
        if not once:
            result.update(cron_expression=row["cron_expression"], next_due=row["next_due"],
                          next_run_time=datetime.fromtimestamp(row["next_due"], TZ).isoformat(),
                          latest_delivery=row.get("latest_delivery"))
        else:
            result.update({key: row.get(key) for key in (
                "attempts", "message_id", "error", "last_attempt", "finished", "next_attempt")})
            result["run_at"] = datetime.fromtimestamp(row["due"], TZ).isoformat()
        return result

    def _format_job(self, job, mgr, row=None):
        if row:
            return self._format_receipt(row)
        p = job.payload if isinstance(job.payload, dict) else {}
        next_run = mgr.get_next_run_time(job.job_id) or getattr(job, "next_run_time", None)
        if isinstance(next_run, datetime):
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=dt_timezone.utc)
            next_run = next_run.astimezone(TZ).isoformat()
        return dict(job_id=job.job_id, name=job.name, note=p.get("note", ""),
                    run_once=bool(job.run_once), run_at=p.get("run_at"),
                    cron_expression=job.cron_expression, next_run_time=next_run,
                    timezone=job.timezone or "Asia/Shanghai", delivery_registered=False,
                    status="untracked", notice="此旧任务未登记可靠回执，发送状态未知")

    async def _compensate(self, service, mgr, jid, session, sender):
        errors = []
        try:
            row = service.get(jid)
            if row:
                await service.cancel(jid, session, sender)
        except Exception as exc:
            errors.append(f"回执取消失败: {exc}")
        # Always attempt native removal, including when register partially succeeded.
        try:
            await mgr.delete_job(jid)
        except Exception as exc:
            errors.append(f"原生任务清理失败: {exc}")
        return errors

    @filter.llm_tool(name="shirnk_reminder")
    async def reminder(self, event: AstrMessageEvent, action: str, content: str = "",
                       time_str: str = "", job_id: str = "", run_once: bool = True,
                       cron_expression: str = ""):
        """管理未来提醒，仅管理员可用；单次和周期提醒均登记插件持久回执，周期按次记录发送结果。

        Args:
            action(string): create 创建、list 查看当前会话自己的提醒及回执、delete 取消。
            content(string): create 必填的提醒事项。
            time_str(string): ISO 时间、HH:MM 或中文时间；显式过去时间拒绝。
            job_id(string): delete 必填的提醒 ID。
            run_once(boolean): 是否单次提醒，默认 True。
            cron_expression(string): 周期提醒必填的五段式 cron 表达式。
        """
        def reply(**data):
            return json.dumps(data, ensure_ascii=False, default=str)
        if not event.is_admin():
            return reply(ok=False, error="仅管理员可调用提醒工具")
        mgr = getattr(self.context, "cron_manager", None)
        if mgr is None:
            return reply(ok=False, error="原生 cron_manager 不可用")
        act = (action or "").strip().lower()
        session, sender = str(event.unified_msg_origin), str(event.get_sender_id())
        try:
            if act == "list":
                service = self._delivery()
                rows = {r["id"]: r for r in service.listing() if self._owns_receipt(r, session, sender)}
                items = {jid: self._format_receipt(r) for jid, r in rows.items()}
                for job in await mgr.list_jobs():
                    if self._belongs_to_caller(job, session, sender):
                        row = service.get(job.job_id)
                        if row and not self._owns_receipt(row, session, sender):
                            continue
                        items[job.job_id] = self._format_job(job, mgr, row)
                return reply(ok=True, action=act, count=len(items), reminders=list(items.values()),
                             receipt_history_limit=100)
            if act == "delete":
                jid = (job_id or "").strip()
                if not jid:
                    return reply(ok=False, error="删除提醒需要提供 job_id")
                service = self._delivery()
                row = service.get(jid)
                if row:
                    if not self._owns_receipt(row, session, sender):
                        return reply(ok=False, error="只能取消自己在当前会话创建的提醒")
                    result = await service.cancel(jid, session, sender)
                    return reply(ok=True, action=act, deleted_job_id=jid, status=result["status"],
                                 reminder=self._format_receipt(service.get(jid)))
                job = await mgr.db.get_cron_job(jid)
                if not job or not self._belongs_to_caller(job, session, sender):
                    return reply(ok=False, error="提醒不存在或不属于当前会话创建者")
                await mgr.delete_job(jid)
                return reply(ok=True, action=act, deleted_job_id=jid, status="cancelled",
                             delivery_registered=False, notice="旧任务无发送回执")
            if act != "create":
                return reply(ok=False, error="action 仅支持 create、list、delete")
            note = (content or "").strip()
            if not note:
                return reply(ok=False, error="提醒内容 content 不能为空")
            once = True if run_once is None else bool(run_once)
            expr = (cron_expression or "").strip() or None
            if not once and not expr:
                return reply(ok=False, error="周期提醒必须提供 cron_expression")
            async with self._create_lock:
                target = None
                service = self._delivery()
                if once:
                    target, err = self._parse_target_datetime(time_str, datetime.now(TZ))
                    if err:
                        return reply(ok=False, error=err)
                    expr = None
                for old in await mgr.list_jobs():
                    if not self._belongs_to_caller(old, session, sender) or bool(old.run_once) != once:
                        continue
                    row = service.get(old.job_id)
                    if not self._owns_receipt(row, session, sender) or bool(row.get("run_once", True)) != once:
                        continue
                    if row["note"] != note:
                        continue
                    same = (row["due"] == target.timestamp() and row["status"] in {"pending", "retry", "unknown", "sending"}) if once else (row["status"] == "active" and row["cron_expression"] == expr)
                    if same:
                        return reply(ok=True, action=act, deduplicated=True, job_id=old.job_id,
                                     reminder=self._format_job(old, mgr, row))
                payload = dict(session=session, sender_id=sender, note=note, origin=ORIGIN)
                if once:
                    payload["run_at"] = target.isoformat()
                job = await mgr.add_active_job(name=f"提醒:{note[:24]}", cron_expression=expr,
                    payload=payload, description=note, timezone="Asia/Shanghai", run_once=once, run_at=target)
                try:
                    row = await service.register(job)
                    if not self._owns_receipt(row, session, sender) or bool(row.get("run_once", True)) != once:
                        raise RuntimeError("未返回有效登记记录")
                except BaseException as exc:
                    errors = await asyncio.shield(self._compensate(service, mgr, job.job_id, session, sender))
                    if not isinstance(exc, Exception):
                        raise
                    return reply(ok=False, error=f"回执登记失败: {exc}", job_id=job.job_id,
                                 compensated=not errors, cleanup_errors=errors)
                return reply(ok=True, action=act, deduplicated=False, job_id=job.job_id,
                             reminder=self._format_job(job, mgr, row))
        except Exception as exc:
            return reply(ok=False, error=f"{type(exc).__name__}: {exc}")
