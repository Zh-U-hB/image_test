import os
import uuid
import base64
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, render_template, send_from_directory
import requests
from PIL import Image

from config import (
    API_KEY, IMAGE_GPT_URL, NANO_IMAGE_URL, QUERY_URL, TMPFILES_UPLOAD_URL,
    UPLOAD_FOLDER, RESULTS_FOLDER, CONVERT_FOLDER, PANORAMA_FOLDER,
    MAX_CONTENT_LENGTH, VALID_SIZES, NANO_RESOLUTIONS, NANO_ASPECT_RATIOS,
    PANORAMA_IMAGES_DIR, COORDINATES_CSV
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESULTS_FOLDER, exist_ok=True)
os.makedirs(CONVERT_FOLDER, exist_ok=True)
os.makedirs(PANORAMA_FOLDER, exist_ok=True)
os.makedirs(os.path.join(os.path.dirname(__file__), "templates"), exist_ok=True)

MIME_MAP = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def _upload_to_hosting(image_data, filename):
    """Upload image to tmpfiles.org, return public download URL."""
    ext = os.path.splitext(filename)[1].lower() if filename else ".png"
    content_type = MIME_MAP.get(ext, "image/png")
    resp = requests.post(TMPFILES_UPLOAD_URL,
                         files={"file": (filename or "image.png", image_data, content_type)},
                         timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") != "success":
        raise Exception("图床上传失败: " + str(result))
    # Convert http://tmpfiles.org/xxx/file.png -> https://tmpfiles.org/dl/xxx/file.png
    raw_url = result["data"]["url"]
    dl_url = raw_url.replace("http://tmpfiles.org/", "https://tmpfiles.org/dl/")
    return dl_url


def _submit_task(prompt, size, image_urls=None):
    payload = {"prompt": prompt}
    if size and size != "auto":
        payload["size"] = size
    if image_urls:
        payload["urls"] = image_urls

    resp = requests.post(IMAGE_GPT_URL,
                         headers={"Authorization": API_KEY, "Content-Type": "application/json"},
                         json=payload, timeout=30)
    return resp.json()


def _convert_to_jpg(image_url):
    """Download image from URL and convert to JPG. Returns local JPG URL."""
    try:
        resp = requests.get(image_url, timeout=30)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content))
        fmt = img.format or ""

        filename = f"{uuid.uuid4().hex}.jpg"
        filepath = os.path.join(CONVERT_FOLDER, filename)

        if fmt == "JPEG":
            with open(filepath, "wb") as f:
                f.write(resp.content)
            return f"/static/converted/{filename}"

        if img.mode in ("RGBA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        img.save(filepath, "JPEG", quality=92)
        return f"/static/converted/{filename}"
    except Exception:
        return None


def _query_result(task_id):
    resp = requests.get(QUERY_URL, headers={"Authorization": API_KEY},
                        params={"id": task_id}, timeout=30)
    return resp.json()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload_image():
    if "file" not in request.files:
        return jsonify({"error": "未选择文件"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "未选择文件"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        return jsonify({"error": f"不支持的文件格式: {ext}"}), 400

    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    # convert to base64 data URL
    with open(filepath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
    data_url = f"data:{mime_map.get(ext, 'image/jpeg')};base64,{b64}"

    return jsonify({"filename": filename, "data_url": data_url})


@app.route("/api/generate", methods=["POST"])
def generate_image():
    if not API_KEY:
        return jsonify({"error": "未配置 API KEY，请设置 SUCHUANG_API_KEY 环境变量"}), 500

    prompt = request.form.get("prompt", "").strip()
    size = request.form.get("size", "auto")

    if not prompt:
        return jsonify({"error": "请输入提示词"}), 400
    if size not in VALID_SIZES:
        return jsonify({"error": f"不支持的尺寸: {size}"}), 400

    image_urls = None
    image_file = request.files.get("file")
    if image_file and image_file.filename:
        image_data = image_file.read()
        try:
            public_url = _upload_to_hosting(image_data, image_file.filename)
            image_urls = [public_url]
        except requests.RequestException as e:
            return jsonify({"error": f"图床上传失败: {str(e)}"}), 500

    try:
        result = _submit_task(prompt, size, image_urls)
    except requests.RequestException as e:
        return jsonify({"error": f"API请求失败: {str(e)}"}), 500

    if result.get("code") != 200:
        return jsonify({"error": result.get("msg", "提交任务失败")}), 500

    task_id = result["data"]["id"]

    return jsonify({"task_id": task_id, "message": "任务已提交"})


@app.route("/api/result/<task_id>", methods=["GET"])
def get_result(task_id):
    if not API_KEY:
        return jsonify({"error": "未配置 API KEY"}), 500

    try:
        result = _query_result(task_id)
    except requests.RequestException as e:
        return jsonify({"error": f"查询请求失败: {str(e)}"}), 500

    code = result.get("code")
    data = result.get("data", {})

    if code != 200:
        return jsonify({"status": "error", "error": result.get("msg", "查询失败")}), 500

    status = data.get("status", -1)
    result_list = data.get("result", [])
    message = data.get("message", "")

    if status == 2 and result_list:
        png_url = result_list[0]
        jpg_url = _convert_to_jpg(png_url)
        return jsonify({
            "status": "success",
            "image_url": jpg_url or png_url,
            "original_url": png_url,
            "all_results": result_list,
            "raw_data": data
        })

    if status == 3:
        return jsonify({"status": "failed", "message": message or "生成失败"})

    return jsonify({"status": "processing", "message": message or "任务处理中..."})


@app.route("/static/<path:subpath>")
def serve_static(subpath):
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    return send_from_directory(static_dir, subpath)


@app.route("/convert")
def convert_page():
    return render_template("convert.html")


@app.route("/api/convert", methods=["POST"])
def convert_image():
    data = request.get_json() or {}
    image_url = data.get("url", "").strip()
    if not image_url:
        return jsonify({"error": "请提供图片URL"}), 400

    try:
        resp = requests.get(image_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"下载图片失败: {str(e)}"}), 500

    img_bytes = resp.content
    img = Image.open(BytesIO(img_bytes))
    original_format = img.format or ""

    if original_format == "JPEG":
        filename = f"{uuid.uuid4().hex}.jpg"
        filepath = os.path.join(CONVERT_FOLDER, filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)
        return jsonify({
            "converted": False,
            "original_format": "JPEG",
            "image_url": f"/static/converted/{filename}",
            "message": "原图已是JPG格式，无需转换"
        })

    if img.mode in ("RGBA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    filename = f"{uuid.uuid4().hex}.jpg"
    filepath = os.path.join(CONVERT_FOLDER, filename)
    img.save(filepath, "JPEG", quality=92)
    file_size = os.path.getsize(filepath)

    return jsonify({
        "converted": True,
        "original_format": original_format,
        "image_url": f"/static/converted/{filename}",
        "file_size_kb": round(file_size / 1024, 1),
        "message": f"已从{original_format}转换为JPG"
    })


@app.route("/panorama")
def panorama_page():
    return render_template("panorama.html")


@app.route("/api/panorama/upload", methods=["POST"])
def upload_panorama():
    if "file" not in request.files:
        return jsonify({"error": "未选择文件"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "未选择文件"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        return jsonify({"error": f"不支持的文件格式: {ext}"}), 400

    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(PANORAMA_FOLDER, filename)
    file.save(filepath)

    return jsonify({"image_url": f"/static/panorama/{filename}", "filename": filename})


# ── NanoBanana2 4K module ─────────────────────────────────────
@app.route("/nano")
def nano_page():
    return render_template("nano.html")


@app.route("/api/nano/generate", methods=["POST"])
def nano_generate():
    if not API_KEY:
        return jsonify({"error": "未配置 API KEY"}), 500

    prompt = request.form.get("prompt", "").strip()
    resolution = request.form.get("resolution", "4K")
    aspect_ratio = request.form.get("aspect_ratio", "auto")

    if not prompt:
        return jsonify({"error": "请输入提示词"}), 400
    if resolution not in NANO_RESOLUTIONS:
        return jsonify({"error": f"不支持的分辨率: {resolution}"}), 400
    if aspect_ratio not in NANO_ASPECT_RATIOS:
        return jsonify({"error": f"不支持的比例: {aspect_ratio}"}), 400

    image_urls = None
    image_file = request.files.get("file")
    if image_file and image_file.filename:
        image_data = image_file.read()
        try:
            public_url = _upload_to_hosting(image_data, image_file.filename)
            image_urls = [public_url]
        except requests.RequestException as e:
            return jsonify({"error": f"图床上传失败: {str(e)}"}), 500

    payload = {"prompt": prompt, "size": resolution, "aspectRatio": aspect_ratio}
    if image_urls:
        payload["urls"] = image_urls

    try:
        resp = requests.post(NANO_IMAGE_URL,
                             headers={"Authorization": API_KEY, "Content-Type": "application/json"},
                             json=payload, timeout=30)
        result = resp.json()
    except requests.RequestException as e:
        return jsonify({"error": f"API请求失败: {str(e)}"}), 500

    if result.get("code") != 200:
        return jsonify({"error": result.get("msg", "提交任务失败")}), 500

    return jsonify({"task_id": result["data"]["id"], "message": "任务已提交"})


@app.route("/api/nano/result/<task_id>", methods=["GET"])
def nano_result(task_id):
    if not API_KEY:
        return jsonify({"error": "未配置 API KEY"}), 500

    try:
        result = _query_result(task_id)
    except requests.RequestException as e:
        return jsonify({"error": f"查询失败: {str(e)}"}), 500

    code = result.get("code")
    data = result.get("data", {})

    if code != 200:
        return jsonify({"status": "error", "error": result.get("msg", "查询失败")}), 500

    status = data.get("status", -1)
    result_list = data.get("result", [])
    message = data.get("message", "")

    if status == 2 and result_list:
        raw_url = result_list[0]
        jpg_url = _convert_to_jpg(raw_url)
        return jsonify({
            "status": "success",
            "image_url": jpg_url or raw_url,
            "original_url": raw_url,
            "resolution": data.get("request", {}).get("size", "4K")
        })

    if status == 3:
        return jsonify({"status": "failed", "message": message or "生成失败"})

    return jsonify({"status": "processing", "message": message or "任务处理中..."})


# ── Campus Map module ─────────────────────────────────────────
@app.route("/map")
def map_page():
    return render_template("map.html")


@app.route("/api/map/points")
def map_points():
    points = []
    if not os.path.exists(COORDINATES_CSV):
        return jsonify({"error": "CSV file not found"}), 500
    try:
        with open(COORDINATES_CSV, "r", encoding="utf-8") as f:
            header = f.readline()
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 4:
                    continue
                uuid_raw = parts[0].strip()
                lat = parts[1].strip()
                lon = parts[2].strip()
                if not lat or not lon:
                    continue
                # uuid format: SET_NUM → folder=panorama_images_SET, file=NUM.jpg
                if "_" not in uuid_raw:
                    continue
                set_id, num = uuid_raw.rsplit("_", 1)
                folder = f"panorama_images_{set_id}"
                filename = f"{num}.jpg"
                filepath = os.path.join(PANORAMA_IMAGES_DIR, folder, filename)
                if not os.path.exists(filepath):
                    continue
                points.append({
                    "id": uuid_raw,
                    "lat": float(lat),
                    "lon": float(lon),
                    "image_url": f"/panorama_images/{folder}/{filename}"
                })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(points)


@app.route("/panorama_images/<path:subpath>")
def serve_panorama_images(subpath):
    return send_from_directory(PANORAMA_IMAGES_DIR, subpath)


@app.route("/sigs")
def sigs_page():
    return render_template("sigs.html")


if __name__ == "__main__":
    print(f"API KEY configured: {'Yes' if API_KEY else 'No'}")
    print("Starting server at http://127.0.0.1:5000")
    app.run(debug=True, host="127.0.0.1", port=5000)
