# app/scraper.py
import asyncio
from playwright.async_api import Page, async_playwright
from .utils import parse_vtt
import os
import httpx
from urllib.parse import urljoin
import re 
import uuid
import logging
import requests
import subprocess
import whisper
#from faster_whisper import WhisperModel

#NEW
CABLECAST_H2_MAX_CONN = int(os.getenv("CABLECAST_H2_MAX_CONN", "48"))
CABLECAST_H2_MAX_KEEPALIVE = int(os.getenv("CABLECAST_H2_MAX_KEEPALIVE", "48"))
CABLECAST_FETCH_TIMEOUT = float(os.getenv("CABLECAST_FETCH_TIMEOUT", "6.0"))  # per-request
CABLECAST_RETRIES = int(os.getenv("CABLECAST_RETRIES", "3"))
#NEW

async def fetch_youtube_transcript(video_id: str):
    """
    Calls the RapidAPI service to get a transcript for a YouTube video.
    """
    api_key = os.getenv("RAPIDAPI_KEY")
    if not api_key:
        raise ValueError("RAPIDAPI_KEY environment variable not set.")

    api_url = f"https://youtube-captions.p.rapidapi.com/transcript?videoId={video_id}"
    headers = {
        "x-rapidapi-host": "youtube-captions.p.rapidapi.com",
        "x-rapidapi-key": api_key,
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(api_url, headers=headers, timeout=30.0)
        # Raise an exception for bad status codes (4xx or 5xx)
        response.raise_for_status() 
        
        data = response.json()

        # Assuming the API returns a list of caption segments, each with a 'text' key.
        # We join them together to form the full transcript.
        if not isinstance(data, list):
            raise TypeError("Expected a list of captions from the YouTube API.")
        
        transcript_lines = [item.get("text", "") for item in data]
        return "\n".join(transcript_lines)

async def handle_granicus_url(page: 'Page'):
    """Performs the UI trigger sequence for Granicus (Dublin) pages."""
    print("  - Detected Granicus platform. Executing trigger sequence...")
#    await page.screenshot(path='/app/screenshots/before_click.png')
    await page.locator(".flowplayer").hover(timeout=10000)


    element = page.locator(".fp-menu").get_by_text("On", exact=True)
    await element.scroll_into_view_if_needed(timeout=10000)
    await element.click(force=True)
#    await page.screenshot(path='/app/screenshots/after_click.png')


async def handle_viebit_url(page: 'Page'):
    """Performs the UI trigger sequence for Viebit (Fremont) pages."""
    print("  - Detected Viebit platform. Executing trigger sequence...")
    await page.locator(".vjs-big-play-button").click(timeout=10000)
    await page.locator(".vjs-play-control").click(timeout=10000)
    await page.wait_for_timeout(500)
    await page.locator("button.vjs-subs-caps-button").click(timeout=10000)
    await page.locator('.vjs-menu-item:has-text("English")').click(timeout=10000)

async def handle_cablecast_url(page: 'Page'):
    """UI trigger for Cablecast (video.js) players."""
    print("  - Detected Cablecast platform. Executing trigger sequence...")
    # Try the big play button, then fallback to the toolbar play control.
    try:
        await page.locator(".vjs-big-play-button").click(timeout=8000)
    except Exception:
        try:
            await page.locator(".vjs-play-control").click(timeout=8000)
        except Exception:
            pass

    # Give the player a moment to initialize HLS/captions
    await page.wait_for_timeout(500)

    # Try to open CC menu and enable the first available track (English if present).
    try:
        await page.locator(".vjs-subs-caps-button, .vjs-captions-button").click(timeout=8000)
        # Prefer “English”, else just the first unchecked item.
        english = page.locator(".vjs-menu-item:has-text('English')")
        if await english.count() > 0:
            await english.first.click(timeout=8000)
        else:
            unchecked = page.locator(".vjs-menu-item[aria-checked='false']")
            if await unchecked.count() > 0:
                await unchecked.first.click(timeout=8000)
    except Exception:
        # Some streams auto-enable captions or expose them as default tracks.
        pass

# NEW

async def _stitch_vtt_from_m3u8(playlist_url: str) -> str:
    """
    Download a captions .m3u8 and stitch all .vtt segments into a single VTT text.
    Optimized for Cablecast: HTTP/2, pooled connections, bounded concurrency, retries.
    Returns the stitched VTT TEXT (not file).
    """
    limits = httpx.Limits(
        max_connections=CABLECAST_H2_MAX_CONN,
        max_keepalive_connections=CABLECAST_H2_MAX_KEEPALIVE,
    )
    timeout = httpx.Timeout(CABLECAST_FETCH_TIMEOUT)

    async with httpx.AsyncClient(http2=True, limits=limits, timeout=timeout) as client:
        # 1) Fetch playlist
        pl = await client.get(playlist_url)
        pl.raise_for_status()
        base = playlist_url.rsplit("/", 1)[0] + "/"

        # 2) Build absolute segment URLs (ignore #EXT* comment lines)
        seg_urls = []
        for line in pl.text.splitlines():
            ln = line.strip()
            if ln and not ln.startswith("#"):
                seg_urls.append(urljoin(base, ln))

        if not seg_urls:
            raise RuntimeError("Captions playlist contained no segments")

        # 3) Fetch segments with bounded concurrency + retries
        sem = asyncio.Semaphore(CABLECAST_H2_MAX_CONN)

        async def fetch_seg(idx: int, seg_url: str) -> tuple[int, str]:
            attempt = 0
            while True:
                attempt += 1
                try:
                    async with sem:
                        r = await client.get(seg_url)
                    if r.status_code != 200 or not r.text.strip():
                        raise httpx.HTTPError(f"bad status {r.status_code}")
                    return idx, r.text
                except Exception:
                    if attempt > CABLECAST_RETRIES:
                        return idx, ""
                    await asyncio.sleep(0.05 * attempt)

        tasks = [fetch_seg(i, u) for i, u in enumerate(seg_urls)]
        results = await asyncio.gather(*tasks)

    # 4) Reassemble in-order; skip empties
    results.sort(key=lambda t: t[0])
    chunks = [txt for _, txt in results if txt]

    if not chunks:
        raise RuntimeError("No caption segments could be downloaded")

    # 5) Prepend header and join
    stitched_vtt = "WEBVTT\n\n" + "\n\n".join(chunks) + "\n"
    return stitched_vtt

async def get_mp3_url(url: str):
    """
    Use Playwright to scrape the Audio download link from CVTV DOM.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle")

        try:
            mp3_href = await page.locator("a[href$='.mp3']").get_attribute("href")
            if mp3_href:
                print(f"[CVTV] Found MP3 link in DOM: {mp3_href}")
                return mp3_href
        except Exception as e:
            print(f"[CVTV] Failed to extract MP3 link: {e}")
        finally:
            await browser.close()

    return None


async def process_cvtv(url: str):
    """
    Full pipeline for CVTV: scrape MP3 link from DOM → download → Whisper.
    """
    mp3_url = await get_mp3_url(url)
    if not mp3_url:
        return {"error": "Failed to capture MP3 stream"}

    uid = str(uuid.uuid4())
    audio_file = f"audio_{uid}.mp3"

    print(f"[CVTV] Downloading MP3: {mp3_url}")
    resp = requests.get(mp3_url, stream=True)
    with open(audio_file, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)

    print(f"Now calling run_whisper_transcription method....")
    transcript = run_whisper_openai(audio_file, whisper_model="tiny")

    if os.path.exists(audio_file):
        os.remove(audio_file)
        print(f"[Cleanup] Deleted {audio_file}")

    return transcript

def run_whisper_openai(file_path, whisper_model="tiny"):
    """
    Convert MP3 → WAV (16kHz mono) and transcribe with Whisper.
    """
    print(f"[Whisper] Starting transcription on {file_path}")

    # Convert MP3 to WAV
    wav_path = os.path.splitext(file_path)[0] + ".wav"
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-i", file_path,
            "-ar", "16000",   # sample rate 16kHz
            "-ac", "1",       # mono
            wav_path
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"[Whisper] Converted to WAV: {wav_path}")
    except subprocess.CalledProcessError as e:
        print(f"[FFmpeg] Failed: {e.stderr.decode()}")
        return {"error": f"FFmpeg failed: {e.stderr.decode()}"}
    

    """
    Transcribe audio using OpenAI's whisper library.
    """
    try:
        model = whisper.load_model(whisper_model)  # tiny, base, small, medium, large
        result = model.transcribe(wav_path)
        print(result["text"])
        return result["text"]
    except Exception as e:
        print(f"[Whisper] Failed: {e}")
        return {"error": str(e)}
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
            print(f"[Cleanup] Deleted {wav_path}")
    

def run_whisper_transcription(file_path: str, whisper_model="tiny"):
    """
    Convert MP3 → WAV (16kHz mono) and transcribe with Whisper.
    """
    print(f"[Whisper] Starting transcription on {file_path}")

    # Convert MP3 to WAV
    wav_path = os.path.splitext(file_path)[0] + ".wav"
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-i", file_path,
            "-ar", "16000",   # sample rate 16kHz
            "-ac", "1",       # mono
            wav_path
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"[Whisper] Converted to WAV: {wav_path}")
    except subprocess.CalledProcessError as e:
        print(f"[FFmpeg] Failed: {e.stderr.decode()}")
        return {"error": f"FFmpeg failed: {e.stderr.decode()}"}

    # Load Whisper model and transcribe
    try:
        model = WhisperModel("base", device="cpu", compute_type="float32")

        #model = whisper.load_model(whisper_model)
        segments, info = model.transcribe(wav_path,language="en")
        transcript_parts = []
        for i, seg in enumerate(segments):
            print(seg)
            try:
                if seg.text:
                    print(seg.text)
                    #transcript_parts.append(seg.text.strip())
            except Exception as e:
                print(f"[Whisper Warning] Failed at segment {i}: {e}")
                continue
        

        transcript = " ".join(transcript_parts)

        print("[Whisper] Transcription finished successfully.")
        return transcript

        #return {"text": result["text"]}
    except Exception as e:
        print(f"[Whisper] Failed: {e}")
        return {"error": str(e)}
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
            print(f"[Cleanup] Deleted {wav_path}")

async def fetch_transcript_for_url(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        # capture either direct VTT TEXT or a captions.m3u8 URL
        loop = asyncio.get_event_loop()
        captions_future: asyncio.Future = loop.create_future()

        async def handle_response(response):
            if captions_future.done():
                return
            try:
                resp_url = (response.url or "").lower()
            except Exception:
                return

            # Prefer captions playlists (we'll stitch segments later)
            if resp_url.endswith(".m3u8") and "captions" in resp_url:
                if not captions_future.done():
                    captions_future.set_result(("m3u8", response.url))
                return

            # Otherwise accept any .vtt file content directly
            if ".vtt" in resp_url:
                try:
                    vtt_text = await response.text()
                    if not captions_future.done():
                        captions_future.set_result(("vtt", vtt_text))
                except Exception as e:
                    if not captions_future.done():
                        captions_future.set_exception(e)

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="load", timeout=45000)

            # strict platform routing (no generic fallback)
            if "granicus.com" in url:
                await handle_granicus_url(page)
            elif "viebit.com" in url:
                await handle_viebit_url(page)
            elif ".cablecast.tv" in url:
                await handle_cablecast_url(page)
            elif ".cvtv.org" in url:
                return await process_cvtv(url)
            else:
                raise ValueError("Unknown platform. Could not process URL.")

            # wait for either a .vtt payload or a captions.*.m3u8 URL
            kind, payload = await asyncio.wait_for(captions_future, timeout=15)

            if kind == "vtt":
                print("vtt is called")
                return parse_vtt(payload)

            if kind == "m3u8":
                print("m3u8 is called")
                stitched_vtt_text = await _stitch_vtt_from_m3u8(payload)
                return parse_vtt(stitched_vtt_text)

            raise ValueError("Unknown captions kind received")

        finally:
            await browser.close()

async def fetch_transcript_for_url_old(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})  # Set a standard viewport size
        page = await context.new_page()
        vtt_future = asyncio.Future()

        async def handle_response(response):
            if ".vtt" in response.url and not vtt_future.done():
                try: vtt_future.set_result(await response.text())
                except Exception as e:
                    if not vtt_future.done(): vtt_future.set_exception(e)
        
        page.on("response", handle_response)
        
        try:
            await page.goto(url, wait_until="load", timeout=45000)
            if "granicus.com" in url:
                await handle_granicus_url(page)
            elif "viebit.com" in url:
                await handle_viebit_url(page)
            else:
                raise ValueError("Unknown platform. Could not process URL.")
            
            vtt_content = await asyncio.wait_for(vtt_future, timeout=20)
            return parse_vtt(vtt_content)
        finally:
            await browser.close()
 