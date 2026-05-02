"""Small in-memory LRU caches for external API responses."""

from collections import OrderedDict
import hashlib
import threading


MAX_CACHE_SIZE = 500
MAX_TRANSLATION_CACHE_SIZE = 1000

tts_cache = OrderedDict()
translation_cache = OrderedDict()
cache_lock = threading.Lock()
translation_cache_lock = threading.Lock()


def get_cache_key(text, voice_id):
    content = f"{text}|{voice_id}"
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def get_translation_cache_key(text, target_language):
    content = f"{text}|{target_language}"
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def get_from_cache(cache_key):
    with cache_lock:
        if cache_key in tts_cache:
            tts_cache.move_to_end(cache_key)
            return tts_cache[cache_key]
    return None


def add_to_cache(cache_key, data):
    with cache_lock:
        if cache_key in tts_cache:
            tts_cache.move_to_end(cache_key)
        else:
            if len(tts_cache) >= MAX_CACHE_SIZE:
                tts_cache.popitem(last=False)
            tts_cache[cache_key] = data


def get_from_translation_cache(cache_key):
    with translation_cache_lock:
        if cache_key in translation_cache:
            translation_cache.move_to_end(cache_key)
            return translation_cache[cache_key]
    return None


def add_to_translation_cache(cache_key, translated_text):
    with translation_cache_lock:
        if cache_key in translation_cache:
            translation_cache.move_to_end(cache_key)
        else:
            if len(translation_cache) >= MAX_TRANSLATION_CACHE_SIZE:
                translation_cache.popitem(last=False)
            translation_cache[cache_key] = translated_text


def get_cache_stats():
    with cache_lock:
        tts_size = len(tts_cache)
    with translation_cache_lock:
        translation_size = len(translation_cache)

    return {
        'tts_cache': {'size': tts_size, 'max': MAX_CACHE_SIZE},
        'translation_cache': {
            'size': translation_size,
            'max': MAX_TRANSLATION_CACHE_SIZE
        }
    }
