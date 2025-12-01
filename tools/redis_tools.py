"""
Ferramentas Redis para buffer de mensagens e cooldown
Apenas funcionalidades essenciais mantidas + Regras de Tempo (40min/10min)
"""
import redis
from typing import Optional, Dict, List, Tuple
from config.settings import settings
from config.logger import setup_logger

logger = setup_logger(__name__)

# Conexão global com Redis
_redis_client: Optional[redis.Redis] = None
# Buffer local em memória (fallback quando Redis não está disponível)
_local_buffer: Dict[str, List[str]] = {}


def get_redis_client() -> Optional[redis.Redis]:
    """
    Retorna a conexão com o Redis (singleton)
    """
    global _redis_client
    
    if _redis_client is None:
        try:
            _redis_client = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password if settings.redis_password else None,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            # Testar conexão
            _redis_client.ping()
            logger.info(f"Conectado ao Redis: {settings.redis_host}:{settings.redis_port}")
        
        except redis.exceptions.ConnectionError as e:
            logger.error(f"Erro ao conectar ao Redis: {e}")
            _redis_client = None
        
        except Exception as e:
            logger.error(f"Erro inesperado ao conectar ao Redis: {e}")
            _redis_client = None
    
    return _redis_client


# ============================================
# Buffer de mensagens (concatenação por janela)
# ============================================

def buffer_key(telefone: str) -> str:
    """Retorna a chave da lista de buffer de mensagens no Redis."""
    return f"msgbuf:{telefone}"


def push_message_to_buffer(telefone: str, mensagem: str, ttl_seconds: int = 300) -> bool:
    """
    Empilha a mensagem recebida em uma lista no Redis para o telefone.
    """
    client = get_redis_client()
    if client is None:
        # Fallback em memória
        msgs = _local_buffer.get(telefone)
        if msgs is None:
            _local_buffer[telefone] = [mensagem]
        else:
            msgs.append(mensagem)
        logger.info(f"[fallback] Mensagem empilhada em memória para {telefone}")
        return True

    key = buffer_key(telefone)
    try:
        client.rpush(key, mensagem)
        # Se não houver TTL, definir um TTL padrão para evitar lixo acumulado
        if client.ttl(key) in (-1, -2):  # -2 = key não existe, -1 = sem TTL
            client.expire(key, ttl_seconds)
        logger.info(f"Mensagem empilhada no buffer: {key}")
        return True
    except redis.exceptions.RedisError as e:
        logger.error(f"Erro ao empilhar mensagem no Redis: {e}")
        return False


def get_buffer_length(telefone: str) -> int:
    """Retorna o tamanho atual do buffer de mensagens para o telefone."""
    client = get_redis_client()
    if client is None:
        msgs = _local_buffer.get(telefone) or []
        return len(msgs)
    try:
        return int(client.llen(buffer_key(telefone)))
    except redis.exceptions.RedisError as e:
        logger.error(f"Erro ao consultar tamanho do buffer: {e}")
        return 0


def pop_all_messages(telefone: str) -> list[str]:
    """
    Obtém todas as mensagens do buffer e limpa a chave.
    """
    client = get_redis_client()
    if client is None:
        msgs = _local_buffer.get(telefone) or []
        _local_buffer.pop(telefone, None)
        logger.info(f"[fallback] Buffer consumido para {telefone}: {len(msgs)} mensagens")
        return msgs
    key = buffer_key(telefone)
    try:
        pipe = client.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        msgs, _ = pipe.execute()
        msgs = [m for m in (msgs or []) if isinstance(m, str)]
        logger.info(f"Buffer consumido para {telefone}: {len(msgs)} mensagens")
        return msgs
    except redis.exceptions.RedisError as e:
        logger.error(f"Erro ao consumir buffer: {e}")
        return []


# ============================================
# Cooldown do agente (pausa de automação)
# ============================================

def cooldown_key(telefone: str) -> str:
    """Chave do cooldown no Redis."""
    return f"cooldown:{telefone}"


def set_agent_cooldown(telefone: str, ttl_seconds: int = 60) -> bool:
    """Define uma chave de cooldown para o telefone."""
    client = get_redis_client()
    if client is None:
        logger.warning(f"[fallback] Cooldown não persistido para {telefone}")
        return False
    try:
        key = cooldown_key(telefone)
        client.set(key, "1", ex=ttl_seconds)
        logger.info(f"Cooldown definido para {telefone} por {ttl_seconds}s")
        return True
    except redis.exceptions.RedisError as e:
        logger.error(f"Erro ao definir cooldown: {e}")
        return False


def is_agent_in_cooldown(telefone: str) -> Tuple[bool, int]:
    """Verifica se há cooldown ativo e retorna (ativo, ttl_restante)."""
    client = get_redis_client()
    if client is None:
        return (False, -1)
    try:
        key = cooldown_key(telefone)
        val = client.get(key)
        if val is None:
            return (False, -1)
        ttl = client.ttl(key)
        ttl = ttl if isinstance(ttl, int) else -1
        return (True, ttl)
    except redis.exceptions.RedisError as e:
        logger.error(f"Erro ao consultar cooldown: {e}")
        return (False, -1)


# ============================================
# NOVAS REGRAS: Sessão e Janela de Edição
# ============================================

def check_and_refresh_session(telefone: str, ttl_minutes: int = 40) -> bool:
    """
    Verifica se a sessão de pedido do cliente ainda está ativa (Redis).
    Retorna:
        True  -> Sessão ATIVA (continua o pedido).
        False -> Sessão EXPIROU (deve iniciar novo pedido).
    """
    client = get_redis_client()
    if client is None:
        return True # Se Redis falhar, assume que continua para não quebrar.

    # Remove caracteres não numéricos
    phone = "".join(filter(str.isdigit, telefone))
    key = f"session_order:{phone}"
    
    # Verifica se existe (antes de renovar)
    exists = client.exists(key)
    
    # Renova o tempo (TTL) para 40 minutos (ou o que for passado)
    client.set(key, "1", ex=ttl_minutes * 60)
    
    # Se não existia antes desta chamada, retorna False (Expirou/Novo)
    # Se já existia, retorna True (Ativo)
    return bool(exists)


def set_order_edit_window(telefone: str, minutes: int = 10) -> bool:
    """
    Cria uma chave temporária indicando que o pedido pode ser alterado.
    Chamado logo após fechar um pedido com sucesso.
    """
    client = get_redis_client()
    if not client: return False
    
    phone = "".join(filter(str.isdigit, telefone))
    key = f"edit_window:{phone}"
    
    try:
        client.set(key, "1", ex=minutes * 60)
        return True
    except redis.exceptions.RedisError as e:
        logger.error(f"Erro ao definir janela de edição: {e}")
        return False


def is_order_editable(telefone: str) -> bool:
    """
    Verifica se ainda está dentro do prazo de alteração (10 min).
    """
    client = get_redis_client()
    if not client: return False
    
    phone = "".join(filter(str.isdigit, telefone))
    key = f"edit_window:{phone}"
    
    # Se a chave existe, retorna True. Se expirou, retorna False.
    return bool(client.exists(key))
