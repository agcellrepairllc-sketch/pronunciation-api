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
  POST /encrypt-pdf   -> PDF encryption with pikepdf
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
    if raw[:4] == b'RIFF':                      return None
    if raw[:4] == b'OggS':                      return '.ogg'
    if raw[:4] == b'fLaC':                      return '.flac'
    if raw[:3] == b'ID3' or raw[:2] == b'\xff\xfb': return '.mp3'
    if raw[:4] == b'\x1a\x45\xdf\xa3':         return '.webm'
    if raw[:4] == b'\x00\x00\x00\x20' or raw[4:8] == b'ftyp': return '.mp4'
    return '.webm'

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
    if audio_bytes[:4] == b'RIFF':
        content_type = 'audio/wav; codecs=audio/pcm; samplerate=16000'
    elif audio_bytes[:4] == b'\x1a\x45\xdf\xa3':
        content_type = 'audio/webm; codecs=opus'
    elif audio_bytes[:4] == b'OggS':
        content_type = 'audio/ogg; codecs=opus'
    else:
        content_type = 'audio/webm; codecs=opus'
    headers = {
        "Ocp-Apim-Subscription-Key": azure_key,
        "Content-Type": content_type,
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
        pa    = nbest.get('PronunciationAssessment', {}) or {}
        pron  = round(float(nbest.get('PronScore',         pa.get('PronScore',         0)) or 0), 1)
        acc   = round(float(nbest.get('AccuracyScore',     pa.get('AccuracyScore',     0)) or 0), 1)
        flu   = round(float(nbest.get('FluencyScore',      pa.get('FluencyScore',      0)) or 0), 1)
        comp  = round(float(nbest.get('CompletenessScore', pa.get('CompletenessScore', 0)) or 0), 1)
        pros  = round(float(nbest.get('ProsodyScore',      pa.get('ProsodyScore',      0)) or 0), 1)
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
        ws   = round(float(w.get('AccuracyScore', w_pa.get('AccuracyScore', 0)) or 0), 1)
        werr = w.get('ErrorType', w_pa.get('ErrorType', 'None')) or 'None'
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
            "health":     "GET  /health",
            "encrypt":    "POST /encrypt-pdf"
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
        return jsonify(format_response(call_azure(raw, reference_text, language), mode="audio"))

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


@app.route('/encrypt-pdf', methods=['POST'])
def encrypt_pdf():
    try:
        import pikepdf
        import tempfile, os

        password = None
        pdf_bytes = None

        if request.content_type and 'multipart/form-data' in request.content_type:
            if 'pdf' not in request.files:
                return jsonify({"success": False, "error": "No PDF file provided"}), 400
            password = request.form.get('password', '')
            pdf_bytes = request.files['pdf'].read()
        else:
            data = request.get_json()
            if not data:
                return jsonify({"success": False, "error": "No data provided"}), 400
            password = data.get('password', '')
            pdf_b64  = data.get('pdf_base64', '')
            if not pdf_b64:
                return jsonify({"success": False, "error": "No pdf_base64 provided"}), 400
            # Fix base64 padding
            padding = 4 - len(pdf_b64) % 4
            if padding != 4:
                pdf_b64 += '=' * padding
            pdf_bytes = base64.b64decode(pdf_b64)

        if not password:
            return jsonify({"success": False, "error": "No password provided"}), 400

        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f_in:
            f_in.write(pdf_bytes)
            in_path = f_in.name

        out_path = in_path.replace('.pdf', '_encrypted.pdf')

        try:
            with pikepdf.open(in_path, suppress_warnings=True, attempt_recovery=True) as pdf:
                pdf.save(
                    out_path,
                    encryption=pikepdf.Encryption(
                        user=password,
                        owner=password + '-OWNER',
                        R=4,
                        allow=pikepdf.Permissions(
                            print_lowres=False,
                            print_highres=False,
                            extract=False,
                            modify_annotation=False,
                            modify_form=False,
                            modify_other=False,
                            modify_assembly=False,
                        )
                    )
                )

            with open(out_path, 'rb') as f:
                encrypted_bytes = f.read()

            from flask import Response
            return Response(
                encrypted_bytes,
                mimetype='application/pdf',
                headers={'Content-Disposition': 'attachment; filename=encrypted.pdf'}
            )

        finally:
            try: os.unlink(in_path)
            except: pass
            try: os.unlink(out_path)
            except: pass

    except ImportError:
        return jsonify({"success": False, "error": "pikepdf not installed"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
