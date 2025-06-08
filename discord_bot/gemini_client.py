import os
import logging
import google.generativeai as genai

logger = logging.getLogger('discord_digest_bot')

GEMINI_API_KEY = os.environ.get('GOOGLE_GENAI_API_KEY')
if not GEMINI_API_KEY:
    logger.warning(
        "WARNING: GOOGLE_GENAI_API_KEY environment variable not set. Summarization will fail.")
    gemini_model = None
    role_model = None
else:
    try:
        genai.configure(api_key=GEMINI_API_KEY)

        gemini_model = genai.GenerativeModel("gemini-2.5-flash-preview-05-20")
        logger.info(f"Initialized Gemini Model: {gemini_model.model_name}")

        role_model = genai.GenerativeModel("gemini-2.0-flash-lite")
        logger.info(f"Initialized Gemma Model: {role_model.model_name}")
    except Exception as e:
        logger.error(f"Error initializing Gemini model: {e}. Summarization might fail.")
        gemini_model = None
