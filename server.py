"""
Servidor FastAPI para receber mensagens do WhatsApp e processar com o agente
Suporta: Texto, Áudio (Transcrição Whisper Local), Imagem (Visão) e PDF
Regras: Sessão de 40min e Transcrição Inteligente
"""
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
import requests
from datetime import datetime
import time
import random
import threading
import re
import io
import os
from openai import OpenAI

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from config.settings import settings
from config.logger import setup_logger
from agent_langgraph_simple import run_agent_langgraph as run_agent, get_session_history
from tools.redis_tools import (
    push_message_to_buffer,
    get_buffer_length,
    pop_all_messages,
    set_agent_cooldown,
    is_agent_in_cooldown,
    check_and_refresh_session # <--- NOVO IMPORT
)

logger = setup_logger(__name__)

app = FastAPI(title="Agente de Supermercado", version="1.7.0")

# --- Models ---
class WhatsAppMessage(BaseModel):
    telefone: str
    mensagem: str
    message_id: Optional[str] = None
    timestamp: Optional[str] = None
    message_type: Optional[str] = "text"

class AgentResponse(BaseModel):
    success: bool
    response: str
    telefone: str
    timestamp: str
    error: Optional[str] = None

# --- Helpers ---

def get_api_base_url() -> str:
    return (settings.uaz_api_url or settings.whatsapp_api_url or "").strip().rstrip("/")

def get_media_url_uaz(message_id: str) -> Optional[str]:
    if not message_id: return None
    base = get_api_base_url()
    if not base: return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        url = f"{parsed.scheme}://{parsed.netloc}/message/download"
    except:
        url = f"{base.split('/message')[0]}/message/download"
    
    headers = {"Content-Type": "application/json", "token": (settings.whatsapp_token or "").strip()}
    payload = {"id": message_id, "return_link": True, "return_base64": False}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("fileURL") or data.get("url")
    except Exception as e:
        logger.error(f"Erro link mídia: {e}")
    return None

def process_pdf_uaz(message_id: str) -> Optional[str]:
    if not PdfReader: return "[PDF não suportado]"
    url = get_media_url_uaz(message_id)
    if not url: return None
    try:
        resp = requests.get(url, timeout=20)
        f = io.BytesIO(resp.content)
        reader = PdfReader(f)
        full_text = "\n".join([page.extract_text() for page in reader.pages])
        return re.sub(r'\s+', ' ', full_text).strip()
    except Exception as e:
        logger.error(f"Erro PDF: {e}")
        return None

def transcribe_audio_uaz(message_id: str) -> Optional[str]:
    """Baixa áudio e transcreve com Whisper (OpenAI) e contexto de supermercado."""
    if not message_id: return None
    url = get_media_url_uaz(message_id)
    if not url: return None
    
    temp_filename = f"temp_{message_id}.ogg"
    try:
        logger.info(f"🎧 Baixando áudio: {message_id}")
        headers = {}
        if settings.whatsapp_token: headers["token"] = settings.whatsapp_token
        resp = requests.get(url, headers=headers, timeout=20)
        with open(temp_filename, "wb") as f: f.write(resp.content)
        
        client = OpenAI(api_key=settings.openai_api_key)
        with open(temp_filename, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file, 
                language="pt",
                prompt="Lista de compras, supermercado, marcas: Ypê, Coca-Cola, Skol, Brahma, Heineken, Omo, Tixan, Arroz Camil, Feijão Kicaldo, Ninho, Aptamil, Piracanjuba."
            )
        
        if os.path.exists(temp_filename): os.remove(temp_filename)
        return transcript.text
    except Exception as e:
        logger.error(f"❌ Erro transcrição: {e}")
        if os.path.exists(temp_filename): 
            try: os.remove(temp_filename) 
            except: pass
        return None

def _extract_incoming(payload: Dict[str, Any]) -> Dict[str, Any]:
    # ... (mesma lógica de extração do arquivo anterior) ...
    # Para economizar espaço aqui, mantenha a função _extract_incoming original
    # Ela não mudou em relação à versão anterior.
    chat = payload.get("chat") or {}
    message_any = payload.get("message") or {}
    if isinstance(payload.get("messages"), list):
        try:
            m0 = payload["messages"][0]
            message_any = m0
            chat = {"wa_id": m0.get("sender") or m0.get("chatid")}
        except: pass

    telefone = None
    candidates = [message_any.get("sender"), message_any.get("chatid"), chat.get("id"), chat.get("wa_id"), chat.get("phone"), payload.get("from"), payload.get("sender")]
    for cand in candidates:
        if cand and isinstance(cand, str) and "@lid" not in cand and "@g.us" not in cand:
            clean = re.sub(r"\D", "", cand.split("@")[0])
            if 10 <= len(clean) <= 15:
                telefone = clean
                break
    
    if not telefone and payload.get("from"): telefone = re.sub(r"\D", "", str(payload.get("from")))

    mensagem_texto = payload.get("text")
    message_id = payload.get("id") or payload.get("messageid")
    from_me = bool(message_any.get("fromMe") or message_any.get("wasSentByApi") or False)
    
    if isinstance(message_any, dict):
        content = message_any.get("content")
        if isinstance(content, str) and not mensagem_texto: mensagem_texto = content
        elif isinstance(content, dict): mensagem_texto = content.get("text") or content.get("caption")
        if not mensagem_texto:
             txt = message_any.get("text")
             mensagem_texto = txt.get("body") if isinstance(txt, dict) else txt

    raw_type = str(message_any.get("messageType") or "").lower()
    media_type = str(message_any.get("mediaType") or "").lower()
    mimetype = str(message_any.get("mimetype") or "").lower()
    
    message_type = "text"
    if "audio" in raw_type or "ptt" in media_type: message_type = "audio"
    elif "image" in raw_type or "image" in media_type: message_type = "image"
    elif "document" in raw_type or "pdf" in mimetype: message_type = "document"

    if message_type == "audio" and not mensagem_texto:
        trans = transcribe_audio_uaz(message_id) if message_id else None
        mensagem_texto = f"[Áudio]: {trans}" if trans else "[Áudio inaudível]"
    elif message_type == "image":
        url = get_media_url_uaz(message_id) if message_id else ""
        mensagem_texto = f"{mensagem_texto or ''} [MEDIA_URL: {url}]".strip()
    elif message_type == "document" and "pdf" in mimetype:
        url = get_media_url_uaz(message_id)
        extracted = process_pdf_uaz(message_id) if message_id else ""
        mensagem_texto = f"PDF Recebido. {extracted[:500]}... [MEDIA_URL: {url}]"

    return {"telefone": telefone, "mensagem_texto": mensagem_texto, "message_type": message_type, "message_id": message_id, "from_me": from_me}

def send_whatsapp_message(telefone: str, mensagem: str) -> bool:
    base = get_api_base_url()
    if not base: return False
    url = f"{base}/send/text" if "send/text" not in base else base
    headers = {"Content-Type": "application/json", "token": (settings.whatsapp_token or "").strip()}
    try:
        requests.post(url, headers=headers, json={"number": re.sub(r"\D", "", telefone), "text": mensagem, "openTicket": "1"}, timeout=10)
        return True
    except: return False

def send_presence(num, type_):
    base = get_api_base_url()
    if not base: return
    url = f"{base}/message/presence" if "presence" not in base else base
    try: requests.post(url, headers={"token": settings.whatsapp_token}, json={"number": re.sub(r"\D","",num), "presence": type_}, timeout=5)
    except: pass

# --- Processamento Assíncrono com Regra de 40 Minutos ---
def process_async(tel, msg, mid=None):
    try:
        num = re.sub(r"\D", "", tel)
        
        # 1. VERIFICAÇÃO DE SESSÃO (40 MINUTOS)
        # Se retornar False, a sessão expirou -> Força novo pedido
        sessao_ativa = check_and_refresh_session(num, ttl_minutes=40)
        
        mensagem_final = msg
        if not sessao_ativa:
            logger.info(f"🕒 Sessão expirada para {num}. Forçando novo pedido.")
            mensagem_final = f"[SISTEMA: A sessão anterior expirou (passou de 40min). IGNORE o pedido antigo e comece um NOVO PEDIDO do zero agora.] {msg}"

        # 2. Simula comportamento humano
        time.sleep(random.uniform(2.0, 4.0))
        send_presence(num, "composing")
        
        # 3. Processa com a IA
        res = run_agent(tel, mensagem_final)
        txt = res.get("output", "Erro ao processar.")
        
        send_presence(num, "paused")
        time.sleep(0.5)
        send_whatsapp_message(tel, txt)
        
    except Exception as e:
        logger.error(f"Erro async: {e}")

# Buffer Loop (mantido igual)
buffer_sessions = {}
def buffer_loop(tel):
    try:
        n = re.sub(r"\D","",tel)
        prev, stall = get_buffer_length(n), 0
        while stall < 3:
            time.sleep(3.5)
            curr = get_buffer_length(n)
            if curr > prev: prev, stall = curr, 0
            else: stall += 1
        msgs = pop_all_messages(n)
        final = " ".join([m for m in msgs if m.strip()])
        if final: process_async(n, final)
    except: pass
    finally: buffer_sessions.pop(re.sub(r"\D","",tel), None)

# --- Endpoints ---
@app.post("/webhook/whatsapp")
async def webhook(req: Request, tasks: BackgroundTasks):
    try:
        pl = await req.json()
        data = _extract_incoming(pl)
        tel, txt, from_me = data["telefone"], data["mensagem_texto"], data["from_me"]
        if not tel or not txt: return JSONResponse(content={"status":"ignored"})
        if from_me: return JSONResponse(content={"status":"ignored_self"})

        num = re.sub(r"\D","",tel)
        active, _ = is_agent_in_cooldown(num)
        if active:
            push_message_to_buffer(num, txt)
            return JSONResponse(content={"status":"cooldown"})

        if push_message_to_buffer(num, txt):
            if not buffer_sessions.get(num):
                buffer_sessions[num] = True
                threading.Thread(target=buffer_loop, args=(num,), daemon=True).start()
        else:
            tasks.add_task(process_async, tel, txt)
        return JSONResponse(content={"status":"buffering"})
    except Exception as e:
        logger.error(f"Erro webhook: {e}")
        return JSONResponse(status_code=500, detail=str(e))
