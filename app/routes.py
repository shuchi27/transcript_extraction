from flask import Blueprint, request, jsonify, Response, render_template
from .scraper import fetch_transcript_for_url, fetch_youtube_transcript
from .utils import extract_youtube_video_id

api_bp = Blueprint("api", __name__, template_folder="templates")

# HTML page
@api_bp.route("/gettranscript", methods=["GET"])
def get_form():
    return render_template("index.html")

# Transcript API (POST from the form)
@api_bp.route("/transcript", methods=["POST"])
async def get_transcript():
    url = request.form.get("url")
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        vid = extract_youtube_video_id(url)
        text = await (fetch_youtube_transcript(vid) if vid else fetch_transcript_for_url(url))
        return Response(text, mimetype="text/plain")
    except Exception as e:
        return jsonify({"error": "Failed to process transcript", "details": str(e)}), 500

@api_bp.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"}), 200
