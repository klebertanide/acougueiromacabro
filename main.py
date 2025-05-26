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
            mp3_path = Path(f"{slug}_audio.mp3")
            with open(mp3_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return str(mp3_path)
    except requests.RequestException as e:
        raise RuntimeError(f"Erro ao chamar ElevenLabs: {e}")

def parse_ts(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000

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

@app.route("/falar", methods=["POST"])
def falar():
    try:
        data = request.get_json(force=True)
        texto = data.get("texto")
        if not texto:
            return jsonify(error="Campo 'texto' é obrigatório"), 400

        slug = slugify(texto[:40])
        audio_path_str = elevenlabs_tts(texto, slug)
        audio_path = Path(audio_path_str)

        drive = get_drive_service()
        folder_id = criar_subpasta(slug, drive, GOOGLE_DRIVE_ROOT_FOLDER)
        upload_para_drive(audio_path, audio_path.name, folder_id, drive)

        with open(audio_path, "rb") as fobj:
            raw_srt = client.audio.transcriptions.create(
                model="whisper-1", file=fobj, response_format="srt"
            )

        blocks = []
        for blk in raw_srt.strip().split("\n\n"):
            parts = blk.split("\n")
            if len(parts) < 3:
                continue
            st, en = parts[1].split(" --> ")
            txt = " ".join(parts[2:])
            inicio = parse_ts(st)
            fim = parse_ts(en)
            blocks.append({"inicio": inicio, "fim": fim, "texto": txt})
        total = blocks[-1]["fim"] if blocks else 0

        srt_path = Path(f"{slug}_legenda.srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(raw_srt)
        upload_para_drive(srt_path, srt_path.name, folder_id, drive)

        prompts_gerados = gerar_prompts_via_chatgpt(raw_srt)
        return jsonify({
            "slug": slug,
            "duracao_total": total,
            "folder_url": f"https://drive.google.com/drive/folders/{folder_id}",
            "transcricao": blocks,
            "prompts": [p[1] for p in prompts_gerados]
        })

    except Exception as e:
        import traceback
        return jsonify({
            "erro": str(e),
            "trace": traceback.format_exc()
        }), 500


@app.route("/", methods=["GET"])
def index():
    return "Servidor do Açougueiro Macabro está online e rodando."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))  # <- deve ser 10000
    app.run(host="0.0.0.0", port=port)
