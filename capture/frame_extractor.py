"""Flash analysis utilities for WCAG 2.3.1 (Three Flashes or Below Threshold).

Extracts frames from observation video via ffmpeg and analyses
consecutive-frame luminance changes to detect hazardous flash rates.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

from config import FLASH_DETECTION_FPS

logger = logging.getLogger(__name__)

_ffmpeg_checked = False


def ensure_ffmpeg() -> bool:
    """Ensure ffmpeg is available, auto-installing if necessary.

    Returns True if ffmpeg is available after this call.
    """
    global _ffmpeg_checked
    if _ffmpeg_checked:
        return shutil.which("ffmpeg") is not None

    _ffmpeg_checked = True

    if shutil.which("ffmpeg"):
        logger.debug("ffmpeg: found on PATH")
        return True

    logger.info("ffmpeg not found — attempting auto-install...")

    # Try Playwright's bundled ffmpeg first (cross-platform)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "ffmpeg"],
            capture_output=True, timeout=120,
        )
        if result.returncode == 0 and shutil.which("ffmpeg"):
            logger.info("ffmpeg installed via Playwright")
            return True
    except Exception as e:
        logger.debug("Playwright ffmpeg install failed: %s", e)

    # Platform-specific fallbacks
    if sys.platform == "win32":
        # Try winget (rc=0 fresh install, rc=43 already installed)
        try:
            result = subprocess.run(
                ["winget", "install", "--id", "Gyan.FFmpeg",
                 "--accept-source-agreements", "--accept-package-agreements",
                 "--silent"],
                capture_output=True, timeout=300,
            )
            if result.returncode in (0, 43):
                if result.returncode == 0:
                    logger.info("ffmpeg installed via winget")
                # Find the binary in winget package dirs and add to PATH
                _add_winget_ffmpeg_to_path()
                if shutil.which("ffmpeg"):
                    return True
        except FileNotFoundError:
            logger.debug("winget not available")
        except Exception as e:
            logger.debug("winget ffmpeg install failed: %s", e)

        # Even if winget didn't run, try to find an existing install
        _add_winget_ffmpeg_to_path()
        if shutil.which("ffmpeg"):
            return True

        # Try choco
        try:
            result = subprocess.run(
                ["choco", "install", "ffmpeg", "-y", "--no-progress"],
                capture_output=True, timeout=300,
            )
            if result.returncode == 0 and shutil.which("ffmpeg"):
                logger.info("ffmpeg installed via chocolatey")
                return True
        except FileNotFoundError:
            logger.debug("chocolatey not available")
        except Exception as e:
            logger.debug("choco ffmpeg install failed: %s", e)

    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["brew", "install", "ffmpeg"],
                capture_output=True, timeout=300,
            )
            if result.returncode == 0 and shutil.which("ffmpeg"):
                logger.info("ffmpeg installed via homebrew")
                return True
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug("brew ffmpeg install failed: %s", e)

    else:  # Linux
        for cmd in [
            ["sudo", "apt-get", "install", "-y", "ffmpeg"],
            ["sudo", "dnf", "install", "-y", "ffmpeg"],
            ["sudo", "pacman", "-S", "--noconfirm", "ffmpeg"],
        ]:
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=300)
                if result.returncode == 0 and shutil.which("ffmpeg"):
                    logger.info("ffmpeg installed via %s", cmd[1])
                    return True
            except FileNotFoundError:
                continue
            except Exception:
                continue

    logger.warning("Could not auto-install ffmpeg — flash analysis will be skipped")
    return False


def _add_winget_ffmpeg_to_path() -> None:
    """Try to find winget-installed ffmpeg and add it to the current PATH."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    common_locations = [
        Path(local_app) / "Microsoft" / "WinGet" / "Links",
        Path(os.environ.get("PROGRAMFILES", "")) / "FFmpeg" / "bin",
    ]
    # Scan winget package dirs for ffmpeg bin folders (versioned paths)
    winget_pkgs = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
    if winget_pkgs.exists():
        for pkg_dir in winget_pkgs.iterdir():
            if "ffmpeg" in pkg_dir.name.lower():
                for bin_dir in pkg_dir.rglob("bin"):
                    if (bin_dir / "ffmpeg.exe").exists():
                        common_locations.insert(0, bin_dir)

    for loc in common_locations:
        if loc.exists() and (loc / "ffmpeg.exe").exists():
            os.environ["PATH"] = str(loc) + os.pathsep + os.environ.get("PATH", "")
            logger.info("Added %s to PATH for ffmpeg", loc)
            return

# WCAG threshold: more than 3 general flashes per second
MAX_FLASHES_PER_SECOND = 3

# Luminance change threshold (relative, 0-255 scale) to count as a flash
# A "general flash" is defined as a pair of opposing changes in relative
# luminance of 10% or more of the maximum relative luminance, where the
# darker image has a relative luminance below 0.80.
LUMINANCE_CHANGE_THRESHOLD = 25.5  # ~10% of 255

# WCAG 2.3.1 general flash: luminance change > 0.1 from dark content
# or > 20 cd/m² change for bright content
GENERAL_FLASH_THRESHOLD = 0.1


async def extract_frames(
    video_path: str,
    fps: float,
    output_dir: str,
) -> list[str]:
    """Extract frames from a video file using ffmpeg.

    Args:
        video_path: Path to the video file (e.g. webm from Playwright).
        fps: Frames per second to extract.
        output_dir: Directory to write extracted frame PNGs.

    Returns:
        Sorted list of extracted frame file paths.
    """
    if not ensure_ffmpeg():
        logger.warning("ffmpeg unavailable — skipping frame extraction")
        return []

    os.makedirs(output_dir, exist_ok=True)
    output_pattern = os.path.join(output_dir, "frame_%06d.png")

    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-vf", f"fps={fps}",
        "-q:v", "2",
        output_pattern,
        "-y",  # overwrite
        "-loglevel", "error",
    ]

    logger.info("Extracting frames at %.2f fps from %s", fps, video_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace").strip()
        logger.error("ffmpeg frame extraction failed (rc=%d): %s", proc.returncode, err_msg)
        return []

    # Collect sorted frame paths
    frames = sorted(
        str(p) for p in Path(output_dir).glob("frame_*.png")
    )
    logger.info("Extracted %d frames to %s", len(frames), output_dir)
    return frames


async def analyze_flash_rate(frames_dir: str) -> dict:
    """Analyse extracted frames for hazardous flash rates.

    Compares consecutive frame luminance to detect transitions
    that exceed the WCAG general-flash threshold (>3 flashes/s).

    Args:
        frames_dir: Directory containing ordered frame_*.png files.

    Returns:
        Dict with keys:
            has_violation (bool): True if >3 flashes/s detected.
            max_flashes_per_second (float): Peak flash rate found.
            flash_events (list[dict]): Details of each detected flash.
            total_frames (int): Number of frames analysed.
            fps_used (int): FPS that was used for extraction.
            general_flash_violations (int): Frame pairs with luminance delta > 0.1.
            max_luminance_delta (float): Largest luminance change observed (0-1).
            red_flash_events (list[dict]): Transitions involving saturated red.
            area_limitation_note (str): Note about flash area measurement limitation.
    """
    from PIL import Image

    frames = sorted(str(p) for p in Path(frames_dir).glob("frame_*.png"))
    result: dict[str, Any] = {
        "has_violation": False,
        "max_flashes_per_second": 0.0,
        "flash_events": [],
        "total_frames": len(frames),
        "fps_used": FLASH_DETECTION_FPS,
        "general_flash_violations": 0,
        "max_luminance_delta": 0.0,
        "red_flash_events": [],
        "area_limitation_note": (
            "Flash area could not be measured per-pixel. Results assume "
            "full-frame flash. WCAG 2.3.1 only applies to flashes exceeding "
            "21,824 sq pixels in any 10-degree visual field."
        ),
    }

    if len(frames) < 2:
        logger.info("Not enough frames for flash analysis (%d)", len(frames))
        return result

    # Precompute luminance values and RGB averages for all frames
    luminances: list[float] = []
    rgb_averages: list[tuple[float, float, float]] = []
    for frame_path in frames:
        try:
            lum = _compute_luminance(frame_path)
            luminances.append(lum)
        except Exception:
            logger.debug("Failed to compute luminance for %s", frame_path)
            luminances.append(0.0)
        try:
            rgb = _compute_rgb_averages(frame_path)
            rgb_averages.append(rgb)
        except Exception:
            logger.debug("Failed to compute RGB averages for %s", frame_path)
            rgb_averages.append((0.0, 0.0, 0.0))

    # Detect flash transitions (pairs of opposing luminance changes)
    flash_events: list[dict] = []
    red_flash_events: list[dict] = []
    general_flash_violations = 0
    max_luminance_delta = 0.0

    # A flash = luminance drops then rises (or vice-versa) beyond threshold
    # in consecutive frame pairs.
    prev_delta: float | None = None

    for i in range(1, len(luminances)):
        delta = luminances[i] - luminances[i - 1]
        abs_delta = abs(delta)

        # Track the normalised luminance delta (0-1 scale) for
        # WCAG general flash threshold comparison
        normalised_delta = abs_delta / 255.0
        max_luminance_delta = max(max_luminance_delta, normalised_delta)

        # WCAG 2.3.1 general flash: luminance change > 0.1
        if normalised_delta > GENERAL_FLASH_THRESHOLD:
            general_flash_violations += 1

        # Red flash check
        if _is_red_flash(rgb_averages[i - 1], rgb_averages[i]):
            red_flash_events.append({
                "frame_index": i,
                "frame_path": frames[i],
                "rgb_before": tuple(round(c, 4) for c in rgb_averages[i - 1]),
                "rgb_after": tuple(round(c, 4) for c in rgb_averages[i]),
                "timestamp_approx": round(i / FLASH_DETECTION_FPS, 3),
            })

        if abs_delta >= LUMINANCE_CHANGE_THRESHOLD:
            # Check if this is an opposing transition (flash)
            darker = min(luminances[i], luminances[i - 1])
            if darker < 0.80 * 255:  # darker image below 0.80 relative
                if prev_delta is not None and _is_opposing(prev_delta, delta):
                    flash_events.append({
                        "frame_index": i,
                        "frame_path": frames[i],
                        "luminance_before": round(luminances[i - 1], 2),
                        "luminance_after": round(luminances[i], 2),
                        "delta": round(delta, 2),
                        "timestamp_approx": round(i / FLASH_DETECTION_FPS, 3),
                    })

        if abs_delta >= LUMINANCE_CHANGE_THRESHOLD:
            prev_delta = delta
        else:
            prev_delta = None

    # Calculate peak flash rate within any 1-second sliding window
    max_per_second = 0.0
    if flash_events:
        timestamps = [e["timestamp_approx"] for e in flash_events]
        for t_start in timestamps:
            t_end = t_start + 1.0
            count = sum(1 for t in timestamps if t_start <= t < t_end)
            max_per_second = max(max_per_second, count)

    result["flash_events"] = flash_events
    result["max_flashes_per_second"] = max_per_second
    result["general_flash_violations"] = general_flash_violations
    result["max_luminance_delta"] = round(max_luminance_delta, 4)
    result["red_flash_events"] = red_flash_events
    result["has_violation"] = (
        max_per_second > MAX_FLASHES_PER_SECOND
        or len(red_flash_events) > 0
    )

    if result["has_violation"]:
        reasons = []
        if max_per_second > MAX_FLASHES_PER_SECOND:
            reasons.append(f"{max_per_second:.1f} flashes/s (threshold: {MAX_FLASHES_PER_SECOND})")
        if red_flash_events:
            reasons.append(f"{len(red_flash_events)} red flash event(s)")
        logger.warning(
            "Flash violation detected: %s", "; ".join(reasons),
        )
    else:
        logger.info(
            "Flash analysis passed: %.1f flashes/s, %d general flash delta(s), "
            "max delta=%.4f, %d red flash(es)",
            max_per_second, general_flash_violations,
            max_luminance_delta, len(red_flash_events),
        )

    return result


def _compute_luminance(frame_path: str) -> float:
    """Compute mean relative luminance of a frame image.

    Uses ITU-R BT.709 coefficients: L = 0.2126R + 0.7152G + 0.0722B
    Returns luminance on 0-255 scale.
    """
    from PIL import Image

    img = Image.open(frame_path).convert("RGB")
    arr = np.array(img, dtype=np.float64)
    # Weighted luminance per pixel, then mean across all pixels
    luminance = (
        0.2126 * arr[:, :, 0]
        + 0.7152 * arr[:, :, 1]
        + 0.0722 * arr[:, :, 2]
    )
    return float(np.mean(luminance))


def _is_opposing(delta_a: float, delta_b: float) -> bool:
    """Return True if two luminance deltas have opposing signs."""
    return (delta_a > 0 and delta_b < 0) or (delta_a < 0 and delta_b > 0)


def _compute_rgb_averages(frame_path: str) -> tuple[float, float, float]:
    """Compute mean R, G, B channel values for a frame, normalised to 0-1."""
    from PIL import Image

    img = Image.open(frame_path).convert("RGB")
    arr = np.array(img, dtype=np.float64) / 255.0
    r = float(np.mean(arr[:, :, 0]))
    g = float(np.mean(arr[:, :, 1]))
    b = float(np.mean(arr[:, :, 2]))
    return (r, g, b)


def _is_red_flash(frame_a_rgb: tuple[float, float, float],
                  frame_b_rgb: tuple[float, float, float]) -> bool:
    """Check if transition involves saturated red (WCAG red flash)."""
    r_a, g_a, b_a = frame_a_rgb
    r_b, g_b, b_b = frame_b_rgb
    # Red flash: red channel dominant and changes significantly
    is_saturated_a = r_a > 0.5 and r_a > (g_a + b_a)
    is_saturated_b = r_b > 0.5 and r_b > (g_b + b_b)
    return (is_saturated_a or is_saturated_b) and abs(r_a - r_b) > 0.2
