"""
Ferramentas HTTP para interação com a API do Supermercado e Supabase
"""
import requests
import json
from typing import Dict, Any
from config.settings import settings
from config.logger import setup_logger

logger = setup_logger(__name__)


def get_auth_headers() -> Dict[str, str]:
    """Retorna os headers de autenticação para as requisições"""
    return {
        "Authorization": settings.supermercado_auth_token,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def estoque(url: str) -> str:
    """Consulta o estoque e preço de produtos no sistema do supermercado."""
    if not url.startswith("http"):
        base = (settings.supermercado_base_url or "").rstrip("/")
        path = url.lstrip("/")
        url = f"{base}/{path}"

    logger.info(f"Consultando estoque: {url}")
    try:
        response = requests.get(url, headers=get_auth_headers(), timeout=10)
        response.raise_for_status()
        data = response.json()
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Erro estoque: {e}")
        return f"Erro ao consultar estoque: {e}"


def pedidos(json_body: str) -> str:
    """Envia um pedido finalizado para o painel."""
    url = f"{settings.supermercado_base_url}/pedidos/"
    logger.info(f"Enviando pedido para: {url}")
    
    try:
        data = json.loads(json_body)
        response = requests.post(url, headers=get_auth_headers(), json=data, timeout=15)
        
        if response.status_code in [400, 422]:
            error_msg = response.text
            logger.error(f"❌ API recusou o pedido ({response.status_code}): {error_msg}")
            return f"ERRO API ({response.status_code}): O formato do pedido está incorreto. Detalhes: {error_msg}. Corrija o JSON e tente novamente."

        response.raise_for_status()
        result = response.json()
        return f"✅ Pedido enviado com sucesso!\n\nResposta: {json.dumps(result, indent=2, ensure_ascii=False)}"
    
    except json.JSONDecodeError:
        return "Erro: O conteúdo enviado não é um JSON válido."
    except Exception as e:
        logger.error(f"Erro pedidos: {e}")
        return f"Erro ao enviar pedido: {e}"


def alterar(telefone: str, json_body: str) -> str:
    """Atualiza um pedido existente no painel."""
    telefone_limpo = "".join(filter(str.isdigit, telefone))
    url = f"{settings.supermercado_base_url}/pedidos/telefone/{telefone_limpo}"
    logger.info(f"Atualizando pedido {telefone_limpo}")
    try:
        data = json.loads(json_body)
        response = requests.put(url, headers=get_auth_headers(), json=data, timeout=10)
        
        if response.status_code in [400, 422]:
             return f"ERRO AO ALTERAR ({response.status_code}): {response.text}"

        response.raise_for_status()
        result = response.json()
        return f"✅ Pedido atualizado!\n\nResposta: {json.dumps(result, indent=2, ensure_ascii=False)}"
    except Exception as e:
        logger.error(f"Erro alterar: {e}")
        return f"Erro ao atualizar pedido: {e}"


def ean_lookup(query: str) -> str:
    """Busca informações/EAN do produto via Supabase."""
    url = (settings.smart_responder_url or "").strip().replace("`", "")
    auth_token = (settings.smart_responder_auth or settings.smart_responder_token or "").strip()
    
    if not url or not auth_token:
        return "Erro: SMART_RESPONDER_URL não configurado."

    headers = {
        "Authorization": auth_token if auth_token.lower().startswith("bearer ") else f"Bearer {auth_token}",
        "Content-Type": "application/json"
    }
    
    # --- AJUSTE 1: PRODUTOS (Queremos variedade) ---
    payload = {
        "query": query,
        "match_count": 3  # Traz até 3 produtos similares
    }
    # -----------------------------------------------
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        return resp.text 
    except Exception as e:
        return f"Erro na busca EAN: {e}"


def estoque_preco(ean: str) -> str:
    """Consulta preço e disponibilidade pelo EAN na API do ERP."""
    base = (settings.estoque_ean_base_url or "").strip().rstrip("/")
    ean_digits = "".join(ch for ch in ean if ch.isdigit())
    if not base or not ean_digits:
        return "Erro: Configuração inválida ou EAN vazio."

    url = f"{base}/{ean_digits}"
    try:
        resp = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Erro ao consultar preço (ERP): {e}"


def search_rules(query: str) -> str:
    """Busca regras no Supabase (type='rules')."""
    url = (settings.smart_responder_url or "").strip()
    auth_token = (settings.smart_responder_auth or settings.smart_responder_token or "").strip()
    
    if not url or not auth_token:
        return ""

    headers = {
        "Authorization": auth_token if auth_token.lower().startswith("bearer ") else f"Bearer {auth_token}",
        "Content-Type": "application/json"
    }
    
    # --- AJUSTE 2: REGRAS (Queremos precisão) ---
    payload = {
        "query": query,
        "type": "rules",
        "match_count": 1  # Traz APENAS a regra mais relevante
    }
    # --------------------------------------------
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=5)
        if response.status_code != 200:
            return ""

        data = response.json()
        if isinstance(data, list) and data:
            regras_texto = ""
            for item in data:
                conteudo = item.get('content', '')
                if conteudo:
                    regras_texto += f"- {conteudo}\n"
            return regras_texto
        return ""
    except Exception as e:
        logger.error(f"Erro ao buscar regras automáticas: {e}")
        return ""
