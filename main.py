import os
import re
import csv
import tempfile
import traceback
import requests
from flask import Flask, request, jsonify
from datetime import datetime
import openai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Configuração de chaves e APIs
openai.api_key = os.getenv("OPENAI_API_KEY")
ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
SERVICE_ACCOUNT_FILE = "/etc/secrets/service_account.json"
GOOGLE_DRIVE_ROOT_FOLDER = "1NelNODHVBTbAuVqrfRNmLZP8MVQpF1aX"
VOICE_ID = "NgBYGKDDq2Z8Hnhatgma"  # Atlas

# Flask app
app = Flask(__name__)

# Google Drive
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive_service = build("drive", "v3", credentials=credentials)

FIXED_TAGS = "macabre, scary, terrifying. scribbled. grain. dirty texture. grunge. screen scratches."

# === Funções ===

def gerar_historia(prompt_usuario):
    resposta = openai.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Você é um narrador macabro. Crie histórias curtas e perturbadoras de até 2 minutos. Comece com um título chamativo. Use um tom sombrio, poético, desconfortável."},
            {"role": "user", "content": prompt_usuario}
        ],
        temperature=0.9,
        max_tokens=800
    )
    return resposta.choices[0].message.content.strip()

def narrar_com_elevenlabs(texto):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json"
    }
    body = {
        "text": texto,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.8
        }
    }

    try:
        response = requests.post(url, headers=headers, json=body, timeout=90)
        response.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(response.content)
        tmp.close()
        return tmp.name
    except Exception as e:
        print("Erro ElevenLabs:", e)
        if 'response' in locals():
            try:
                print("Resposta:", response.text)
            except:
                print("Resposta inválida")
        else:
            print("Resposta: Nenhuma")
        raise Exception("Falha na chamada da ElevenLabs")

# (Demais funções continuam iguais...)

@app.route("/teste-elevenlabs", methods=["GET"])
def teste_elevenlabs():
    texto = "oi, tudo bem? estou funcionando."
    try:
        mp3_path = narrar_com_elevenlabs(texto)
        return jsonify({"mensagem": "Narração concluída.", "arquivo": mp3_path})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return "Servidor do TerrorGPT rodando..."

if __name__ == "__main__":
    app.run()
