#!/usr/bin/env python3
"""
VidSnatch MCP Streamable HTTP Server

Proper MCP server using the official 'mcp' Python SDK with Streamable HTTP transport.
Designed for cloud deployment (Render.com free tier).

Features:
- All original VidSnatch MCP tools
- New trim_and_serve tool for cloud workflow (returns download URL)
- Temporary file serving endpoint for cross-MCP file transfer
- Auto-cleanup of temporary files
- Health endpoint for keep-alive pings
"""

import os
import sys
import json
import time
import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, FileResponse, Response
from starlette.requests import Request

from .mcp_config import load_config, ensure_download_directory
from .mcp_tools import MCPTools

# --- Configuration ---
PORT = int(os.environ.get("PORT", 10000))
HOST = "0.0.0.0"
BASE_URL = os.environ.get("BASE_URL", "")  # Set to Render URL in production
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/vidsnatch")
CLEANUP_MAX_AGE_SECONDS = 1800  # 30 minutes

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("vidsnatch-mcp-streamable")

# --- Initialize VidSnatch tools ---
config = load_config()
# Override download directory for cloud deployment
config["download_directory"] = DOWNLOAD_DIR
ensure_download_directory(config)
tools = MCPTools(config, logger)

# --- Track temporary files ---
# Maps filename -> {"created": timestamp, "path": filepath}
temp_files: dict = {}

# --- Initialize MCP Server ---
mcp = FastMCP(
    "vidsnatch",
    stateless_http=True,
)

# =====================================================================
# MCP TOOLS - Register all VidSnatch tools
# =====================================================================

@mcp.tool()
def get_video_info(url: str) -> str:
    """
    Get detailed information about a YouTube video including title, duration,
    and available formats.

    Use this tool to understand video content before processing. For long videos
    where users want specific segments, consider following up with download_transcript
    to get timestamped content that can help locate specific topics or discussions.

    Args:
        url: YouTube video URL or video ID

    Returns:
        JSON string containing video information including title, duration, formats, etc.
    """
    return tools.get_video_info(url)


@mcp.tool()
def search_youtube(query: str, max_results: int = 10) -> str:
    """
    Search YouTube for videos matching a query.

    Args:
        query: Search query string
        max_results: Maximum number of results to return (default 10)

    Returns:
        JSON string with list of matching videos including titles, URLs, etc.
    """
    return tools.search_youtube(query, max_results)


@mcp.tool()
def download_video(
    url: str,
    quality: str = "highest",
    resolution: Optional[str] = None
) -> str:
    """
    Download a YouTube video to the cloud server's temporary directory.

    Args:
        url: YouTube video URL or video ID
        quality: Video quality preference ("highest", "lowest", or specific like "720p")
        resolution: Specific resolution (e.g., "1080p", "720p") - overrides quality

    Returns:
        JSON string with download status and file path on the server
    """
    return tools.download_video(url, quality, resolution)


@mcp.tool()
def download_audio(
    url: str,
    quality: str = "highest",
    format: str = "mp3"
) -> str:
    """
    Download audio from a YouTube video.

    Args:
        url: YouTube video URL or video ID
        quality: Audio quality preference
        format: Audio format ("mp3", "m4a", "wav")

    Returns:
        JSON string with download status and file path
    """
    return tools.download_audio(url, quality, format)


@mcp.tool()
def download_transcript(
    url: str,
    language: str = "en"
) -> str:
    """
    Download the transcript of a YouTube video with timestamps.

    Useful for locating specific topics or discussions within a video before trimming.

    Args:
        url: YouTube video URL or video ID
        language: Language code for transcript (default "en")

    Returns:
        JSON string with transcript text and timestamps
    """
    return tools.download_transcript(url, language)


@mcp.tool()
def trim_video(
    url: str,
    start_time: str,
    end_time: str,
    quality: str = "highest",
    output_filename: Optional[str] = None
) -> str:
    """
    Download and trim a specific segment of a YouTube video.

    Args:
        url: YouTube video URL or video ID
        start_time: Start timestamp (format: "HH:MM:SS" or "MM:SS" or seconds)
        end_time: End timestamp (format: "HH:MM:SS" or "MM:SS" or seconds)
        quality: Video quality preference
        output_filename: Optional custom output filename

    Returns:
        JSON string with trim status and file path on the server
    """
    return tools.trim_video(url, start_time, end_time, quality, output_filename)


@mcp.tool()
def stitch_clips(
    file_paths: list[str],
    output_filename: Optional[str] = None
) -> str:
    """
    Join multiple video clips into a single compilation video.

    Args:
        file_paths: List of file paths to video clips to stitch together
        output_filename: Optional custom output filename for the stitched result

    Returns:
        JSON string with stitch status and output file path
    """
    return tools.stitch_clips(file_paths, output_filename)


@mcp.tool()
def trim_and_serve(
    url: str,
    start_time: str,
    end_time: str,
    segment_number: int,
    quality: str = "highest"
) -> str:
    """
    Download a YouTube video, trim it to exact timestamps, and return a temporary
    download URL for the trimmed clip.

    THIS IS THE PRIMARY TOOL FOR THE CLOUD WORKFLOW.

    The returned download URL can be passed to Composio's Google Drive upload tool
    to upload the clip directly to Google Drive without saving anything locally.

    File naming: Segment{segment_number}-yt.mp4

    After the file has been uploaded to Google Drive, call the cleanup_temp_file
    tool to delete the temporary file from the cloud server.

    Args:
        url: YouTube video URL or video ID
        start_time: Start timestamp (format: "HH:MM:SS" or "MM:SS" or seconds)
        end_time: End timestamp (format: "HH:MM:SS" or "MM:SS" or seconds)
        segment_number: Segment number for file naming (1, 2, 3, etc.)
        quality: Video quality preference (default "highest")

    Returns:
        JSON string with:
        - status: "success" or "error"
        - filename: The clip filename (e.g., "Segment1-yt.mp4")
        - download_url: Temporary URL to download the clip
        - file_size_mb: Size of the trimmed clip in MB
        - message: Human-readable status message
    """
    try:
        filename = f"Segment{segment_number}-yt.mp4"
        output_path = os.path.join(DOWNLOAD_DIR, filename)

        # Check if file already exists (don't overwrite)
        if os.path.exists(output_path):
            return json.dumps({
                "status": "error",
                "message": f"{filename} already exists. Use a different segment number or clean up first.",
                "filename": filename
            })

        logger.info(f"trim_and_serve: Trimming {url} [{start_time} -> {end_time}] as {filename}")

        # Use VidSnatch's trim_video with custom output filename
        result_json = tools.trim_video(url, start_time, end_time, quality, filename)
        result = json.loads(result_json)

        if result.get("status") != "success":
            return json.dumps({
                "status": "error",
                "message": f"Trim failed: {result.get('error', 'Unknown error')}",
                "filename": filename
            })

        # Get the actual file path from the trim result
        file_path = result.get("file_path", output_path)

        # If the file was saved with a different name, rename it
        if os.path.exists(file_path) and file_path != output_path:
            os.rename(file_path, output_path)
            file_path = output_path
        elif not os.path.exists(file_path) and os.path.exists(output_path):
            file_path = output_path

        if not os.path.exists(file_path):
            # Check if the file ended up somewhere in the download dir
            for f in Path(DOWNLOAD_DIR).glob("*.mp4"):
                if f.name != filename:
                    os.rename(str(f), output_path)
                    file_path = output_path
                    break

        if not os.path.exists(file_path):
            return json.dumps({
                "status": "error",
                "message": "Trim appeared to succeed but output file not found",
                "filename": filename
            })

        file_size_mb = round(os.path.getsize(file_path) / (1024 * 1024), 2)

        # Construct download URL
        base = BASE_URL.rstrip("/") if BASE_URL else f"https://vidsnatch-mcp.onrender.com"
        download_url = f"{base}/download/{filename}"

        # Track the temp file
        temp_files[filename] = {
            "created": time.time(),
            "path": file_path
        }

        logger.info(f"trim_and_serve: Success - {filename} ({file_size_mb} MB) at {download_url}")

        return json.dumps({
            "status": "success",
            "filename": filename,
            "download_url": download_url,
            "file_size_mb": file_size_mb,
            "message": f"Trimmed clip ready. Pass the download_url to Composio Google Drive upload to save to Drive. After upload, call cleanup_temp_file with filename='{filename}' to delete the temporary file."
        })

    except Exception as e:
        logger.error(f"trim_and_serve error: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "message": str(e),
            "filename": f"Segment{segment_number}-yt.mp4"
        })


@mcp.tool()
def cleanup_temp_file(filename: str) -> str:
    """
    Delete a temporary file from the cloud server after it has been uploaded
    to Google Drive.

    Call this after confirming the Google Drive upload was successful.

    Args:
        filename: The filename to delete (e.g., "Segment1-yt.mp4")

    Returns:
        JSON string with cleanup status
    """
    try:
        file_path = os.path.join(DOWNLOAD_DIR, filename)

        # Also clean up any source video files
        cleaned = []

        if os.path.exists(file_path):
            os.remove(file_path)
            cleaned.append(filename)

        # Remove from tracking
        temp_files.pop(filename, None)

        # Clean up any other temp files in the download directory
        # (source videos from the download step)
        for f in Path(DOWNLOAD_DIR).iterdir():
            if f.is_file() and f.suffix in ('.mp4', '.webm', '.mkv', '.part', '.temp'):
                # Don't delete files that are tracked and belong to other segments
                if f.name not in temp_files:
                    try:
                        f.unlink()
                        cleaned.append(f.name)
                    except OSError:
                        pass

        logger.info(f"cleanup_temp_file: Cleaned up {cleaned}")

        return json.dumps({
            "status": "success",
            "cleaned_files": cleaned,
            "message": f"Deleted {len(cleaned)} temporary file(s) from cloud server"
        })

    except Exception as e:
        logger.error(f"cleanup_temp_file error: {e}")
        return json.dumps({
            "status": "error",
            "message": str(e)
        })


@mcp.tool()
def list_temp_files() -> str:
    """
    List all temporary files currently on the cloud server.

    Returns:
        JSON string with list of temporary files and their sizes
    """
    try:
        files = []
        download_path = Path(DOWNLOAD_DIR)
        if download_path.exists():
            for f in download_path.iterdir():
                if f.is_file():
                    files.append({
                        "filename": f.name,
                        "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
                        "age_seconds": round(time.time() - f.stat().st_mtime)
                    })

        return json.dumps({
            "status": "success",
            "files": files,
            "total_files": len(files)
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# =====================================================================
# HTTP ENDPOINTS - File serving, cleanup, health
# =====================================================================

async def health_endpoint(request: Request) -> JSONResponse:
    """Health check endpoint for keep-alive pings."""
    return JSONResponse({
        "status": "healthy",
        "service": "VidSnatch MCP",
        "version": "1.0.0",
        "temp_files": len(temp_files)
    })


async def download_endpoint(request: Request) -> Response:
    """Serve a temporary file for download (used by Composio to fetch clips)."""
    filename = request.path_params["filename"]
    file_path = os.path.join(DOWNLOAD_DIR, filename)

    # Security: only serve files from our download directory
    if not os.path.abspath(file_path).startswith(os.path.abspath(DOWNLOAD_DIR)):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    if not os.path.exists(file_path):
        return JSONResponse({"error": f"File '{filename}' not found"}, status_code=404)

    logger.info(f"Serving file: {filename}")
    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=filename
    )


async def root_endpoint(request: Request) -> JSONResponse:
    """Root endpoint with server info."""
    return JSONResponse({
        "name": "VidSnatch MCP Streamable HTTP Server",
        "version": "1.0.0",
        "description": "Cloud-deployed MCP server for YouTube video processing",
        "mcp_endpoint": "/mcp",
        "transport": "streamable-http",
        "tools": [
            "get_video_info", "search_youtube", "download_video",
            "download_audio", "download_transcript", "trim_video",
            "stitch_clips", "trim_and_serve", "cleanup_temp_file",
            "list_temp_files"
        ]
    })


# =====================================================================
# BACKGROUND CLEANUP TASK
# =====================================================================

def cleanup_old_files():
    """Background thread that periodically cleans up old temporary files."""
    while True:
        try:
            time.sleep(300)  # Check every 5 minutes
            now = time.time()
            download_path = Path(DOWNLOAD_DIR)
            if download_path.exists():
                for f in download_path.iterdir():
                    if f.is_file():
                        age = now - f.stat().st_mtime
                        if age > CLEANUP_MAX_AGE_SECONDS:
                            logger.info(f"Auto-cleanup: Deleting {f.name} (age: {int(age)}s)")
                            f.unlink()
                            temp_files.pop(f.name, None)
        except Exception as e:
            logger.error(f"Cleanup thread error: {e}")


# =====================================================================
# APP ASSEMBLY & ENTRY POINT
# =====================================================================

def create_app() -> Starlette:
    """
    Create the combined Starlette application with both MCP and custom routes.
    """
    # Get the MCP ASGI app
    mcp_app = mcp.streamable_http_app()

    # Create the main app with custom routes + mount MCP
    routes = [
        Route("/", root_endpoint),
        Route("/health", health_endpoint),
        Route("/download/{filename}", download_endpoint),
        Mount("/", app=mcp_app),
    ]

    app = Starlette(routes=routes)
    return app


def main():
    """Entry point for the server."""
    import uvicorn

    # Ensure download directory exists
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Start background cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
    cleanup_thread.start()

    logger.info(f"Starting VidSnatch MCP Streamable HTTP Server on {HOST}:{PORT}")
    logger.info(f"Download directory: {DOWNLOAD_DIR}")
    logger.info(f"Base URL: {BASE_URL or '(auto-detect)'}")
    logger.info(f"MCP endpoint: http://{HOST}:{PORT}/mcp")
    logger.info(f"Health endpoint: http://{HOST}:{PORT}/health")

    uvicorn.run(
        create_app(),
        host=HOST,
        port=PORT,
        log_level="info"
    )


if __name__ == "__main__":
    main()
