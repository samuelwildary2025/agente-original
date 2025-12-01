"""
Ferramentas Redis para buffer de mensagens e cooldown
Inclui regras de Sessão (40min) e Janela de Edição (10min)
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
    """Retorna a conexão com o Redis (singleton)"""
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
            _redis_client.ping()
            logger.info(f"Conectado ao Redis: {settings.redis_host}:{settings.redis_port}")
        except Exception as e:
            logger.error(f"Erro ao conectar ao Redis: {e}")
            _redis_client = None
    return _redis_client


# ============================================
# Buffer de mensagens
# ============================================

def buffer_key(telefone: str) -> str:
    return f"msgbuf:{telefone}"

def push_message_to_buffer(telefone: str, mensagem: str, ttl_seconds: int = 300) -> bool:
    client = get_redis_client()
    if client is None:
        msgs = _local_buffer.get(telefone)
        if msgs is None: _local_buffer[telefone] = [mensagem]
        else: msgs.append(mensagem)
        return True

    key = buffer_key(telefone)
    try:
        client.rpush(key, mensagem)
        if client.ttl(key) in (-1, -2):
            client.expire(key, ttl_seconds)
        return True
    except redis.exceptions.RedisError as e:
        logger.error(f"Erro Redis push: {e}")
        return False

def get_buffer_length(telefone: str) -> int:
    client = get_redis_client()
    if client is None: return len(_local_buffer.get(telefone) or [])
    try: return int(client.llen(buffer_key(telefone)))
    except: return 0

def pop_all_messages(telefone: str) -> list[str]:
    client = get_redis_client()
    if client is None:
        msgs = _local_buffer.get(telefone) or []
        _local_buffer.pop(telefone, None)
        return msgs
    key = buffer_key(telefone)
    try:
        pipe = client.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        msgs, _ = pipe.execute()
        return [m for m in (msgs or []) if isinstance(m, str)]
    except: return []


# ============================================
# Cooldown do agente
# ============================================

def cooldown_key(telefone: str) -> str:
    return f"cooldown:{telefone}"

def set_agent_cooldown(telefone: str, ttl_seconds: int = 60) -> bool:
    client = get_redis_client()
    if client is None: return False
    try:
        client.set(cooldown_key(telefone), "1", ex=ttl_seconds)
        return True
    except: return False

def is_agent_in_cooldown(telefone: str) -> Tuple[bool, int]:
    client = get_redis_client()
    if client is None: return (False, -1)
    try:
        key = cooldown_key(telefone)
        if client.exists(key):
            return (True, client.ttl(key))
        return (False, -1)
    except: return (False, -1)


# ============================================
# NOVAS REGRAS: Sessão e Janela de Edição
# ============================================

def check_and_refresh_session(telefone: str, ttl_minutes: int = 40) -> bool:
    """
    Verifica se a sessão de pedido do cliente ainda está ativa.
    Retorna True se ativa (e renova), False se expirou.
    """
    client = get_redis_client()
    if client is None: return True # Fallback seguro

    phone = "".join(filter(str.isdigit, telefone))
    key = f"session_order:{phone}"
    
    # Verifica existência antes de renovar
    exists = client.exists(key)
    
    # Renova/Cria a sessão com o tempo definido (default 40 min)
    client.set(key, "1", ex=ttl_minutes * 60)
    
    # Se não existia antes desta chamada (mas agora existe), retorna False indicando que expirou
    # Se já existia, retorna True
    return bool(exists)

def set_order_edit_window(telefone: str, minutes: int = 10) -> bool:
    """Ativa a janela de alteração pós-pedido."""
    client = get_redis_client()
    if not client: return False
    
    phone = "".join(filter(str.isdigit, telefone))
    key = f"edit_window:{phone}"
    
    try:
        client.set(key, "1", ex=minutes * 60)
        return True
    except: return False

def is_order_editable(telefone: str) -> bool:
    """Verifica se ainda é permitido alterar o pedido anterior."""
    client = get_redis_client()
    if not client: return False
    
    phone = "".join(filter(str.isdigit, telefone))
    key = f"edit_window:{phone}"
    
    return bool(client.exists(key))
