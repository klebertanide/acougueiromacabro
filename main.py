import os
import io
import csv
import re
import requests
import unidecode
import json
import uuid
import math
import tempfile
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
GOOGLE_DRIVE_ROOT_FOLDER = "1NelNODHVBTbAuVqrfRNmLZP8MVQpF1aX"
SERVICE_ACCOUNT_FILE     = "/etc/secrets/service_account.json"
ELEVEN_API_KEY           = os.getenv("ELEVENLABS_API_KEY")

DEFAULT_CSV_HEADER = [
    "Prompt", "Visibility", "Aspect_ratio", "Magic_prompt", "Model",
    "Seed_number", "Rendering", "Negative_prompt", "Style", "color_palette", "Num_images"
]

DEFAULT_CSV_ROW = lambda prompt: [
    prompt,
    "private",
    "9:16",
    "on",
    "3",
    "",
    "turbo",
    "sem palavras, sem frases, sem textos, palavras, textos, frases, words, sentences, texts, paragraphs, letters, captions, watermark, logos",
    "auto",
    "",
    "4"
]

def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def criar_subpasta(nome: str, drive, parent_folder_id: str):
    try:
        results = drive.files().list(
            q=f"name='{nome}' and mimeType='application/vnd.google-apps.folder' and '{parent_folder_id}' in parents",
            spaces='drive',
            fields='files(id, name)'
        ).execute()

        items = results.get('files', [])
        if items:
            return items[0]['id']
    except Exception:
        pass

    meta = {
        "name": nome,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_folder_id]
    }
    return drive.files().create(body=meta).execute()["id"]

def upload_para_drive(path: Path, nome: str, folder_id: str, drive):
    media = MediaFileUpload(str(path), resumable=True)
    drive.files().create(body={"name": nome, "parents": [folder_id]}, media_body=media).execute()

def gerar_slug():
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + str(uuid.uuid4())[:6]

def slugify(text: str, limit: int = 30) -> str:
    txt = unidecode.unidecode(text or "")
    txt = re.sub(r"[^\w\s]", "", txt)
    txt = txt.strip().replace(" ", "_").lower()
    return txt[:limit] if txt else gerar_slug()

def elevenlabs_tts(text: str) -> str:
    if not ELEVEN_API_KEY:
        raise RuntimeError("VariÃ¡vel ELEVEN_API_KEY nÃ£o definida")

    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json"
    }

    voice_id = "NgBYGKDDq2Z8Hnhatgma"
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.9,
            "similarity_boost": 0.9,
            "style": 0.1,
            "use_speaker_boost": True
        }
    }

    try:
        with requests.post(url, headers=headers, json=payload, stream=True, timeout=120) as r:
            r.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_audio:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        temp_audio.write(chunk)
                return temp_audio.name
    except requests.RequestException as e:
        raise RuntimeError(f"Erro ao chamar ElevenLabs: {e}")

def parse_ts(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000

def gerar_prompts_dobrados_do_srt(srt_content: str):
    blocks = []
    for blk in srt_content.strip().split("\n\n"):
        parts = blk.strip().split("\n")
        if len(parts) < 3:
            continue
        st, en = parts[1].split(" --> ")
        txt = " ".join(parts[2:])
        h, m, s_ms = st.split(":")
        s, ms = s_ms.split(",")
        inicio_seg = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) // 1000
        blocks.append((inicio_seg, txt.strip()))

    prompts = []
    for i in range(len(blocks)):
        t1, txt1 = blocks[i]
        descricao = f"{t1}, {txt1}, horror atmosphere, low lighting, unsettling shadows, digital artifacts, surreal digital painting, cold color palette, glitch aesthetic, paranoia, liminal space, grain, creepy. Nunca inserir texto, frases ou palavras."
        prompts.append((t1, descricao))
        if i < len(blocks) - 1:
            t2, _ = blocks[i + 1]
            meio = (t1 + t2) // 2
            descricao_intermediaria = f"{meio}, {txt1}, horror atmosphere, low lighting, unsettling shadows, digital artifacts, surreal digital painting, cold color palette, glitch aesthetic, paranoia, liminal space, grain, creepy. Nunca inserir texto, frases ou palavras."
            prompts.append((meio, descricao_intermediaria))

    return prompts

@app.route("/transcrever", methods=["POST"])
def transcrever():
    data = request.get_json(force=True) or {}
    audio_ref = data.get("audio_url") or data.get("audio_file")
    slug = data.get("slug")

    if not audio_ref:
        return jsonify(error="campo 'audio_url' ou 'audio_file' obrigatÃ³rio"), 400

    if not slug:
        slug = Path(audio_ref).stem
        if "_audio" in slug:
            slug = slug.replace("_audio", "")

    try:
        if os.path.exists(audio_ref):
            fobj = open(audio_ref, "rb")
        else:
            resp = requests.get(audio_ref, timeout=60)
            resp.raise_for_status()
            fobj = io.BytesIO(resp.content)
            fobj.name = Path(audio_ref).name or "audio.mp3"
    except Exception as e:
        return jsonify(error="falha ao carregar Ã¡udio", detalhe=str(e)), 400

    try:
        raw_srt = client.audio.transcriptions.create(model="whisper-1", file=fobj, response_format="srt")
        blocks = []
        for blk in raw_srt.strip().split("\n\n"):
            parts = blk.split("\n")
            if len(parts) < 3:
                continue
            st, en = parts[1].split(" --> ")
            txt = " ".join(parts[2:])
            inicio = parse_ts(st)
            fim = parse_ts(en)
            blocks.append((inicio, fim, txt))
        total = blocks[-1][1] if blocks else 0

        srt_path = Path(f"{slug}_legenda.srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(raw_srt)

        drive = get_drive_service()
        folder_id = criar_subpasta(slug, drive, GOOGLE_DRIVE_ROOT_FOLDER)
        upload_para_drive(srt_path, srt_path.name, folder_id, drive)

        prompts_gerados = gerar_prompts_dobrados_do_srt(raw_srt)

        csv_path = Path(f"{slug}_prompts.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(DEFAULT_CSV_HEADER)
            for _, prompt in prompts_gerados:
                writer.writerow(DEFAULT_CSV_ROW(prompt))

        upload_para_drive(csv_path, csv_path.name, folder_id, drive)

        return jsonify(
            transcricao=[{"inicio": i, "fim": f, "texto": t} for i, f, t in blocks],
            duracao_total=total,
            slug=slug,
            prompts=prompts_gerados,
            folder_url=f"https://drive.google.com/drive/folders/{folder_id}"
        )
    except Exception as e:
        return jsonify(error="falha na transcriÃ§Ã£o", detalhe=str(e)), 500
    finally:
        try: fobj.close()
        except: pass

@app.route("/falar", methods=["POST"])
def falar():
    import traceback
    try:
        data = request.get_json(force=True)
        texto = data.get("texto")

        if not texto:
            return jsonify(error="Campo 'texto' Ã© obrigatÃ³rio"), 400

        slug = slugify(texto[:40])
        audio_path_str = elevenlabs_tts(texto)
        audio_path = Path(audio_path_str)

        drive = get_drive_service()
        folder_id = criar_subpasta(slug, drive, GOOGLE_DRIVE_ROOT_FOLDER)
        upload_para_drive(audio_path, audio_path.name, folder_id, drive)

        return jsonify({
            "audio_url": f"https://drive.google.com/drive/folders/{folder_id}",
            "slug": slug,
            "drive_folder_url": f"https://drive.google.com/drive/folders/{folder_id}"
        })

    except Exception as e:
        trace = traceback.format_exc()
        print("ERRO NO ENDPOINT /falar:\n", trace)
        return jsonify({
            "erro": str(e),
            "trace": trace
        }), 500

@app.route("/", methods=["GET"])
def index():
    return "Servidor do AÃ§ougueiro Macabro estÃ¡ online e rodando."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)