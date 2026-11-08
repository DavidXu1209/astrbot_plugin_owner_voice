"""Offline owner-scope/trigger/fallback checks using synthetic events."""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from astrbot.api.message_components import Plain, Record

spec = importlib.util.spec_from_file_location('voice_test_plugin', Path(__file__).resolve().parent.parent / 'main.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OWNER = 'acceptance:FriendMessage:00000001'


class VoiceChecks(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.plugin = object.__new__(module.OwnerVoice)
        self.plugin.config = {'enabled': True, 'owner_session': OWNER}
        self.plugin.root = Path(self.temp.name)
        self.extra = {}
        self.result = SimpleNamespace(chain=[Plain('这是合成测试。')], is_llm_result=lambda: True)
        self.event = SimpleNamespace(
            unified_msg_origin=OWNER, get_sender_id=lambda: '00000001',
            get_platform_name=lambda: 'aiocqhttp', is_private_chat=lambda: True,
            get_message_str=lambda: '语音：你好', get_extra=self.extra.get,
            set_extra=self.extra.__setitem__, get_result=lambda: self.result,
        )

    def tearDown(self):
        self.temp.cleanup()

    async def test_scope_and_explicit_trigger(self):
        for value in ('你好', '你会用语音回复吗', '文档里写着“语音：你好”', '别用语音回复', '不要发语音了',
                      '我刚才听了语音', '语音识别好难', '我发语音给你了', '不用语音，打字就行', '你会不会发语音？'):
            self.assertFalse(module.requested(value), value)
        for value in ('语音：你好', '请用语音回答我', '用语音回复我一句你好', '/语音 你好', '用语音说', '发个语音给我',
                      '你用语音给我加加油', '语音回我', '说给我听', '我想听你的声音', '给我发条语音', '能用语音说一下吗'):
            self.assertTrue(module.requested(value), value)
        self.assertTrue(self.plugin.allowed(self.event))
        self.event.unified_msg_origin = 'acceptance:GroupMessage:00000001'
        self.assertFalse(self.plugin.allowed(self.event))
        self.event.unified_msg_origin = OWNER
        self.event.get_sender_id = lambda: '00000002'
        self.assertFalse(self.plugin.allowed(self.event))

    async def test_normal_reply_does_not_contact_tts(self):
        self.plugin.synthesize = AsyncMock()
        await self.plugin.speak_when_requested(self.event)
        self.plugin.synthesize.assert_not_awaited()
        self.assertIsInstance(self.result.chain[0], Plain)

    async def test_voice_request_uses_mandarin_without_extra_call(self):
        req = SimpleNamespace(system_prompt='原有人设')
        await self.plugin.mark_requested(self.event, req)
        self.assertTrue(self.extra[module.MARKER])
        self.assertIn('标准普通话', req.system_prompt)
        self.assertIn('不用方言', req.system_prompt)

    async def test_failure_preserves_complete_text_without_retry(self):
        self.extra[module.MARKER] = True
        self.plugin.synthesize = AsyncMock(side_effect=TimeoutError())
        await self.plugin.speak_when_requested(self.event)
        self.assertEqual(self.result.chain[0].text, '这是合成测试。')
        self.plugin.synthesize.assert_awaited_once()

    async def test_success_produces_native_record(self):
        self.extra[module.MARKER] = True
        audio = self.plugin.root / ('a'*32+'.mp3')
        audio.write_bytes(b'synthetic')
        self.plugin.synthesize = AsyncMock(return_value=audio)
        await self.plugin.speak_when_requested(self.event)
        self.assertEqual(len(self.result.chain), 1)
        self.assertIsInstance(self.result.chain[0], Record)
        self.assertEqual(self.result.chain[0].text, '这是合成测试。')

    async def test_long_text_remains_text(self):
        self.extra[module.MARKER] = True
        self.result.chain = [Plain('字'*601)]
        self.plugin.synthesize = AsyncMock()
        await self.plugin.speak_when_requested(self.event)
        self.plugin.synthesize.assert_not_awaited()


class SpontaneousChecks(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.plugin = object.__new__(module.OwnerVoice)
        self.plugin.config = {'enabled': True, 'owner_session': OWNER, 'spontaneous_probability': 1.0}
        self.plugin.root = Path(self.temp.name) / 'audio'
        self.plugin.root.mkdir()
        self.plugin.state_path = Path(self.temp.name) / 'spontaneous.json'
        self.extra = {}
        self.msg = '我去睡了，晚安'
        self.event = SimpleNamespace(
            unified_msg_origin=OWNER, get_sender_id=lambda: '00000001',
            get_platform_name=lambda: 'aiocqhttp', is_private_chat=lambda: True,
            get_message_str=lambda: self.msg, get_extra=self.extra.get,
            set_extra=self.extra.__setitem__,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_goodnight_words(self):
        for value in ('晚安', '我去睡了', '先睡啦，明天见', '好困，洗洗睡', '我这就去睡觉'):
            self.assertTrue(module.GOODNIGHT.search(value) and not module.QUESTION.search(value), value)
        for value in ('你睡了吗', '昨晚没睡好', '你晚安了么', '睡觉前吃什么？', '今天好忙'):
            self.assertFalse(module.GOODNIGHT.search(value) and not module.QUESTION.search(value), value)

    def test_window_and_quota(self):
        from datetime import datetime
        self.assertTrue(module.goodnight_window(datetime(2026, 11, 7, 23, 30, tzinfo=module.CST)))
        self.assertTrue(module.goodnight_window(datetime(2026, 11, 8, 1, 0, tzinfo=module.CST)))
        self.assertFalse(module.goodnight_window(datetime(2026, 11, 7, 15, 0, tzinfo=module.CST)))
        now = 1_000_000.0
        self.assertTrue(module.quota_ok([], now, 2, 2))
        self.assertFalse(module.quota_ok([now - 86400], now, 2, 2))
        self.assertTrue(module.quota_ok([now - 3 * 86400], now, 2, 2))
        self.assertFalse(module.quota_ok([now - 5 * 86400, now - 3 * 86400], now, 2, 2))
        self.assertTrue(module.quota_ok([now - 8 * 86400, now - 3 * 86400], now, 2, 2))

    async def test_goodnight_marks_only_in_window(self):
        from unittest.mock import patch
        req = SimpleNamespace(system_prompt='人设')
        with patch.object(module, 'goodnight_window', return_value=True):
            await self.plugin.mark_requested(self.event, req)
        self.assertTrue(self.extra.get(module.MARKER))
        self.assertTrue(self.extra.get(module.SPONTANEOUS))
        self.assertIn('晚安', req.system_prompt)
        self.extra.clear()
        req = SimpleNamespace(system_prompt='人设')
        with patch.object(module, 'goodnight_window', return_value=False):
            await self.plugin.mark_requested(self.event, req)
        self.assertFalse(self.extra)
        self.msg = '今天吃了火锅'
        with patch.object(module, 'goodnight_window', return_value=True):
            await self.plugin.mark_requested(self.event, req)
        self.assertFalse(self.extra)

    async def test_disabled_and_quota_block(self):
        from unittest.mock import patch
        import json, time
        self.plugin.config['spontaneous_goodnight'] = False
        req = SimpleNamespace(system_prompt='人设')
        with patch.object(module, 'goodnight_window', return_value=True):
            await self.plugin.mark_requested(self.event, req)
        self.assertFalse(self.extra)
        self.plugin.config['spontaneous_goodnight'] = True
        self.plugin.state_path.write_text(json.dumps({'sent': [time.time() - 3600]}), encoding='utf-8')
        with patch.object(module, 'goodnight_window', return_value=True):
            await self.plugin.mark_requested(self.event, req)
        self.assertFalse(self.extra)

    async def test_success_records_usage(self):
        self.extra[module.MARKER] = True
        self.extra[module.SPONTANEOUS] = True
        result = SimpleNamespace(chain=[Plain('晚安，早点睡。')], is_llm_result=lambda: True)
        self.event.get_result = lambda: result
        audio = self.plugin.root / ('b'*32+'.wav')
        audio.write_bytes(b'x')
        self.plugin.synthesize = AsyncMock(return_value=audio)
        await self.plugin.speak_when_requested(self.event)
        self.assertIsInstance(result.chain[0], Record)
        self.assertEqual(len(self.plugin._history()), 1)


class BreezeChecks(IsolatedAsyncioTestCase):
    def setUp(self):
        import asyncio as _asyncio
        self.temp = TemporaryDirectory()
        self.plugin = object.__new__(module.OwnerVoice)
        self.plugin.config = {'enabled': True, 'owner_session': OWNER}
        self.plugin.root = Path(self.temp.name)
        self.plugin.lock = _asyncio.Lock()

    def tearDown(self):
        self.temp.cleanup()

    def fake_wav(self):
        async def conv(src):
            out = src.with_suffix('.wav')
            out.write_bytes(b'w' * 600)
            return out
        return conv

    def test_ready_needs_key_and_sane_voice(self):
        self.assertFalse(self.plugin.breeze_ready())
        self.plugin.config.update(breeze_api_key='k', breeze_voice_id='voc_abc123')
        self.assertTrue(self.plugin.breeze_ready())
        self.plugin.config['breeze_voice_id'] = '../../x'
        self.assertFalse(self.plugin.breeze_ready())

    async def test_breeze_used_when_configured(self):
        from unittest.mock import patch
        import sys, types
        self.plugin.config.update(breeze_api_key='k', breeze_voice_id='voc_abc123')

        async def breeze(text, path):
            path.write_bytes(b'b' * 800)
        self.plugin._breeze_mp3 = AsyncMock(side_effect=breeze)
        edge = types.SimpleNamespace(Communicate=AsyncMock())
        with patch.object(module, 'prepare_wav', self.fake_wav()), patch.dict(sys.modules, {'edge_tts': edge}):
            wav = await self.plugin.synthesize('晚安')
        self.assertTrue(wav.name.endswith('.wav'))
        self.plugin._breeze_mp3.assert_awaited_once()
        edge.Communicate.assert_not_called()
        self.assertFalse(wav.with_suffix('.mp3').exists())

    async def test_breeze_failure_falls_back_to_edge(self):
        from unittest.mock import patch
        import sys, types
        self.plugin.config.update(breeze_api_key='k', breeze_voice_id='voc_abc123')
        self.plugin._breeze_mp3 = AsyncMock(side_effect=RuntimeError('breeze http 402'))

        class Comm:
            def __init__(self, text, voice, rate=None, pitch=None):
                self.voice = voice

            async def save(self, p):
                Path(p).write_bytes(b'e' * 700)
        edge = types.SimpleNamespace(Communicate=Comm)
        with patch.object(module, 'prepare_wav', self.fake_wav()), patch.dict(sys.modules, {'edge_tts': edge}):
            wav = await self.plugin.synthesize('晚安')
        self.assertTrue(wav.is_file())
        self.plugin._breeze_mp3.assert_awaited_once()

    async def test_no_config_uses_edge_only(self):
        from unittest.mock import patch
        import sys, types
        self.plugin._breeze_mp3 = AsyncMock()

        class Comm:
            def __init__(self, *a, **k):
                pass

            async def save(self, p):
                Path(p).write_bytes(b'e' * 700)
        with patch.object(module, 'prepare_wav', self.fake_wav()), patch.dict(sys.modules, {'edge_tts': types.SimpleNamespace(Communicate=Comm)}):
            await self.plugin.synthesize('你好')
        self.plugin._breeze_mp3.assert_not_awaited()


class SpeechTextChecks(IsolatedAsyncioTestCase):
    def test_cleanup_and_chunks(self):
        t = '我是小助手，人可不憨啊。\n\n平时就爱跟你拌嘴（斜眼）\n明天见。（笑）[[meme:开心]]'
        s = module.speech_text(t)
        self.assertEqual(s, '我是小助手，人可不憨啊。平时就爱跟你拌嘴，明天见。[笑]')
        long = '一二三四五六七八九十，' * 9 + '好。'
        parts = module.chunks(long, 60)
        self.assertTrue(all(len(p) <= 60 for p in parts))
        self.assertEqual(''.join(parts), long)
        self.assertEqual(module.chunks('短句。还有一句！最后？'), ['短句。还有一句！最后？'])

    def test_truncation_rule(self):
        text = '平时就爱跟你拌嘴，你逗我我就怼回去，但你真有事的时候我肯定认真陪着。'
        self.assertTrue(module.looks_truncated(text, 16000 * 1))
        self.assertFalse(module.looks_truncated(text, 16000 * 8))
        self.assertFalse(module.looks_truncated('好呀', 2000))

    async def test_chunk_retries_short_and_busy(self):
        plugin = object.__new__(module.OwnerVoice)
        plugin.config = {}
        good, short = b'g' * 16000 * 9, b's' * 16000
        calls = []

        async def once(session, text):
            calls.append(1)
            if len(calls) == 1:
                raise module.BreezeBusy('429')
            return short if len(calls) == 2 else good
        plugin._breeze_once = once
        from unittest.mock import patch
        with patch.object(module.asyncio, 'sleep', AsyncMock()):
            data = await plugin._breeze_chunk(None, '平时就爱跟你拌嘴，你逗我我就怼回去，但你真有事的时候我肯定认真陪着。')
        self.assertEqual(data, good)
        self.assertEqual(len(calls), 3)

        async def always_short(session, text):
            return short
        plugin._breeze_once = always_short
        with self.assertRaises(ValueError):
            await plugin._breeze_chunk(None, '平时就爱跟你拌嘴，你逗我我就怼回去，但你真有事的时候我肯定认真陪着。')

    async def test_normal_reply_is_one_request(self):
        plugin = object.__new__(module.OwnerVoice)
        plugin.config = {}
        seen = []

        async def chunk(session, text):
            seen.append(text)
            return b'g' * 1000
        plugin._breeze_chunk = chunk
        text = '我是小助手，名字听着挺憨，人可不憨啊。' * 7  # ~140 chars
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        await plugin._breeze_mp3(text, Path(temp.name) / 'x.mp3')
        self.assertEqual(seen, [text])

    async def test_synthesize_speaks_cleaned_text(self):
        from unittest.mock import patch
        import asyncio as _asyncio
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        plugin = object.__new__(module.OwnerVoice)
        plugin.config = {'breeze_api_key': 'k', 'breeze_voice_id': 'voc_abc123'}
        plugin.root = Path(temp.name)
        plugin.lock = _asyncio.Lock()
        seen = []

        async def breeze(text, path):
            seen.append(text)
            path.write_bytes(b'b' * 800)
        plugin._breeze_mp3 = breeze

        async def conv(src):
            out = src.with_suffix('.wav')
            out.write_bytes(b'w' * 600)
            return out
        with patch.object(module, 'prepare_wav', conv):
            await plugin.synthesize('晚安（小声）\n\n明天见')
        self.assertEqual(seen, ['晚安，明天见'])


class BreezeReadChecks(IsolatedAsyncioTestCase):
    async def test_reads_whole_body_not_first_chunk(self):
        plugin = object.__new__(module.OwnerVoice)
        plugin.config = {'breeze_api_key': 'k', 'breeze_voice_id': 'voc_abc123', 'breeze_speed': 1.05,
                         'breeze_instructions': '吐字清晰。'}
        blocks = [b'a' * 300, b'b' * 16000, b'c' * 50000]
        sent = {}

        class Content:
            async def iter_chunked(self, n):
                for b in blocks:
                    yield b

        class Resp:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Session:
            def post(self, url, params=None, json=None, headers=None):
                sent.update(url=url, params=params, json=json, headers=headers)
                return Resp()

        data = await plugin._breeze_once(Session(), '你好呀')
        self.assertEqual(len(data), 300 + 16000 + 50000)
        self.assertTrue(sent['url'].endswith('/voc_abc123'))
        self.assertEqual(sent['json']['voice_settings']['speed'], 1.05)
        self.assertEqual(sent['headers']['xi-api-key'], 'k')

        blocks[:] = [b'x' * 100]
        with self.assertRaises(ValueError):
            await plugin._breeze_once(Session(), '你好呀')
        Resp.status = 429
        with self.assertRaises(module.BreezeBusy):
            await plugin._breeze_once(Session(), '你好呀')
