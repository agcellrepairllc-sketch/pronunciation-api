"""
Pronunciation Assessment Middleware API
Bridges PaygoGPT + ChatbotBuilder -> Azure Speech Services

Endpoints:
  GET  /              -> health check
  GET  /health        -> detailed health + ffmpeg check
  POST /assess        -> one-shot mode (CBB + quick test)
  POST /assess-text   -> text-only mode (social media)
  WS   /assess-stream -> continuous mode (full session, real-time)
  GET  /languages     -> supported locales
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sock import Sock
import os, requests, base64, json, subprocess, tempfile, struct, threading, time

app  = Flask(__name__)
CORS(app)
sock = Sock(app)

def get_azure_key():    return os.environ.get('AZURE_SPEECH_KEY', '')
def get_azure_region(): return os.environ.get('AZURE_SPEECH_REGION', 'canadaeast')

# Audio helpers

def convert_to_wav(input_bytes, input_suffix='.webm'):
    try:
        with tempfile.NamedTemporaryFile(suffix=input_suffix, delete=False) as f:
            f.write(input_bytes)
            in_path = f.name
        out_path = in_path.replace(input_suffix, '.wav')
        result = subprocess.run([
            'ffmpeg', '-y', '-i', in_path,
            '-ar', '16000', '-ac', '1', '-sample_fmt', 's16', out_path
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
    try:
        r = requests.get(audio_url, timeout=30)
        r.raise_for_status()
        wav, err = convert_to_wav(r.content, '.mp3')
        if wav: return wav, None
        return None, err
    except Exception as e:
        return None, str(e)

def build_silent_wav(duration_ms=200, sample_rate=16000):
    num_samples = int(sample_rate * duration_ms / 1000)
    pcm_data    = b'\x00\x00' * num_samples
    data_size   = len(pcm_data)
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE', b'fmt ', 16,
        1, 1, sample_rate, sample_rate * 2, 2, 16,
        b'data', data_size)
    return header + pcm_data

def detect_audio_suffix(raw):
    """Detect audio format from magic bytes."""
    if raw[:4] == b'RIFF':                      return None          # Already WAV
    if raw[:4] == b'OggS':                      return '.ogg'
    if raw[:4] == b'fLaC':                      return '.flac'
    if raw[:3] == b'ID3' or raw[:2] == b'\xff\xfb': return '.mp3'
    if raw[:4] == b'\x1a\x45\xdf\xa3':         return '.webm'       # WebM/MKV
    if raw[:4] == b'\x00\x00\x00\x20' or raw[4:8] == b'ftyp': return '.mp4'
    return '.webm'  # Default fallback

# Azure REST call (one-shot)

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
    try:
        data       = azure_result.get('data', {}) or {}
        nbest_list = data.get('NBest', [])
        nbest      = nbest_list[0] if isinstance(nbest_list, list) and nbest_list else {}
        pa         = nbest.get('PronunciationAssessment', {}) or {}
        pron  = round(float(pa.get('PronScore',         0) or 0), 1)
        acc   = round(float(pa.get('AccuracyScore',     0) or 0), 1)
        flu   = round(float(pa.get('FluencyScore',      0) or 0), 1)
        comp  = round(float(pa.get('CompletenessScore', 0) or 0), 1)
        pros  = round(float(pa.get('ProsodyScore',      0) or 0), 1)
    except Exception as ex:
        return {
            "success": False, "mode": mode,
            "error": f"Score parsing failed: {str(ex)}",
            "details": str(azure_result.get('data', '')),
            "feedback": "Assessment received but scores could not be parsed."
        }

    if   pron >= 90: feedback = f"Excellent! Pronunciation score: {pron}/100."
    elif pron >= 75: feedback = f"Good job! Pronunciation score: {pron}/100."
    elif pron >= 60: feedback = f"Not bad! Pronunciation score: {pron}/100."
    else:            feedback = f"Keep practicing! Pronunciation score: {pron}/100."

    words_out, weak = [], []
    for w in (nbest.get('Words', []) or []):
        w_pa = w.get('PronunciationAssessment', {}) or {}
        ws   = round(float(w_pa.get('AccuracyScore', 0) or 0), 1)
        werr = w_pa.get('ErrorType', 'None') or 'None'
        words_out.append({"word": w.get('Word',''), "accuracy": ws, "error": werr})
        if ws < 70 or werr not in ('None', ''):
            weak.append(w.get('Word',''))
    if weak:
        feedback += f" Focus on: {', '.join(weak[:3])}."

    return {
        "success": True, "mode": mode,
        "pronunciation_score": pron, "accuracy_score": acc,
        "fluency_score": flu, "completeness_score": comp,
        "prosody_score": pros, "feedback": feedback,
        "words": words_out,
        "recognized_text": nbest.get('Display', nbest.get('Lexical', ''))
    }

# Continuous assessment WebSocket

def run_continuous_session(ws, language, topic):
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError:
        ws.send(json.dumps({"type": "error", "message": "Azure Speech SDK not installed"}))
        return

    azure_key    = get_azure_key()
    azure_region = get_azure_region()

    speech_config = speechsdk.SpeechConfig(subscription=azure_key, region=azure_region)
    speech_config.speech_recognition_language = language

    pron_config = speechsdk.PronunciationAssessmentConfig(
        reference_text="",
        grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
        granularity=speechsdk.PronunciationAssessmentGranularity.Phoneme,
        enable_miscue=False
    )
    pron_config.enable_prosody_assessment()
    if topic:
        pron_config.enable_content_assessment_with_topic(topic)

    push_stream  = speechsdk.audio.PushAudioInputStream()
    audio_config = speechsdk.audio.AudioConfig(stream=push_stream)
    recognizer   = speechsdk.SpeechRecognizer(
        speech_config=speech_config,
        audio_config=audio_config
    )
    pron_config.apply_to(recognizer)

    session_scores = []
    done           = threading.Event()

    def on_recognized(evt):
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
            pa   = speechsdk.PronunciationAssessmentResult(evt.result)
            pron = round(pa.pronunciation_score    or 0, 1)
            acc  = round(pa.accuracy_score         or 0, 1)
            flu  = round(pa.fluency_score          or 0, 1)
            comp = round(pa.completeness_score     or 0, 1)

            words_out = []
            try:
                detail = json.loads(evt.result.properties.get(
                    speechsdk.PropertyId.SpeechServiceResponse_JsonResult, '{}'))
                for w in (detail.get('NBest', [{}])[0].get('Words') or []):
                    w_pa = w.get('PronunciationAssessment', {})
                    words_out.append({
                        "word":     w.get('Word', ''),
                        "accuracy": round(w_pa.get('AccuracyScore', 0), 1),
                        "error":    w_pa.get('ErrorType', 'None')
                    })
            except Exception:
                pass

            session_scores.append(pron)
            try:
                ws.send(json.dumps({
                    "type":                "sentence_result",
                    "text":                evt.result.text,
                    "pronunciation_score": pron,
                    "accuracy_score":      acc,
                    "fluency_score":       flu,
                    "completeness_score":  comp,
                    "words":               words_out
                }))
            except Exception:
                done.set()

    def on_stopped(evt):  done.set()
    def on_canceled(evt):
        done.set()
        try: ws.send(json.dumps({"type": "error", "message": "Recognition canceled"}))
        except Exception: pass

    recognizer.recognized.connect(on_recognized)
    recognizer.session_stopped.connect(on_stopped)
    recognizer.canceled.connect(on_canceled)

    recognizer.start_continuous_recognition()
    ws.send(json.dumps({"type": "session_started", "language": language}))

    while not done.is_set():
        try:
            msg = ws.receive(timeout=30)
            if msg is None:
                break
            if isinstance(msg, bytes):
                push_stream.write(msg)
            else:
                data = json.loads(msg)
                if data.get('action') == 'stop':
                    break
        except Exception:
            break

    push_stream.close()
    recognizer.stop_continuous_recognition()
    done.wait(timeout=3)

    if session_scores:
        avg = round(sum(session_scores) / len(session_scores), 1)
        if   avg >= 90: sfb = f"Outstanding session! Average: {avg}/100."
        elif avg >= 75: sfb = f"Great work! Average: {avg}/100."
        elif avg >= 60: sfb = f"Good effort! Average: {avg}/100."
        else:           sfb = f"Keep it up! Average: {avg}/100."
        try:
            ws.send(json.dumps({
                "type":           "session_summary",
                "sentence_count": len(session_scores),
                "average_score":  avg,
                "highest_score":  round(max(session_scores), 1),
                "lowest_score":   round(min(session_scores), 1),
                "feedback":       sfb
            }))
        except Exception:
            pass

    try: ws.send(json.dumps({"type": "session_ended"}))
    except Exception: pass


@sock.route('/assess-stream')
def assess_stream(ws):
    try:
        init_msg = ws.receive(timeout=10)
        if not init_msg:
            ws.send(json.dumps({"type": "error", "message": "No init message"}))
            return
        init_data = json.loads(init_msg)
        language  = init_data.get('language', 'fr-CA')
        topic     = init_data.get('topic', 'general')
        run_continuous_session(ws, language, topic)
    except Exception as e:
        try: ws.send(json.dumps({"type": "error", "message": str(e)}))
        except Exception: pass


# REST routes

@app.route('/health', methods=['GET'])
def health():
    import shutil
    ffmpeg_path = shutil.which('ffmpeg')
    ffmpeg_ok   = ffmpeg_path is not None
    test_result = None
    if ffmpeg_ok:
        try:
            r = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
            test_result = r.stdout.decode()[:100]
        except Exception as e:
            test_result = str(e)
    return jsonify({
        "status":       "running",
        "ffmpeg_found": ffmpeg_ok,
        "ffmpeg_path":  ffmpeg_path,
        "ffmpeg_info":  test_result,
        "azure_key":    len(get_azure_key()) > 0,
        "region":       get_azure_region(),
    })


@app.route('/', methods=['GET'])
def home():
    key = get_azure_key()
    return jsonify({
        "status": "running",
        "service": "LTA Pronunciation Assessment API",
        "azure_configured": len(key) > 0,
        "region": get_azure_region(),
        "endpoints": {
            "one_shot":   "POST /assess",
            "text_mode":  "POST /assess-text",
            "continuous": "WS   /assess-stream",
            "health":     "GET  /health"
        }
    })


@app.route('/assess', methods=['POST'])
def assess():
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

   if audio_base64:
        try: raw = base64.b64decode(audio_base64)
        except Exception:
            return jsonify({"success": False, "error": "Invalid audio_base64"}), 400

        suffix = detect_audio_suffix(raw)
        magic  = raw[:8].hex()
        size   = len(raw)
        print(f"[DEBUG] audio received: {size} bytes, magic={magic}, detected suffix={suffix}")

        if suffix is None:
            print(f"[DEBUG] Already WAV — sending directly")
            return jsonify(format_response(call_azure(raw, reference_text, language), mode="audio"))

        wav, err = convert_to_wav(raw, suffix)
        if not wav:
            print(f"[DEBUG] ffmpeg FAILED ({suffix}): {err}")
            azure_result = call_azure(raw, reference_text, language)
            result = format_response(azure_result, mode="audio")
            result['debug'] = f"ffmpeg failed: {err[:200]}"
            return jsonify(result)

        print(f"[DEBUG] WAV converted: {len(wav)} bytes — sending to Azure")
        azure_result = call_azure(wav, reference_text, language)
        print(f"[DEBUG] Azure response: {str(azure_result)[:300]}")
        result = format_response(azure_result, mode="audio")
        result['debug_azure'] = str(azure_result.get('data', ''))[:500]
        return jsonify(result)
        

    if audio_url:
        wav, err = download_and_convert(audio_url)
        if not wav:
            return jsonify({"success": False, "error": f"Audio failed: {err}"}), 400
        return jsonify(format_response(call_azure(wav, reference_text, language), mode="audio"))

    return jsonify(format_response(call_azure(build_silent_wav(), reference_text, language), mode="text"))


@app.route('/assess-text', methods=['POST'])
def assess_text():
    if not get_azure_key():
        return jsonify({"success": False, "error": "Azure key not configured"}), 500
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON data"}), 400
    reference_text = data.get('reference_text', data.get('text', '')).strip()
    language       = data.get('locale', data.get('language', 'fr-CA'))
    if not reference_text:
        return jsonify({"success": False, "error": "reference_text is required"}), 400
    return jsonify(format_response(call_azure(build_silent_wav(), reference_text, language), mode="text"))


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
