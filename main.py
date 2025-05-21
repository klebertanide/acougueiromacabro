import os
import io
import csv
import re
import requests
import unidecode
import json
import uuid
import math
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

def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def criar_subpasta(nome: str, drive, parent_folder_id: str):
    # Verificar se a pasta já existe
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
        pass  # Se falhar, continua e cria uma nova pasta
    
    # Criar nova pasta
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

def elevenlabs_tts(text: str) -> bytes:
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json"
    }

    voice_id = "ysswSXp8U9dFpzPJqFje"  # ou qualquer outro que você esteja usando
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",  # modelo correto, com suporte a textos maiores (~40k)
        "voice_settings": {
            "stability": 0.6,
            "similarity_boost": 0.9,
            "style": 0.15,
            "use_speaker_boost": True
        }
    }

    for tentativa in range(2):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=600)
            r.raise_for_status()
            return r.content
        except Exception as e:
            if tentativa == 1:
                raise e

def parse_ts(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000

@app.route("/falar", methods=["POST"])
def falar():
    data = request.get_json(force=True) or {}
    texto = data.get("texto")
    if not texto:
        return jsonify(error="campo 'texto' obrigatório"), 400

    slug = slugify(texto)
    mp3_path = Path(f"{slug}_audio.mp3")
    txt_path = Path(f"{slug}_texto.txt")

    # Salvar o texto original em um arquivo TXT
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(texto)
    except Exception as e:
        return jsonify(error="falha ao salvar arquivo de texto", detalhe=str(e)), 500

    try:
        audio_bytes = elevenlabs_tts(texto)
        if not audio_bytes or len(audio_bytes) < 1000:
            raise Exception("Áudio gerado é vazio ou muito pequeno.")
        mp3_path.write_bytes(audio_bytes)
    except Exception as e:
        return jsonify(error="falha ElevenLabs", detalhe=str(e)), 500

    try:
        drive = get_drive_service()
        folder_id = criar_subpasta(slug, drive, GOOGLE_DRIVE_ROOT_FOLDER)
        
        # Upload do MP3
        upload_para_drive(mp3_path, mp3_path.name, folder_id, drive)
        
        # Upload do TXT com o texto original
        upload_para_drive(txt_path, txt_path.name, folder_id, drive)
    except Exception as e:
        return jsonify(error="falha no upload para o Drive", detalhe=str(e)), 500

    return jsonify(audio_url=str(mp3_path.resolve()), slug=slug, folder_id=folder_id)

@app.route("/transcrever", methods=["POST"])
def transcrever():
    data = request.get_json(force=True) or {}
    audio_ref = data.get("audio_url") or data.get("audio_file")
    slug = data.get("slug")
    
    if not audio_ref:
        return jsonify(error="campo 'audio_url' ou 'audio_file' obrigatório"), 400
    
    if not slug:
        # Tentar extrair slug do nome do arquivo
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
        return jsonify(error="falha ao carregar áudio", detalhe=str(e)), 400

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
        
        # Salvar o SRT em um arquivo
        srt_path = Path(f"{slug}_legenda.srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(raw_srt)
        
        # Upload do SRT para o Drive
        try:
            drive = get_drive_service()
            folder_id = criar_subpasta(slug, drive, GOOGLE_DRIVE_ROOT_FOLDER)
            upload_para_drive(srt_path, srt_path.name, folder_id, drive)
        except Exception as e:
            print(f"Erro ao fazer upload do SRT: {e}")
            # Continua mesmo com erro no upload
        
        return jsonify(transcricao=[{"inicio": i, "fim": f, "texto": t} for i, f, t in blocks], duracao_total=total, slug=slug)
    except Exception as e:
        return jsonify(error="falha na transcrição", detalhe=str(e)), 500
    finally:
        try: fobj.close()
        except: pass

@app.route("/gerar_csv", methods=["POST"])
def gerar_csv():
    data = request.get_json(force=True) or {}
    transcricao = data.get("transcricao")
    prompts = data.get("prompts")
    texto_original = data.get("texto_original")
    slug = data.get("slug")
    aspect_ratio = data.get("aspect_ratio", "9:16")  # Padrão 9:16 se não especificado
    intervalo_segundos = data.get("intervalo_segundos", 4)  # Intervalo fixo entre prompts, padrão 4 segundos

    if not transcricao or not prompts:
        return jsonify(error="transcricao e prompts são obrigatórios"), 400
    
    # Se não tiver slug nem texto_original, gera um slug aleatório
    if not slug and not texto_original:
        slug = gerar_slug()
    elif not slug:
        slug = slugify(texto_original)

    try:
        drive = get_drive_service()
        pasta_id = criar_subpasta(slug, drive, GOOGLE_DRIVE_ROOT_FOLDER)

        # CSV no formato exato do modelo
        csv_path = Path(f"{slug}_prompts.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # Cabeçalho exato conforme o modelo
            writer.writerow([
                "Prompt", "Visibility", "Aspect_ratio", "Magic_prompt", "Model", 
                "Seed_number", "Rendering", "Negative_prompt", "Style", "color_palette", "Num_images"
            ])
            
            # Valores padrão para as colunas fixas
            negative_prompt = "words, sentences, texts, paragraphs, letters, numbers, syllables, low quality, ofingers"
            
            # Calcular a duração total do áudio
            duracao_total = max([bloco["fim"] for bloco in transcricao]) if transcricao else 0
            
            # Gerar tempos em intervalos fixos de 4 segundos
            tempos_fixos = list(range(0, math.ceil(duracao_total), intervalo_segundos))
            
            # Associar cada prompt ao tempo fixo mais próximo
            prompts_com_tempo = []
            for i, (prompt_texto, bloco) in enumerate(zip(prompts, transcricao)):
                # Encontrar o tempo fixo mais próximo do início do bloco
                tempo_mais_proximo = min(tempos_fixos, key=lambda t: abs(t - bloco["inicio"]))
                
                # Se este tempo já foi usado, usar o próximo tempo sequencial
                while tempo_mais_proximo in [p[0] for p in prompts_com_tempo]:
                    tempo_mais_proximo += intervalo_segundos
                    if tempo_mais_proximo not in tempos_fixos:
                        tempos_fixos.append(tempo_mais_proximo)
                
                prompts_com_tempo.append((tempo_mais_proximo, prompt_texto, bloco))
            
            # Ordenar por tempo
            prompts_com_tempo.sort(key=lambda x: x[0])
            
            # Escrever no CSV
            for tempo, prompt_texto, bloco in prompts_com_tempo:
                # Formatar o tempo de início como inteiro
                tempo_inicio = f"{tempo}"
                
                # Construir o prompt completo: tempo + prompt + informações de aquarela
                prompt_completo = f"{tempo_inicio}, {prompt_texto}, Images that look like sketches made by a sick maniac, macabre scribbles, fear, dread, terror, panic, phobia, fright, frightening, terrifying, frightening. cruelty, monstrosity, barbarity, atrocity, hideousness, savagery, inhumanity, bestiality, macabreness, misfortune, unhappiness, sadness, dissatisfaction, displeasure, displeasure, setback, difficulty, misfortune, misfortune, misfortune"
                
                # Escrever a linha com todos os valores conforme o modelo
                writer.writerow([
                    prompt_completo,  # Prompt completo com tempo, texto 
                    "private",        # Visibility
                    aspect_ratio,     # Aspect_ratio (9:16 por padrão)
                    "on",             # Magic_prompt
                    "3",              # Model
                    "",               # Seed_number (vazio)
                    "turbo",        # Rendering
                    negative_prompt,  # Negative_prompt
                    "design",           # Style
                    "",               # color_palette (vazio)
                    "4"               # Num_images
                ])

        # Upload
        upload_para_drive(csv_path, csv_path.name, pasta_id, drive)

        return jsonify(
            slug=slug, 
            folder_url=f"https://drive.google.com/drive/folders/{pasta_id}",
            intervalo_segundos=intervalo_segundos,
            num_prompts=len(prompts_com_tempo)
        )
    except Exception as e:
        return jsonify(error="falha ao gerar CSV ou fazer upload", detalhe=str(e)), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
