"""
Pronunciation Assessment Middleware API
Bridges PaygoGPT + ChatbotBuilder → Azure Speech Services
Supports: audio mode (voice widget) + text mode (social media)
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests
import base64
import json
import subprocess
import tempfile
import struct

app = Flask(__name__)
CORS(app)

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_azure_key():
    return os.environ.get('AZURE_SPEECH_KEY', '')

def get_azure_region():
    return os.environ.get('AZURE_SPEECH_REGION', 'canadaeast')

def build_silent_wav(duration_ms=200, sample_rate=16000):
    """Build a valid PCM WAV with silence — used for text-only assessment."""
    num_samples = int(sample_rate * duration_ms / 1000)
    pcm_data    = b'\x00\x00' * num_samples          # 16-bit silence
    data_size   = len(pcm_data)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size,
        b'WAVE', b'fmt ', 16,
        1, 1,                                         # PCM, mono
        sample_rate, sample_rate * 2,                 # byte rate
        2, 16,                                        # block align, bits
        b'data', data_size
    )
    return header + pcm_data

def download_audio(audio_url):
    """Download audio and convert to 16kHz mono WAV using ffmpeg."""
    try:
        response = requests.get(audio_url, timeout=30)
        response.raise_for_status()
        audio_data = response.content

        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as mp3_file:
            mp3_file.write(audio_data)
            mp3_path = mp3_file.name

        wav_path = mp3_path.replace('.mp3', '.wav')

        try:
            subprocess.run([
                'ffmpeg', '-y', '-i', mp3_path,
                '-ar', '16000', '-ac', '1', '-sample_fmt', 's16',
                wav_path
            ], capture_output=True, check=True)

            with open(wav_path, 'rb') as f:
                wav_data = f.read()

            os.unlink(mp3_path)
            os.unlink(wav_path)
            return wav_data

        except subprocess.CalledProcessError:
            os.unlink(mp3_path)
            return audio_data

    except Exception:
        return None

def call_azure(audio_bytes, reference_text, language):
    """POST audio (real or silent) to Azure Pronunciation Assessment REST API."""
    azure_key    = get_azure_key()
    azure_region = get_azure_region()

    pron_config = {
        "ReferenceText": reference_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
        "EnableMiscue": True,
        "EnableProsodyAssessment": "true"
    }
    pron_config_b64 = base64.b64encode(json.dumps(pron_config).encode()).decode()

    url = (
        f"https://{azure_region}.stt.speech.microsoft.com"
        f"/speech/recognition/conversation/cognitiveservices/v1"
        f"?language={language}&format=detailed&usePipelineVersion=0"
    )
    headers = {
        "Ocp-Apim-Subscription-Key": azure_key,
        "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
        "Pronunciation-Assessment": pron_config_b64,
        "Accept": "application/json"
    }

    try:
        resp = requests.post(url, headers=headers, data=audio_bytes, timeout=30)
        if resp.status_code == 200:
            return {"success": True, "data": resp.json()}
        return {"success": False, "error": f"Azure error {resp.status_code}", "details": resp.text}
    except Exception as e:
        return {"success": False, "error": str(e)}

def format_response(azure_result, mode="audio"):
    """Convert raw Azure JSON into a clean PaygoGPT-friendly response."""
    if not azure_result.get('success'):
        return {
            "success": False,
            "mode": mode,
            "error": azure_result.get('error', 'Unknown error'),
            "details": azure_result.get('details', ''),
            "feedback": "Sorry, I couldn't assess the pronunciation. Please try again."
        }

    data  = azure_result.get('data', {})
    nbest = data.get('NBest', [{}])[0] if data.get('NBest') else {}
    pa    = nbest.get('PronunciationAssessment', nbest)   # both formats

    pron_score   = round(pa.get('PronScore',        nbest.get('PronScore',        0)), 1)
    accuracy     = round(pa.get('AccuracyScore',    nbest.get('AccuracyScore',    0)), 1)
    fluency      = round(pa.get('FluencyScore',     nbest.get('FluencyScore',     0)), 1)
    completeness = round(pa.get('CompletenessScore',nbest.get('CompletenessScore',0)), 1)
    prosody      = round(pa.get('ProsodyScore',     nbest.get('ProsodyScore',     0)), 1)

    # Human-friendly feedback (for PaygoGPT agent to use)
    if pron_score >= 90:
        feedback = f"🌟 Excellent! Pronunciation score: {pron_score}/100."
    elif pron_score >= 75:
        feedback = f"👍 Good job! Pronunciation score: {pron_score}/100."
    elif pron_score >= 60:
        feedback = f"📚 Not bad! Pronunciation score: {pron_score}/100."
    else:
        feedback = f"💪 Keep practicing! Pronunciation score: {pron_score}/100."

    # Word-level breakdown
    words_feedback = []
    problem_words  = []
    for word in nbest.get('Words', []):
        w_pa    = word.get('PronunciationAssessment', word)
        w_score = round(w_pa.get('AccuracyScore', word.get('AccuracyScore', 0)), 1)
        w_error = w_pa.get('ErrorType', word.get('ErrorType', 'None'))
        words_feedback.append({
            "word":     word.get('Word', ''),
            "accuracy": w_score,
            "error":    w_error
        })
        if w_score < 70 or w_error not in ('None', ''):
            problem_words.append(word.get('Word', ''))

    if problem_words:
        feedback += f" Focus on: {', '.join(problem_words[:3])}."

    return {
        "success":            True,
        "mode":               mode,
        "pronunciation_score": pron_score,
        "accuracy_score":     accuracy,
        "fluency_score":      fluency,
        "completeness_score": completeness,
        "prosody_score":      prosody,
        "feedback":           feedback,
        "words":              words_feedback,
        "recognized_text":    nbest.get('Display', nbest.get('Lexical', ''))
    }

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def home():
    key = get_azure_key()
    return jsonify({
        "status":           "running",
        "service":          "LTA Pronunciation Assessment API",
        "azure_configured": len(key) > 0,
        "region":           get_azure_region(),
        "endpoints": {
            "audio_mode": "POST /assess        — audio_url + reference_text + language",
            "text_mode":  "POST /assess-text   — reference_text + language (no audio)",
            "unified":    "POST /assess        — omit audio_url to auto-switch to text mode"
        }
    })


@app.route('/assess', methods=['POST'])
def assess():
    """
    Unified endpoint — works for both audio and text mode.
    PaygoGPT sends: { reference_text, locale/language, audio_url (optional), audio_base64 (optional) }
    If no audio is provided, falls back to text-mode assessment automatically.
    """
    azure_key = get_azure_key()
    if not azure_key:
        return jsonify({"success": False, "error": "Azure key not configured"}), 500

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON data"}), 400

    reference_text = data.get('reference_text', data.get('text', '')).strip()
    # Accept both "locale" (PaygoGPT) and "language" (CBB) keys
    language       = data.get('locale', data.get('language', 'fr-CA'))
    audio_url      = data.get('audio_url', '')
    audio_base64   = data.get('audio_base64', '')
    mode           = data.get('mode', 'audio' if (audio_url or audio_base64) else 'text')

    if not reference_text:
        return jsonify({"success": False, "error": "reference_text is required"}), 400

    # ── TEXT MODE (social media — no real audio) ──────────────────────────────
    if mode == 'text' or (not audio_url and not audio_base64):
        silent_wav   = build_silent_wav(duration_ms=200)
        azure_result = call_azure(silent_wav, reference_text, language)
        return jsonify(format_response(azure_result, mode="text"))

    # ── AUDIO MODE (voice widget — real speech) ───────────────────────────────
    if audio_base64:
        try:
            audio_bytes = base64.b64decode(audio_base64)
        except Exception:
            return jsonify({"success": False, "error": "Invalid audio_base64"}), 400
    else:
        audio_bytes = download_audio(audio_url)
        if audio_bytes is None:
            return jsonify({"success": False, "error": "Failed to download audio from audio_url"}), 400

    azure_result = call_azure(audio_bytes, reference_text, language)
    return jsonify(format_response(azure_result, mode="audio"))


@app.route('/assess-text', methods=['POST'])
def assess_text():
    """
    Dedicated text-only endpoint (no audio needed).
    Ideal for social media channels: Instagram, Facebook, WhatsApp.
    Body: { "reference_text": "...", "locale": "fr-CA" }
    """
    azure_key = get_azure_key()
    if not azure_key:
        return jsonify({"success": False, "error": "Azure key not configured"}), 500

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON data"}), 400

    reference_text = data.get('reference_text', data.get('text', '')).strip()
    language       = data.get('locale', data.get('language', 'fr-CA'))

    if not reference_text:
        return jsonify({"success": False, "error": "reference_text is required"}), 400

    silent_wav   = build_silent_wav(duration_ms=200)
    azure_result = call_azure(silent_wav, reference_text, language)
    return jsonify(format_response(azure_result, mode="text"))


@app.route('/languages', methods=['GET'])
def languages():
    return jsonify([
        {"code": "fr-CA", "name": "French (Canada) — Primary"},
        {"code": "en-CA", "name": "English (Canada) — Primary"},
        {"code": "en-US", "name": "English (US)"},
        {"code": "fr-FR", "name": "French (France)"},
        {"code": "es-ES", "name": "Spanish (Spain)"},
        {"code": "es-MX", "name": "Spanish (Mexico)"},
        {"code": "es-AR", "name": "Spanish (Argentina)"},
        {"code": "es-CO", "name": "Spanish (Colombia)"},
        {"code": "es-CL", "name": "Spanish (Chile)"},
        {"code": "es-PE", "name": "Spanish (Peru)"},
        {"code": "es-VE", "name": "Spanish (Venezuela)"},
        {"code": "es-GT", "name": "Spanish (Guatemala)"},
        {"code": "es-CU", "name": "Spanish (Cuba)"},
        {"code": "es-BO", "name": "Spanish (Bolivia)"},
        {"code": "es-DO", "name": "Spanish (Dominican Republic)"},
        {"code": "es-HN", "name": "Spanish (Honduras)"},
        {"code": "es-PY", "name": "Spanish (Paraguay)"},
        {"code": "es-SV", "name": "Spanish (El Salvador)"},
        {"code": "es-NI", "name": "Spanish (Nicaragua)"},
        {"code": "es-CR", "name": "Spanish (Costa Rica)"},
        {"code": "es-PA", "name": "Spanish (Panama)"},
        {"code": "es-UY", "name": "Spanish (Uruguay)"},
        {"code": "es-PR", "name": "Spanish (Puerto Rico)"},
        {"code": "es-US", "name": "Spanish (US)"},
    ])


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
