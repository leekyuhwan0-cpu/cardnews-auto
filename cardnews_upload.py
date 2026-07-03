import os
import re
import sys
import time
import random
import tempfile
import uuid
import requests
import boto3
from pathlib import Path
from urllib.parse import quote
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
from PIL import Image, ImageFilter

from cardnews_config import (
    ACCOUNTS, R2_BUCKET, R2_PUBLIC_URL, R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY,
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
)

# ── Google Drive 인증 ─────────────────────────────────────────
def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    return build("drive", "v3", credentials=creds)

# ── R2 클라이언트 ─────────────────────────────────────────────
s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)

# ── Drive 파일 스캔 ───────────────────────────────────────────
def scan_drive_folder(folder_id):
    """
    Google Drive 폴더 스캔 → 번호별로 파일 묶기
    반환: { 584: [{"id":..., "name":"584.png", "type":"img_0"}, ...], ... }
    """
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = results.get("files", [])
    groups = {}

    for f in files:
        name = f["name"]
        file_id = f["id"]

        # 소파영상.mp4 / 584영상.mp4
        m = re.match(r'^([^.\-\s]+)영상\.mp4$', name)
        if m:
            base = m.group(1)
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "video"})
            continue

        # 소파-1.png/webp/jpg/jpeg / 584-1.png
        m = re.match(r'^([^.\-\s]+)-(\d+)\.(png|webp|jpg|jpeg)$', name)
        if m:
            base = m.group(1)
            idx = int(m.group(2))
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": f"img_{idx}"})
            continue

        # 소파.png/webp/jpg/jpeg / 584.png
        m = re.match(r'^([^.\-\s]+)\.(png|webp|jpg|jpeg)$', name)
        if m:
            base = m.group(1)
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "img_0"})
            continue

        # 소파.txt / 584.txt
        m = re.match(r'^([^.\-\s]+)\.txt$', name)
        if m:
            base = m.group(1)
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "txt"})
            continue

    # 정렬: img_0 → img_1 → img_2 → video (txt 제외)
    for base in groups:
        groups[base].sort(key=lambda x: (0 if x["type"].startswith("img") else (1 if x["type"] == "video" else 2), x["type"]))

    return groups

# ── Drive 파일 다운로드 ───────────────────────────────────────
def download_from_drive(file_id, filename, tmp_dir):
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fpath = os.path.join(tmp_dir, filename)
    with open(fpath, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return fpath

# ── Drive 파일 삭제 ───────────────────────────────────────────
def delete_from_drive(file_id, filename):
    service = get_drive_service()
    service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
    print(f"  Drive 삭제: {filename}")

# ── R2 업로드 ─────────────────────────────────────────────────
def upload_to_r2(file_path, filename):
    key = f"temp/{uuid.uuid4().hex}{Path(filename).suffix}"
    ext = filename.rsplit(".", 1)[-1].lower()
    content_type_map = {"mp4": "video/mp4", "webp": "image/webp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
    content_type = content_type_map.get(ext, "image/png")
    s3.upload_file(
        file_path,
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": content_type}
    )
    return f"{R2_PUBLIC_URL}/temp/{quote(filename)}"

# ── Instagram API ─────────────────────────────────────────────
def create_single_media(ig_user_id, token, url, caption, is_video=False):
    data = {"access_token": token, "caption": caption}
    if is_video:
        data["video_url"] = url
        data["media_type"] = "REELS"
    else:
        data["image_url"] = url
    res = requests.post(f"https://graph.instagram.com/v21.0/{ig_user_id}/media", data=data)
    j = res.json()
    if "id" not in j:
        print(f"  [API 오류] {j}")
    return j.get("id")

def create_carousel_item(ig_user_id, token, url, is_video=False):
    data = {"access_token": token, "is_carousel_item": "true"}
    if is_video:
        data["video_url"] = url
        data["media_type"] = "VIDEO"
    else:
        data["image_url"] = url
    res = requests.post(f"https://graph.instagram.com/v21.0/{ig_user_id}/media", data=data)
    result = res.json()
    print(f"  캐러셀 아이템: {result}")
    return result.get("id")

def create_carousel_container(ig_user_id, token, children_ids, caption):
    data = {
        "access_token": token,
        "media_type": "CAROUSEL",
        "children": ",".join(children_ids),
        "caption": caption,
    }
    res = requests.post(f"https://graph.instagram.com/v21.0/{ig_user_id}/media", data=data)
    result = res.json()
    print(f"  캐러셀 컨테이너: {result}")
    return result.get("id")

def publish_media(ig_user_id, token, container_id):
    res = requests.post(
        f"https://graph.instagram.com/v21.0/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token}
    )
    return res.json()

def make_story_image(img_path, tmp_dir):
    """
    4:5 피드 이미지 → 9:16 스토리 포맷 (인스타 '피드 스토리 공유' 스타일)
    배경: 원본 블러 + 어둡게, 중앙에 원본 카드 배치
    """
    card = Image.open(img_path).convert("RGB")
    card = card.resize((1080, 1350), Image.LANCZOS)

    # 배경: 카드를 1080x1920으로 늘린 후 블러
    bg = card.resize((1080, 1920), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
    # 약간 어둡게
    dark = Image.new("RGB", (1080, 1920), (0, 0, 0))
    bg = Image.blend(bg, dark, alpha=0.35)

    # 카드 중앙 배치 (상하 285px 여백)
    top = (1920 - 1350) // 2
    bg.paste(card, (0, top))

    out_path = os.path.join(tmp_dir, "story_" + os.path.basename(img_path))
    bg.save(out_path, format="PNG")
    return out_path


# ── Facebook 게시 ─────────────────────────────────────────────
def post_facebook_feed(page_id, page_token, url, caption):
    data = {
        "access_token": page_token,
        "url": url,
        "caption": caption,
    }
    res = requests.post(f"https://graph.facebook.com/v21.0/{page_id}/photos", data=data)
    j = res.json()
    if j.get("id"):
        print(f"  [FB 피드] 게시 완료: {j['id']}")
    else:
        print(f"  [FB 피드 오류] {j}")

def post_facebook_story(page_id, page_token, url):
    # 1단계: 스토리 이미지 업로드
    data = {
        "access_token": page_token,
        "url": url,
    }
    res = requests.post(f"https://graph.facebook.com/v21.0/{page_id}/photo_stories", data=data)
    j = res.json()
    if j.get("id"):
        print(f"  [FB 스토리] 게시 완료: {j['id']}")
    else:
        print(f"  [FB 스토리 오류] {j}")


def post_story(ig_user_id, token, url, is_video=False):
    data = {"access_token": token, "media_type": "STORIES"}
    if is_video:
        data["video_url"] = url
    else:
        data["image_url"] = url
    res = requests.post(f"https://graph.instagram.com/v21.0/{ig_user_id}/media", data=data)
    j = res.json()
    story_id = j.get("id")
    if not story_id:
        print(f"  [스토리 오류] {j}")
        return
    time.sleep(3)
    res2 = requests.post(
        f"https://graph.instagram.com/v21.0/{ig_user_id}/media_publish",
        data={"creation_id": story_id, "access_token": token}
    )
    print(f"  [스토리] 게시 결과: {res2.json()}")

# ── 메인 업로드 함수 ──────────────────────────────────────────
def post_group(lang, base, file_items):
    config = ACCOUNTS[lang]
    ig_user_id = config["ig_user_id"]
    token = config["access_token"]

    # txt 파일 분리
    media_items = [f for f in file_items if f["type"] != "txt"]
    txt_items   = [f for f in file_items if f["type"] == "txt"]

    print(f"\n[{lang}] {base}번 업로드 시작 (미디어 {len(media_items)}개)")

    story_url = None
    story_is_video = False

    with tempfile.TemporaryDirectory() as tmp_dir:
        # 캡션 읽기
        caption = ""
        if txt_items:
            txt_path = download_from_drive(txt_items[0]["id"], txt_items[0]["name"], tmp_dir)
            caption = open(txt_path, encoding="utf-8").read().strip()

        # 단일 파일
        if len(media_items) == 1:
            item = media_items[0]
            is_video = (item["type"] == "video")
            fpath = download_from_drive(item["id"], item["name"], tmp_dir)
            url = upload_to_r2(fpath, item["name"])
            print(f"  URL: {url}")
            if not is_video:
                story_fpath = make_story_image(fpath, tmp_dir)
                story_url = upload_to_r2(story_fpath, "story_" + item["name"])
            else:
                story_url, story_is_video = url, is_video
            container_id = create_single_media(ig_user_id, token, url, caption, is_video)
            print(f"  컨테이너 ID: {container_id}")

        # 캐러셀
        else:
            children_ids = []
            for item in media_items:
                is_video = (item["type"] == "video")
                fpath = download_from_drive(item["id"], item["name"], tmp_dir)
                url = upload_to_r2(fpath, item["name"])
                print(f"  URL: {url}")
                if story_url is None:
                    if not is_video:
                        story_fpath = make_story_image(fpath, tmp_dir)
                        story_url = upload_to_r2(story_fpath, "story_" + item["name"])
                    else:
                        story_url, story_is_video = url, is_video
                if is_video:
                    time.sleep(5)
                item_id = create_carousel_item(ig_user_id, token, url, is_video)
                children_ids.append(item_id)
                time.sleep(2)
            container_id = create_carousel_container(ig_user_id, token, children_ids, caption)

    if not container_id:
        print(f"  [오류] 컨테이너 생성 실패")
        return False

    time.sleep(3)
    result = publish_media(ig_user_id, token, container_id)
    print(f"  게시 결과: {result}")

    if result.get("id"):
        # 업로드 성공 → Drive에서 파일 전부 삭제
        for item in file_items:
            delete_from_drive(item["id"], item["name"])
        print(f"  [{lang}] {base}번 업로드 완료!")

        # 인스타 스토리 업로드
        if story_url:
            print(f"  [스토리] 업로드 시작...")
            time.sleep(2)
            post_story(ig_user_id, token, story_url, story_is_video)

        # Facebook 피드 + 스토리 업로드
        fb_page_id    = config.get("fb_page_id")
        fb_page_token = config.get("fb_page_token")
        if fb_page_id and fb_page_token and story_url and not story_is_video:
            print(f"  [Facebook] 피드 업로드 시작...")
            time.sleep(2)
            post_facebook_feed(fb_page_id, fb_page_token, story_url, caption)
            print(f"  [Facebook] 스토리 업로드 시작...")
            time.sleep(2)
            post_facebook_story(fb_page_id, fb_page_token, story_url)

        return True
    else:
        print(f"  [오류] 게시 실패: {result}")
        return False

# ── 단건 업로드 (스케줄러 호출용) ────────────────────────────
def post_one(lang):
    folder_id = ACCOUNTS[lang]["drive_folder_id"]
    groups = scan_drive_folder(folder_id)

    # txt만 있는 항목 제외, 미디어 없는 항목 제외
    available = [b for b, items in groups.items() if any(f["type"] != "txt" for f in items)]

    if not available:
        print(f"[{lang}] 업로드 가능한 파일 없음")
        return

    base = random.choice(available)
    post_group(lang, base, groups[base])

if __name__ == "__main__":
    lang = sys.argv[1] if len(sys.argv) > 1 else "tr"
    post_one(lang)
