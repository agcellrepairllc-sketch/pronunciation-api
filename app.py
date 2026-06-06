"""
Pronunciation Assessment Middleware API
Bridges PaygoGPT + ChatbotBuilder -> Azure Speech Services
Supports: audio_url (CBB), audio_base64 (browser), text mode (social)
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import os, requests, base64, json, subprocess, tempfile, struct

app = Flask(__name__)
CORS(app)

def get_azure_key():    return os.environ.get('AZURE_SPEECH_KEY', '')
def get_azure_region(): return os.environ.get('AZURE_SPEECH_REGION', 'canadaeast')

# ── Audio helpers ─────────────────────────────────────────────────────────────

def convert_to_wav(input_bytes, input_suffix='.webm'):
    """Convert any audio format to 16kHz mono PCM WAV using ffmpeg."""
    try:
        with tempfile.NamedTemporaryFile(suffix=input_suffix, delete=False) as f:
            f.write(input_bytes)
            in_path = f.name
        out_path = in_path.replace(input_suffix, '.wav')
        result = subprocess.run([
            'ffmpeg', '-y', '-i', in_path,
            '-ar', '16000', '-ac', '1', '-sample_fmt', 's16',
            out_path
        ], capture_output=True)
        os.unlink(in_path)
        if result.returncode != 0:
            return None, result.stderr.decode()
        with open(out_path, 'rb') as f:
            wav = f.read()
        os.unlink(out_path)
        return wav, None
    except Exception as e:
        return None, str(e)

def download_and_convert(audio_url):
    """Download audio from URL and convert to WAV."""
    try:
        r = requests.get(audio_url, timeout=30)
        r.raise_for_status()
        wav, err = convert_to_wav(r.content, '.mp3')
        if wav: return wav, None
        return None, err
    except Exception as e:
        return None, str(e)

def build_silent_wav(duration_ms=200, sample_rate=16000):
    """Build valid silent WAV for text-mode assessment."""
    num_samples = int(sample_rate * duration_ms / 1000)
    pcm_data    = b'\x00\x00' * num_samples
    data_size   = len(pcm_data)
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE', b'fmt ', 16,
        1, 1, sample_rate, sample_rate * 2, 2, 16,
        b'data', data_size)
    return header + pcm_data

# ── Azure call ────────────────────────────────────────────────────────────────

def call_azure(audio_bytes, reference_text, language):
    azure_key    = get_azure_key()
    azure_region = get_azure_region()
    pron_config  = {
        "ReferenceText": reference_text,
        "GradingSystem": "HundredMark",
        "Granularity":   "Phoneme",
        "Dimension":     "Comprehensive",
        "EnableMiscue":  True,
        "EnableProsodyAssessment": "true"
    }
    pron_b64 = base64.b64encode(json.dumps(pron_config).encode()).decode()
    url = (f"https://{azure_region}.stt.speech.microsoft.com"
           f"/speech/recognition/conversation/cognitiveservices/v1"
           f"?language={language}&format=detailed&usePipelineVersion=0")
    headers = {
        "Ocp-Apim-Subscription-Key": azure_key,
        "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
        "Pronunciation-Assessment": pron_b64,
        "Accept": "application/json"
    }
    try:
        resp = requests.post(url, headers=headers, data=audio_bytes, timeout=30)
        if resp.status_code == 200:
            return {"success": True, "data": resp.json()}
        return {"success": False, "error": f"Azure {resp.status_code}", "details": resp.text}
    except Exception as e:
        return {"success": False, "error": str(e)}

def format_response(azure_result, mode="audio"):
    if not azure_result.get('success'):
        return {
            "success": False, "mode": mode,
            "error":   azure_result.get('error', 'Unknown error'),
            "details": azure_result.get('details', ''),
            "feedback": "Sorry, assessment failed. Please try again."
        }
    data  = azure_result.get('data', {})
    nbest = data.get('NBest', [{}])[0] if data.get('NBest') else {}
    pa    = nbest.get('PronunciationAssessment', nbest)

    pron  = round(pa.get('PronScore',         nbest.get('PronScore',         0)), 1)
    acc   = round(pa.get('AccuracyScore',      nbest.get('AccuracyScore',     0)), 1)
    flu   = round(pa.get('FluencyScore',       nbest.get('FluencyScore',      0)), 1)
    comp  = round(pa.get('CompletenessScore',  nbest.get('CompletenessScore', 0)), 1)
    pros  = round(pa.get('ProsodyScore',       nbest.get('ProsodyScore',      0)), 1)

    if   pron >= 90: feedback = f"🌟 Excellent! Pronunciation score: {pron}/100."
    elif pron >= 75: feedback = f"👍 Good job! Pronunciation score: {pron}/100."
    elif pron >= 60: feedback = f"📚 Not bad! Pronunciation score: {pron}/100."
    else:            feedback = f"💪 Keep practicing! Pronunciation score: {pron}/100."

    words_out, weak = [], []
    for w in nbest.get('Words', []):
        w_pa  = w.get('PronunciationAssessment', w)
        ws    = round(w_pa.get('AccuracyScore', w.get('AccuracyScore', 0)), 1)
        werr  = w_pa.get('ErrorType', w.get('ErrorType', 'None'))
        words_out.append({"word": w.get('Word',''), "accuracy": ws, "error": werr})
        if ws < 70 or werr not in ('None', ''):
            weak.append(w.get('Word',''))

    if weak:
        feedback += f" Focus on: {', '.join(weak[:3])}."

    return {
        "success": True, "mode": mode,
        "pronunciation_score": pron,
        "accuracy_score":      acc,
        "fluency_score":       flu,
        "completeness_score":  comp,
        "prosody_score":       pros,
        "feedback":            feedback,
        "words":               words_out,
        "recognized_text":     nbest.get('Display', nbest.get('Lexical', ''))
    }

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET'])
def home():
    key = get_azure_key()
    return jsonify({
        "status": "running",
        "service": "LTA Pronunciation Assessment API",
        "azure_configured": len(key) > 0,
        "region": get_azure_region(),
        "endpoints": {
            "audio_mode": "POST /assess — audio_url or audio_base64 + reference_text + language/locale",
            "text_mode":  "POST /assess-text — reference_text + language/locale (no audio)"
        }
    })


@app.route('/assess', methods=['POST'])
def assess():
    """
    Unified endpoint.
    - audio_url    : CBB mode — download MP3, convert, assess
    - audio_base64 : Browser mode — convert WebM to WAV, assess
    - neither      : Text mode — silent WAV, assess reference text
    """
    if not get_azure_key():
        return jsonify({"success": False, "error": "Azure key not configured"}), 500

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON data"}), 400

    reference_text = data.get('reference_text', data.get('text', '')).strip()
    language       = data.get('locale', data.get('language', 'fr-CA'))
    audio_url      = data.get('audio_url', '')
    audio_base64   = data.get('audio_base64', '')

    if not reference_text:
        return jsonify({"success": False, "error": "reference_text is required"}), 400

    # ── Browser audio (base64 WebM) ───────────────────────────────────────────
    if audio_base64:
        try:
            raw = base64.b64decode(audio_base64)
        except Exception:
            return jsonify({"success": False, "error": "Invalid audio_base64"}), 400

        # Detect format from magic bytes
        suffix = '.webm'
        if raw[:4] == b'OggS':            suffix = '.ogg'
        elif raw[:4] == b'fLaC':          suffix = '.flac'
        elif raw[:3] == b'ID3' or raw[:2] == b'\xff\xfb': suffix = '.mp3'
        elif raw[:4] == b'RIFF':
            # Already WAV — send directly
            result = call_azure(raw, reference_text, language)
            return jsonify(format_response(result, mode="audio"))

        wav, err = convert_to_wav(raw, suffix)
        if not wav:
            # ffmpeg not available — try sending raw and hope Azure accepts it
            result = call_azure(raw, reference_text, language)
        else:
            result = call_azure(wav, reference_text, language)
        return jsonify(format_response(result, mode="audio"))

    # ── URL audio (CBB) ───────────────────────────────────────────────────────
    if audio_url:
        wav, err = download_and_convert(audio_url)
        if not wav:
            return jsonify({"success": False, "error": f"Audio download/convert failed: {err}"}), 400
        result = call_azure(wav, reference_text, language)
        return jsonify(format_response(result, mode="audio"))

    # ── Text mode fallback ────────────────────────────────────────────────────
    silent  = build_silent_wav()
    result  = call_azure(silent, reference_text, language)
    return jsonify(format_response(result, mode="text"))


@app.route('/assess-text', methods=['POST'])
def assess_text():
    """Dedicated text-only endpoint — no audio needed."""
    if not get_azure_key():
        return jsonify({"success": False, "error": "Azure key not configured"}), 500
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON data"}), 400
    reference_text = data.get('reference_text', data.get('text', '')).strip()
    language       = data.get('locale', data.get('language', 'fr-CA'))
    if not reference_text:
        return jsonify({"success": False, "error": "reference_text is required"}), 400
    result = call_azure(build_silent_wav(), reference_text, language)
    return jsonify(format_response(result, mode="text"))


@app.route('/languages', methods=['GET'])
def languages():
    return jsonify([
        {"code": "fr-CA", "name": "French (Canada) — Primary"},
        {"code": "en-CA", "name": "English (Canada) — Primary"},
        {"code": "en-US", "name": "English (US)"},
        {"code": "fr-FR", "name": "French (France)"},
        {"code": "es-MX", "name": "Spanish (Mexico)"},
        {"code": "es-ES", "name": "Spanish (Spain)"},
    ])


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
