import os
import re
import csv
import tempfile
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Configurações
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
SERVICE_ACCOUNT_FILE = "/etc/secrets/service_account.json"
GOOGLE_DRIVE_ROOT_FOLDER = "1NelNODHVBTbAuVqrfRNmLZP8MVQpF1aX"
VOICE_ID = "NgBYGKDDq2Z8Hnhatgma"

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

# Setup Google Drive
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=['https://www.googleapis.com/auth/drive']
)
drive_service = build('drive', 'v3', credentials=credentials)

FIXED_TAGS = "macabre, scary, terrifying. scribbled. grain. dirty texture. grunge. screen scratches."

# Funções
def gerar_historia(prompt_usuario):
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Você é um narrador macabro. Crie histórias curtas e perturbadoras de até 2 minutos. Comece com um título chamativo. Use um tom sombrio, poético, desconfortável."},
            {"role": "user", "content": prompt_usuario}
        ],
        temperature=0.9,
        max_tokens=800
    )
    return response.choices[0].message.content.strip()

def narrar_com_elevenlabs(texto):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json"
    }
    body = {
        "text": texto,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": { "stability": 0.4, "similarity_boost": 0.8 }
    }
    response = requests.post(url, headers=headers, json=body)
    if response.status_code == 200:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(response.content)
        tmp.close()
        return tmp.name
    else:
        raise Exception(f"Erro ElevenLabs: {response.status_code} - {response.text}")

def transcrever_whisper(mp3_path):
    with open(mp3_path, "rb") as f:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="srt",
            language="pt"
        )
    srt_path = mp3_path.replace(".mp3", ".srt")
    with open(srt_path, "w", encoding="utf-8") as srt_file:
        srt_file.write(transcript)
    return srt_path

def gerar_slug(texto):
    txt = re.sub(r'\W+', '-', texto.strip().lower())[:30]
    return f"{datetime.now().strftime('%Y-%m-%d')}_{txt}"

def parse_srt(srt_path):
    with open(srt_path, "r", encoding="utf-8") as f:
        srt = f.read()
    blocks = re.findall(r"(\d+)\s+(\d{2}:\d{2}:\d{2}),\d+\s+-->\s+.*?\s+(.+?)(?=\n\d|\Z)", srt, re.DOTALL)
    segments = []
    for _, start_time, text in blocks:
        h, m, s = map(int, start_time.split(":"))
        total_seconds = h * 3600 + m * 60 + s
        segments.append((total_seconds, text.strip().replace("\n", " ")))
    return segments

def gerar_prompt_imagem(texto, segundos):
    prompt_gpt = f"""You are an expert in horror visual prompts.

The following is a horror sentence: "{texto}"

Write a long, highly detailed standalone image prompt. Include:
- who is in the scene
- where they are
- what they are doing
- clothing, expressions, atmosphere
Start the output with "{segundos}," and end with:
"{FIXED_TAGS}"
"""
    resp = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt_gpt}],
        temperature=0.9,
        max_tokens=500
    )
    return resp.choices[0].message.content.strip()

def gerar_csv_prompts(srt_path):
    segmentos = parse_srt(srt_path)
    csv_path = srt_path.replace(".srt", ".csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["second", "prompt"])
        for segundos, texto in segmentos:
            prompt = gerar_prompt_imagem(texto, segundos)
            writer.writerow([segundos, prompt])
    return csv_path

def salvar_txt(texto, slug):
    path = os.path.join(tempfile.gettempdir(), f"{slug}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(texto)
    return path

def create_drive_folder(name, parent_id):
    metadata = {
        'name': name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = drive_service.files().create(body=metadata, fields='id').execute()
    return folder.get('id')

def upload_to_drive(filepath, filename, folder_id):
    media = MediaFileUpload(filepath, resumable=True)
    file_metadata = {'name': filename, 'parents': [folder_id]}
    drive_service.files().create(body=file_metadata, media_body=media).execute()

@app.route("/falar", methods=["POST"])
def falar():
    data = request.get_json()
    prompt = data.get("prompt")

    if not prompt:
        return jsonify({"erro": "Envie um prompt no corpo da requisição"}), 400

    try:
        historia = gerar_historia(prompt)
        slug = gerar_slug(historia)

        mp3_path = narrar_com_elevenlabs(historia)
        srt_path = transcrever_whisper(mp3_path)
        csv_path = gerar_csv_prompts(srt_path)
        txt_path = salvar_txt(historia, slug)

        # Cria pasta no Drive e faz upload dos arquivos
        folder_id = create_drive_folder(slug, GOOGLE_DRIVE_ROOT_FOLDER)
        upload_to_drive(mp3_path, f"{slug}.mp3", folder_id)
        upload_to_drive(srt_path, f"{slug}.srt", folder_id)
        upload_to_drive(csv_path, f"{slug}.csv", folder_id)
        upload_to_drive(txt_path, f"{slug}.txt", folder_id)

        return jsonify({
            "slug": slug,
            "mensagem": "Todos os arquivos foram gerados e enviados para o Google Drive com sucesso."
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return "Servidor do TerrorGPT rodando..."

if __name__ == "__main__":
    app.run()
