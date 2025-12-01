"""
Pronunciation Assessment Middleware API
Bridges ChatbotBuilder AI → Azure Speech Services
Accepts audio URL, sends to Azure, returns scores
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests
import base64
import json

app = Flask(__name__)
CORS(app)

# Azure Configuration - Set these in Render environment variables
AZURE_SPEECH_KEY = os.getenv('AZURE_SPEECH_KEY')
AZURE_SPEECH_REGION = os.getenv('AZURE_SPEECH_REGION', 'canadaeast')


def download_audio(audio_url):
    """Download audio file from URL"""
    try:
        response = requests.get(audio_url, timeout=30)
        response.raise_for_status()
        return response.content
    except Exception as e:
        return None, str(e)


def assess_pronunciation(audio_data, reference_text, language='en-US'):
    """Send audio to Azure for pronunciation assessment"""
    
    # Build pronunciation assessment config
    pron_config = {
        "ReferenceText": reference_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Word",
        "Dimension": "Comprehensive",
        "EnableProsodyAssessment": "true"
    }
    
    # Base64 encode the config
    pron_config_b64 = base64.b64encode(json.dumps(pron_config).encode()).decode()
    
    # Azure endpoint
    url = f"https://{AZURE_SPEECH_REGION}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
    url += f"?language={language}&format=detailed"
    
    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
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
    """Format Azure response for ChatbotBuilder"""
    
    if not azure_result.get('success'):
        return {
            "success": False,
            "error": azure_result.get('error', 'Unknown error'),
            "feedback": "Sorry, I couldn't assess your pronunciation. Please try again."
        }
    
    data = azure_result.get('data', {})
    nbest = data.get('NBest', [{}])[0] if data.get('NBest') else {}
    
    # Extract scores
    pron_score = round(nbest.get('PronScore', 0), 1)
    accuracy = round(nbest.get('AccuracyScore', 0), 1)
    fluency = round(nbest.get('FluencyScore', 0), 1)
    completeness = round(nbest.get('CompletenessScore', 0), 1)
    prosody = round(nbest.get('ProsodyScore', 0), 1) if 'ProsodyScore' in nbest else None
    
    # Generate feedback message
    if pron_score >= 90:
        feedback = f"🌟 Excellent! Your pronunciation scored {pron_score}/100. Outstanding work!"
    elif pron_score >= 75:
        feedback = f"👍 Good job! Your pronunciation scored {pron_score}/100. Keep practicing!"
    elif pron_score >= 60:
        feedback = f"📚 Not bad! Your pronunciation scored {pron_score}/100. Try speaking more slowly and clearly."
    else:
        feedback = f"💪 Keep trying! Your pronunciation scored {pron_score}/100. Practice makes perfect!"
    
    # Word-by-word feedback
    words_feedback = []
    problem_words = []
    
    for word in nbest.get('Words', []):
        word_score = round(word.get('AccuracyScore', 0), 1)
        error_type = word.get('ErrorType', 'None')
        
        words_feedback.append({
            "word": word.get('Word'),
            "score": word_score,
            "error": error_type
        })
        
        if word_score < 70 or error_type != 'None':
            problem_words.append(word.get('Word'))
    
    # Add word-specific feedback
    if problem_words:
        feedback += f"\n\n⚠️ Words to practice: {', '.join(problem_words)}"
    
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


# ============== ROUTES ==============

@app.route('/', methods=['GET'])
def home():
    """Health check"""
    return jsonify({
        "status": "running",
        "service": "Pronunciation Assessment Middleware",
        "azure_configured": bool(AZURE_SPEECH_KEY),
        "region": AZURE_SPEECH_REGION
    })


@app.route('/assess', methods=['POST'])
def assess():
    """
    Main endpoint for ChatbotBuilder
    
    Expected JSON:
    {
        "audio_url": "https://...",
        "reference_text": "Hello how are you",
        "language": "en-US"  (optional, defaults to en-US)
    }
    """
    
    if not AZURE_SPEECH_KEY:
        return jsonify({
            "success": False,
            "error": "Azure Speech API key not configured",
            "feedback": "Service configuration error. Please contact support."
        }), 500
    
    # Get request data
    data = request.get_json()
    
    if not data:
        return jsonify({
            "success": False,
            "error": "No JSON data received",
            "feedback": "Please send a voice message to assess."
        }), 400
    
    audio_url = data.get('audio_url')
    reference_text = data.get('reference_text', data.get('text', ''))
    language = data.get('language', 'en-US')
    
    if not audio_url:
        return jsonify({
            "success": False,
            "error": "audio_url is required",
            "feedback": "No audio received. Please send a voice message."
        }), 400
    
    if not reference_text:
        return jsonify({
            "success": False,
            "error": "reference_text is required",
            "feedback": "Please provide the text you want to practice."
        }), 400
    
    # Download audio from URL
    audio_data = download_audio(audio_url)
    
    if audio_data is None or isinstance(audio_data, tuple):
        return jsonify({
            "success": False,
            "error": "Failed to download audio",
            "feedback": "Couldn't access your voice message. Please try again."
        }), 400
    
    # Send to Azure for assessment
    azure_result = assess_pronunciation(audio_data, reference_text, language)
    
    # Format and return response
    formatted = format_response(azure_result)
    
    return jsonify(formatted)


@app.route('/languages', methods=['GET'])
def languages():
    """Return supported languages"""
    return jsonify([
        {"code": "en-US", "name": "English (US)"},
        {"code": "en-GB", "name": "English (UK)"},
        {"code": "es-ES", "name": "Spanish (Spain)"},
        {"code": "es-MX", "name": "Spanish (Mexico)"},
        {"code": "fr-FR", "name": "French"},
        {"code": "de-DE", "name": "German"},
        {"code": "pt-BR", "name": "Portuguese (Brazil)"},
        {"code": "zh-CN", "name": "Chinese (Mandarin)"}
    ])


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
