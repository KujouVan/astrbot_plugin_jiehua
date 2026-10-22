import asyncio
import time
from dataclasses import dataclass, field

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, At
from astrbot.api.star import Context, Star, register
from .policy import options, decision, TURN_RULES


@dataclass
class Batch:
    event: object
    texts: list
    first: float
    wake: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Run:
    event: object
    texts: list
    created: float
    superseded: bool = False
    tools: bool = False
    suppressed: bool = False
    finished: bool = False
    sent: bool = False


@register('astrbot_plugin_jiehua', 'KujouVan', '接话：连续发言等待与上下文衔接，不增加判断模型调用', '1.0.0')
class JiehuaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pending = {}
        self.held = {}
        self.runs = {}
        self.closed = False

    def key(self, event):
        return (event.unified_msg_origin, str(event.get_sender_id()))

    def eligible(self, event):
        if event.get_platform_name() != 'aiocqhttp' or not event.is_at_or_wake_command:
            return False
        if event.call_llm or event._has_send_oper or event.get_extra('provider_request') is not None:
            return False
        if event.get_extra('handlers_parsed_params'):
            return False
        text = event.get_message_str().strip()
        if not text or text.startswith(('/', '!', '！')):
            return False
        # Do not merge attachments, references, commands or messages addressing somebody else.
        return all(isinstance(c, Plain) or (isinstance(c, At) and str(c.qq) == str(event.get_self_id())) for c in event.get_messages())

    def prune(self):
        now = time.monotonic()
        self.held = {k: v for k, v in self.held.items() if now-v[0] < 600}
        self.runs = {k: v for k, v in self.runs.items() if now-v.created < 600}
        if len(self.held) > 500:
            self.held.pop(next(iter(self.held)))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000000000)
    async def receive(self, event: AstrMessageEvent):
        cfg = options(self.config)
        if self.closed or not cfg['enabled'] or not self.eligible(event):
            return
        self.prune()
        key = self.key(event)
        text = event.get_message_str().strip()
        now = time.monotonic()
        carry = []
        old_run = self.runs.get(key)
        if cfg['update_unsent']:
            # Buffer this event's answer rather than streaming fragments that cannot be recalled.
            event.set_extra('enable_streaming', False)
            if old_run and old_run.event.get_extra('enable_streaming') is False and not old_run.finished and not old_run.tools and not old_run.sent and not old_run.event._has_send_oper:
                old_run.superseded = True
                carry.append(old_run)
        old = self.pending.get(key)
        if old:
            texts = old.texts + [text]
            first = old.first
            carry = old.event.get_extra('_jiehua_carry', []) + carry
            old.wake.set()
        else:
            held = self.held.pop(key, None)
            texts = (held[1] if held else []) + [text]
            if held:
                carry = held[2] + carry
            first = now
        # Bounded input storage; release a full batch rather than silently dropping messages.
        force_flush = len(texts) >= 50 or sum(map(len, texts)) >= 16000
        batch = Batch(event, texts, first)
        self.pending[key] = batch
        event.set_extra('_jiehua_carry', carry)
        kind, delay = decision(text, cfg)
        if force_flush and kind not in ('closing', 'defer'):
            delay = 0
        delay = min(delay, max(0, cfg['max_wait_seconds']-(now-first)))
        if delay:
            try:
                await asyncio.wait_for(batch.wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        if self.pending.get(key) is not batch:
            event.stop_event()
            return
        if self.pending.get(key) is batch:
            self.pending.pop(key, None)
        event.set_extra('_jiehua_texts', texts)
        if not self.closed and self.config.get('enabled', True):
            if kind in ('closing', 'defer') or (cfg['wait_for_followup'] and kind != 'reply' and not cfg['timeout_reply']):
                self.held[key] = (time.monotonic(), texts, carry)
                event.stop_event()
                return
            if kind == 'incomplete' and not cfg['clarify_incomplete']:
                self.held[key] = (time.monotonic(), texts, carry)
                event.stop_event()
                return
            if kind == 'incomplete':
                event.set_extra('_jiehua_clarify', True)
        # Preserve the current event identity and @ components; only plain text is combined.
        combined = '\n'.join(texts)
        event.message_str = combined
        event.message_obj.message = [c for c in event.get_messages() if isinstance(c, At)] + [Plain(combined)]

    @filter.on_llm_request(priority=1000000000)
    async def prepare(self, event, req):
        cfg = options(self.config)
        if not cfg['enabled'] or event.get_platform_name() != 'aiocqhttp':
            return
        texts = event.get_extra('_jiehua_texts')
        if texts:
            carried = []
            seen = set()
            for previous in event.get_extra('_jiehua_carry', []):
                if previous.suppressed and id(previous) not in seen:
                    seen.add(id(previous))
                    carried.extend(previous.texts)
            if carried:
                req.prompt = '【同一发送者此前尚未得到回答的发言】\n' + '\n'.join(carried) + '\n【本次补充】\n' + (req.prompt or '')
            event.set_extra('_jiehua_run', Run(event, carried+texts, time.monotonic()))
            self.runs[self.key(event)] = event.get_extra('_jiehua_run')
        if cfg['context_linking'] and TURN_RULES not in (req.system_prompt or ''):
            req.system_prompt = (req.system_prompt or '') + '\n\n' + TURN_RULES
        if event.get_extra('_jiehua_clarify'):
            req.system_prompt += '\n当前发言可能缺少后文。先结合前文判断；如果仍缺少必要信息，只简短问一句需要补充什么，不猜测内容。'

    @filter.on_using_llm_tool(priority=1000000000)
    async def tool_started(self, event, tool, tool_args):
        run = event.get_extra('_jiehua_run')
        if run:
            run.tools = True
            run.superseded = False

    @filter.on_llm_response(priority=1000000000)
    async def response(self, event, response):
        run = event.get_extra('_jiehua_run')
        if run:
            run.finished = True
            if run.superseded and not run.tools and not run.sent and not event._has_send_oper:
                run.suppressed = True
                event.stop_event()

    @filter.after_message_sent(priority=1000000000)
    async def sent(self, event):
        run = event.get_extra('_jiehua_run')
        if run:
            run.sent = True

    async def terminate(self):
        self.closed = True
        for batch in list(self.pending.values()):
            batch.wake.set()
