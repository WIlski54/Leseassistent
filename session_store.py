"""In-memory session storage for LeseAssistent.

API keys are intentionally kept in RAM only and are removed when a session ends
or expires.
"""

from datetime import datetime, timedelta
import random
import secrets
import threading


sessions = {}
sessions_lock = threading.Lock()

SESSION_CODE_LENGTH = 6
SESSION_TIMEOUT_HOURS = 3
CLEANUP_INTERVAL_SECONDS = 300

ANONYMOUS_ANIMALS = [
    ('ðŸ¦Š', 'Fuchs'), ('ðŸ»', 'BÃ¤r'), ('ðŸ¦', 'LÃ¶we'), ('ðŸ¯', 'Tiger'),
    ('ðŸ¦‹', 'Schmetterling'), ('ðŸ¢', 'SchildkrÃ¶te'), ('ðŸ¦‰', 'Eule'), ('ðŸ¬', 'Delfin'),
    ('ðŸ¦…', 'Adler'), ('ðŸº', 'Wolf'), ('ðŸ¦Œ', 'Hirsch'), ('ðŸ˜', 'Elefant'),
    ('ðŸ¦’', 'Giraffe'), ('ðŸ¼', 'Panda'), ('ðŸ¦œ', 'Papagei'), ('ðŸ¨', 'Koala'),
    ('ðŸ¦©', 'Flamingo'), ('ðŸ¸', 'Frosch'), ('ðŸ¦”', 'Igel'), ('ðŸ¿ï¸', 'EichhÃ¶rnchen'),
    ('ðŸ¦­', 'Robbe'), ('ðŸ§', 'Pinguin'), ('ðŸ¦š', 'Pfau'), ('ðŸ', 'Biene'),
    ('ðŸ¦Ž', 'Eidechse'), ('ðŸ™', 'Oktopus'), ('ðŸ¦€', 'Krabbe'), ('ðŸŒ', 'Schnecke')
]


def get_anonymous_name(session_code, student_sid):
    """Generiert einen anonymen Tiernamen fÃ¼r einen SchÃ¼ler."""
    with sessions_lock:
        if session_code in sessions:
            used_indices = set()
            for sid, student_data in sessions[session_code]['students'].items():
                if 'animal_index' in student_data:
                    used_indices.add(student_data['animal_index'])

            for i in range(len(ANONYMOUS_ANIMALS)):
                if i not in used_indices:
                    return i, ANONYMOUS_ANIMALS[i]

            idx = random.randint(0, len(ANONYMOUS_ANIMALS) - 1)
            emoji, name = ANONYMOUS_ANIMALS[idx]
            return idx, (emoji, f"{name} {len(sessions[session_code]['students']) + 1}")

    return 0, ANONYMOUS_ANIMALS[0]


def generate_session_code():
    """Generiert einen 6-stelligen alphanumerischen Code ohne leicht verwechselbare Zeichen."""
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    while True:
        code = ''.join(random.choices(chars, k=SESSION_CODE_LENGTH))
        with sessions_lock:
            if code not in sessions:
                return code


def create_session(teacher_sid, keys, pin=''):
    """Erstellt eine neue Session fÃ¼r einen Lehrer."""
    code = generate_session_code()
    with sessions_lock:
        sessions[code] = {
            'keys': keys,
            'teacher_sid': teacher_sid,
            'teacher_token': secrets.token_urlsafe(32),
            'created': datetime.now(),
            'expires': datetime.now() + timedelta(hours=SESSION_TIMEOUT_HOURS),
            'students': {},
            'text': '',
            'pin': pin,
            'tasks': [],
            'tasks_available': False,
            'translation_requests': {},
            'simplification_enabled': False,
            'student_levels': {},
        }
    return code


def get_session(code):
    """Holt Session-Daten, ohne API-Keys nach außen zu exponieren."""
    with sessions_lock:
        if code in sessions:
            session = sessions[code]
            if datetime.now() < session['expires']:
                return session

            del sessions[code]
    return None


def get_session_keys(code):
    """Holt die API-Keys fÃ¼r eine Session, nur fÃ¼r Server-interne Nutzung."""
    session = get_session(code)
    if session:
        return session['keys']
    return None


def has_teacher_access(code, teacher_token=None, sid=None):
    """PrÃ¼ft, ob eine Anfrage die aktive Lehrkraft-Session steuern darf."""
    session = get_session(code)
    if not session:
        return False

    if sid and session.get('teacher_sid') == sid:
        return True

    return bool(teacher_token) and secrets.compare_digest(
        teacher_token,
        session.get('teacher_token', '')
    )


def end_session(code):
    """Beendet eine Session und lÃ¶scht alle Keys."""
    with sessions_lock:
        if code in sessions:
            del sessions[code]
            return True
    return False


def add_student_to_session(code, student_sid, student_name=None):
    """FÃ¼gt einen SchÃ¼ler zur Session hinzu mit anonymem Tiernamen."""
    animal_index, (emoji, animal_name) = get_anonymous_name(code, student_sid)

    with sessions_lock:
        if code in sessions:
            sessions[code]['students'][student_sid] = {
                'joined': datetime.now(),
                'name': student_name,
                'animal_index': animal_index,
                'animal_emoji': emoji,
                'animal_name': animal_name,
                'anonymous_id': f"{emoji} {animal_name}"
            }
            return sessions[code]['students'][student_sid]
    return None


def remove_student_from_session(code, student_sid):
    """Entfernt einen SchÃ¼ler aus der Session."""
    with sessions_lock:
        if code in sessions and student_sid in sessions[code]['students']:
            del sessions[code]['students'][student_sid]
            return True
    return False


def get_student_count(code):
    """Gibt die Anzahl der verbundenen SchÃ¼ler zurÃ¼ck."""
    session = get_session(code)
    if session:
        return len(session['students'])
    return 0


def cleanup_expired_sessions():
    """Entfernt abgelaufene Sessions und gibt deren Codes zurÃ¼ck."""
    with sessions_lock:
        expired = [
            code for code, session in sessions.items()
            if datetime.now() >= session['expires']
        ]
        for code in expired:
            del sessions[code]
        return expired
