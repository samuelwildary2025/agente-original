"""
Agente de IA para Atendimento de Supermercado usando LangGraph
Versão: RAG Dinâmico (Regras via Banco Vetorial)
"""

from typing import Dict, Any, TypedDict, Sequence, List
import re
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode, tools_condition, create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from pathlib import Path
import json
import os

from config.settings import settings
from config.logger import setup_logger
# Importamos a nova search_rules aqui
from tools.http_tools import estoque, pedidos, alterar, ean_lookup, estoque_preco, search_rules
from tools.time_tool import get_current_time, search_message_history
from memory.limited_postgres_memory import LimitedPostgresChatMessageHistory
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
    """Enviar o pedido finalizado. Abre janela de edição de 10min."""
    resultado = pedidos(json_body)
    if "sucesso" in resultado.lower() or "✅" in resultado:
        try:
            data = json.loads(json_body)
            tel = data.get("telefone") or data.get("cliente_telefone")
            if tel:
                set_order_edit_window(tel, minutes=10)
        except: pass
    return resultado

@tool
def alterar_tool(telefone: str, json_body: str) -> str:
    """Atualizar pedido (apenas se permitido)."""
    return alterar(telefone, json_body)

@tool
def check_edit_window_tool(telefone: str) -> str:
    """Verifica se pedido ainda pode ser alterado."""
    if is_order_editable(telefone):
        return "PERMITIDO: Pedido fechado há menos de 10 min."
    return "EXPIRADO: Tempo acabou. Crie NOVO PEDIDO."

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
    """Buscar produto na base."""
    q = (query or "").strip()
    if q.startswith("{") and q.endswith("}"): q = ""
    return ean_lookup(q)

@tool("estoque")
def estoque_preco_alias(ean: str) -> str:
    """Consulta preço pelo EAN."""
    return estoque_preco(ean)

ACTIVE_TOOLS = [
    ean_tool_alias,
    estoque_preco_alias,
    estoque_tool,
    time_tool,
    search_history_tool,
    pedidos_tool,
    alterar_tool,
    check_edit_window_tool,
]

# ============================================
# Configuração do Grafo e Prompt
# ============================================

def load_system_prompt() -> str:
    base_dir = Path(__file__).resolve().parent
    # MUDANÇA: Carrega o prompt minimalista
    prompt_path = str((base_dir / "prompts" / "agent_system_minimal.md"))
    try:
        text = Path(prompt_path).read_text(encoding="utf-8")
        # Mantemos os replaces caso você use no futuro, mas o minimal talvez não precise
        text = text.replace("{base_url}", settings.supermercado_base_url)
        return text
    except Exception as e:
        logger.error(f"Falha ao carregar prompt minimalista: {e}")
        # Fallback simples caso arquivo não exista
        return "Você é um assistente de supermercado. Siga as regras injetadas no contexto."

def _build_llm():
    model = getattr(settings, "llm_model", "gpt-4o-mini")
    temp = float(getattr(settings, "llm_temperature", 0.0))
    return ChatOpenAI(model=model, openai_api_key=settings.openai_api_key, temperature=temp)

def create_agent_with_history():
    system_prompt = load_system_prompt()
    llm = _build_llm()
    memory = MemorySaver()
    # O prompt base é o minimalista
    agent = create_react_agent(llm, ACTIVE_TOOLS, prompt=system_prompt, checkpointer=memory)
    return agent

_agent_graph = None
def get_agent_graph():
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = create_agent_with_history()
    return _agent_graph

# ============================================
# Função Principal (Com Injeção Vetorial)
# ============================================

def run_agent_langgraph(telefone: str, mensagem: str) -> Dict[str, Any]:
    print(f"[AGENT] Telefone: {telefone} | Msg bruta: {mensagem[:50]}...")
    
    image_url = None
    clean_message = mensagem
    
    media_match = re.search(r"\[MEDIA_URL:\s*(.*?)\]", mensagem)
    if media_match:
        image_url = media_match.group(1)
        clean_message = mensagem.replace(media_match.group(0), "").strip() or "Analise esta imagem."

    # Salvar User no DB
    try:
        hist = get_session_history(telefone)
        hist.add_user_message(mensagem)
    except: pass

    try:
        # --- LÓGICA RAG: BUSCA REGRAS AUTOMATICAMENTE ---
        logger.info(f"🔍 Buscando regras para: '{clean_message[:30]}...'")
        regras_encontradas = search_rules(clean_message)
        
        system_injection = ""
        if regras_encontradas:
            logger.info("✅ Regras encontradas e injetadas no contexto.")
            system_injection = f"""
🚨 [REGRAS DE OURO - SIGA ESTRITAMENTE]
O sistema encontrou estas regras no manual da empresa para o contexto atual:

{regras_encontradas}

APLIQUE ESSAS REGRAS NA SUA RESPOSTA IMEDIATAMENTE.
"""
        else:
            system_injection = "" # Nenhuma regra específica, segue o padrão
        
        # ------------------------------------------------
        
        agent = get_agent_graph()
        
        messages_payload = []
        
        # 1. Injeta a Regra como SystemMessage (Alta prioridade)
        if system_injection:
            messages_payload.append(SystemMessage(content=system_injection))
        
        # 2. Adiciona a mensagem do usuário
        if image_url:
            messages_payload.append(HumanMessage(content=[
                {"type": "text", "text": clean_message},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]))
        else:
            messages_payload.append(HumanMessage(content=clean_message))

        initial_state = {"messages": messages_payload}
        config = {"configurable": {"thread_id": telefone}}
        
        result = agent.invoke(initial_state, config)
        
        output = "Desculpe, erro técnico."
        if isinstance(result, dict) and "messages" in result:
            output = str(result["messages"][-1].content)
        
        # Salvar AI no DB
        try: hist.add_ai_message(output)
        except: pass

        return {"output": output, "error": None}
        
    except Exception as e:
        logger.error(f"Falha agente: {e}", exc_info=True)
        return {"output": "Erro no sistema.", "error": str(e)}

def get_session_history(session_id: str) -> LimitedPostgresChatMessageHistory:
    return LimitedPostgresChatMessageHistory(
        connection_string=settings.postgres_connection_string,
        session_id=session_id,
        table_name=settings.postgres_table_name,
        max_messages=settings.postgres_message_limit
    )

run_agent = run_agent_langgraph
