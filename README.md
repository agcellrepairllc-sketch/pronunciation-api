# Pronunciation Assessment Middleware

Bridges ChatbotBuilder AI → Azure Speech Services

## Deploy to Render.com (Free)

### Step 1: Create GitHub Repository

1. Go to [github.com](https://github.com) and sign in (or create account)
2. Click **"New repository"** (green button)
3. Name it `pronunciation-api`
4. Keep it **Public**
5. Click **Create repository**
6. Upload all files from this folder (drag & drop on GitHub)

### Step 2: Deploy on Render

1. Go to [render.com](https://render.com) and sign up (free, use GitHub login)
2. Click **"New +"** → **"Web Service"**
3. Connect your GitHub account
4. Select your `pronunciation-api` repository
5. Settings:
   - **Name:** `pronunciation-api`
   - **Region:** Oregon (or closest)
   - **Branch:** `main`
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Plan:** `Free`
6. Click **"Create Web Service"**

### Step 3: Add Azure Key

1. In Render dashboard, go to your service
2. Click **"Environment"** tab
3. Add variable:
   - **Key:** `AZURE_SPEECH_KEY`
   - **Value:** `your_key1_from_azure`
4. Add variable:
   - **Key:** `AZURE_SPEECH_REGION`
   - **Value:** `canadaeast`
5. Click **"Save Changes"**

### Step 4: Get Your URL

After deploy completes (2-3 min), you'll get a URL like:
```
https://pronunciation-api.onrender.com
```

## ChatbotBuilder Setup

### API Request Settings

**Request URL:**
```
POST https://pronunciation-api.onrender.com/assess
```

**Headers:** (none needed)

**Body (JSON):**
```json
{
  "audio_url": "{{audio_variable}}",
  "reference_text": "Hello how are you",
  "language": "en-US"
}
```

### Response Mapping

The API returns:
```json
{
  "success": true,
  "pronunciation_score": 87,
  "accuracy_score": 92,
  "fluency_score": 85,
  "completeness_score": 100,
  "feedback": "👍 Good job! Your pronunciation scored 87/100...",
  "words": [...],
  "recognized_text": "Hello how are you"
}
```

Use `{{feedback}}` in your chatbot response to show the user their results.

## Testing

Test your deployment:
```bash
curl https://pronunciation-api.onrender.com/
```

Should return:
```json
{"status": "running", "azure_configured": true, "region": "canadaeast"}
```
