from __future__ import annotations

import base64
import io
import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass

from .runtime import MoraError


@dataclass
class ScannerDevice:
    id: str
    label: str


def load_image(path: str):
    try:
        from PIL import Image
    except ImportError as exc:
        raise MoraError("image faculty requires Pillow (Arch: python-pillow)") from exc
    image = Image.open(path).convert("RGBA")
    image.load()
    return image


def list_scanners() -> list[ScannerDevice]:
    try:
        result = subprocess.run(["scanimage", "-L"], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise MoraError("scanner faculty requires SANE scanimage (Arch: sane)") from exc
    if result.returncode != 0:
        raise MoraError(result.stderr.strip() or "could not list scanners")
    devices: list[ScannerDevice] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line.startswith("device "):
            continue
        rest = line[len("device "):]
        if len(rest) < 3 or rest[0] not in {"'", "`"}:
            continue
        end = rest.find("'", 1)
        if end < 0:
            continue
        ident = rest[1:end].strip()
        desc = rest[end + 1:].strip()
        if desc.startswith("is a "):
            desc = desc[5:]
        if ident:
            devices.append(ScannerDevice(ident, desc or ident))
    return devices


def acquire_scan(device_id: str, dpi: int = 600):
    fd, path = tempfile.mkstemp(prefix="mora-scan-", suffix=".png")
    os.close(fd)
    try:
        try:
            result = subprocess.run(
                [
                    "scanimage", "-d", device_id, "--format=png", "--mode", "Color",
                    "--resolution", str(dpi), "-o", path,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise MoraError("scanner faculty requires SANE scanimage (Arch: sane)") from exc
        if result.returncode != 0:
            raise MoraError(result.stderr.strip() or "scanner returned an error")
        return load_image(path)
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def keyring_get(application: str, kind: str) -> str | None:
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", application, "type", kind],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MoraError("system-keyring faculty requires secret-tool (Arch: libsecret)") from exc
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def keyring_set(application: str, kind: str, value: str, label: str):
    try:
        result = subprocess.run(
            [
                "secret-tool", "store", f"--label={label}",
                "application", application, "type", kind,
            ],
            input=value,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MoraError("system-keyring faculty requires secret-tool (Arch: libsecret)") from exc
    if result.returncode != 0:
        raise MoraError(result.stderr.strip() or "could not store secret")


def point_distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def is_valid_quad(points) -> bool:
    if len(points) != 4:
        return False
    for i in range(4):
        if point_distance(points[i], points[(i + 1) % 4]) < 20.0:
            return False
    sign = 0.0
    for i in range(4):
        a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) < 1.0:
            return False
        current = 1.0 if cross > 0 else -1.0
        if sign == 0.0:
            sign = current
        elif sign != current:
            return False
    return True


def point_in_quad(point, points) -> bool:
    pos = neg = False
    for i in range(4):
        a, b = points[i], points[(i + 1) % 4]
        cross = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])
        pos = pos or cross > 0
        neg = neg or cross < 0
        if pos and neg:
            return False
    return True


def move_quad(points, dx, dy, image_w, image_h):
    out = [[float(x), float(y)] for x, y in points]
    min_x = min(p[0] for p in out)
    min_y = min(p[1] for p in out)
    max_x = max(p[0] for p in out)
    max_y = max(p[1] for p in out)
    dx = max(-min_x, min(float(image_w) - max_x, dx))
    dy = max(-min_y, min(float(image_h) - max_y, dy))
    return [[p[0] + dx, p[1] + dy] for p in out]


def _snap(value: float, targets: list[float], radius: float) -> float:
    best = value
    best_dist = radius + 1.0
    for target in targets:
        d = abs(value - target)
        if d <= radius and d < best_dist:
            best, best_dist = target, d
    return best


def _snap_quad(points, image_w, image_h, radius):
    if radius <= 0:
        return points
    candidate = [
        [_snap(p[0], [0.0, float(image_w)], radius), _snap(p[1], [0.0, float(image_h)], radius)]
        for p in points
    ]
    return candidate if is_valid_quad(candidate) else points


def move_corner(points, index, dx, dy, image_w, image_h, snap_radius=0.0):
    out = [[float(x), float(y)] for x, y in points]
    out[index][0] = max(0.0, min(float(image_w), out[index][0] + dx))
    out[index][1] = max(0.0, min(float(image_h), out[index][1] + dy))
    out = _snap_quad(out, image_w, image_h, snap_radius)
    return out if is_valid_quad(out) else points


def move_edge(points, a_index, b_index, dx, dy, image_w, image_h, snap_radius=0.0):
    out = [[float(x), float(y)] for x, y in points]
    a, b = out[a_index], out[b_index]
    ex, ey = b[0] - a[0], b[1] - a[1]
    length = math.hypot(ex, ey)
    if length < 1.0:
        return points
    nx, ny = -ey / length, ex / length
    offset = dx * nx + dy * ny
    for i in (a_index, b_index):
        out[i][0] = max(0.0, min(float(image_w), out[i][0] + nx * offset))
        out[i][1] = max(0.0, min(float(image_h), out[i][1] + ny * offset))
    out = _snap_quad(out, image_w, image_h, snap_radius)
    return out if is_valid_quad(out) else points


def frame_bounds(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def expand_quad(points, margin, image_w, image_h):
    if margin <= 0:
        return points
    cx = sum(p[0] for p in points) / 4.0
    cy = sum(p[1] for p in points) / 4.0
    out = []
    for p in points:
        vx, vy = p[0] - cx, p[1] - cy
        length = max(1.0, math.hypot(vx, vy))
        out.append([
            max(0.0, min(float(image_w), p[0] + vx / length * margin)),
            max(0.0, min(float(image_h), p[1] + vy / length * margin)),
        ])
    return out if is_valid_quad(out) else points


def perspective_crop(pil_image, points):
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise MoraError("geometry faculty requires OpenCV, NumPy and Pillow") from exc
    width = max(2, int(round(max(point_distance(points[0], points[1]), point_distance(points[3], points[2])))))
    height = max(2, int(round(max(point_distance(points[0], points[3]), point_distance(points[1], points[2])))))
    rgba = np.array(pil_image.convert("RGBA"))
    src = np.array(points, dtype=np.float32)
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(rgba, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT)
    return Image.fromarray(warped, "RGBA")


def detect_photos_openai(pil_image, api_key: str, model: str = "gpt-5.6-terra", margin: int = 0):
    try:
        import requests
    except ImportError as exc:
        raise MoraError("vision faculty requires requests") from exc
    try:
        from PIL import Image
    except ImportError as exc:
        raise MoraError("vision faculty requires Pillow") from exc

    preview = pil_image.copy()
    preview.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    preview.convert("RGB").save(buf, format="JPEG", quality=88)
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    point_schema = {
        "type": "object",
        "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 1000},
            "y": {"type": "number", "minimum": 0, "maximum": 1000},
        },
        "required": ["x", "y"],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "photos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"p1": point_schema, "p2": point_schema, "p3": point_schema, "p4": point_schema},
                    "required": ["p1", "p2", "p3", "p4"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["photos"],
        "additionalProperties": False,
    }
    prompt = (
        "Detect only the separate physical photographic prints lying on the scanned surface. "
        "Do not detect people, faces, objects, frames, buildings, trees, or other content inside a photograph. "
        "Do not return the scanner/page boundary. Include faded, low-contrast, black-and-white, damaged, and slightly rotated prints. "
        "For every physical print return its four outer corners. Coordinates are normalized 0..1000 relative to the exact submitted image. "
        "Order points clockwise; p1 is visually closest to top-left. Return no object unless it is itself a physical photo print."
    )
    body = {
        "model": model,
        "store": False,
        "max_output_tokens": 1500,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": data_url, "detail": "high"},
            ],
        }],
        "text": {"format": {"type": "json_schema", "name": "photo_print_layout", "strict": True, "schema": schema}},
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=45,
    )
    if not response.ok:
        message = response.text[:500]
        try:
            message = response.json().get("error", {}).get("message", message)
        except Exception:
            pass
        raise MoraError(f"OpenAI API returned {response.status_code}: {message}")
    payload = response.json()
    output_text = payload.get("output_text")
    if output_text is None:
        for item in payload.get("output", []):
            for item_content in item.get("content", []):
                if item_content.get("type") == "output_text":
                    output_text = item_content.get("text")
                    break
    if output_text is None:
        raise MoraError("OpenAI response did not contain output_text")
    layout = json.loads(output_text)
    width, height = pil_image.size
    frames = []
    for photo in layout.get("photos", []):
        points = []
        for key in ("p1", "p2", "p3", "p4"):
            p = photo[key]
            points.append([
                max(0.0, min(1000.0, float(p["x"]))) / 1000.0 * width,
                max(0.0, min(1000.0, float(p["y"]))) / 1000.0 * height,
            ])
        left, top, right, bottom = frame_bounds(points)
        bw, bh = right - left, bottom - top
        area = (bw * bh) / max(1.0, float(width * height))
        if bw < min(width, height) * 0.04 or bh < min(width, height) * 0.04 or not (0.003 <= area <= 0.80):
            continue
        edge = min(width, height) * 0.02
        touched = int(left <= edge) + int(top <= edge) + int(right >= width - edge) + int(bottom >= height - edge)
        if touched >= 3:
            continue
        frames.append({"corners": expand_quad(points, margin, width, height)})

    frames.sort(key=lambda frame: (frame_bounds(frame["corners"])[1] // 40, frame_bounds(frame["corners"])[0]))
    kept = []
    for frame in frames:
        duplicate = False
        a = frame_bounds(frame["corners"])
        for other in kept:
            b = frame_bounds(other["corners"])
            left, top = max(a[0], b[0]), max(a[1], b[1])
            right, bottom = min(a[2], b[2]), min(a[3], b[3])
            if right <= left or bottom <= top:
                continue
            inter = (right - left) * (bottom - top)
            area_a = (a[2] - a[0]) * (a[3] - a[1])
            area_b = (b[2] - b[0]) * (b[3] - b[1])
            if inter / max(1.0, min(area_a, area_b)) > 0.85:
                duplicate = True
                break
        if not duplicate:
            kept.append(frame)
    return kept
