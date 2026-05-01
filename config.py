import os

API_KEY = os.environ.get("SUCHUANG_API_KEY", "")
API_BASE = "https://api.wuyinkeji.com"

IMAGE_GPT_URL = f"{API_BASE}/api/async/image_gpt"
NANO_IMAGE_URL = f"{API_BASE}/api/async/image_nanoBanana2"
QUERY_URL = f"{API_BASE}/api/async/detail"

# tmpfiles.org - free anonymous file hosting (files kept ~1 hour)
TMPFILES_UPLOAD_URL = "https://tmpfiles.org/api/v1/upload"

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
RESULTS_FOLDER = os.path.join(os.path.dirname(__file__), "static", "results")
CONVERT_FOLDER = os.path.join(os.path.dirname(__file__), "static", "converted")
PANORAMA_FOLDER = os.path.join(os.path.dirname(__file__), "static", "panorama")
PANORAMA_IMAGES_DIR = os.path.join(os.path.dirname(__file__), "panorama_images")
COORDINATES_CSV = os.path.join(PANORAMA_IMAGES_DIR, "coordinates.csv")
MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32MB for panorama images

VALID_SIZES = [
    "auto", "1:1", "3:2", "2:3", "16:9", "9:16",
    "4:3", "3:4", "21:9", "9:21", "1:3", "3:1", "2:1", "1:2"
]

# NanoBanana2 specific
NANO_RESOLUTIONS = ["1K", "2K", "4K"]
NANO_ASPECT_RATIOS = ["auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9"]
