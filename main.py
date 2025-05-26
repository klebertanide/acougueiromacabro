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
import openai
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
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
    return build("drive", "v3", credentials=creds, cache_discovery=False)

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
    except Exception as e:
        print("Erro ao buscar subpasta:", e)

    try:
        meta = {
            "name": nome,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id]
        }
        return drive.files().create(body=meta).execute()["id"]
    except Exception as e:
        print("Erro ao criar subpasta:", e)
        raise RuntimeError("Falha ao criar subpasta no Google Drive.")

def upload_para_drive(path: Path, nome: str, folder_id: str, drive):
    media = MediaFileUpload(str(path))
    drive.files().create(body={"name": nome, "parents": [folder_id]}, media_body=media).execute()

def gerar_slug():
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + str(uuid.uuid4())[:6]

def slugify(text: str, limit: int = 30) -> str:
    txt = unidecode.unidecode(text or "")
    txt = re.sub(r"[^\w\s]", "", txt)
    txt = txt.strip().replace(" ", "_").lower()
    return txt[:limit] if txt else gerar_slug()

def elevenlabs_tts(text: str, slug: str) -> str:
    if not ELEVEN_API_KEY:
        raise RuntimeError("Variável ELEVEN_API_KEY não definida")

    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json"
    }

    voice_id = "NgBYGKDDq2Z8Hnhatgma"  # Atlas
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
            mp3_path = Path(f"{slug}_audio.mp3")
            with open(mp3_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return str(mp3_path)
    except requests.RequestException as e:
        raise RuntimeError(f"Erro ao chamar ElevenLabs: {e}")

def gerar_prompts_via_chatgpt(srt_content: str, modelo="gpt-4"):
    def parse_srt(srt: str):
        blocks = []
        for blk in srt.strip().split("\n\n"):
            parts = blk.strip().split("\n")
            if len(parts) < 3:
                continue
            st, en = parts[1].split(" --> ")
            txt = " ".join(parts[2:])
            h, m, s_ms = st.split(":")
            s, ms = s_ms.split(",")
            inicio_seg = int(h) * 3600 + int(m) * 60 + int(s)
            blocks.append((inicio_seg, txt.strip()))
        return blocks

    instrucoes = (
        "Você receberá um trecho de legenda de vídeo. Para cada trecho, gere um prompt de imagem "
        "que seja completo, autossuficiente e visual. O prompt deve incluir: o personagem ou entidade envolvida "
        "(mesmo que implícito), a ação que está ocorrendo, o ambiente onde se passa, e a emoção predominante. "
        "Finalize o prompt SEM repetir o texto original, mas criando uma descrição nova, visual e cinematográfica. "
        "Padronize com: 'horror atmosphere, low lighting, unsettling shadows, digital artifacts, surreal digital painting, "
        "cold color palette, glitch aesthetic, paranoia, liminal space, grain, creepy. Nunca inserir texto, frases ou palavras.'"
    )

    openai.api_key = os.getenv("OPENAI_API_KEY")
    blocos = parse_srt(srt_content)
    resultados = []

    for tempo, legenda in blocos:
        mensagem = [
            {"role": "system", "content": instrucoes},
            {"role": "user", "content": f"Legenda: {legenda}"}
        ]

        try:
            resposta = openai.ChatCompletion.create(
                model=modelo,
                messages=mensagem,
                temperature=0.8
            )
            prompt = resposta.choices[0].message.content.strip()
            linha_final = f"{tempo}, {prompt}"
            resultados.append((tempo, linha_final))
        except Exception as e:
            print(f"Erro no tempo {tempo}: {e}")
            resultados.append((tempo, f"{tempo}, ERRO AO GERAR PROMPT"))

        time.sleep(1.5)

    return resultados

@app.route("/gerar_audio_csv", methods=["POST"])
def gerar_audio_csv():
    data = request.get_json()
    texto_base = data.get("texto", "")
    modelo = data.get("modelo", "gpt-4")

    if not texto_base:
        return jsonify({"error": "Texto base ausente"}), 400

    try:
        # Etapa 1: Gerar áudio
        audio_slug = gerar_slug()
        mp3_path = elevenlabs_tts(texto_base, audio_slug)

        # Etapa 2: Transcrever com Whisper
        openai.api_key = os.getenv("OPENAI_API_KEY")
        with open(mp3_path, "rb") as f:
            transcricao = openai.Audio.transcribe("whisper-1", f)

        srt_content = transcricao.get("text", "")
        if not srt_content:
            return jsonify({"error": "Transcrição vazia"}), 500

        # Etapa 3: Criar SRT simples
        frases = [f.strip() for f in srt_content.split(".") if f.strip()]
        srt_simples = ""
        tempo_atual = 0
        for i, frase in enumerate(frases, start=1):
            inicio = tempo_atual
            fim = tempo_atual + 4
            srt_simples += f"{i}\n"
            srt_simples += f"00:00:{inicio:02},000 --> 00:00:{fim:02},000\n"
            srt_simples += f"{frase.strip()}.\n\n"
            tempo_atual += 4

        # Etapa 4: Gerar prompts
        prompts = gerar_prompts_via_chatgpt(srt_simples, modelo)
        if not prompts or not prompts[0][1]:
            return jsonify({"error": "Nenhum prompt gerado"}), 500

        # Etapa 5: Gerar slug com base no primeiro prompt
        primeiro_prompt = prompts[0][1].split(", ", 1)[1]
        slug = slugify(primeiro_prompt, 30)

        # Etapa 6: Criar pasta no Drive
        drive = get_drive_service()
        pasta_id = criar_subpasta(slug, drive, GOOGLE_DRIVE_ROOT_FOLDER)

        # Etapa 7: Criar CSV
        csv_path = Path(f"{slug}_prompts.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(DEFAULT_CSV_HEADER)
            for _, linha in prompts:
                prompt_texto = linha.split(", ", 1)[1]
                writer.writerow(DEFAULT_CSV_ROW(prompt_texto))

        # Etapa 8: Upload dos arquivos
        upload_para_drive(mp3_path, mp3_path.name, pasta_id, drive)
        upload_para_drive(csv_path, csv_path.name, pasta_id, drive)

        return jsonify({
            "slug": slug,
            "pasta_drive_id": pasta_id,
            "arquivos": [mp3_path.name, csv_path.name],
            "quantidade_prompts": len(prompts)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return "Servidor do Açougueiro Macabro está online e rodando."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)