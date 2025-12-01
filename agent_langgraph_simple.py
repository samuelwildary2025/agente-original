"""
Agente de IA para Atendimento de Supermercado
Inclui: Ferramentas de Pedido, Janela de Alteração e Histórico
"""
from typing import Dict, Any
import re
import json
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from pathlib import Path

from config.settings import settings
from config.logger import setup_logger
from tools.http_tools import estoque, pedidos, alterar, ean_lookup, estoque_preco
from tools.time_tool import get_current_time, search_message_history
from memory.limited_postgres_memory import LimitedPostgresChatMessageHistory
# NOVOS IMPORTS
from tools.redis_tools import set_order_edit_window, is_order_editable

logger = setup_logger(__name__)

# ============================================
# Definição das Ferramentas (Tools)
# ============================================

@tool
def estoque_tool(url: str) -> str:
    """Consultar estoque e preço atual."""
    return estoque(url)

@tool
def pedidos_tool(json_body: str) -> str:
    """
    Enviar o pedido finalizado para o painel.
    Automaticamente abre uma janela de 10 minutos para alterações.
    """
    resultado = pedidos(json_body)
    
    # Se sucesso, ativa o timer de 10min no Redis
    if "sucesso" in resultado.lower() or "✅" in resultado:
        try:
            data = json.loads(json_body)
            # Tenta pegar telefone do payload ou usa string vazia (agente deve garantir)
            tel = data.get("telefone") or data.get("cliente_telefone")
            if tel:
                set_order_edit_window(tel, minutes=10)
                logger.info(f"⏳ Janela de edição de 10min aberta para {tel}")
        except Exception as e:
            logger.error(f"Erro ao definir janela de edição: {e}")
            
    return resultado

@tool
def alterar_tool(telefone: str, json_body: str) -> str:
    """Atualizar o pedido no painel (apenas se permitido)."""
    return alterar(telefone, json_body)

@tool
def check_edit_window_tool(telefone: str) -> str:
    """
    Verifica se o pedido anterior ainda pode ser alterado.
    Retorna: 'PERMITIDO' ou 'EXPIRADO'.
    Use isso OBRIGATORIAMENTE quando o cliente quiser mudar algo após fechar o pedido.
    """
    if is_order_editable(telefone):
        return "PERMITIDO: O pedido foi fechado há menos de 10 minutos. Use 'alterar_tool'."
    else:
        return "EXPIRADO: O tempo de edição acabou. Crie um NOVO PEDIDO com 'pedidos_tool'."

@tool
def search_history_tool(telefone: str, keyword: str = None) -> str:
    """Busca mensagens anteriores."""
    return search_message_history(telefone, keyword)

@tool
def time_tool() -> str:
    """Data e hora atual."""
    return get_current_time()

@tool("ean")
def ean_tool_alias(query: str) -> str:
    """Buscar EAN/infos do produto."""
    q = (query or "").strip()
    if q.startswith("{") and q.endswith("}"): q = ""
    return ean_lookup(q)

@tool("estoque")
def estoque_preco_alias(ean: str) -> str:
    """Consulta preço pelo EAN (apenas dígitos)."""
    return estoque_preco(ean)

# Lista atualizada de ferramentas ativas
ACTIVE_TOOLS = [
    ean_tool_alias,
    estoque_preco_alias,
    estoque_tool,
    time_tool,
    search_history_tool,
    pedidos_tool,
    alterar_tool,
    check_edit_window_tool, # <--- Nova ferramenta adicionada
]

# ============================================
# Configuração do Grafo e Agente
# ============================================

def load_system_prompt() -> str:
    base_dir = Path(__file__).resolve().parent
    prompt_path = str((base_dir / "prompts" / "agent_system.md"))
    try:
        text = Path(prompt_path).read_text(encoding="utf-8")
        return text
    except Exception as e:
        logger.error(f"Falha ao carregar prompt: {e}")
        return "Você é um assistente de supermercado."

def _build_llm():
    return ChatOpenAI(
        model=settings.llm_model, 
        openai_api_key=settings.openai_api_key, 
        temperature=settings.llm_temperature
    )

_agent_graph = None
def get_agent_graph():
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = create_react_agent(_build_llm(), ACTIVE_TOOLS, prompt=load_system_prompt(), checkpointer=MemorySaver())
    return _agent_graph

def run_agent_langgraph(telefone: str, mensagem: str) -> Dict[str, Any]:
    # (Mesma lógica de multimodal e execução do arquivo original)
    image_url = None
    clean_message = mensagem
    media_match = re.search(r"\[MEDIA_URL:\s*(.*?)\]", mensagem)
    if media_match:
        image_url = media_match.group(1)
        clean_message = mensagem.replace(media_match.group(0), "").strip() or "Analise esta imagem."

    try:
        get_session_history(telefone).add_user_message(mensagem)
        agent = get_agent_graph()
        
        content = [{"type": "text", "text": clean_message}]
        if image_url: content.append({"type": "image_url", "image_url": {"url": image_url}})
        
        result = agent.invoke({"messages": [HumanMessage(content=content)]}, {"configurable": {"thread_id": telefone}})
        
        output = result["messages"][-1].content
        get_session_history(telefone).add_ai_message(output)
        return {"output": output, "error": None}
    except Exception as e:
        logger.error(f"Falha agente: {e}")
        return {"output": "Erro técnico, tente novamente.", "error": str(e)}

def get_session_history(session_id: str):
    return LimitedPostgresChatMessageHistory(
        connection_string=settings.postgres_connection_string,
        session_id=session_id,
        table_name=settings.postgres_table_name,
        max_messages=settings.postgres_message_limit
    )
