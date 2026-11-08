"""On-demand Chinese female speech: BreezeBlue voice when configured, else rany2/edge-tts; no extra LLM.

Also, rarely, a spontaneous short goodnight voice in the owner's private chat
(late evening, owner says goodnight, at most 2 per rolling 7 days, >=2 days apart).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import random
from pathlib import Path
import re
import subprocess
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

VOICE = 'zh-CN-XiaoyiNeural'
VOICE_RATE = '+5%'
VOICE_PITCH = '+12Hz'
MARKER = '_owner_voice_requested'
SPONTANEOUS = '_owner_voice_spontaneous'
GOODNIGHT = re.compile(r'晚安|(我|先|要|该|准备|这就)去?睡(了|啦|觉|咯|喽)|洗洗睡|困死了|好困')
QUESTION = re.compile(r'[吗么?？]')
CST = timezone(timedelta(hours=8))
BREEZE_URL = 'https://api.breeze.blue/v1/text-to-speech/{voice}'
VOICE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{4,64}$')
REQUEST = re.compile(r'^\s*(?:[/／]语音(?:\s|[：:])|(?:请)?(?:用|发条|发一条)?语音(?:回复|回答|说|聊|[：:]))')
# Natural asks anywhere in the message: 用语音说 / 语音语音 / 我要听语音 / 语音版 / 说给我听 ...
VOICE_ASK = re.compile(
    r'(?:用|发|来|回|录)(?:个|条|一条|一段|段|句|一句|遍|一遍|一下)?语音'
    r'|语音(?:回复|回答|回我|说|讲|念|喊|聊|发我|给我)'
    r'|(?:^|[\s，,])(?:我?要|想要?|请|给)?听?语音版'
    r'|(?:说|念|读|讲|喊|唱)给我听'
    r'|想听(?:听)?你(?:的)?(?:声音|说话|讲话|说)'
    r'|听听你的声音'
    r'|语音[\s，,、！!。.~～]{0,3}语音'
    r'|(?<![不别甭])(?:想|要|想要|让我|给我|请|再)听(?![了过到见完着])(?:一?下|个|条|一遍|你的?)?语音'
    r'|(?:^|[\s，,。！!你请])听(?:一?下|个|条|一遍)?语音'
    r'|(?:给|请给)我(?:一?[个条句])?语音'
    r'|(?:^|\s)语音(?:呢|啊|呀|吧|嘛|呗|[!！])')
VOICE_NO = re.compile(
    r'(?:别|不要|不用|不想|甭|无需|不必)(?:再)?(?:给我)?(?:用|发|回|来|听)?(?:个|条|一下|一遍)?语音'
    r'|(?<![给帮跟对和向])我(?:刚|刚才|已经|又)?(?:发|用|录)了?(?:个|条|一条)?语音'
    r'|会(?:不会)?(?:用|发)?语音.*[吗么?？]'
    r'|[“"「『].{0,20}语音.{0,20}[”"」』]'
    r'|[不别甭]想听')
# 刚明确要过语音的几分钟里，再补一句很短的恳求，也当成还在要语音
PLEA = re.compile(
    r'^(?:你|请)?(?:求你了?|拜托了?|再来一?次|再发一?次|还是没有啊?|没有啊?|没听到|听不到|再说一?遍|再讲一?遍|再念一?遍)[!！。…~～啊呀]*$')
PLEA_SECONDS = 180
HINT_TEXT = '\n系统事实，不要复述：你上一条发出去的是文字，不是语音。不要说「刚才那条就是语音」「我已经发过了」「连发了几条」。'
HINT_VOICE = '\n系统事实，不要复述：你上一条发出去的确实是语音。他要是还想听，就把该说的话再讲一遍，不要怪他没点开，也不要编造你发过几条。'
HINT_UNKNOWN = '\n系统事实，不要复述：你无法判断以前哪条是语音。不要说「刚才那条就是语音」，也不要说你连发了几条。'
VOICE_PROMPT = (
    '\n用户这次想听你的语音，这条回复会由程序念成语音发给他，你直接说要说的话就行，不要说发不了语音或先欠着。'
    '只写标准普通话口语，不用方言、地域口音词或方言句尾；用自然温软的简短中文正文，尽量不超过180字。'
    '程序负责语音合成，不要解释合成过程，不加动作描写、颜文字或表情配图标记。'
    '最前面单独写一个语气标记，用几个字说这段话该怎么说，比如【语气：惊喜开心】【语气：委屈带哭腔】【语气：小声撒娇】【语气：嘴硬又有点害羞】，按你此刻真实的情绪来选；程序会读掉它，不会念出来。'
    '哭腔、撒娇这些交给语气标记，正文只写正常说的话，不写“呜呜”“嘤嘤”这类拟声字，也别用一串省略号硬造停顿。'
    '不要写「语音版」「刚才那条就是语音」「我已经发过了」「连发了几条」「你有没有点开」。'
    '历史里留下的都是文字，你并不知道哪条真的变成了语音；正文只留你这次要对他说的话。'
)


def requested(message: str) -> bool:
    message = message or ''
    if REQUEST.match(message):
        return True
    return bool(VOICE_ASK.search(message)) and not VOICE_NO.search(message)


def needs_delivery_fact(message: str, asking: bool) -> bool:
    """Whether this turn needs a hard fact about the previous reply actually being voice or text."""
    message = message or ''
    if asking:
        return True
    if re.search(r'没听[到见着清]?|听不到|听不见|没收到|没有声音|没声音|没点开|不是语音|没发语音|没有语音', message):
        return True
    compact = re.sub(r'\s+', '', message)
    return len(compact) <= 6 and '求你' in compact


def goodnight_window(now: datetime) -> bool:
    hour = now.astimezone(CST).hour
    return hour >= 21 or hour < 3


def quota_ok(history: list, now: float, per_week: int, min_gap_days: float) -> bool:
    recent = [t for t in history if now - t < 7 * 86400]
    if len(recent) >= per_week:
        return False
    return not recent or now - max(recent) >= min_gap_days * 86400


class BreezeBusy(RuntimeError):
    pass


TONE_TAG = re.compile(r'[【\[]\s*语气\s*[:：]\s*([^】\]\n]{1,24})\s*[】\]]')
# 试听：同一句话用几种语气各念一遍（标签, 语气描述, 念的文字）
TRYOUT_LINE = '你终于回来啦，我等你好久了。'
TRYOUT_ITEMS = [
    ('① 平常', '', TRYOUT_LINE),
    ('② 惊喜', '惊喜又开心，声音上扬，语速稍快', TRYOUT_LINE),
    ('③ 委屈带哭腔', '很委屈，带着哭腔，说话有点抽泣、断断续续', TRYOUT_LINE),
    ('④ 小声撒娇', '小声撒娇，软软的，像贴着耳朵说悄悄话', TRYOUT_LINE),
]
# 语音引擎会把这些拟声字原样念出来（“呜呜”念成 wu wu），念之前去掉
SOUND_WORDS = re.compile(r'(?:呜|嘤)+[～~…。，,！!]*')


def split_tone(text: str):
    """Pull the hidden 【语气：…】 tag out of a reply. Returns (clean_text, tone or '')."""
    text = text or ''
    m = TONE_TAG.search(text)
    tone = m.group(1).strip() if m else ''
    return TONE_TAG.sub('', text).strip(), tone


LAUGH = re.compile(r'[（(][^（）()]{0,6}笑[^（）()]{0,6}[）)]')
STAGE = re.compile(r'[（(][^（）()]{1,12}[）)]')
SENT = re.compile(r'[^。！？!?…~]*[。！？!?…~]+[”’」』]*|[^。！？!?…~]+$')


FALSE_CLAIM = re.compile(
    r'[^。！？!?\n]*?(?:'
    r'(?:刚才|上一?)[那这]?[条句][^。！？!?\n]{0,18}语音'
    r'|连发[了过]?[一二两三四五六七八九十\d]+条语音'
    r'|发了[一二两三四五六七八九十\d]+条语音'
    r'|语音版(?:再)?听'
    r'|(?:在哪儿|在哪里|在哪)听'
    r'|没点开'
    r'|不就是语音'
    r')[^。！？!?\n]*[。！？!?]?'
)


def drop_false_claims(text: str) -> str:
    """Drop sentences where she insists a previous text was already a voice message."""
    cleaned = FALSE_CLAIM.sub('', text or '')
    cleaned = re.sub(r'^[，,、。！？!?\s]+', '', cleaned)
    cleaned = re.sub(r'[，,、]{2,}', '，', cleaned)
    cleaned = re.sub(r'([。！？!?])[，,、]+', r'\1', cleaned)
    return cleaned.strip()


def speech_text(text: str) -> str:
    """Make a chat reply speakable: drop stage directions, keep laughs as [笑], join paragraphs."""
    text = re.sub(r'\[\[meme:[^\]]+\]\]', '', text or '')
    text = LAUGH.sub('[笑]', text)
    text = STAGE.sub('', text)
    parts = [p.strip() for p in re.split(r'\s*\n+\s*', text) if p.strip()]
    out = ''
    for part in parts:
        if out and not re.search(r'[。！？!?…~，,、]$', out):
            out += '，'
        out += part
    return re.sub(r'[ \t]+', ' ', out).strip()


def chunks(text: str, limit: int = 60) -> list:
    """Split into sentence groups of at most ~limit chars (long sentences split at commas)."""
    pieces = []
    for sent in SENT.findall(text):
        sent = sent.strip()
        while len(sent) > limit:
            cut = max(sent.rfind('，', 0, limit), sent.rfind(',', 0, limit))
            cut = cut + 1 if cut > 0 else limit
            pieces.append(sent[:cut])
            sent = sent[cut:].strip()
        if sent:
            pieces.append(sent)
    out = []
    for piece in pieces:
        if out and len(out[-1]) + len(piece) <= limit:
            out[-1] += piece
        else:
            out.append(piece)
    return out


def spoken_chars(text: str) -> int:
    return len(re.sub(r'\[[^\]]*\]|[\s，。！？!?…~、,.“”‘’「」『』]', '', text))


def looks_truncated(text: str, mp3_bytes: int) -> bool:
    """mp3_24000_128 is CBR: 16000 bytes per second. Normal speech is ~0.2 s per char."""
    chars = spoken_chars(text)
    return chars >= 6 and mp3_bytes / 16000 < chars * 0.12


async def prepare_wav(source: Path) -> Path:
    """Use an existing project-local binary, without changing PATH or core code."""
    import imageio_ffmpeg
    target = source.with_suffix('.wav')
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-v', 'error', '-y',
        '-i', str(source), '-acodec', 'pcm_s16le', '-ar', '24000', '-ac', '1',
        str(target), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    try:
        async with asyncio.timeout(12):
            await process.communicate()
        if process.returncode != 0 or not target.is_file() or target.stat().st_size < 512:
            raise ValueError('Voice conversion failed')
        return target
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        target.unlink(missing_ok=True)
        raise


class OwnerVoice(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.root = Path(StarTools.get_data_dir('astrbot_plugin_owner_voice')) / 'audio'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.state_path = self.root.parent / 'spontaneous.json'

    def _load_state(self) -> dict:
        path = getattr(self, 'state_path', None)
        if not path:
            return {}
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, data: dict) -> None:
        self.state_path.write_text(json.dumps(data), encoding='utf-8')

    def _history(self) -> list:
        try:
            return [float(t) for t in self._load_state().get('sent', [])]
        except Exception:
            return []

    def _record_spontaneous(self) -> None:
        now = time.time()
        data = self._load_state()
        history = []
        for t in data.get('sent', []):
            try:
                t = float(t)
            except (TypeError, ValueError):
                continue
            if now - t < 30 * 86400:
                history.append(t)
        history.append(now)
        data['sent'] = history
        self._save_state(data)

    def _note_delivery(self, kind: str) -> None:
        """Remember whether the owner actually received voice or text. The model cannot see this."""
        if not getattr(self, 'state_path', None):
            return
        try:
            data = self._load_state()
            data['last_delivery'] = kind
            data['last_delivery_at'] = time.time()
            self._save_state(data)
        except OSError:
            pass

    def _note_ask(self) -> None:
        if not getattr(self, 'state_path', None):
            return
        try:
            data = self._load_state()
            data['last_ask'] = time.time()
            self._save_state(data)
        except OSError:
            pass

    def followup_voice(self, message: str) -> bool:
        if not PLEA.match((message or '').strip()):
            return False
        try:
            asked = float(self._load_state().get('last_ask'))
        except (TypeError, ValueError):
            return False
        return time.time() - asked <= PLEA_SECONDS

    def _delivery_hint(self, message: str, asking: bool) -> str:
        if not needs_delivery_fact(message, asking):
            return ''
        kind = self._load_state().get('last_delivery')
        if kind == 'voice':
            return HINT_VOICE
        if kind == 'text':
            return HINT_TEXT
        if asking:
            return HINT_UNKNOWN
        return ''

    def wants_spontaneous(self, message: str) -> bool:
        if not self.config.get('spontaneous_goodnight', True):
            return False
        if not GOODNIGHT.search(message or '') or QUESTION.search(message or ''):
            return False
        if not goodnight_window(datetime.now(CST)):
            return False
        if not quota_ok(self._history(), time.time(), int(self.config.get('spontaneous_per_week', 2)), 2):
            return False
        return random.random() < float(self.config.get('spontaneous_probability', 0.5))

    def allowed(self, event: AstrMessageEvent) -> bool:
        owner = str(self.config.get('owner_session', ''))
        pieces = owner.split(':', 2)
        return (
            bool(self.config.get('enabled', False))
            and len(pieces) == 3 and pieces[1] == 'FriendMessage'
            and bool(pieces[2]) and pieces[2].isascii() and pieces[2].isdigit()
            and event.get_platform_name() == 'aiocqhttp'
            and event.is_private_chat()
            and event.unified_msg_origin == owner
            and str(event.get_sender_id()) == pieces[2]
        )

    @filter.on_llm_request()
    async def mark_requested(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.allowed(event):
            return
        message = event.get_message_str()
        asking = requested(message) or self.followup_voice(message)
        hint = self._delivery_hint(message, asking)
        base = req.system_prompt or ''
        if not asking:
            if self.wants_spontaneous(message):
                event.set_extra(MARKER, True)
                event.set_extra(SPONTANEOUS, True)
                req.system_prompt = base + '\n他要去睡了。这次回复会由程序念成一条晚安语音发给他：只写一两句标准普通话口语的晚安话，温柔一点、带点你的小性子，不超过60字，不用方言，不加动作描写、颜文字或表情配图标记，也不要提语音。最前面单独写一个语气标记，比如【语气：困困的，温柔小声】，程序会读掉它，不会念出来。' + hint
                return
            if hint:
                req.system_prompt = base + hint
            return
        # The existing provider is non-streaming. No second chat model request.
        event.set_extra(MARKER, True)
        self._note_ask()
        req.system_prompt = base + VOICE_PROMPT + hint

    def breeze_ready(self) -> bool:
        key = str(self.config.get('breeze_api_key', '') or '').strip()
        voice = str(self.config.get('breeze_voice_id', '') or '').strip()
        return bool(key) and bool(VOICE_ID_RE.match(voice))

    async def _breeze_once(self, session, text: str, tone: str = '') -> bytes:
        key = str(self.config.get('breeze_api_key', '')).strip()
        voice = str(self.config.get('breeze_voice_id', '')).strip()
        body = {'text': text, 'language_code': 'zh',
                'voice_settings': {'speed': min(max(float(self.config.get('breeze_speed', 1.0) or 1.0), 0.5), 2.0)}}
        instructions = str(self.config.get('breeze_instructions', '') or '').strip()
        if tone:
            instructions = (f'这段话的语气：{tone}。' + instructions).strip()
        if instructions:
            body['instructions'] = instructions[:300]
        async with session.post(BREEZE_URL.format(voice=voice), params={'output_format': 'mp3_24000_128'},
                                json=body, headers={'xi-api-key': key, 'Content-Type': 'application/json'}) as resp:
            if resp.status == 429:
                raise BreezeBusy('breeze http 429')
            if resp.status != 200:
                raise RuntimeError(f'breeze http {resp.status}')
            # content.read(n) may return only the first network chunk; collect the whole body.
            data = bytearray()
            async for block in resp.content.iter_chunked(65536):
                data += block
                if len(data) > 5_000_000:
                    raise ValueError('breeze audio size')
        if len(data) < 512:
            raise ValueError('breeze audio size')
        return bytes(data)

    async def _breeze_chunk(self, session, text: str, tone: str = '') -> bytes:
        """The model sometimes stops early, and the free plan allows one request at a time:
        retry a short or 429 result, then give up (caller falls back)."""
        for attempt in range(3):
            try:
                data = await (self._breeze_once(session, text, tone) if tone else self._breeze_once(session, text))
            except BreezeBusy:
                await asyncio.sleep(2 + attempt * 2)
                continue
            if not looks_truncated(text, len(data)):
                return data
        raise ValueError('breeze audio cut short or busy')

    async def _breeze_mp3(self, text: str, path: Path, tone: str = '') -> None:
        """BreezeBlue 云端音色；整段合成（超长才分段），任何失败都抛出，由调用方退回微软声音。"""
        import aiohttp
        # One request keeps the voice consistent; split only very long text (chunks can drift in timbre).
        parts = chunks(text, 300) or [text]
        if len(parts) > 12:
            raise ValueError('too many chunks')
        timeout = aiohttp.ClientTimeout(total=60)
        audio = []
        async with asyncio.timeout(130):
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for part in parts:  # one at a time: concurrent calls get 429 on the free plan
                    audio.append(await (self._breeze_chunk(session, part, tone) if tone else self._breeze_chunk(session, part)))
        path.write_bytes(b''.join(audio))  # CBR MP3 frames concatenate cleanly

    async def synthesize(self, text: str, tone: str = '') -> Path:
        text, tag_tone = split_tone(text)
        tone = tone or tag_tone
        text = SOUND_WORDS.sub('', text).strip() or text
        text = speech_text(text) or text
        path = self.root / (uuid.uuid4().hex + '.mp3')
        try:
            async with self.lock:
                done = False
                if self.breeze_ready():
                    try:
                        await (self._breeze_mp3(text, path, tone) if tone else self._breeze_mp3(text, path))
                        done = True
                    except Exception as exc:  # never log the key or the text
                        path.unlink(missing_ok=True)
                        logger.warning(f'BreezeBlue 音色暂不可用（{type(exc).__name__}: {str(exc)[:60]}），改用微软声音。')
                if not done:
                    import edge_tts
                    async with asyncio.timeout(35):
                        await edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH).save(str(path))
                if not path.is_file() or not 512 <= path.stat().st_size <= 5_000_000:
                    raise ValueError('Empty or excessive audio')
                wav = await prepare_wav(path)
            path.unlink(missing_ok=True)
            return wav
        except BaseException:
            path.unlink(missing_ok=True)
            path.with_suffix('.wav').unlink(missing_ok=True)
            raise

    @filter.on_decorating_result()
    async def speak_when_requested(self, event: AstrMessageEvent):
        if not self.allowed(event):
            return
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return
        tone = ''
        for p in result.chain:  # never let the hidden tone tag show up as text
            if isinstance(p, Plain) and TONE_TAG.search(p.text or ''):
                p.text, found = split_tone(p.text)
                tone = tone or found
        if event.get_extra(MARKER) is not True:
            self._note_delivery('text')
            return
        text = '\n'.join(p.text for p in result.chain if isinstance(p, Plain)).strip()
        text = re.sub(r'\[\[meme:[^\]]+\]\]', '', text).strip()
        if any(isinstance(p, Record) for p in result.chain):
            self._note_delivery('voice')
            return
        if not text or len(text) > 600:
            # Preserve the complete text when it is unsuitable for speech.
            why = '空文本' if not text else '超过600字'
            logger.warning(f'按需语音：这条没有合成（{why}），保留文字回复。')
            self._note_delivery('text')
            return
        text = drop_false_claims(text) or text
        try:
            path = await (self.synthesize(text, tone) if tone else self.synthesize(text))
        except Exception:
            logger.warning('按需语音暂不可用，保留本次文字回复。')
            self._note_delivery('text')
            return
        result.chain = [Record.fromFileSystem(str(path), text=text)]
        if event.get_extra(SPONTANEOUS) is True:
            try:
                self._record_spontaneous()
            except OSError:
                pass
        self._note_delivery('voice')
        # Retain generated files for delayed platform reads; remove only our old audio.
        cutoff = time.time() - 86400
        for old in self.root.iterdir():
            try:
                if re.fullmatch(r'[a-f0-9]{32}\.(?:mp3|wav)', old.name) and old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    @filter.command('语音试听')
    async def tryout(self, event: AstrMessageEvent):
        """主人私聊：同一句话用几种语气各念一遍，听听效果。逐条直接发送，最后再结束事件。"""
        if not self.allowed(event):
            return
        if not self.breeze_ready():
            await event.send(MessageChain(chain=[Plain('现在没配好 Breeze 声音，备用声音不支持换语气。')]))
            event.stop_event()
            return
        for label, tone, line in TRYOUT_ITEMS:
            try:
                path = await (self.synthesize(line, tone) if tone else self.synthesize(line))
            except Exception:
                await event.send(MessageChain(chain=[Plain(f'{label}：这条没合成出来。')]))
                continue
            await event.send(MessageChain(chain=[Plain(label)]))
            await event.send(MessageChain(chain=[Record.fromFileSystem(str(path), text=line)]))
        event.stop_event()
