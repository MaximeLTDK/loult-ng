import logging
import re
from colorsys import hsv_to_rgb
from datetime import datetime, timedelta
from io import BytesIO
from re import sub, compile as regex
from shlex import quote
from struct import pack
from asyncio import get_event_loop, create_subprocess_shell
from asyncio.subprocess import PIPE
from typing import List, Union

import numpy
from scipy.io import wavfile

from config import (
        FLOOD_DETECTION_WINDOW,
        FLOOD_DETECTION_MSG_PER_SEC,
        FLOOD_WARNING_TIMEOUT
    )
from tools import pokemons
from tools.audio_tools import resample
from tools.phonems import PhonemList, Phonem


logger = logging.getLogger('tools')


class ToolsError(Exception):
    pass


class VoiceParameters:

    def __init__(self, speed : int, pitch : int, voice_id : int):
        self.speed = speed
        self.pitch = pitch
        self.voice_id = voice_id

    @classmethod
    def from_cookie_hash(cls, cookie_hash):
        return cls((cookie_hash[5] % 80) + 90, # speed
                   cookie_hash[0] % 100, # pitch
                   cookie_hash[1]) # voice_id


class PokeParameters:

    def __init__(self, color, poke_id):
        self.color = color
        self.poke_id = poke_id
        self.pokename = pokemons.pokemon[self.poke_id]

    @classmethod
    def from_cookie_hash(cls, cookie_hash):
        color_rgb = hsv_to_rgb(cookie_hash[4] / 255, 0.8, 0.9)
        return cls('#' + pack('3B', *(int(255 * i) for i in color_rgb)).hex(), # color
                   (cookie_hash[2] | (cookie_hash[3] << 8)) % len(pokemons.pokemon) + 1) # poke id


class UserState:

    detection_window = timedelta(seconds=FLOOD_DETECTION_WINDOW)

    def __init__(self):
        from .effects import AudioEffect, HiddenTextEffect, ExplicitTextEffect, PhonemicEffect, \
            VoiceEffect

        self.effects = {cls: [] for cls in
                        (AudioEffect, HiddenTextEffect, ExplicitTextEffect, PhonemicEffect, VoiceEffect)}

        self.last_attack = datetime.now()  # any user has to wait some time before attacking, after entering the chan
        self.last_shelling = datetime.now() # last flooder attack, user has to wait too
        self.last_msgs_timestamps = [] #type:List[datetime]
        self.has_been_warned = False # User has been warned he shouldn't flood
        self.is_shadowmuted = False # User has been shadowmuted

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name == "has_been_warned" and value:
            # it's safe since the whole application only
            # uses the default loop
            loop = get_event_loop()
            loop.call_later(FLOOD_WARNING_TIMEOUT, self._reset_warning)

    def _reset_warning(self):
        """
        Helper with a better debug representation than
        a lambda for use as a callback in the event loop.
        """
        self.has_been_warned = False

    def add_effect(self, effect):
        """Adds an effect to one of the active tools list (depending on the effect type)"""
        from .effects import EffectGroup, AudioEffect, HiddenTextEffect, ExplicitTextEffect, PhonemicEffect, \
            VoiceEffect

        if isinstance(effect, EffectGroup):  # if the effect is a meta-effect (a group of several tools)
            added_effects = effect.effects
        else:
            added_effects = [effect]

        for efct in added_effects:
            for cls in (AudioEffect, HiddenTextEffect, ExplicitTextEffect, PhonemicEffect, VoiceEffect):
                if isinstance(efct, cls):
                    if len(self.effects[cls]) == 5:  # only 5 effects of one type allowed at a time
                        self.effects[cls].pop(0)
                    self.effects[cls].append(efct)
                    break

    def log_msg(self):
        """Add a timestamp for a user's message, and clears timestamps which are too old"""
        # removing msg timestamps that are out of the detection window
        now = datetime.now()
        self._refresh_timestamps(now)
        self.last_msgs_timestamps.append(now)

    @property
    def is_flooding(self):
        self._refresh_timestamps()
        return len(self.last_msgs_timestamps) > FLOOD_DETECTION_MSG_PER_SEC * FLOOD_DETECTION_WINDOW

    def _refresh_timestamps(self, now=None):
        # now has to be a possible argument else there might me slight
        # time differences between the current time of the calling function
        # and this one's current time.
        now = now if now else datetime.now()
        # removing msg timestamps that are out of the detection window
        updated = [timestamp for timestamp in self.last_msgs_timestamps
                   if timestamp + self.detection_window > now]
        self.last_msgs_timestamps = updated


class AudioRenderer:
    lang_voices_mapping = {"fr": ("fr", (1, 2, 3, 4, 5, 6, 7)),
                           "en": ("us", (1, 2, 3)),
                           "es": ("es", (1, 2)),
                           "de": ("de", (4, 5, 6, 7))}

    volumes_presets = {'fr1': 1.17138, 'fr2': 1.60851, 'fr3': 1.01283, 'fr4': 1.0964, 'fr5': 2.64384, 'fr6': 1.35412,
                       'fr7': 1.96092, 'us1': 1.658, 'us2': 1.7486, 'us3': 3.48104, 'es1': 3.26885, 'es2': 1.84053}

    def _get_additional_params(self, lang, voice_params : VoiceParameters):
        """Uses the msg's lang field to figure out the voice, sex, and volume of the synth"""
        lang, voices = self.lang_voices_mapping.get(lang, self.lang_voices_mapping["fr"])
        voice = voices[voice_params.voice_id % len(voices)]

        if lang != 'fr':
            sex = voice
        else:
            sex = 4 if voice in (2, 4) else 1

        volume = 1
        if lang != 'de':
            volume = self.volumes_presets['%s%d' % (lang, voice)] * 0.5

        return lang, voice, sex, volume

    def _wav_format(self, wav : bytes):
        return wav[:4] + pack('<I', len(wav) - 8) + wav[8:40] + pack('<I', len(wav) - 44) + wav[44:]

    async def string_to_audio(self, text : str, lang : str, voice_params : VoiceParameters) -> bytes:
        lang, voice, sex, volume = self._get_additional_params(lang, voice_params)
        synth_string = 'MALLOC_CHECK_=0 espeak -s %d -p %d --pho -q -v mb/mb-%s%d %s ' \
                       '| MALLOC_CHECK_=0 mbrola -v %g -e /usr/share/mbrola/%s%d/%s%d - -.wav' \
                       % (voice_params.speed, voice_params.pitch, lang, sex, text,
                          volume, lang, voice, lang, voice)
        logger.debug("Running synth command %s" % synth_string)
        process = await create_subprocess_shell(synth_string, stderr=PIPE, stdout=PIPE)
        wav, err = await process.communicate()
        return self._wav_format(wav)

    async def phonemes_to_audio(self, phonemes : PhonemList, lang : str, voice_params : VoiceParameters) -> bytes:
        lang, voice, sex, volume = self._get_additional_params(lang, voice_params)
        audio_synth_string = 'MALLOC_CHECK_=0 mbrola -v %g -e /usr/share/mbrola/%s%d/%s%d - -.wav' \
                             % (volume, lang, voice, lang, voice)
        logger.debug("Running mbrola command %s" % audio_synth_string)
        process = await create_subprocess_shell(audio_synth_string, stdout=PIPE,
                                                stdin=PIPE, stderr=PIPE)
        wav, err = await process.communicate(input=str(phonemes).encode('utf-8'))
        return self._wav_format(wav)

    async def string_to_phonemes(self, text : str, lang : str, voice_params : VoiceParameters) -> PhonemList:
        lang, voice, sex, volume = self._get_additional_params(lang, voice_params)
        phonem_synth_string = 'MALLOC_CHECK_=0 espeak -s %d -p %d --pho -q -v mb/mb-%s%d %s ' \
                              % (voice_params.speed, voice_params.pitch, lang, sex, text)
        logger.debug("Running espeak command %s" % phonem_synth_string)
        process = await create_subprocess_shell(phonem_synth_string,
                                                stdout=PIPE, stderr=PIPE)
        phonems, err = await process.communicate()
        return PhonemList(phonems.decode('utf-8').strip())

    @staticmethod
    async def to_f32_16k(wav : bytes) -> numpy.ndarray:
        # converting the wav to ndarray, which is much easier to use for DSP
        rate, data = wavfile.read(BytesIO(wav))
        # casting the data array to the right format (float32, for usage by pysndfx)
        data = (data / (2. ** 15)).astype('float32')
        if rate != 16000:
            data = await resample(data, rate)
            rate = 16000

        return rate, data

    @staticmethod
    def to_wav_bytes(data : numpy.ndarray, rate : int) -> bytes:
        # casting it back to int16
        data = (data * (2. ** 15)).astype("int16")
        # then, converting it back to binary data
        bytes_obj = bytes()
        bytes_buff = BytesIO(bytes_obj)
        wavfile.write(bytes_buff, rate, data)
        return bytes_buff.read()


class UtilitaryEffect:
    pass


class SpoilerBipEffect(UtilitaryEffect):
    """If there are ** phonems markers in the text, replaces their phonemic render by
    an equally long beep. If not, just returns the text"""
    _tags_phonems = {
        "en" : ("k_hIN", "dZINk"),
        "fr" : ("kiN", "ZiNk"),
        "de" : ("kIN", "gINk"),
        "es" : ("kin", "xink"),
    }

    def __init__(self, renderer : AudioRenderer, voice_params : VoiceParameters):
        super().__init__()
        self.renderer = renderer
        self.voice_params = voice_params

    def _gen_beep(self, duration : int, lang : str):
        i_phonem = "i:" if lang == "de" else "i" # "i" phonem is not the same i german. Damn krauts
        return PhonemList(PhonemList([Phonem("b", 103),
                                      Phonem(i_phonem, duration, [(0, 103 * 3), (80, 103 * 3), (100, 103 * 3)]),
                                      Phonem("p", 228)]))

    async def process(self, text: str, lang : str) -> Union[str, PhonemList]:
        """Beeps out parts of the text that are tagged with double asterisks.
        It basicaly replaces the opening and closing asterisk with two opening and closing 'stop words'
        then finds the phonemic form of these two and replaces the phonems inside with an equivalently long beep"""
        occ_list = re.findall(r"\*\*.+?\*\*", text)
        if occ_list:
            # replace the "**text**" by "king text gink"
            tagged_occ_list = [" king %s gink " % occ.strip("*") for occ in occ_list]
            for occ, tagged_occ in zip(occ_list, tagged_occ_list):
                text = text.replace(occ, tagged_occ)
            # getting the phonemic form of the text
            phonems = await self.renderer.string_to_phonemes(text, lang, self.voice_params)
            # then using a simple state machine (in_beep is the state), replaces the phonems between
            # the right phonemic occurence with the phonems of a beep
            in_beep = False
            output, buffer = PhonemList([]), PhonemList([])
            while phonems:
                if PhonemList(phonems[:3]).phonemes_str == self._tags_phonems[lang][0] and not in_beep:
                    in_beep, buffer = True, PhonemList([])
                    phonems = PhonemList(phonems[3:])
                elif PhonemList(phonems[:4]).phonemes_str == self._tags_phonems[lang][1] and in_beep:
                    in_beep = False
                    # creating a beep of the buffer's duration
                    if buffer:
                        output += self._gen_beep(sum([pho.duration for pho in buffer]), lang)
                    phonems = phonems[4:]
                elif not in_beep:
                    output.append(phonems.pop(0))
                elif in_beep:
                    buffer.append(phonems.pop(0))
            return output
        else:
            return text


class BannedWords(list):

    words = None

    def __init__(self, words):
        self.words = [regex(word) for word in words]

    def __call__(self, word):
        return any(regex_word.fullmatch(word) for regex_word in self.words)


links_translation = {'fr': 'cliquez mes petits chatons',
                     'de': 'Klick drauf!',
                     'es': 'Clico JAJAJA',
                     'en': "Click it mate"}


def prepare_text_for_tts(text : str, lang : str) -> str:
    text = sub('(https?://[^ ]*[^.,?! :])', links_translation[lang], text)
    text = text.replace('#', 'hashtag ')
    return quote(text.strip(' -"\'`$();:.'))
