import os
import uuid
import base64
import json
import re
import hashlib
from io import BytesIO
from functools import wraps
import threading

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from flask import Flask, request, jsonify, render_template, send_from_directory, session
import requests
from PIL import Image

from config import (
    API_KEY, IMAGE_GPT_URL, NANO_IMAGE_URL, QUERY_URL, TMPFILES_UPLOAD_URL,
    UPLOAD_FOLDER, RESULTS_FOLDER, CONVERT_FOLDER, PANORAMA_FOLDER,
    MAX_CONTENT_LENGTH, VALID_SIZES, NANO_RESOLUTIONS, NANO_ASPECT_RATIOS,
    PANORAMA_IMAGES_DIR, COORDINATES_CSV,
    LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", uuid.uuid4().hex)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# ── User & Community Data ─────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
POSTS_FILE = os.path.join(DATA_DIR, "community_posts.json")
PROJECTS_FILE = os.path.join(DATA_DIR, "projects.json")
DATA_LOCK = threading.Lock()

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _hash_pw(password):
    return hashlib.sha256(password.encode()).hexdigest()

def _require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("username"):
            return jsonify({"error": "请先登录"}), 401
        return f(*args, **kwargs)
    return wrapper

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
    return render_template("sigs.html")


@app.route("/generate")
def generate_page():
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


@app.route("/assets/<path:subpath>")
def serve_assets(subpath):
    assets_dir = os.path.join(os.path.dirname(__file__), "assets")
    return send_from_directory(assets_dir, subpath)


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


# ── LLM Agent Session Management ──────────────────────────────
agent_sessions = {}

class AgentSession:
    def __init__(self):
        self.id = str(uuid.uuid4())
        self.panorama_url = ""
        self.messages = []       # OpenAI-format conversation history
        self.goal_fields = {"domain": None, "style": None, "elements": None, "atmosphere": None, "special": None}
        self.round = 0
        self.phase = "guiding"   # guiding | confirming | confirmed
        self.final_prompt = ""
        self.design_summary = ""

SYSTEM_PROMPT = """你是"小清"，一个校园设计助手 Agent。你的任务是帮助用户重新设计清华SIGS校园的某个场景。

## 当前场景
用户上传了一张 360° 全景图（equirectangular 投影）。请先仔细分析图中内容。

## 对话规则（五轮引导）
- 第1轮：描述当前场景（2-3句话，热情语气），给出3-5个天马行空的创意设计方向供选择。
- 第2-4轮：根据用户选择逐步深入，每轮问1-2个具体问题，给出新选项（3-5个）。
  - 第2轮：细化场景主题/功能定位
  - 第3轮：细化风格/色调
  - 第4轮：细化具体元素/氛围
- 第5轮：整合前四轮信息，生成设计概念总结并请求确认。格式严格如下：
  "以下是我根据你的想法整理的设计概念，你看一下是否符合你的想法：

  【设计概念总结】
  （用2-3段话总结设计概念，包括主题定位、风格色调、具体元素和氛围）

  如果没问题，请点击「确认并生成」；如果还想调整，请告诉我你想改什么。"
  同时给出两个选项：["确认并生成", "继续修改"]

- 如果用户选择「继续修改」，进入额外轮次：根据用户反馈调整概念，再次展示总结和确认按钮。

## GOAL 标记（重要：所有值必须用英文填写）
每轮回复末尾添加以下标记（JSON格式，单行，所有字段值用英文）：
[GOAL:{"domain":"scene theme in English","style":"design style in English","elements":"specific elements in English","atmosphere":"atmosphere in English","special":"special requirements in English"}]

- 第1轮填充 domain，其余字段为 null
- 第2-4轮逐步填充 style、elements、atmosphere（用英文描述）
- 第5轮填充 special 并完成所有字段（用英文描述）
- 尚未确定的字段填 null
- 此标记仅后端可见，玩家不会看到

## 限制
- 每轮只给3-5个选项（除了确认轮给2个）
- 用中文交流
- 选项以列表形式给出，每个选项一行
- 如果设计概念中需要添加学校名称元素，必须使用完整全称：「清华大学深圳国际研究生院」，不可缩写或省略
- 全景图是360° equirectangular投影，生成图像时必须确保画面左右边缘完全无缝衔接（元素、色彩、光影在左边缘和右边缘处完美对应），避免出现画面割裂"""


def _call_llm(messages, max_tokens=800):
    """Call OpenAI-compatible LLM API."""
    if not LLM_API_KEY:
        raise Exception("LLM not configured")
    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.8,
    }
    resp = requests.post(url, headers={
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json"
    }, json=payload, timeout=120)
    if resp.status_code != 200:
        raise Exception(f"LLM API error {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"]


def _parse_goal(text):
    """Extract GOAL tag from LLM response, return (clean_text, goal_dict)."""
    pattern = r'\[GOAL:\s*(\{[^]]+\})\s*\]'
    match = re.search(pattern, text)
    goal = {}
    if match:
        try:
            goal = json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
        clean = text[:match.start()] + text[match.end():]
        return clean.strip(), goal
    return text.strip(), goal


def _compile_final_prompt(goal_fields):
    """Compile goal_fields into English image generation prompt."""
    parts = ["Transform this equirectangular 360-degree panorama photo at Tsinghua SIGS campus."]
    mapping = [
        ("domain", "Domain focus: {}."),
        ("style", "Apply {} style."),
        ("elements", "Feature {}."),
        ("atmosphere", "Create {} atmosphere."),
        ("special", "Special requirements: {}."),
    ]
    for key, tmpl in mapping:
        val = goal_fields.get(key)
        if val:
            parts.append(tmpl.format(val))
    parts.append("Preserve the 360-degree equirectangular projection format. Maintain the original scene layout and perspective.")
    parts.append("CRITICAL: The left and right edges of this equirectangular image must connect seamlessly — ensure perfect edge-to-edge continuity in elements, colors, lighting, and shadows to avoid visible seams in the 360-degree panorama.")
    return " ".join(parts)


def _panorama_to_data_url(panorama_url):
    """Convert local panorama path to base64 data URL for LLM vision API."""
    local_path = os.path.join(os.path.dirname(__file__), panorama_url.lstrip("/"))
    if not os.path.exists(local_path):
        return None
    try:
        img = Image.open(local_path)
        # Compress if image is very large (> 5MB as base64)
        w, h = img.size
        max_dim = 2048
        if max(w, h) > max_dim:
            ratio = max_dim / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


@app.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    if not LLM_API_KEY:
        return jsonify({"error": "LLM not configured"}), 503

    data = request.get_json() or {}
    action = data.get("action", "start")
    msg = data.get("message", "").strip()
    session_id = data.get("session_id", "")
    panorama_url = data.get("panorama_url", "")

    # Load or create session
    sess = agent_sessions.get(session_id) if session_id else None
    if not sess:
        sess = AgentSession()
        agent_sessions[sess.id] = sess

    try:
        if action == "start":
            # Reset session for new design conversation
            sess.panorama_url = panorama_url
            sess.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            sess.goal_fields = {"domain": None, "style": None, "elements": None, "atmosphere": None, "special": None}
            sess.round = 1
            sess.phase = "guiding"
            sess.final_prompt = ""
            sess.design_summary = ""

            # Build multimodal user message
            data_url = _panorama_to_data_url(panorama_url)
            if not data_url:
                return jsonify({"error": "Panorama image not found or too large"}), 400

            user_content = [
                {"type": "text", "text": "请分析这张全景图，然后开始第1轮引导。"},
                {"type": "image_url", "image_url": {"url": data_url}}
            ]
            sess.messages.append({"role": "user", "content": user_content})
            llm_response = _call_llm(sess.messages)
            clean_text, goal = _parse_goal(llm_response)
            sess.messages.append({"role": "assistant", "content": llm_response})
            if goal:
                sess.goal_fields.update({k: v for k, v in goal.items() if v is not None})

            return jsonify({
                "session_id": sess.id, "agent_text": clean_text,
                "round": sess.round, "phase": sess.phase,
                "design_summary": None, "final_prompt": None,
                "options": _extract_options(clean_text)
            })

        elif action == "reply":
            if not msg:
                return jsonify({"error": "message is required"}), 400
            sess.round += 1
            round_hint = ""
            if sess.round == 5:
                round_hint = "\n[这是第5轮/最后一轮，请整合所有信息，输出设计概念总结并请求用户确认。]"
            elif sess.round > 5:
                round_hint = "\n[这是额外修改轮次，请根据用户反馈调整设计概念，然后再次展示总结和确认选项。]"
            sess.messages.append({"role": "user", "content": msg + round_hint})
            llm_response = _call_llm(sess.messages)
            clean_text, goal = _parse_goal(llm_response)
            sess.messages.append({"role": "assistant", "content": llm_response})
            if goal:
                sess.goal_fields.update({k: v for k, v in goal.items() if v is not None})

            # Check if this is the confirming phase (round 5+)
            is_confirming = sess.round >= 5 and "确认并生成" in clean_text
            if is_confirming:
                sess.phase = "confirming"
                sess.design_summary = clean_text

            return jsonify({
                "session_id": sess.id, "agent_text": clean_text,
                "round": sess.round, "phase": sess.phase,
                "design_summary": sess.design_summary if sess.phase == "confirming" else None,
                "final_prompt": None,
                "options": _extract_options(clean_text) if not is_confirming else ["确认并生成", "继续修改"]
            })

        elif action == "confirm":
            sess.phase = "confirmed"
            sess.final_prompt = _compile_final_prompt(sess.goal_fields)
            return jsonify({
                "session_id": sess.id, "agent_text": "好的，开始生成你的设计！",
                "round": sess.round, "phase": "confirmed",
                "design_summary": sess.design_summary,
                "final_prompt": sess.final_prompt,
                "options": []
            })

        elif action == "revise":
            sess.phase = "guiding"
            sess.round += 1
            feedback = msg or "我想调整一下设计概念"
            sess.messages.append({"role": "user", "content": f"[继续修改] {feedback}"})
            llm_response = _call_llm(sess.messages)
            clean_text, goal = _parse_goal(llm_response)
            sess.messages.append({"role": "assistant", "content": llm_response})
            if goal:
                sess.goal_fields.update({k: v for k, v in goal.items() if v is not None})

            # Check if revised response is confirming
            is_confirming = "确认并生成" in clean_text
            if is_confirming:
                sess.phase = "confirming"
                sess.design_summary = clean_text

            return jsonify({
                "session_id": sess.id, "agent_text": clean_text,
                "round": sess.round, "phase": sess.phase,
                "design_summary": sess.design_summary if sess.phase == "confirming" else None,
                "final_prompt": None,
                "options": _extract_options(clean_text) if not is_confirming else ["确认并生成", "继续修改"]
            })

        else:
            return jsonify({"error": f"Unknown action: {action}"}), 400

    except Exception as e:
        return jsonify({"error": f"Agent error: {str(e)}"}), 502


def _extract_options(text):
    """Simple option extraction from agent text - lines starting with numbers or bullet chars."""
    options = []
    for line in text.split("\n"):
        stripped = line.strip()
        # Match lines like "1. xxx", "2. xxx", "- xxx", "• xxx"
        if stripped and (stripped[0].isdigit() and "." in stripped[:4] or stripped[0] in "-•→►◎"):
            opt = stripped.split(".", 1)[-1].strip() if "." in stripped[:4] else stripped[1:].strip()
            if opt and len(opt) > 1:
                options.append(opt)
    # If no structured options found, use common sense splitting
    if not options:
        options = [o.strip() for o in text.replace("、", ",").replace("；", ";").split(",") if len(o.strip()) > 2]
    return options[:6]


@app.route("/sigs")
def sigs_page():
    return render_template("sigs.html")


# ── Auth Endpoints ────────────────────────────────────────────
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(username) < 2 or len(username) > 20:
        return jsonify({"error": "用户名需 2-20 个字符"}), 400
    if len(password) < 3:
        return jsonify({"error": "密码需至少 3 个字符"}), 400

    with DATA_LOCK:
        users = _load_json(USERS_FILE, {})
        if username in users:
            return jsonify({"error": "用户名已存在"}), 409
        users[username] = {"password": _hash_pw(password), "created_at": __import__("time").time()}
        _save_json(USERS_FILE, users)

    session["username"] = username
    return jsonify({"username": username, "message": "注册成功"})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    users = _load_json(USERS_FILE, {})
    user = users.get(username)
    if not user or user["password"] != _hash_pw(password):
        return jsonify({"error": "用户名或密码错误"}), 401

    session["username"] = username
    return jsonify({"username": username, "message": "登录成功"})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("username", None)
    return jsonify({"message": "已退出"})


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    if session.get("username"):
        users = _load_json(USERS_FILE, {})
        u = users.get(session["username"], {})
        return jsonify({"username": session["username"], "logged_in": True, "admin": u.get("admin", False)})
    return jsonify({"logged_in": False})


# ── Community Endpoints (server-side) ─────────────────────────
@app.route("/api/community/posts", methods=["GET"])
def community_posts():
    posts = _load_json(POSTS_FILE, [])
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 12, type=int)
    total = len(posts)
    start = (page - 1) * per_page
    items = posts[start:start + per_page]
    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, -(-total // per_page))
    })


@app.route("/api/community/posts", methods=["POST"])
@_require_auth
def community_create_post():
    data = request.get_json() or {}
    post = {
        "id": str(uuid.uuid4()),
        "author": session["username"],
        "prompt": data.get("prompt", ""),
        "image_url": data.get("image_url", ""),
        "original_pano": data.get("original_pano", ""),
        "point_id": data.get("point_id", ""),
        "selections": data.get("selections", {}),
        "timestamp": __import__("time").time(),
        "likes": 0,
    }
    with DATA_LOCK:
        posts = _load_json(POSTS_FILE, [])
        posts.insert(0, post)
        _save_json(POSTS_FILE, posts)
    return jsonify(post)


@app.route("/api/community/posts/<post_id>", methods=["PUT"])
@_require_auth
def community_update_post(post_id):
    data = request.get_json() or {}
    with DATA_LOCK:
        posts = _load_json(POSTS_FILE, [])
        for p in posts:
            if p["id"] == post_id:
                if p.get("author") != session["username"]:
                    return jsonify({"error": "只能编辑自己的帖子"}), 403
                if "image_url" in data:
                    p["image_url"] = data["image_url"]
                _save_json(POSTS_FILE, posts)
                return jsonify(p)
    return jsonify({"error": "帖子未找到"}), 404


@app.route("/api/community/posts/<post_id>/like", methods=["POST"])
@_require_auth
def community_like_post(post_id):
    with DATA_LOCK:
        posts = _load_json(POSTS_FILE, [])
        for p in posts:
            if p["id"] == post_id:
                p["likes"] = p.get("likes", 0) + 1
                _save_json(POSTS_FILE, posts)
                return jsonify({"likes": p["likes"]})
    return jsonify({"error": "Post not found"}), 404


@app.route("/api/community/posts/<post_id>/unlike", methods=["POST"])
@_require_auth
def community_unlike_post(post_id):
    with DATA_LOCK:
        posts = _load_json(POSTS_FILE, [])
        for p in posts:
            if p["id"] == post_id:
                p["likes"] = max(0, p.get("likes", 0) - 1)
                _save_json(POSTS_FILE, posts)
                return jsonify({"likes": p["likes"]})
    return jsonify({"error": "Post not found"}), 404


@app.route("/api/community/posts/<post_id>", methods=["DELETE"])
@_require_auth
def community_delete_post(post_id):
    users = _load_json(USERS_FILE, {})
    u = users.get(session["username"], {})
    if not u.get("admin"):
        return jsonify({"error": "需要管理员权限"}), 403
    with DATA_LOCK:
        posts = _load_json(POSTS_FILE, [])
        posts = [p for p in posts if p["id"] != post_id]
        _save_json(POSTS_FILE, posts)
    return jsonify({"message": "已删除"})


@app.route("/api/community/my-posts", methods=["GET"])
@_require_auth
def my_posts():
    posts = _load_json(POSTS_FILE, [])
    mine = [p for p in posts if p.get("author") == session["username"]]
    return jsonify(mine)


# ═══════════════════════════════════════════
#  Projects (background generation)
# ═══════════════════════════════════════════
@app.route("/api/projects", methods=["POST"])
@_require_auth
def create_project():
    prompt = request.form.get("prompt", "").strip()
    point_id = request.form.get("point_id", "")
    point_lat = request.form.get("point_lat", "")
    point_lon = request.form.get("point_lon", "")
    panorama_url = request.form.get("panorama_url", "")
    design_summary = request.form.get("design_summary", "")
    size = request.form.get("size", "auto")

    if not prompt:
        return jsonify({"error": "请输入提示词"}), 400
    if not API_KEY:
        return jsonify({"error": "未配置 API KEY"}), 500

    # Submit generation to 速创 API
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
    project_id = uuid.uuid4().hex[:12]
    project = {
        "id": project_id,
        "username": session["username"],
        "point_id": point_id,
        "point_lat": point_lat,
        "point_lon": point_lon,
        "panorama_url": panorama_url,
        "prompt": prompt,
        "design_summary": design_summary,
        "task_id": task_id,
        "status": "generating",
        "result_url": None,
        "created_at": __import__("datetime").datetime.now().isoformat()
    }

    with DATA_LOCK:
        projects = _load_json(PROJECTS_FILE, [])
        projects.insert(0, project)
        _save_json(PROJECTS_FILE, projects)

    return jsonify(project)


@app.route("/api/projects", methods=["GET"])
@_require_auth
def list_projects():
    projects = _load_json(PROJECTS_FILE, [])
    mine = [p for p in projects if p.get("username") == session["username"]]
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 8, type=int)
    total = len(mine)
    start = (page - 1) * per_page
    items = mine[start:start + per_page]
    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, -(-total // per_page))
    })


@app.route("/api/projects/<project_id>/status", methods=["GET"])
@_require_auth
def project_status(project_id):
    projects = _load_json(PROJECTS_FILE, [])
    project = None
    for p in projects:
        if p.get("id") == project_id and p.get("username") == session["username"]:
            project = p
            break
    if not project:
        return jsonify({"error": "项目未找到"}), 404

    if project.get("status") == "generating":
        try:
            result = _query_result(project["task_id"])
        except requests.RequestException:
            return jsonify({"status": "generating"})

        code = result.get("code")
        data = result.get("data", {})
        if code == 200:
            status = data.get("status", -1)
            result_list = data.get("result", [])
            if status == 2 and result_list:
                png_url = result_list[0]
                try:
                    jpg_url = _convert_to_jpg(png_url)
                except Exception:
                    jpg_url = png_url
                with DATA_LOCK:
                    projs = _load_json(PROJECTS_FILE, [])
                    for pp in projs:
                        if pp.get("id") == project_id:
                            pp["status"] = "done"
                            pp["result_url"] = jpg_url
                            _save_json(PROJECTS_FILE, projs)
                            break
                project["status"] = "done"
                project["result_url"] = jpg_url
            elif status == -1 and data.get("message"):
                # Failed
                with DATA_LOCK:
                    projs = _load_json(PROJECTS_FILE, [])
                    for pp in projs:
                        if pp.get("id") == project_id:
                            pp["status"] = "failed"
                            _save_json(PROJECTS_FILE, projs)
                            break
                project["status"] = "failed"

    return jsonify(project)


@app.route("/api/projects/<project_id>", methods=["DELETE"])
@_require_auth
def delete_project(project_id):
    with DATA_LOCK:
        projects = _load_json(PROJECTS_FILE, [])
        projects = [p for p in projects if not (p.get("id") == project_id and p.get("username") == session["username"])]
        _save_json(PROJECTS_FILE, projects)
    return jsonify({"message": "已删除"})


if __name__ == "__main__":
    print(f"API KEY configured: {'Yes' if API_KEY else 'No'}")
    print("Starting server at http://127.0.0.1:5000")
    app.run(debug=True, host="127.0.0.1", port=5000)
