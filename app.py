"""
Pronunciation Assessment Middleware API
Bridges ChatbotBuilder AI → Azure Speech Services
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests
import base64
import json

app = Flask(__name__)
CORS(app)

def get_azure_key():
    return os.environ.get('AZURE_SPEECH_KEY', '')

def get_azure_region():
    return os.environ.get('AZURE_SPEECH_REGION', 'canadaeast')

def download_audio(audio_url):
    try:
        response = requests.get(audio_url, timeout=30)
        response.raise_for_status()
        return response.content
    except:
        return None

def assess_pronunciation(audio_data, reference_text, language='en-US'):
    azure_key = get_azure_key()
    azure_region = get_azure_region()
    
    pron_config = {
        "ReferenceText": reference_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Word",
        "Dimension": "Comprehensive",
        "EnableProsodyAssessment": "true"
    }
    
    pron_config_b64 = base64.b64encode(json.dumps(pron_config).encode()).decode()
    
    url = f"https://{azure_region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
    url += f"?language={language}&format=detailed"
    
    headers = {
        "Ocp-Apim-Subscription-Key": azure_key,
        "Content-Type": "audio/ogg; codecs=opus",
        "Pronunciation-Assessment": pron_config_b64,
        "Accept": "application/json"
    }
    
    try:
        response = requests.post(url, headers=headers, data=audio_data, timeout=30)
        if response.status_code == 200:
            return {"success": True, "data": response.json()}
        else:
            return {"success": False, "error": f"Azure error {response.status_code}", "details": response.text}
    except Exception as e:
        return {"success": False, "error": str(e)}

def format_response(azure_result):
    if not azure_result.get('success'):
        return {
            "success": False,
            "error": azure_result.get('error', 'Unknown error'),
            "details": azure_result.get('details', ''),
            "feedback": "Sorry, I couldn't assess your pronunciation. Please try again."
        }
    
    data = azure_result.get('data', {})
    nbest = data.get('NBest', [{}])[0] if data.get('NBest') else {}
    
    pron_score = round(nbest.get('PronScore', 0), 1)
    accuracy = round(nbest.get('AccuracyScore', 0), 1)
    fluency = round(nbest.get('FluencyScore', 0), 1)
    completeness = round(nbest.get('CompletenessScore', 0), 1)
    prosody = round(nbest.get('ProsodyScore', 0), 1) if 'ProsodyScore' in nbest else None
    
    if pron_score >= 90:
        feedback = f"🌟 Excellent! Your pronunciation scored {pron_score}/100."
    elif pron_score >= 75:
        feedback = f"👍 Good job! Your pronunciation scored {pron_score}/100."
    elif pron_score >= 60:
        feedback = f"📚 Not bad! Your pronunciation scored {pron_score}/100."
    else:
        feedback = f"💪 Keep trying! Your pronunciation scored {pron_score}/100."
    
    words_feedback = []
    problem_words = []
    
    for word in nbest.get('Words', []):
        word_score = round(word.get('AccuracyScore', 0), 1)
        error_type = word.get('ErrorType', 'None')
        words_feedback.append({"word": word.get('Word'), "score": word_score, "error": error_type})
        if word_score < 70 or error_type != 'None':
            problem_words.append(word.get('Word'))
    
    if problem_words:
        feedback += f" Words to practice: {', '.join(problem_words)}"
    
    return {
        "success": True,
        "pronunciation_score": pron_score,
        "accuracy_score": accuracy,
        "fluency_score": fluency,
        "completeness_score": completeness,
        "prosody_score": prosody,
        "feedback": feedback,
        "words": words_feedback,
        "recognized_text": nbest.get('Display', '')
    }

@app.route('/', methods=['GET'])
def home():
    azure_key = get_azure_key()
    return jsonify({
        "status": "running",
        "service": "Pronunciation Assessment Middleware",
        "azure_configured": len(azure_key) > 0,
        "key_length": len(azure_key),
        "region": get_azure_region()
    })

@app.route('/assess', methods=['POST'])
def assess():
    azure_key = get_azure_key()
    
    if not azure_key:
        return jsonify({"success": False, "error": "Azure key not configured"}), 500
    
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON data"}), 400
    
    audio_url = data.get('audio_url')
    reference_text = data.get('reference_text', data.get('text', ''))
    language = data.get('language', 'en-US')
    
    if not audio_url:
        return jsonify({"success": False, "error": "audio_url required"}), 400
    if not reference_text:
        return jsonify({"success": False, "error": "reference_text required"}), 400
    
    audio_data = download_audio(audio_url)
    if audio_data is None:
        return jsonify({"success": False, "error": "Failed to download audio"}), 400
    
    azure_result = assess_pronunciation(audio_data, reference_text, language)
    return jsonify(format_response(azure_result))

@app.route('/languages', methods=['GET'])
def languages():
    return jsonify([
        {"code": "en-US", "name": "English (US)"},
        {"code": "es-MX", "name": "Spanish (Mexico)"},
        {"code": "fr-FR", "name": "French"}
    ])

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
