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
# Natural asks anywhere in the message: 用语音说 / 发个语音 / 语音回我 / 说给我听 / 想听你的声音 ...
VOICE_ASK = re.compile(
    r'(?:用|发|来|回|录)(?:个|条|一条|一段|段|句|一句)?语音'
    r'|语音(?:回复|回答|回我|说|讲|念|喊|聊|发我|给我)'
    r'|(?:说|念|读|讲|喊|唱)给我听'
    r'|想听(?:听)?你(?:的)?(?:声音|说话|讲话|说)'
    r'|听听你的声音')
VOICE_NO = re.compile(
    r'(?:别|不要|不用|不想|甭|无需|不必)(?:再)?(?:给我)?(?:用|发|回|来)?(?:个|条)?语音'
    r'|(?<![给帮跟对和向])我(?:刚|刚才|已经|又)?(?:发|用|录)了?(?:个|条|一条)?语音'
    r'|会(?:不会)?(?:用|发)?语音.*[吗么?？]'
    r'|[“"「『].{0,20}语音.{0,20}[”"」』]')


def requested(message: str) -> bool:
    message = message or ''
    if REQUEST.match(message):
        return True
    return bool(VOICE_ASK.search(message)) and not VOICE_NO.search(message)


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

    def _history(self) -> list:
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            return [float(t) for t in data.get('sent', [])]
        except Exception:
            return []

    def _record_spontaneous(self) -> None:
        now = time.time()
        history = [t for t in self._history() if now - t < 30 * 86400] + [now]
        self.state_path.write_text(json.dumps({'sent': history}), encoding='utf-8')

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
        if not requested(message):
            if self.wants_spontaneous(message):
                event.set_extra(MARKER, True)
                event.set_extra(SPONTANEOUS, True)
                req.system_prompt += '\n他要去睡了。这次回复会由程序念成一条晚安语音发给他：只写一两句标准普通话口语的晚安话，温柔一点、带点你的小性子，不超过60字，不用方言，不加动作描写、颜文字或表情配图标记，也不要提语音。最前面单独写一个语气标记，比如【语气：困困的，温柔小声】，程序会读掉它，不会念出来。'
            return
        # The existing provider is non-streaming. No second chat model request.
        event.set_extra(MARKER, True)
        req.system_prompt += '\n用户这次想听你的语音，这条回复会由程序念成语音发给他，你直接说要说的话就行，不要说发不了语音或先欠着。只写标准普通话口语，不用方言、地域口音词或方言句尾；用自然温软的简短中文正文，尽量不超过180字。程序负责语音合成，不要解释合成过程，不加动作描写、颜文字或表情配图标记。最前面单独写一个语气标记，用几个字说这段话该怎么说，比如【语气：惊喜开心】【语气：委屈带哭腔】【语气：小声撒娇】【语气：嘴硬又有点害羞】，按你此刻真实的情绪来选；程序会读掉它，不会念出来。哭腔、撒娇这些交给语气标记，正文只写正常说的话，不写“呜呜”“嘤嘤”这类拟声字，也别用一串省略号硬造停顿。'

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
            return
        text = '\n'.join(p.text for p in result.chain if isinstance(p, Plain)).strip()
        text = re.sub(r'\[\[meme:[^\]]+\]\]', '', text).strip()
        if not text or len(text) > 600 or any(isinstance(p, Record) for p in result.chain):
            return  # Preserve the complete text when it is unsuitable for speech.
        try:
            path = await (self.synthesize(text, tone) if tone else self.synthesize(text))
        except Exception:
            logger.warning('按需语音暂不可用，保留本次文字回复。')
            return
        result.chain = [Record.fromFileSystem(str(path), text=text)]
        if event.get_extra(SPONTANEOUS) is True:
            try:
                self._record_spontaneous()
            except OSError:
                pass
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
