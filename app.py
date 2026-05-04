"""
LeseAssistent - Flask Backend mit Session-System
=================================================
Sicheres Session-basiertes System für den Unterricht.

- Lehrer erstellt Session mit seinen API-Keys
- Schüler treten mit Session-Code bei
- Keys bleiben NUR auf dem Server (im RAM)
- Session-Ende = Keys gelöscht
"""

from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room
import requests
import os
import json
import base64
import re
import qrcode
from io import BytesIO
from datetime import datetime
import threading
import time

from cache_store import (
    add_to_cache,
    add_to_translation_cache,
    get_cache_key,
    get_cache_stats,
    get_from_cache,
    get_from_translation_cache,
    get_translation_cache_key,
)
from session_store import (
    CLEANUP_INTERVAL_SECONDS,
    add_student_to_session,
    cleanup_expired_sessions,
    create_session,
    end_session,
    get_session,
    get_session_keys,
    has_teacher_access,
    sessions,
    sessions_lock,
)

# Für Datei-Verarbeitung
try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'leseassistent-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_UPLOAD_MB', '20')) * 1024 * 1024

CORS_ORIGINS_ENV = os.environ.get('CORS_ORIGINS', '*').strip()
CORS_ORIGINS = '*' if CORS_ORIGINS_ENV == '*' else [
    origin.strip() for origin in CORS_ORIGINS_ENV.split(',') if origin.strip()
]
CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}})

@app.errorhandler(413)
def file_too_large(error):
    return jsonify({'error': 'Datei ist zu gross'}), 413

# Für lokale Entwicklung 'threading', für Production (Render) 'gevent'
ASYNC_MODE = os.environ.get('ASYNC_MODE', 'threading')
socketio = SocketIO(app, cors_allowed_origins=CORS_ORIGINS, async_mode=ASYNC_MODE)

# =============================================================================
# SESSION MANAGEMENT
# =============================================================================

MAX_AI_TEXT_CHARS = int(os.environ.get('MAX_AI_TEXT_CHARS', '20000'))
MAX_TTS_CHARS = int(os.environ.get('MAX_TTS_CHARS', '10000'))
GEMINI_TEXT_MODEL = os.environ.get('GEMINI_TEXT_MODEL', 'gemini-2.5-flash')

def get_request_teacher_token(data=None):
    """Liest das Lehrer-Token aus Header oder JSON-Body."""
    if data is None:
        data = request.get_json(silent=True) or {}
    return (request.headers.get('X-Teacher-Token') or data.get('teacher_token') or '').strip()

def require_teacher_access(code, data=None):
    """Gemeinsame Prüfung für HTTP-Endpunkte, die nur die Lehrkraft nutzen darf."""
    if not get_session(code):
        return jsonify({'error': 'Session nicht gefunden'}), 404

    if not has_teacher_access(code, teacher_token=get_request_teacher_token(data)):
        return jsonify({'error': 'Keine Berechtigung für diese Session'}), 403

    return None

# Background-Thread für Session-Cleanup
def session_cleanup_thread():
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        for code in cleanup_expired_sessions():
            app.logger.info(f"Session {code} expired and cleaned up")

# Cleanup-Thread starten (nur wenn nicht im Import-Modus)
cleanup_thread = threading.Thread(target=session_cleanup_thread, daemon=True)

# =============================================================================
# TEXT CLEANUP
# =============================================================================

def cleanup_extracted_text(text):
    """Bereinigt aus PDFs/DOCX extrahierten Text."""
    if not text:
        return text
    text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)
    text = re.sub(r'(\w)-\s+(\w)', r'\1\2', text)
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# =============================================================================
# FRONTEND ROUTES
# =============================================================================

@app.route('/')
def index():
    """Startseite - Auswahl Lehrer/Schüler."""
    return render_template('index.html')

@app.route('/teacher')
def teacher_dashboard():
    """Lehrer-Dashboard für Session-Management."""
    return render_template('teacher.html')

@app.route('/student')
def student_view():
    """Schüler-Ansicht (nach Session-Beitritt)."""
    return render_template('student.html')

@app.route('/aufgaben')
def aufgaben():
    """Aufgaben-Seite."""
    return render_template('aufgaben.html')

@app.route('/nachsprechen')
def nachsprechen():
    """Nachsprechen-Übung."""
    return render_template('nachsprechen.html')

# =============================================================================
# SESSION API ENDPOINTS
# =============================================================================

@app.route('/api/session/create', methods=['POST'])
def api_create_session():
    """
    Erstellt eine neue Session (nur für Lehrer).
    
    Erwartet JSON:
    {
        "elevenlabs_key": "sk_...",
        "ai_key": "sk-...",
        "ai_provider": "openai" | "anthropic" | "google",
        "voice_id": "21m00Tcm4TlvDq8ikWAM"
    }
    """
    try:
        data = request.json
        
        keys = {
            'elevenlabs': data.get('elevenlabs_key', ''),
            'ai': data.get('ai_key', ''),
            'ai_provider': data.get('ai_provider', 'openai'),
            'voice_id': data.get('voice_id', '21m00Tcm4TlvDq8ikWAM')
        }
        
        if not keys['elevenlabs']:
            return jsonify({'error': 'ElevenLabs API Key erforderlich'}), 400
        
        # Session erstellen (teacher_sid wird später via WebSocket gesetzt)
        code = create_session(None, keys)
        
        return jsonify({
            'success': True,
            'code': code,
            'teacher_token': sessions[code]['teacher_token'],
            'expires': sessions[code]['expires'].isoformat()
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/join', methods=['POST'])
def api_join_session():
    """
    Prüft ob eine Session existiert (für Schüler).
    
    Erwartet JSON:
    {
        "code": "ABC123"
    }
    """
    try:
        data = request.json
        code = data.get('code', '').upper().strip()
        
        session = get_session(code)
        if not session:
            return jsonify({'error': 'Session nicht gefunden oder abgelaufen'}), 404
        
        return jsonify({
            'success': True,
            'code': code,
            'student_count': len(session['students'])
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/end', methods=['POST'])
def api_end_session():
    """
    Beendet eine Session (nur für Lehrer).
    
    Erwartet JSON:
    {
        "code": "ABC123"
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        code = data.get('code', '').upper().strip()

        auth_error = require_teacher_access(code, data)
        if auth_error:
            return auth_error
        
        if end_session(code):
            # Alle Clients in dieser Session benachrichtigen
            socketio.emit('session_ended', {'message': 'Die Session wurde beendet.'}, room=code)
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Session nicht gefunden'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/status/<code>')
def api_session_status(code):
    """Gibt den Status einer Session zurück."""
    code = code.upper().strip()
    session = get_session(code)
    
    if not session:
        return jsonify({'error': 'Session nicht gefunden'}), 404
    
    return jsonify({
        'code': code,
        'student_count': len(session['students']),
        'created': session['created'].isoformat(),
        'expires': session['expires'].isoformat(),
        'has_text': bool(session.get('text'))
    })


@app.route('/api/session/settings/<code>')
def api_session_settings(code):
    """Gibt die Einstellungen einer Session zurück (für Schüler-Module)."""
    code = code.upper().strip()
    session = get_session(code)
    
    if not session:
        return jsonify({'error': 'Session nicht gefunden'}), 404
    
    keys = session.get('keys', {})
    
    return jsonify({
        'code': code,
        'stt_provider': keys.get('stt_provider', 'browser'),  # 'browser' oder 'scribe'
        'voice_id': keys.get('voice_id', '21m00Tcm4TlvDq8ikWAM'),
        'has_elevenlabs': bool(keys.get('elevenlabs'))
    })

@app.route('/api/session/qr/<code>')
def api_session_qr(code):
    """Generiert QR-Code für Session-Beitritt."""
    code = code.upper().strip()
    session = get_session(code)
    
    if not session:
        return jsonify({'error': 'Session nicht gefunden'}), 404
    
    # URL für Schüler-Beitritt
    base_url = request.host_url.rstrip('/')
    join_url = f"{base_url}/student?code={code}"
    
    # QR-Code generieren
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(join_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Als PNG zurückgeben
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    
    return send_file(buffer, mimetype='image/png')

@app.route('/api/session/set-text', methods=['POST'])
def api_set_session_text():
    """
    Setzt den Text für eine Session (Lehrer teilt Text mit Schülern).
    
    Erwartet JSON:
    {
        "code": "ABC123",
        "text": "Der zu lesende Text..."
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        code = data.get('code', '').upper().strip()
        text = data.get('text', '')

        auth_error = require_teacher_access(code, data)
        if auth_error:
            return auth_error

        if len(text) > MAX_AI_TEXT_CHARS:
            return jsonify({'error': f'Text ist zu lang (max. {MAX_AI_TEXT_CHARS} Zeichen)'}), 400
        
        with sessions_lock:
            if code in sessions:
                sessions[code]['text'] = text
                # Alle Schüler benachrichtigen
                socketio.emit('text_updated', {'text': text}, room=code)
                return jsonify({'success': True})
        
        return jsonify({'error': 'Session nicht gefunden'}), 404
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/get-text/<code>')
def api_get_session_text(code):
    """Holt den Text einer Session."""
    code = code.upper().strip()
    session = get_session(code)
    
    if not session:
        return jsonify({'error': 'Session nicht gefunden'}), 404
    
    return jsonify({'text': session.get('text', '')})

# =============================================================================
# WEBSOCKET EVENTS
# =============================================================================

@socketio.on('connect')
def handle_connect():
    app.logger.info(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    app.logger.info(f"Client disconnected: {request.sid}")
    # Schüler aus allen Sessions entfernen
    with sessions_lock:
        for code, session in sessions.items():
            if request.sid in session['students']:
                del session['students'][request.sid]
                # Lehrer über Schüler-Abgang informieren
                if session['teacher_sid']:
                    socketio.emit('student_left', {
                        'count': len(session['students'])
                    }, room=session['teacher_sid'])

@socketio.on('teacher_create_session')
def handle_teacher_create_session(data):
    """Lehrer erstellt Session via WebSocket."""
    keys = {
        'elevenlabs': data.get('elevenlabs_key', ''),
        'ai': data.get('ai_key', ''),
        'ai_provider': data.get('ai_provider', 'openai'),
        'voice_id': data.get('voice_id', '21m00Tcm4TlvDq8ikWAM'),
        'stt_provider': data.get('stt_provider', 'browser')  # 'browser' oder 'scribe'
    }
    
    if not keys['elevenlabs']:
        emit('session_error', {'error': 'ElevenLabs API Key erforderlich'})
        return
    
    pin = data.get('pin', '')
    code = create_session(request.sid, keys, pin)
    
    # Lehrer tritt seinem eigenen Room bei
    join_room(code)
    
    emit('session_created', {
        'code': code,
        'teacher_token': sessions[code]['teacher_token'],
        'expires': sessions[code]['expires'].isoformat(),
        'has_pin': bool(pin)
    })

@socketio.on('student_join_session')
def handle_student_join_session(data):
    """Schüler tritt Session bei."""
    code = data.get('code', '').upper().strip()
    name = data.get('name', 'Anonym')
    
    session = get_session(code)
    if not session:
        emit('join_error', {'error': 'Session nicht gefunden oder abgelaufen'})
        return
    
    # Schüler zur Session hinzufügen (gibt Student-Daten zurück)
    student_data = add_student_to_session(code, request.sid, name)
    join_room(code)
    
    # Schüler bestätigen (inkl. vorhandener Einstellungen, Aufgaben und anonyme ID)
    emit('join_success', {
        'code': code,
        'text': session.get('text', ''),
        'settings': session.get('settings', {}),
        'tasks_available': session.get('tasks_available', False),
        'tasks': session.get('tasks', []) if session.get('tasks_available', False) else [],
        'anonymous_id': student_data.get('anonymous_id', '🐾 Gast') if student_data else '🐾 Gast',
        'animal_emoji': student_data.get('animal_emoji', '🐾') if student_data else '🐾',
        'animal_name': student_data.get('animal_name', 'Gast') if student_data else 'Gast',
        'simplification_enabled': session.get('simplification_enabled', False)
    })
    
    # Lehrer über neuen Schüler informieren (mit anonymer ID)
    student_count = len(session['students'])
    if session['teacher_sid']:
        socketio.emit('student_joined', {
            'count': student_count,
            'name': name,
            'anonymous_id': student_data.get('anonymous_id', '🐾 Gast') if student_data else '🐾 Gast',
            'student_sid': request.sid
        }, room=session['teacher_sid'])

@socketio.on('teacher_end_session')
def handle_teacher_end_session(data):
    """Lehrer beendet Session via WebSocket."""
    code = data.get('code', '').upper().strip()
    
    session = get_session(code)
    if session and session['teacher_sid'] == request.sid:
        # Alle Schüler benachrichtigen
        socketio.emit('session_ended', {'message': 'Die Session wurde vom Lehrer beendet.'}, room=code)
        
        # Session löschen
        end_session(code)
        
        emit('session_ended_confirmed', {'success': True})
    else:
        emit('session_error', {'error': 'Keine Berechtigung oder Session nicht gefunden'})

@socketio.on('teacher_update_settings')
def handle_teacher_update_settings(data):
    """Lehrer sendet Barrierefreiheits-Einstellungen an alle Schüler."""
    code = data.get('code', '').upper().strip()
    settings = data.get('settings', {})
    
    session = get_session(code)
    if session and session['teacher_sid'] == request.sid:
        # Einstellungen in Session speichern
        with sessions_lock:
            if code in sessions:
                sessions[code]['settings'] = settings
        
        # An alle Schüler im Raum senden
        socketio.emit('settings_updated', {'settings': settings}, room=code)
        app.logger.info(f"Settings updated for session {code}")
    else:
        emit('session_error', {'error': 'Keine Berechtigung oder Session nicht gefunden'})

@socketio.on('teacher_release_tasks')
def handle_teacher_release_tasks(data):
    """Lehrer gibt Aufgaben an alle Schüler frei."""
    code = data.get('code', '').upper().strip()
    tasks = data.get('tasks', [])
    
    session = get_session(code)
    if session and session['teacher_sid'] == request.sid:
        # Aufgaben in Session speichern und freigeben
        with sessions_lock:
            if code in sessions:
                sessions[code]['tasks'] = tasks
                sessions[code]['tasks_available'] = True
        
        # An alle Schüler im Raum senden
        socketio.emit('tasks_released', {'tasks': tasks}, room=code)
        app.logger.info(f"Tasks released for session {code}: {len(tasks)} tasks")
    else:
        emit('session_error', {'error': 'Keine Berechtigung oder Session nicht gefunden'})

# =============================================================================
# TRANSLATION REQUESTS
# =============================================================================

LANGUAGE_NAMES = {
    'tr': 'Türkisch',
    'bg': 'Bulgarisch',
    'de': 'Deutsch',
    'ar': 'Arabisch',
    'uk': 'Ukrainisch',
    'en': 'Englisch'
}

@socketio.on('student_request_translation')
def handle_student_request_translation(data):
    """Schüler fordert Übersetzung an."""
    code = data.get('code', '').upper().strip()
    language = data.get('language', '')
    
    session = get_session(code)
    if not session:
        emit('translation_error', {'error': 'Session nicht gefunden'})
        return
    
    # Schüler-Daten holen
    student_data = session['students'].get(request.sid, {})
    anonymous_id = student_data.get('anonymous_id', '🐾 Gast')
    
    # Anfrage speichern
    with sessions_lock:
        if code in sessions:
            sessions[code]['translation_requests'][request.sid] = {
                'language': language,
                'language_name': LANGUAGE_NAMES.get(language, language),
                'status': 'pending',
                'anonymous_id': anonymous_id,
                'requested_at': datetime.now().isoformat()
            }
    
    # Bestätigung an Schüler
    emit('translation_request_sent', {
        'language': language,
        'language_name': LANGUAGE_NAMES.get(language, language)
    })
    
    # Lehrer benachrichtigen
    if session['teacher_sid']:
        socketio.emit('translation_request_received', {
            'student_sid': request.sid,
            'anonymous_id': anonymous_id,
            'language': language,
            'language_name': LANGUAGE_NAMES.get(language, language)
        }, room=session['teacher_sid'])
    
    app.logger.info(f"Translation request from {anonymous_id} for {language} in session {code}")

@socketio.on('teacher_approve_translation')
def handle_teacher_approve_translation(data):
    """Lehrer genehmigt Übersetzungsanfrage."""
    code = data.get('code', '').upper().strip()
    student_sid = data.get('student_sid', '')
    layout = data.get('layout', 'side-by-side')  # Layout setting from teacher
    
    session = get_session(code)
    if not session or session['teacher_sid'] != request.sid:
        emit('session_error', {'error': 'Keine Berechtigung'})
        return
    
    # Anfrage-Daten holen
    translation_request = session.get('translation_requests', {}).get(student_sid, {})
    if not translation_request:
        emit('session_error', {'error': 'Anfrage nicht gefunden'})
        return
    
    language = translation_request.get('language', '')
    text = session.get('text', '')
    
    if not text:
        emit('session_error', {'error': 'Kein Text zum Übersetzen'})
        return
    
    # Text übersetzen
    keys = session.get('keys', {})
    ai_key = keys.get('ai', '')
    ai_provider = keys.get('ai_provider', 'openai')
    
    if not ai_key:
        # Ohne AI-Key einfach genehmigen und Schüler informieren (ohne Übersetzung)
        with sessions_lock:
            if code in sessions:
                sessions[code]['translation_requests'][student_sid]['status'] = 'approved_no_translation'
        
        socketio.emit('translation_approved', {
            'language': language,
            'translated_text': None,
            'layout': layout,
            'message': 'Übersetzung genehmigt, aber kein AI-Key für automatische Übersetzung konfiguriert.'
        }, room=student_sid)
        return
    
    # Übersetzung durchführen
    try:
        translated_text = translate_text_with_ai(text, language, ai_key, ai_provider)
        
        # Status aktualisieren
        with sessions_lock:
            if code in sessions:
                sessions[code]['translation_requests'][student_sid]['status'] = 'approved'
                sessions[code]['translation_requests'][student_sid]['translated_text'] = translated_text
        
        # Übersetzung an Schüler senden (mit Layout)
        socketio.emit('translation_approved', {
            'language': language,
            'language_name': LANGUAGE_NAMES.get(language, language),
            'translated_text': translated_text,
            'layout': layout
        }, room=student_sid)
        
        # Lehrer bestätigen
        emit('translation_sent', {
            'student_sid': student_sid,
            'anonymous_id': translation_request.get('anonymous_id', ''),
            'success': True
        })
        
        app.logger.info(f"Translation approved for {student_sid} in session {code} with layout {layout}")
        
    except Exception as e:
        app.logger.error(f"Translation error: {e}")
        emit('session_error', {'error': f'Übersetzungsfehler: {str(e)}'})

@socketio.on('teacher_deny_translation')
def handle_teacher_deny_translation(data):
    """Lehrer lehnt Übersetzungsanfrage ab."""
    code = data.get('code', '').upper().strip()
    student_sid = data.get('student_sid', '')
    
    session = get_session(code)
    if not session or session['teacher_sid'] != request.sid:
        emit('session_error', {'error': 'Keine Berechtigung'})
        return
    
    # Status aktualisieren
    with sessions_lock:
        if code in sessions and student_sid in sessions[code]['translation_requests']:
            sessions[code]['translation_requests'][student_sid]['status'] = 'denied'
    
    # Schüler benachrichtigen
    socketio.emit('translation_denied', {
        'message': 'Deine Übersetzungsanfrage wurde abgelehnt.'
    }, room=student_sid)
    
    # Lehrer bestätigen
    translation_request = session.get('translation_requests', {}).get(student_sid, {})
    emit('translation_request_removed', {
        'student_sid': student_sid,
        'anonymous_id': translation_request.get('anonymous_id', '')
    })


# =============================================================================
# TEXT SIMPLIFICATION SOCKET EVENTS
# =============================================================================

@socketio.on('teacher_toggle_simplification')
def handle_teacher_toggle_simplification(data):
    """Lehrer aktiviert/deaktiviert Textvereinfachung."""
    code = data.get('code', '').upper().strip()
    enabled = data.get('enabled', False)
    
    session = get_session(code)
    if not session or session['teacher_sid'] != request.sid:
        emit('session_error', {'error': 'Keine Berechtigung'})
        return
    
    with sessions_lock:
        if code in sessions:
            sessions[code]['simplification_enabled'] = enabled
    
    # Alle Schüler in der Session benachrichtigen
    socketio.emit('simplification_status_changed', {
        'enabled': enabled
    }, room=code)
    
    app.logger.info(f"Simplification {'enabled' if enabled else 'disabled'} for session {code}")


@socketio.on('student_using_simplified')
def handle_student_using_simplified(data):
    """Schüler informiert über genutztes Sprachniveau."""
    code = data.get('code', '').upper().strip()
    level = data.get('level', 'original')  # 'original', 'A1', 'A2', 'B1'
    
    session = get_session(code)
    if not session:
        return
    
    # Schüler-Info in Session speichern
    with sessions_lock:
        if code in sessions:
            if 'student_levels' not in sessions[code]:
                sessions[code]['student_levels'] = {}
            
            # Finde anonymous_id für diesen Schüler
            anonymous_id = None
            for sid, info in sessions[code].get('students', {}).items():
                if sid == request.sid:
                    anonymous_id = info.get('anonymous_id', sid[:8])
                    break
            
            sessions[code]['student_levels'][request.sid] = {
                'level': level,
                'anonymous_id': anonymous_id,
                'timestamp': datetime.now().isoformat()
            }
    
    # Lehrer informieren
    teacher_sid = session.get('teacher_sid')
    if teacher_sid:
        socketio.emit('student_level_update', {
            'student_sid': request.sid,
            'anonymous_id': anonymous_id,
            'level': level
        }, room=teacher_sid)


def translate_text_with_ai(text, target_language, ai_key, ai_provider):
    """Übersetzt Text mit der konfigurierten KI."""
    language_name = LANGUAGE_NAMES.get(target_language, target_language)
    
    prompt = f"""Übersetze den folgenden deutschen Text ins {language_name}. 
Gib NUR die Übersetzung zurück, keine Erklärungen oder zusätzlichen Text.

TEXT:
{text}

ÜBERSETZUNG:"""

    if ai_provider == 'openai':
        response = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {ai_key}',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'gpt-4o-mini',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.3
            }
        )
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content'].strip()
            
    elif ai_provider == 'google':
        response = requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={ai_key}',
            headers={'Content-Type': 'application/json'},
            json={
                'contents': [{'parts': [{'text': prompt}]}],
                'generationConfig': {'temperature': 0.3}
            },
            timeout=60
        )
        if response.status_code == 200:
            return response.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        raise Exception(f'Google Error ({response.status_code}): {response.text[:500]}')
            
    elif ai_provider == 'anthropic':
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ai_key,
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': 4000,
                'messages': [{'role': 'user', 'content': prompt}]
            }
        )
        if response.status_code == 200:
            return response.json()['content'][0]['text'].strip()
    
    raise Exception(f'Übersetzung fehlgeschlagen ({ai_provider})')

# =============================================================================
# WORD INFO & VOCABULARY SYSTEM
# =============================================================================

def get_word_info_from_ai(word, target_language, ai_key, ai_provider):
    """Holt Wort-Informationen (Erklärung, Beispielsatz, Übersetzung) von der KI."""
    language_name = LANGUAGE_NAMES.get(target_language, target_language) if target_language else None
    
    translation_part = ""
    if language_name:
        translation_part = f"\n- translation: Übersetzung ins {language_name}"
    
    prompt = f"""Analysiere das deutsche Wort "{word}" und gib die Informationen als JSON zurück.

Antworte NUR mit dem JSON-Objekt, keine Erklärungen davor oder danach.

{{
    "word": "{word}",
    "article": "der/die/das (nur bei Nomen, sonst leer)",
    "plural": "Pluralform (nur bei Nomen, sonst leer)",
    "word_type": "Nomen/Verb/Adjektiv/Adverb/Präposition/etc.",
    "simple_explanation": "Einfache Erklärung in 1-2 Sätzen für Sprachlerner (A1-A2 Niveau)",
    "example_sentence": "Ein einfacher Beispielsatz mit dem Wort",
    "syllables": "Silbentrennung mit Bindestrichen (z.B. Hun-de-hüt-te)",
    "translation": "{f'Übersetzung ins {language_name}' if language_name else 'keine Übersetzung angefordert'}"
}}"""

    try:
        if ai_provider == 'openai':
            response = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {ai_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'gpt-4o-mini',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.3
                },
                timeout=15
            )
            if response.status_code == 200:
                content = response.json()['choices'][0]['message']['content'].strip()
                # JSON extrahieren (falls Text drumherum)
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    return json.loads(json_match.group())
                    
        elif ai_provider == 'google':
            response = requests.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={ai_key}',
                headers={'Content-Type': 'application/json'},
                json={
                    'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'temperature': 0.3}
                },
                timeout=15
            )
            if response.status_code == 200:
                content = response.json()['candidates'][0]['content']['parts'][0]['text'].strip()
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    return json.loads(json_match.group())
                    
        elif ai_provider == 'anthropic':
            response = requests.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'x-api-key': ai_key,
                    'anthropic-version': '2023-06-01',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'claude-sonnet-4-20250514',
                    'max_tokens': 1000,
                    'messages': [{'role': 'user', 'content': prompt}]
                },
                timeout=15
            )
            if response.status_code == 200:
                content = response.json()['content'][0]['text'].strip()
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    return json.loads(json_match.group())
    except Exception as e:
        app.logger.error(f"Word info AI error: {e}")
    
    return None


def generate_word_image_gemini(word, explanation, ai_key):
    """Generiert ein Bild für das Wort mit Gemini 2.5 Flash Image."""
    try:
        prompt = f"""Generate a simple, clear, educational illustration for the German word "{word}".
Meaning: {explanation}

Requirements:
- Simple, clean clipart or illustration style
- White or light background
- No text in the image
- Suitable for language learning
- Child-friendly if applicable"""

        # Gemini 2.5 Flash Image Model für Bildgenerierung
        response = requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent?key={ai_key}',
            headers={'Content-Type': 'application/json'},
            json={
                'contents': [{
                    'parts': [{'text': prompt}]
                }]
            },
            timeout=45
        )
        
        if response.status_code == 200:
            result = response.json()
            # Bild aus Response extrahieren
            for candidate in result.get('candidates', []):
                for part in candidate.get('content', {}).get('parts', []):
                    if 'inlineData' in part:
                        return {
                            'image_base64': part['inlineData']['data'],
                            'mime_type': part['inlineData'].get('mimeType', 'image/png')
                        }
            app.logger.warning(f"Gemini returned no image in response")
        else:
            app.logger.warning(f"Gemini image generation failed: {response.status_code} - {response.text[:300]}")
        
    except requests.exceptions.Timeout:
        app.logger.warning("Gemini image generation timed out")
    except Exception as e:
        app.logger.error(f"Gemini image generation error: {e}")
    
    return None


def search_unsplash_image(word, unsplash_key=None):
    """Sucht ein passendes Bild auf Unsplash."""
    if not unsplash_key:
        return None
    
    try:
        response = requests.get(
            'https://api.unsplash.com/search/photos',
            params={
                'query': word,
                'per_page': 1,
                'orientation': 'squarish'
            },
            headers={
                'Authorization': f'Client-ID {unsplash_key}'
            },
            timeout=10
        )
        
        if response.status_code == 200:
            results = response.json().get('results', [])
            if results:
                return {
                    'image_url': results[0]['urls']['small'],
                    'source': 'unsplash',
                    'attribution': f"Foto von {results[0]['user']['name']} auf Unsplash"
                }
    except Exception as e:
        app.logger.error(f"Unsplash search error: {e}")
    
    return None


@app.route('/api/word-info', methods=['POST'])
def get_word_info():
    """Holt umfassende Informationen zu einem Wort (Erklärung, Bild, Übersetzung)."""
    try:
        data = request.json
        word = data.get('word', '').strip()
        session_code = data.get('session_code', '').upper().strip()
        target_language = data.get('target_language', '')  # Optional: Sprache für Übersetzung
        
        if not word:
            return jsonify({'error': 'Kein Wort angegeben'}), 400
        
        # Wort bereinigen (Interpunktion entfernen)
        clean_word = re.sub(r'[^\w\säöüÄÖÜß-]', '', word).strip()
        if not clean_word:
            return jsonify({'error': 'Ungültiges Wort'}), 400
        
        # Session und Keys holen
        session = get_session(session_code) if session_code else None
        keys = session.get('keys', {}) if session else {}
        ai_key = keys.get('ai', '')
        ai_provider = keys.get('ai_provider', 'google')
        
        result = {
            'word': clean_word,
            'original_word': word
        }
        
        # 1. Wort-Info von AI holen
        if ai_key:
            word_info = get_word_info_from_ai(clean_word, target_language, ai_key, ai_provider)
            if word_info:
                result.update(word_info)
        else:
            # Fallback ohne AI
            result['simple_explanation'] = f'Keine Erklärung verfügbar (kein AI-Key konfiguriert)'
            result['word_type'] = ''
            result['article'] = ''
        
        # 2. Bild suchen/generieren
        # Option A: Unsplash (wenn Key vorhanden - hier nicht implementiert da meist kein Key)
        # Option B: Gemini Bildgenerierung
        if ai_key and ai_provider == 'google':
            explanation = result.get('simple_explanation', clean_word)
            image_data = generate_word_image_gemini(clean_word, explanation, ai_key)
            if image_data:
                result['image'] = image_data
        
        return jsonify(result)
        
    except Exception as e:
        app.logger.error(f"Word info error: {e}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# TEXT SIMPLIFICATION (Sprachniveau-Anpassung)
# =============================================================================

SIMPLIFICATION_PROMPTS = {
    'A1': """Vereinfache den folgenden deutschen Text auf Sprachniveau A1 (Anfänger).

REGELN für A1:
- NUR Präsens verwenden (keine Vergangenheit, kein Konjunktiv)
- Sehr kurze Sätze (maximal 8 Wörter)
- Nur Grundwortschatz (die 500 häufigsten Wörter)
- Keine Nebensätze
- Keine Passivkonstruktionen
- Wiederhole wichtige Wörter statt Pronomen zu verwenden
- Vermeide Metaphern und Redewendungen

ORIGINALTEXT:
{text}

VEREINFACHTER TEXT (A1):""",

    'A2': """Vereinfache den folgenden deutschen Text auf Sprachniveau A2 (Grundkenntnisse).

REGELN für A2:
- Präsens und Perfekt erlaubt
- Kurze, klare Sätze (maximal 12 Wörter)
- Alltagswortschatz
- Einfache Nebensätze mit "weil", "dass", "wenn" erlaubt
- Keine komplexen Passivkonstruktionen
- Einfache Konnektoren: und, aber, oder, dann

ORIGINALTEXT:
{text}

VEREINFACHTER TEXT (A2):""",

    'B1': """Vereinfache den folgenden deutschen Text auf Sprachniveau B1 (Mittelstufe).

REGELN für B1:
- Alle Zeitformen erlaubt, aber klar strukturiert
- Mittellange Sätze (maximal 18 Wörter)
- Erweiterter Wortschatz, aber keine Fachbegriffe ohne Erklärung
- Nebensätze erlaubt
- Klare Textstruktur
- Schwierige Wörter durch einfachere Synonyme ersetzen

ORIGINALTEXT:
{text}

VEREINFACHTER TEXT (B1):"""
}


def simplify_text_with_ai(text, level, ai_key, ai_provider):
    """Vereinfacht Text auf das angegebene Sprachniveau."""
    
    if level not in SIMPLIFICATION_PROMPTS:
        raise ValueError(f"Unbekanntes Niveau: {level}")
    
    prompt = SIMPLIFICATION_PROMPTS[level].format(text=text)
    
    try:
        if ai_provider == 'openai':
            response = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {ai_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'gpt-4o-mini',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.3
                },
                timeout=30
            )
            if response.status_code == 200:
                return response.json()['choices'][0]['message']['content'].strip()
                
        elif ai_provider == 'google':
            response = requests.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={ai_key}',
                headers={'Content-Type': 'application/json'},
                json={
                    'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'temperature': 0.3}
                },
                timeout=30
            )
            if response.status_code == 200:
                return response.json()['candidates'][0]['content']['parts'][0]['text'].strip()
                
        elif ai_provider == 'anthropic':
            response = requests.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'x-api-key': ai_key,
                    'anthropic-version': '2023-06-01',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'claude-sonnet-4-20250514',
                    'max_tokens': 4000,
                    'messages': [{'role': 'user', 'content': prompt}]
                },
                timeout=30
            )
            if response.status_code == 200:
                return response.json()['content'][0]['text'].strip()
                
    except Exception as e:
        app.logger.error(f"Simplification error: {e}")
        raise
    
    raise Exception(f'Textvereinfachung fehlgeschlagen ({ai_provider})')


@app.route('/api/simplify-text', methods=['POST'])
def simplify_text():
    """Vereinfacht einen Text auf das gewünschte Sprachniveau."""
    try:
        data = request.json
        text = data.get('text', '').strip()
        level = data.get('level', 'A2').upper()
        session_code = data.get('session_code', '').upper().strip()
        
        if not text:
            return jsonify({'error': 'Kein Text angegeben'}), 400
        
        if level not in ['A1', 'A2', 'B1']:
            return jsonify({'error': 'Ungültiges Niveau. Erlaubt: A1, A2, B1'}), 400
        
        # Session und Keys holen
        session = get_session(session_code) if session_code else None
        if not session:
            return jsonify({'error': 'Session nicht gefunden'}), 404
        
        # Prüfen ob Feature erlaubt ist
        if not session.get('simplification_enabled', False):
            return jsonify({'error': 'Textvereinfachung ist nicht aktiviert'}), 403
        
        keys = session.get('keys', {})
        ai_key = keys.get('ai', '')
        ai_provider = keys.get('ai_provider', 'google')
        
        if not ai_key:
            return jsonify({'error': 'Kein AI-Key konfiguriert'}), 400
        
        # Text vereinfachen
        simplified = simplify_text_with_ai(text, level, ai_key, ai_provider)
        
        return jsonify({
            'original_text': text,
            'simplified_text': simplified,
            'level': level
        })
        
    except Exception as e:
        app.logger.error(f"Simplify text error: {e}")
        return jsonify({'error': str(e)}), 500


# =============================================================================
# FILE UPLOAD & TEXT EXTRACTION
# =============================================================================

@app.route('/api/extract-text', methods=['POST'])
def extract_text_from_file():
    """Extrahiert Text aus hochgeladenen Dateien."""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'Keine Datei hochgeladen'}), 400
        
        file = request.files['file']
        filename = file.filename.lower()
        
        if filename.endswith('.docx'):
            if not DOCX_AVAILABLE:
                return jsonify({'error': 'DOCX-Verarbeitung nicht verfügbar'}), 500
            doc = Document(BytesIO(file.read()))
            paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
            text = '\n\n'.join(paragraphs)
            
        elif filename.endswith('.pdf'):
            if not PDF_AVAILABLE:
                return jsonify({'error': 'PDF-Verarbeitung nicht verfügbar'}), 500
            text_parts = []
            with pdfplumber.open(BytesIO(file.read())) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            text = '\n\n'.join(text_parts)
            
        elif filename.endswith('.txt'):
            text = file.read().decode('utf-8')
        else:
            return jsonify({'error': 'Nicht unterstütztes Format. Erlaubt: .docx, .pdf, .txt'}), 400
        
        text = cleanup_extracted_text(text)
        
        if not text.strip():
            return jsonify({'error': 'Kein Text in der Datei gefunden'}), 400
        
        return jsonify({'text': text.strip()})
        
    except Exception as e:
        return jsonify({'error': f'Fehler: {str(e)}'}), 500

# =============================================================================
# TASKS GENERATION
# =============================================================================

@app.route('/api/generate-tasks', methods=['POST'])
def api_generate_tasks():
    """
    Generiert Aufgaben für einen Text via KI.
    
    Erwartet JSON:
    {
        "text": "Der zu lesende Text...",
        "session_code": "ABC123"
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        text = data.get('text', '').strip()
        session_code = data.get('session_code', '').upper().strip()
        difficulty = data.get('difficulty', 'mittel')
        
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400

        if len(text) > MAX_AI_TEXT_CHARS:
            return jsonify({'error': f'Text ist zu lang (max. {MAX_AI_TEXT_CHARS} Zeichen)'}), 400
        
        if session_code:
            auth_error = require_teacher_access(session_code, data)
            if auth_error:
                return auth_error

            keys = get_session_keys(session_code)
            ai_key = keys.get('ai', '') if keys else ''
            ai_provider = keys.get('ai_provider', 'openai') if keys else 'openai'
        else:
            ai_key = data.get('api_key', '')
            ai_provider = data.get('provider', data.get('ai_provider', 'openai'))
        
        if not ai_key:
            return jsonify({'error': 'KI API Key nicht konfiguriert'}), 400

        difficulty_notes = {
            'einfach': 'Klasse 5-6: kurze, sehr klare Fragen und einfache Antwortoptionen.',
            'mittel': 'Klasse 7-8: klare Fragen mit moderater Textnaehe.',
            'schwer': 'Klasse 9-10: auch Schlussfolgerungen und Begruendungen einbauen.',
            'oberstufe': 'Oberstufe: anspruchsvollere Analyse- und Deutungsfragen einbauen.'
        }
        difficulty_note = difficulty_notes.get(difficulty, difficulty_notes['mittel'])
        text = f"SCHWIERIGKEIT:\n{difficulty_note}\n\n{text}"
        
        # Prompt für Aufgabengenerierung
        prompt = f"""Erstelle 5 Verständnisaufgaben zu folgendem Text. Die Aufgaben sollen für Schüler mit Leseschwierigkeiten geeignet sein.

TEXT:
{text}

Erstelle genau 5 Aufgaben in diesem JSON-Format (KEINE anderen Texte, NUR das JSON-Array):
[
  {{"type": "multiple_choice", "question": "Frage zum Text?", "options": ["Antwort A", "Antwort B", "Antwort C", "Antwort D"], "correct": 0}},
  {{"type": "true_false", "question": "Eine Aussage zum Text, die richtig oder falsch ist.", "correct": true}},
  {{"type": "fill_blank", "question": "Ein Satz aus dem Text mit einer ___ Lücke.", "correct": "fehlendes Wort"}},
  {{"type": "short_answer", "question": "Eine offene Frage zum Text?", "hint": "Ein kleiner Hinweis"}},
  {{"type": "multiple_choice", "question": "Noch eine Frage?", "options": ["A", "B", "C", "D"], "correct": 2}}
]

WICHTIGE REGELN:
- Bei multiple_choice: "correct" ist der INDEX (0-3) der richtigen Antwort
- Bei true_false: "correct" ist true oder false
- Bei fill_blank: "correct" ist das fehlende Wort
- KEINE order/Sortier-Aufgaben erstellen - diese funktionieren nicht gut
- Verwende verschiedene Aufgabentypen (mindestens 2 multiple_choice, 1 true_false, 1 fill_blank)
- Alle Fragen müssen sich auf den gegebenen Text beziehen

Antworte NUR mit dem JSON-Array, keine anderen Texte."""

        tasks = []
        
        if ai_provider == 'openai':
            import requests as req
            response = req.post(
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {ai_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'gpt-4o-mini',
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': 0.7
                },
                timeout=60
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                # JSON extrahieren
                import json
                import re
                json_match = re.search(r'\[[\s\S]*\]', content)
                if json_match:
                    tasks = json.loads(json_match.group())
                    
        elif ai_provider == 'google':
            import requests as req
            response = req.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={ai_key}',
                headers={'Content-Type': 'application/json'},
                json={
                    'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'temperature': 0.7}
                },
                timeout=60
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['candidates'][0]['content']['parts'][0]['text']
                import json
                import re
                json_match = re.search(r'\[[\s\S]*\]', content)
                if json_match:
                    tasks = json.loads(json_match.group())
                    
        elif ai_provider == 'anthropic':
            import requests as req
            response = req.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'x-api-key': ai_key,
                    'anthropic-version': '2023-06-01',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'claude-sonnet-4-20250514',
                    'max_tokens': 2000,
                    'messages': [{'role': 'user', 'content': prompt}]
                },
                timeout=60
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['content'][0]['text']
                import json
                import re
                json_match = re.search(r'\[[\s\S]*\]', content)
                if json_match:
                    tasks = json.loads(json_match.group())
        
        if not tasks:
            return jsonify({'error': 'Aufgabengenerierung fehlgeschlagen'}), 500
        
        return jsonify({'tasks': tasks})
        
    except Exception as e:
        app.logger.error(f"Task generation error: {e}")
        return jsonify({'error': str(e)}), 500

# =============================================================================
# TTS PROXY (Session-basiert)
# =============================================================================

@app.route('/api/tts', methods=['POST'])
def proxy_tts():
    """
    TTS via Session-Code (Schüler) oder direkt mit Key (Lehrer).
    
    Erwartet JSON:
    {
        "session_code": "ABC123",  // ODER
        "api_key": "sk_...",       // Für Lehrer-Direktzugriff
        "text": "...",
        "voice_id": "..."  // optional
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        text = data.get('text', '').strip()
        
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400

        if len(text) > MAX_TTS_CHARS:
            return jsonify({'error': f'Text ist zu lang für TTS (max. {MAX_TTS_CHARS} Zeichen)'}), 400
        
        # Keys ermitteln (Session oder direkt)
        session_code = data.get('session_code', '').upper().strip()
        
        if session_code:
            keys = get_session_keys(session_code)
            if not keys:
                return jsonify({'error': 'Session nicht gefunden oder abgelaufen'}), 404
            api_key = keys['elevenlabs']
            voice_id = data.get('voice_id') or keys.get('voice_id', '21m00Tcm4TlvDq8ikWAM')
        else:
            api_key = data.get('api_key')
            voice_id = data.get('voice_id', '21m00Tcm4TlvDq8ikWAM')
            if not api_key:
                return jsonify({'error': 'API Key oder Session-Code erforderlich'}), 400
        
        # Language code für multilinguale Stimme (Standard: Deutsch)
        # ElevenLabs multilingual_v2 unterstützt: en, de, pl, es, it, fr, pt, hi, ar, zh, ja, ko, nl, ru, sv, tr
        # Mapping für nicht direkt unterstützte Sprachen
        language_code = data.get('language_code', 'de')
        LANGUAGE_FALLBACKS = {
            'uk': 'ru',  # Ukrainisch → Russisch (ähnlich)
            'bg': 'ru',  # Bulgarisch → Russisch (kyrillisch)
        }
        original_language = language_code
        language_code = LANGUAGE_FALLBACKS.get(language_code, language_code)
        
        if original_language != language_code:
            app.logger.info(f"Language fallback: {original_language} → {language_code}")
        
        # Cache prüfen (inkl. Sprache)
        cache_key = get_cache_key(text + language_code, voice_id)
        cached = get_from_cache(cache_key)
        if cached:
            app.logger.info(f"TTS Cache HIT")
            return jsonify(cached)
        
        # ElevenLabs API aufrufen
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
        
        headers = {
            'xi-api-key': api_key,
            'Content-Type': 'application/json'
        }
        
        payload = {
            'text': text,
            'model_id': data.get('model_id', 'eleven_multilingual_v2'),
            'language_code': language_code,
            'voice_settings': {
                'stability': 0.5,
                'similarity_boost': 0.75
            }
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        
        if response.status_code != 200:
            try:
                error_data = response.json()
                error_detail = error_data.get('detail', {})
                if isinstance(error_detail, dict):
                    error_msg = error_detail.get('message', str(error_data))
                else:
                    error_msg = str(error_detail) or str(error_data)
            except:
                error_msg = f'ElevenLabs API Fehler (Status {response.status_code})'
            app.logger.error(f"ElevenLabs TTS error: {response.status_code} - {error_msg}")
            return jsonify({'error': error_msg}), response.status_code
        
        response_data = response.json()
        add_to_cache(cache_key, response_data)
        
        return jsonify(response_data)
        
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# OCR PROXY (Session-basiert)
# =============================================================================

@app.route('/api/ocr', methods=['POST'])
def ocr_image():
    """OCR via KI-API - Session-basiert oder mit direktem Key."""
    try:
        data = request.json
        image_base64 = data.get('image')
        mime_type = data.get('mime_type', 'image/jpeg')
        
        if not image_base64:
            return jsonify({'error': 'Kein Bild übermittelt'}), 400
        
        # Keys ermitteln
        session_code = data.get('session_code', '').upper().strip()
        
        if session_code:
            auth_error = require_teacher_access(session_code, data)
            if auth_error:
                return auth_error

            keys = get_session_keys(session_code)
            if not keys:
                return jsonify({'error': 'Session nicht gefunden'}), 404
            api_key = keys['ai']
            provider = keys['ai_provider']
        else:
            api_key = data.get('api_key')
            provider = data.get('provider', 'openai')
        
        if not api_key:
            return jsonify({'error': 'KI API Key erforderlich'}), 400
        
        ocr_prompt = """Extrahiere den gesamten Text aus diesem Bild. 
Gib NUR den erkannten Text zurück, ohne Erklärungen.
Behalte Absätze bei. Wenn kein Text erkennbar ist: [KEIN TEXT ERKANNT]"""
        
        if provider == 'openai':
            text = call_openai_vision(api_key, ocr_prompt, image_base64, mime_type)
        elif provider == 'anthropic':
            text = call_anthropic_vision(api_key, ocr_prompt, image_base64, mime_type)
        elif provider == 'google':
            text = call_google_vision(api_key, ocr_prompt, image_base64, mime_type)
        else:
            return jsonify({'error': f'Unbekannter Provider: {provider}'}), 400
        
        if '[KEIN TEXT ERKANNT]' in text:
            return jsonify({'error': 'Kein Text im Bild erkannt'}), 400
        
        return jsonify({'text': text.strip()})
        
    except Exception as e:
        return jsonify({'error': f'OCR-Fehler: {str(e)}'}), 500

# =============================================================================
# TRANSLATION PROXY (Session-basiert)
# =============================================================================

@app.route('/api/translate', methods=['POST'])
def proxy_translate():
    """Übersetzung via KI-API - Session-basiert oder mit direktem Key."""
    try:
        data = request.get_json(silent=True) or {}
        text = data.get('text', '').strip()
        target_language = data.get('target_language', 'de')
        
        if not text:
            return jsonify({'error': 'Text fehlt'}), 400

        if len(text) > MAX_AI_TEXT_CHARS:
            return jsonify({'error': f'Text ist zu lang (max. {MAX_AI_TEXT_CHARS} Zeichen)'}), 400
        
        if target_language == 'de':
            return jsonify({'translated_text': text})
        
        # Keys ermitteln
        session_code = data.get('session_code', '').upper().strip()
        
        if session_code:
            auth_error = require_teacher_access(session_code, data)
            if auth_error:
                return auth_error

            keys = get_session_keys(session_code)
            if not keys:
                return jsonify({'error': 'Session nicht gefunden'}), 404
            api_key = keys['ai']
            provider = keys['ai_provider']
        else:
            api_key = data.get('api_key')
            provider = data.get('provider', 'openai')
        
        if not api_key:
            return jsonify({'error': 'KI API Key erforderlich'}), 400
        
        # Cache prüfen
        cache_key = get_translation_cache_key(text, target_language)
        cached = get_from_translation_cache(cache_key)
        if cached:
            return jsonify({'translated_text': cached, 'cached': True})
        
        language_names = {
            'de': 'Deutsch', 'tr': 'Türkisch', 'bg': 'Bulgarisch',
            'ar': 'Arabisch', 'uk': 'Ukrainisch', 'en': 'Englisch'
        }
        target_name = language_names.get(target_language, 'Deutsch')
        
        system_prompt = f"""Du bist ein professioneller Übersetzer. Übersetze ins {target_name}.
Regeln: NUR die Übersetzung ausgeben, keine Erklärungen. Formatierung beibehalten."""
        
        if provider == 'openai':
            result = call_openai_text(api_key, system_prompt, text)
        elif provider == 'anthropic':
            result = call_anthropic_text(api_key, system_prompt, text)
        elif provider == 'google':
            result = call_google_text(api_key, system_prompt, text)
        else:
            return jsonify({'error': f'Unbekannter Provider'}), 400
        
        add_to_translation_cache(cache_key, result)
        return jsonify({'translated_text': result})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# =============================================================================
# SPEECH-TO-TEXT (Session-basiert)
# =============================================================================

@app.route('/api/speech-to-text', methods=['POST'])
def proxy_speech_to_text():
    """Spracherkennung - Session-basiert oder mit direktem Key."""
    try:
        if 'audio' not in request.files:
            return jsonify({'error': 'Keine Audio-Datei'}), 400
        
        session_code = request.form.get('session_code', '').upper().strip()
        
        if session_code:
            keys = get_session_keys(session_code)
            if not keys:
                return jsonify({'error': 'Session nicht gefunden'}), 404
            api_key = keys['ai']
            provider = keys['ai_provider']
        else:
            api_key = request.form.get('api_key')
            provider = request.form.get('provider', 'openai')
        
        if not api_key:
            return jsonify({'error': 'API Key erforderlich'}), 400
        
        audio_file = request.files['audio']
        language = request.form.get('language', 'de')
        audio_data = audio_file.read()
        
        if provider == 'google':
            return transcribe_with_gemini(api_key, audio_data, language)
        else:
            return transcribe_with_whisper(api_key, audio_data, language)
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/speech-to-text-scribe', methods=['POST'])
def proxy_speech_to_text_scribe():
    """
    Spracherkennung mit ElevenLabs Scribe v2 - höhere Genauigkeit.
    Unterstützt Keyterm-Prompting für bessere Erkennung von Textwörtern.
    """
    try:
        if 'audio' not in request.files:
            app.logger.error("Scribe: Keine Audio-Datei im Request")
            return jsonify({'error': 'Keine Audio-Datei'}), 400
        
        session_code = request.form.get('session_code', '').upper().strip()
        
        if session_code:
            keys = get_session_keys(session_code)
            if not keys:
                app.logger.error(f"Scribe: Session {session_code} nicht gefunden")
                return jsonify({'error': 'Session nicht gefunden'}), 404
            api_key = keys.get('elevenlabs')
        else:
            api_key = request.form.get('api_key')
        
        if not api_key:
            app.logger.error("Scribe: Kein ElevenLabs API Key")
            return jsonify({'error': 'ElevenLabs API Key erforderlich'}), 400
        
        audio_file = request.files['audio']
        language = request.form.get('language', 'de')
        
        audio_data = audio_file.read()
        app.logger.info(f"Scribe: Audio empfangen, {len(audio_data)} bytes, Sprache: {language}")
        
        if len(audio_data) < 100:
            app.logger.error(f"Scribe: Audio zu kurz ({len(audio_data)} bytes)")
            return jsonify({'error': 'Audio-Aufnahme zu kurz'}), 400
        
        # Prepare multipart form data for ElevenLabs
        files = {
            'file': ('audio.webm', audio_data, 'audio/webm')
        }
        
        data = {
            'model_id': 'scribe_v1',
            'tag_audio_events': 'false',
            'timestamps_granularity': 'word'
        }
        
        # Nur language_code hinzufügen wenn nicht auto-detect gewünscht
        if language and language != 'auto':
            data['language_code'] = language
        
        headers = {
            'xi-api-key': api_key
        }
        
        app.logger.info(f"Scribe: Sende Request an ElevenLabs...")
        
        response = requests.post(
            'https://api.elevenlabs.io/v1/speech-to-text',
            headers=headers,
            files=files,
            data=data,
            timeout=30
        )
        
        app.logger.info(f"Scribe: ElevenLabs Response Status: {response.status_code}")
        
        if response.status_code != 200:
            error_detail = response.text
            app.logger.error(f"Scribe: ElevenLabs Fehler: {response.status_code} - {error_detail}")
            try:
                error_json = response.json()
                error_detail = error_json.get('detail', {}).get('message', error_json.get('detail', response.text))
            except:
                pass
            return jsonify({'error': f'ElevenLabs Scribe Fehler: {error_detail}'}), response.status_code
        
        result = response.json()
        app.logger.info(f"Scribe: Transkription erfolgreich: '{result.get('text', '')[:50]}...'")
        
        # Formatiere Antwort ähnlich wie Whisper
        return jsonify({
            'text': result.get('text', ''),
            'language': result.get('language_code', language),
            'words': result.get('words', []),  # Word-level timestamps
            'provider': 'elevenlabs_scribe'
        })
            
    except Exception as e:
        app.logger.error(f"Scribe STT error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# =============================================================================
# AI API HELPER FUNCTIONS
# =============================================================================

def call_openai_text(api_key, system_prompt, user_message):
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': 'gpt-4o',
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_message}
        ],
        'max_tokens': 4000
    }
    response = requests.post('https://api.openai.com/v1/chat/completions', 
                           headers=headers, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"OpenAI Error: {response.text}")
    return response.json()['choices'][0]['message']['content']

def call_anthropic_text(api_key, system_prompt, user_message):
    headers = {
        'x-api-key': api_key,
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01'
    }
    payload = {
        'model': 'claude-sonnet-4-20250514',
        'max_tokens': 4000,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': user_message}]
    }
    response = requests.post('https://api.anthropic.com/v1/messages',
                           headers=headers, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Anthropic Error: {response.text}")
    return response.json()['content'][0]['text']

def call_google_text(api_key, system_prompt, user_message):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={api_key}"
    payload = {
        'contents': [{'parts': [{'text': f"{system_prompt}\n\n{user_message}"}]}]
    }
    response = requests.post(url, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Google Error: {response.text}")
    return response.json()['candidates'][0]['content']['parts'][0]['text']

def call_openai_vision(api_key, prompt, image_base64, mime_type):
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': 'gpt-4o',
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': f'data:{mime_type};base64,{image_base64}'}}
            ]
        }],
        'max_tokens': 4000
    }
    response = requests.post('https://api.openai.com/v1/chat/completions',
                           headers=headers, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"OpenAI Vision Error: {response.text}")
    return response.json()['choices'][0]['message']['content']

def call_anthropic_vision(api_key, prompt, image_base64, mime_type):
    headers = {
        'x-api-key': api_key,
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01'
    }
    payload = {
        'model': 'claude-sonnet-4-20250514',
        'max_tokens': 4000,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': mime_type, 'data': image_base64}},
                {'type': 'text', 'text': prompt}
            ]
        }]
    }
    response = requests.post('https://api.anthropic.com/v1/messages',
                           headers=headers, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Anthropic Vision Error: {response.text}")
    return response.json()['content'][0]['text']

def call_google_vision(api_key, prompt, image_base64, mime_type):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={api_key}"
    payload = {
        'contents': [{
            'parts': [
                {'text': prompt},
                {'inline_data': {'mime_type': mime_type, 'data': image_base64}}
            ]
        }]
    }
    response = requests.post(url, json=payload, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Google Vision Error: {response.text}")
    return response.json()['candidates'][0]['content']['parts'][0]['text']

def transcribe_with_whisper(api_key, audio_data, language):
    headers = {'Authorization': f'Bearer {api_key}'}
    files = {'file': ('audio.webm', audio_data, 'audio/webm')}
    data = {'model': 'whisper-1', 'language': language}
    response = requests.post('https://api.openai.com/v1/audio/transcriptions',
                           headers=headers, files=files, data=data, timeout=60)
    if response.status_code != 200:
        return jsonify({'error': f'Whisper Error: {response.text}'}), response.status_code
    return jsonify({'text': response.json()['text']})

def transcribe_with_gemini(api_key, audio_data, language):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={api_key}"
    audio_base64 = base64.b64encode(audio_data).decode('utf-8')
    payload = {
        'contents': [{
            'parts': [
                {'text': f'Transkribiere diese Audio-Aufnahme auf {language}. Gib NUR den transkribierten Text zurück.'},
                {'inline_data': {'mime_type': 'audio/webm', 'data': audio_base64}}
            ]
        }]
    }
    response = requests.post(url, json=payload, timeout=60)
    if response.status_code != 200:
        return jsonify({'error': f'Gemini Error: {response.text}'}), response.status_code
    text = response.json()['candidates'][0]['content']['parts'][0]['text']
    return jsonify({'text': text})

# =============================================================================
# CACHE STATS
# =============================================================================

@app.route('/api/cache-stats')
def cache_stats():
    """Cache-Statistiken (für Monitoring)."""
    with sessions_lock:
        session_count = len(sessions)

    stats = get_cache_stats()
    stats['active_sessions'] = session_count
    return jsonify(stats)

# =============================================================================
# RUN
# =============================================================================

# Start cleanup thread only once
_cleanup_started = False

def start_cleanup_if_needed():
    global _cleanup_started
    if not _cleanup_started:
        _cleanup_started = True
        cleanup_thread.start()

# For gunicorn with gevent
start_cleanup_if_needed()

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
