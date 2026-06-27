import os
import re
import time
import random
import tempfile
import requests
import boto3
from pathlib import Path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

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
        pageSize=1000
    ).execute()

    files = results.get("files", [])
    groups = {}

    for f in files:
        name = f["name"]
        file_id = f["id"]

        # 584영상.mp4 or 584영상.png
        m = re.match(r'^(\d+)영상\.(mp4|png)$', name)
        if m:
            base = int(m.group(1))
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "video"})
            continue

        # 584-1.png, 584-2.png
        m = re.match(r'^(\d+)-(\d+)\.png$', name)
        if m:
            base = int(m.group(1))
            idx = int(m.group(2))
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": f"img_{idx}"})
            continue

        # 584.png
        m = re.match(r'^(\d+)\.png$', name)
        if m:
            base = int(m.group(1))
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "img_0"})
            continue

        # 584.txt
        m = re.match(r'^(\d+)\.txt$', name)
        if m:
            base = int(m.group(1))
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
    request = service.files().get_media(fileId=file_id)
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
    service.files().delete(fileId=file_id).execute()
    print(f"  Drive 삭제: {filename}")

# ── R2 업로드 ─────────────────────────────────────────────────
def upload_to_r2(file_path, filename):
    key = f"temp/{filename}"
    content_type = "video/mp4" if filename.endswith(".mp4") else "image/png"
    s3.upload_file(
        file_path,
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": content_type}
    )
    return f"{R2_PUBLIC_URL}/{key}"

# ── Instagram API ─────────────────────────────────────────────
def create_single_media(ig_user_id, token, url, caption, is_video=False):
    data = {"access_token": token, "caption": caption}
    if is_video:
        data["video_url"] = url
        data["media_type"] = "REELS"
    else:
        data["image_url"] = url
    res = requests.post(f"https://graph.instagram.com/v21.0/{ig_user_id}/media", data=data)
    return res.json().get("id")

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

# ── 메인 업로드 함수 ──────────────────────────────────────────
def post_group(lang, base, file_items):
    config = ACCOUNTS[lang]
    ig_user_id = config["ig_user_id"]
    token = config["access_token"]

    # txt 파일 분리
    media_items = [f for f in file_items if f["type"] != "txt"]
    txt_items   = [f for f in file_items if f["type"] == "txt"]

    print(f"\n[{lang}] {base}번 업로드 시작 (미디어 {len(media_items)}개)")

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
    post_one("tr")
